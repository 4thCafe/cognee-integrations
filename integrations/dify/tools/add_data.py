import io
import uuid
from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import authorize, base_url_of, error_text, split_csv

ADD_TIMEOUT = 3600


def first_data_id(result: Any) -> tuple[str, int]:
    """``(first data id, item count)`` from a ``PipelineRunInfo`` body (or a list of them)."""
    data_ids: list[str] = []
    for run in result if isinstance(result, list) else [result]:
        if not isinstance(run, dict):
            continue
        for info in run.get("data_ingestion_info") or []:
            if isinstance(info, dict) and info.get("data_id"):
                data_ids.append(str(info["data_id"]))
    return (data_ids[0] if data_ids else ""), len(data_ids)


class AddDataTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)

        dataset_name = tool_parameters.get("dataset_name", "")
        dataset_id = tool_parameters.get("dataset_id", "")
        text_data = tool_parameters["text_data"]
        node_set = split_csv(tool_parameters.get("node_set", ""))

        if not dataset_name and not dataset_id:
            error_msg = "Either dataset_name or dataset_id must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        form_data: dict[str, Any] = {}
        if dataset_name:
            form_data["datasetName"] = dataset_name
        if dataset_id:
            form_data["datasetId"] = dataset_id
        if node_set:
            form_data["node_set"] = node_set

        try:
            with httpx.Client(trust_env=False, follow_redirects=True) as client:
                filename = f"data_{uuid.uuid4().hex[:8]}.txt"
                response = client.post(
                    f"{base_url}/api/v1/add",
                    headers=authorize(client, self.runtime),
                    files={"data": (filename, io.BytesIO(text_data.encode("utf-8")), "text/plain")},
                    data=form_data,
                    timeout=ADD_TIMEOUT,
                )
                response.raise_for_status()
                result = response.json()

                run = result[0] if isinstance(result, list) and result else result
                run = run if isinstance(run, dict) else {}
                resp_dataset_id = str(run.get("dataset_id") or dataset_id or "")
                resp_dataset_name = str(run.get("dataset_name") or dataset_name or "")
                data_id, items_count = first_data_id(result)

                yield self.create_json_message(
                    result if isinstance(result, dict) else {"result": result}
                )
                yield self.create_variable_message("dataset_name", resp_dataset_name)
                yield self.create_variable_message("dataset_id", resp_dataset_id)
                yield self.create_variable_message("data_id", data_id)
                yield self.create_variable_message("items_count", items_count)
                yield self.create_text_message(
                    f"Successfully added data to dataset '{resp_dataset_name}' "
                    f"(id: {resp_dataset_id})."
                )
        except Exception as e:
            error_msg = error_text(e, "Failed to add data")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
