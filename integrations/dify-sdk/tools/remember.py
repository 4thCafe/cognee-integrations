from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import (
    as_bool,
    auth_headers,
    credentials,
    error_text,
    login,
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


class RememberTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url, user_email, user_password = credentials(self.runtime)

        if not tool_parameters.get("dataset_name") and not tool_parameters.get("dataset_id"):
            error_msg = "Either dataset_name or dataset_id must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        form = build_form(tool_parameters)

        try:
            with httpx.Client(trust_env=False) as client:
                token = login(client, base_url, user_email, user_password)

                response = client.post(
                    f"{base_url}/api/v1/remember",
                    headers=auth_headers(token),
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
