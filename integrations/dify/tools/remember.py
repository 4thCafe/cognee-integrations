import time
from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import (
    as_bool,
    authorize,
    base_url_of,
    error_text,
    parse_json,
    split_csv,
)

REMEMBER_TIMEOUT = 21600


def build_form(tool_parameters: dict[str, Any]) -> dict[str, Any]:
    """Multipart/form fields for ``POST /api/v1/remember``.

    The text goes in ``raw_data`` so the server names the document by its
    content hash; no upload filename is involved.
    """
    form: dict[str, Any] = {"raw_data": [tool_parameters["text_data"]]}

    dataset_name = tool_parameters.get("dataset_name", "")
    dataset_id = tool_parameters.get("dataset_id", "")
    if dataset_name:
        form["datasetName"] = dataset_name
    if dataset_id:
        form["datasetId"] = dataset_id

    node_set = split_csv(tool_parameters.get("node_set", ""))
    if node_set:
        form["node_set"] = node_set

    session_id = tool_parameters.get("session_id", "")
    if session_id:
        form["session_id"] = session_id

    custom_prompt = tool_parameters.get("custom_prompt", "")
    if custom_prompt:
        form["custom_prompt"] = custom_prompt

    if as_bool(tool_parameters.get("run_in_background", "false")):
        form["run_in_background"] = "true"

    self_improvement = tool_parameters.get("self_improvement", "default")
    if self_improvement in ("true", "false"):
        form["self_improvement"] = self_improvement

    return form


# --- waiting for an asynchronous build ------------------------------------------
#
# Some deployments (Cognee Cloud) build the graph asynchronously even for a
# blocking request: ``POST /remember`` answers ``status: "running"`` with no
# pipeline run id. The dataset-level status cannot be trusted right after the
# call, because a dataset that was cognified before still reads "completed".
# So the wait keys on the per-item completion stamps instead: the new item has
# to appear (total grows past the baseline taken before the call) and nothing
# may be pending. A dataset-level status is only the fallback for servers
# without the per-item route.

POLL_INTERVAL = 3.0
POLL_TIMEOUT = REMEMBER_TIMEOUT
# How long "nothing pending, but no new item" is tolerated before it is taken
# as "the content was already there" (identical text is deduplicated by hash).
DEDUP_GRACE = 20.0
TERMINAL_STATUSES = {
    "DATASET_PROCESSING_COMPLETED": "completed",
    "DATASET_PROCESSING_ERRORED": "errored",
}


