import io
import uuid
from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import authorize, base_url_of, error_text, parse_json, split_csv

UPDATE_TIMEOUT = 21600


class UpdateDataTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)

        dataset_id = tool_parameters["dataset_id"]
        data_id = tool_parameters["data_id"]
        text_data = tool_parameters["text_data"]
        node_set = split_csv(tool_parameters.get("node_set", ""))

        form_data: dict[str, Any] = {}
        if node_set:
            form_data["node_set"] = node_set

        try:
            with httpx.Client(trust_env=False, follow_redirects=True) as client:
                filename = f"data_{uuid.uuid4().hex[:8]}.txt"
                response = client.patch(
                    f"{base_url}/api/v1/update",
                    params={"data_id": data_id, "dataset_id": dataset_id},
                    headers=authorize(client, self.runtime),
                    files={"data": (filename, io.BytesIO(text_data.encode("utf-8")), "text/plain")},
                    data=form_data,
                    timeout=UPDATE_TIMEOUT,
                )
                response.raise_for_status()
                result = parse_json(response)

                status = result.get("status", "ok") if isinstance(result, dict) else "ok"
                yield self.create_json_message(
                    result if isinstance(result, dict) else {"result": result}
                )
                yield self.create_variable_message("succeeded", True)
                yield self.create_variable_message("dataset_id", dataset_id)
                yield self.create_variable_message("data_id", data_id)
                yield self.create_text_message(
                    f"Successfully updated data item '{data_id}' in dataset '{dataset_id}' "
                    f"(status: {status})."
                )
        except Exception as e:
            error_msg = error_text(e, "Failed to update data")
            yield self.create_json_message({"succeeded": False, "error": error_msg})
            yield self.create_variable_message("succeeded", False)
            yield self.create_variable_message("dataset_id", dataset_id)
            yield self.create_variable_message("data_id", data_id)
            yield self.create_text_message(error_msg)
