from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


def _login(base_url: str, email: str, password: str, client: httpx.Client) -> str:
    response = client.post(
        f"{base_url}/api/v1/auth/login",
        data={"username": email, "password": password},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _item_text(item: Any) -> str:
    """Readable text for one entry of a ``POST /api/v1/search`` response.

    With access control on (the server default) each entry is a per-dataset
    dict ``{"dataset_id", "dataset_name", "search_result"}``; ``search_result``
    is the completion string for completion search types, or a list for
    retrieval-only types. With access control off the entries are the bare
    results. Older shapes carried ``content``/``text`` keys.
    """
    if isinstance(item, dict):
        if "search_result" in item:
            inner = item["search_result"]
            if isinstance(inner, list):
                inner = "\n".join(_item_text(sub) for sub in inner)
            elif not isinstance(inner, str):
                inner = str(inner)
            if item.get("error"):
                inner = f"{inner}\n(error: {item['error']})" if inner else f"error: {item['error']}"
            name = item.get("dataset_name")
            return f"[{name}] {inner}" if name else inner
        for key in ("content", "text"):
            if isinstance(item.get(key), str):
                return item[key]
        return str(item)
    return str(item)


def format_results(result: Any) -> tuple[int, str]:
    """Return ``(results_count, results_text)`` for a search response body."""
    if isinstance(result, list):
        lines = [f"Found {len(result)} result(s):\n"]
        for i, item in enumerate(result, 1):
            lines.append(f"{i}. {_item_text(item)}")
        return len(result), "\n".join(lines)
    return 1, str(result)


class SearchTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = self.runtime.credentials["base_url"].rstrip("/")
        user_email = self.runtime.credentials["user_email"]
        user_password = self.runtime.credentials["user_password"]

        query = tool_parameters["query"]
        datasets_str = tool_parameters.get("datasets", "")
        dataset_ids_str = tool_parameters.get("dataset_ids", "")
        search_type = tool_parameters.get("search_type", "GRAPH_COMPLETION")
        system_prompt = tool_parameters.get("system_prompt", "")
        top_k = tool_parameters.get("top_k", 10)
        only_context = tool_parameters.get("only_context", "false") == "true"

        datasets = [d.strip() for d in datasets_str.split(",") if d.strip()] if datasets_str else []
        dataset_ids = (
            [d.strip() for d in dataset_ids_str.split(",") if d.strip()] if dataset_ids_str else []
        )

        body: dict[str, Any] = {
            "searchType": search_type,
            "query": query,
            "topK": int(top_k),
            "onlyContext": only_context,
        }
        if datasets:
            body["datasets"] = datasets
        if dataset_ids:
            body["datasetIds"] = dataset_ids
        if system_prompt:
            body["systemPrompt"] = system_prompt

        try:
            with httpx.Client(trust_env=False) as client:
                token = _login(base_url, user_email, user_password, client)

                response = client.post(
                    f"{base_url}/api/v1/search",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    timeout=300,
                )
                response.raise_for_status()
                result = response.json()

                yield self.create_json_message(result)

                results_count, formatted = format_results(result)
                yield self.create_variable_message("results_count", results_count)
                yield self.create_variable_message("results_text", formatted)
                yield self.create_text_message(formatted)
        except httpx.HTTPStatusError as e:
            error_msg = f"Cognee API error {e.response.status_code}: {e.response.text}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
        except Exception as e:
            error_msg = f"Search failed: {str(e)}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