def dataset_item_count(
    client: httpx.Client, base_url: str, headers: dict[str, str], dataset_id: str
) -> int | None:
    """Number of data items in the dataset, or None when the route is unavailable."""
    response = client.get(
        f"{base_url}/api/v1/datasets/{dataset_id}/processing-status",
        headers=headers,
        timeout=60,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    body = parse_json(response, default={})
    return int(body.get("total", 0)) if isinstance(body, dict) else None


def resolve_dataset_id(
    client: httpx.Client, base_url: str, headers: dict[str, str], dataset_name: str
) -> str:
    """Dataset id for a name the caller owns, or "" when it does not exist yet."""
    response = client.get(f"{base_url}/api/v1/datasets", headers=headers, timeout=60)
    response.raise_for_status()
    body = parse_json(response, default=[])
    for dataset in body if isinstance(body, list) else []:
        if isinstance(dataset, dict) and dataset.get("name") == dataset_name:
            return str(dataset.get("id", ""))
    return ""


def wait_for_dataset(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    dataset_id: str,
    *,
    baseline_total: int | None = 0,
    sleep=time.sleep,
    deadline: float | None = None,
    now=time.monotonic,
) -> str:
    """Wait until the dataset's build has finished.

    Returns ``"completed"``, ``"errored"``, or ``"running"`` when the deadline
    passes first. ``baseline_total`` is the item count before the remember
    call (None when the per-item route is unavailable, which selects the
    dataset-status fallback).
    """
    started = now()
    end = deadline if deadline is not None else started + POLL_TIMEOUT
    use_items = baseline_total is not None
    seen_in_progress = False
    while True:
        if use_items:
            response = client.get(
                f"{base_url}/api/v1/datasets/{dataset_id}/processing-status",
                headers=headers,
                timeout=60,
            )
            if response.status_code == 404:
                use_items = False
                continue
            response.raise_for_status()
            body = parse_json(response, default={})
            total = int(body.get("total", 0))
            pending = int(body.get("pending", 0))
            if pending == 0 and total > baseline_total:
                return "completed"
            if pending == 0 and now() - started >= DEDUP_GRACE:
                # Nothing new arrived and nothing is pending: the content was
                # already in the dataset (deduplicated), so the memory is ready.
                return "completed"
        else:
            response = client.get(
                f"{base_url}/api/v1/datasets/status",
                params={"dataset": dataset_id},
                headers=headers,
                timeout=60,
            )
            response.raise_for_status()
            body = parse_json(response, default={})
            raw = body.get(dataset_id) if isinstance(body, dict) else None
            if isinstance(raw, dict):
                raw = raw.get("cognify_pipeline") or next(iter(raw.values()), None)
            status = TERMINAL_STATUSES.get(str(raw))
            if status == "errored":
                return status
            if status == "completed" and (seen_in_progress or now() - started >= DEDUP_GRACE):
                # A dataset-level "completed" right after the call may be the
                # previous run; accept it once a run was observed or after the grace.
                return status
            if raw is not None and status is None:
                seen_in_progress = True
        if now() >= end:
            return "running"
        sleep(POLL_INTERVAL)


class RememberTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)

        if not tool_parameters.get("dataset_name") and not tool_parameters.get("dataset_id"):
            error_msg = "Either dataset_name or dataset_id must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        form = build_form(tool_parameters)
        wants_background = as_bool(tool_parameters.get("run_in_background", "false"))

        try:
            with httpx.Client(trust_env=False, follow_redirects=True) as client:
                headers = authorize(client, self.runtime)

                # Baseline for the asynchronous-build wait: how many items the
                # dataset holds before this call. Only needed for blocking runs.
                baseline_total: int | None = 0
                if not wants_background:
                    known_id = tool_parameters.get("dataset_id", "") or resolve_dataset_id(
                        client, base_url, headers, tool_parameters.get("dataset_name", "")
                    )
                    if known_id:
                        baseline_total = dataset_item_count(client, base_url, headers, known_id)

                response = client.post(
                    f"{base_url}/api/v1/remember",
                    headers=headers,
                    data=form,
                    timeout=REMEMBER_TIMEOUT,
                )
                response.raise_for_status()
                result = parse_json(response)

                status = str(result.get("status", "completed"))
                dataset_id = str(result.get("dataset_id") or tool_parameters.get("dataset_id", ""))
                dataset_name = str(
                    result.get("dataset_name") or tool_parameters.get("dataset_name", "")
                )
                pipeline_run_id = str(result.get("pipeline_run_id") or "")
                items_processed = int(result.get("items_processed") or 0)

                waited = False
                if status == "running" and not wants_background and dataset_id:
                    status = wait_for_dataset(
                        client, base_url, headers, dataset_id, baseline_total=baseline_total
                    )
                    result["status"] = status
                    waited = True

                yield self.create_json_message(result)
                yield self.create_variable_message("status", status)
                yield self.create_variable_message("dataset_id", dataset_id)
                yield self.create_variable_message("dataset_name", dataset_name)
                yield self.create_variable_message("pipeline_run_id", pipeline_run_id)
                yield self.create_variable_message("items_processed", items_processed)

                if status == "running":
                    summary = (
                        f"Remember started in the background for dataset '{dataset_name}' "
                        f"(id: {dataset_id}, pipeline run: {pipeline_run_id})."
                    )
                elif status == "errored":
                    summary = (
                        f"Remember failed while building the memory for dataset "
                        f"'{dataset_name}' (id: {dataset_id}): "
                        f"{result.get('error') or 'see server logs'}"
                    )
                elif waited:
                    summary = (
                        f"Remembered the data in dataset '{dataset_name}' (id: {dataset_id}); "
                        "the memory was built asynchronously and is ready to recall."
                    )
                else:
                    summary = (
                        f"Remembered {items_processed} item(s) in dataset '{dataset_name}' "
                        f"(id: {dataset_id}); the memory is ready to recall."
                    )
                yield self.create_text_message(summary)
        except Exception as e:
            error_msg = error_text(e, "Failed to remember")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
