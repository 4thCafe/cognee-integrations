"""Request shapes and result formatting for the cognee 1.6 memory tools."""

import sys
from pathlib import Path

import httpx
import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

NEW_TOOLS = ["remember", "recall", "remember_entry", "forget", "improve"]


# --- registration ---------------------------------------------------------------


def test_provider_registers_every_tool_yaml():
    provider = yaml.safe_load((PLUGIN_ROOT / "provider" / "cognee_sdk.yaml").read_text())
    registered = set(provider["tools"])
    on_disk = {f"tools/{p.name}" for p in (PLUGIN_ROOT / "tools").glob("*.yaml")}
    assert registered == on_disk


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_yaml_points_at_its_source(name):
    spec = yaml.safe_load((PLUGIN_ROOT / "tools" / f"{name}.yaml").read_text())
    assert spec["identity"]["name"] == name
    assert spec["extra"]["python"]["source"] == f"tools/{name}.py"
    assert (PLUGIN_ROOT / "tools" / f"{name}.py").is_file()
    for param in spec["parameters"]:
        assert set(param["label"]) == {"en_US", "zh_Hans", "pt_BR", "ja_JP"}, param["name"]


def test_forget_everything_is_form_only():
    spec = yaml.safe_load((PLUGIN_ROOT / "tools" / "forget.yaml").read_text())
    everything = next(p for p in spec["parameters"] if p["name"] == "everything")
    assert everything["form"] == "form"
    assert everything["default"] == "false"


# --- shared client helpers -----------------------------------------------------


def test_split_csv_and_as_bool():
    from tools.cognee_client import as_bool, split_csv

    assert split_csv(" a, b ,,c ") == ["a", "b", "c"]
    assert split_csv("") == []
    assert split_csv(None) == []
    assert as_bool("true") is True
    assert as_bool("True") is True
    assert as_bool("false") is False
    assert as_bool(True) is True
    assert as_bool(None) is False


def test_error_text_prefers_the_server_body():
    from tools.cognee_client import error_text

    request = httpx.Request("POST", "http://x/api/v1/recall")
    response = httpx.Response(404, text='{"detail":"No datasets found."}', request=request)
    exc = httpx.HTTPStatusError("nope", request=request, response=response)
    assert (
        error_text(exc, "Recall failed") == 'Cognee API error 404: {"detail":"No datasets found."}'
    )
    assert error_text(RuntimeError("boom"), "Recall failed") == "Recall failed: boom"


# --- remember --------------------------------------------------------------------


def test_remember_form_uses_raw_data_and_omits_unset_fields():
    from tools.remember import build_form

    form = build_form({"text_data": "Einstein was born in Ulm.", "dataset_name": "notes"})
    assert form == {"raw_data": ["Einstein was born in Ulm."], "datasetName": "notes"}


def test_remember_form_carries_every_option():
    from tools.remember import build_form

    form = build_form(
        {
            "text_data": "t",
            "dataset_id": "3f2b",
            "node_set": "people, places",
            "session_id": "conv-1",
            "custom_prompt": "Extract people.",
            "run_in_background": "true",
            "self_improvement": "false",
        }
    )
    assert form["datasetId"] == "3f2b"
    assert form["node_set"] == ["people", "places"]
    assert form["session_id"] == "conv-1"
    assert form["custom_prompt"] == "Extract people."
    assert form["run_in_background"] == "true"
    assert form["self_improvement"] == "false"
    assert "datasetName" not in form


def test_remember_form_leaves_self_improvement_to_the_server_by_default():
    from tools.remember import build_form

    form = build_form({"text_data": "t", "dataset_name": "n", "self_improvement": "default"})
    assert "self_improvement" not in form
    assert "run_in_background" not in form


# --- recall ----------------------------------------------------------------------


def test_recall_body_pins_stream_false_and_omits_auto_values():
    from tools.recall import build_body

    body = build_body({"query": "Where was Einstein born?"})
    assert body == {
        "query": "Where was Einstein born?",
        "topK": 15,
        "onlyContext": False,
        "includeReferences": False,
        "stream": False,
    }


def test_recall_body_carries_scope_session_and_datasets():
    from tools.recall import build_body

    body = build_body(
        {
            "query": "q",
            "datasets": "a,b",
            "dataset_ids": "id1",
            "session_id": "conv-1",
            "scope": "session_first",
            "search_type": "GRAPH_COMPLETION",
            "system_prompt": "Be brief.",
            "top_k": 3,
            "only_context": "true",
            "include_references": "true",
        }
    )
    assert body["datasets"] == ["a", "b"]
    assert body["datasetIds"] == ["id1"]
    assert body["sessionId"] == "conv-1"
    assert body["scope"] == "session_first"
    assert body["searchType"] == "GRAPH_COMPLETION"
    assert body["systemPrompt"] == "Be brief."
    assert body["topK"] == 3
    assert body["onlyContext"] is True
    assert body["includeReferences"] is True
    assert body["stream"] is False


def test_recall_headers_ask_for_json():
    from tools.cognee_client import auth_headers

    headers = auth_headers("tok", json=True)
    assert headers["Accept"] == "application/json"
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Content-Type"] == "application/json"


def test_recall_formats_mixed_sources_and_picks_the_graph_answer():
    from tools.recall import format_results

    body = [
        {"source": "session", "question": "Who?", "answer": "Einstein.", "time": "t"},
        {
            "source": "session_context",
            "content": "User prefers short answers",
            "context_profile": "qa",
        },
        {
            "source": "graph",
            "text": "Einstein was born in Ulm.",
            "dataset_name": "notes",
            "kind": "completion",
            "search_type": "HYBRID_COMPLETION",
        },
    ]
    count, text, answer = format_results(body)
    assert count == 3
    assert "1. Q: Who?\nA: Einstein." in text
    assert "2. User prefers short answers" in text
    assert "3. [notes] Einstein was born in Ulm." in text
    assert answer == "Einstein was born in Ulm."


def test_recall_formats_the_warm_up_marker():
    from tools.recall import format_results

    body = [
        {
            "source": "system",
            "status": "memory_warming_up",
            "text": "Memory is still warming up: no knowledge graph data exists yet.",
            "datapoint_count": 0,
            "threshold": 10,
        }
    ]
    count, text, answer = format_results(body)
    assert count == 1
    assert "[memory_warming_up] Memory is still warming up" in text
    assert answer.startswith("Memory is still warming up")


def test_recall_handles_empty_and_non_list_bodies():
    from tools.recall import format_results

    assert format_results([]) == (0, "No results.", "")
    assert format_results("plain") == (1, "plain", "plain")


# --- forget ----------------------------------------------------------------------


def test_forget_body_prefers_dataset_id_and_carries_data_id():
    from tools.forget import build_body, describe

    body = build_body({"dataset_name": "notes", "dataset_id": "3f2b", "data_id": "d1"})
    assert body == {"memoryOnly": False, "everything": False, "datasetId": "3f2b", "dataId": "d1"}
    assert describe(body) == "data item 'd1' in dataset '3f2b'"


def test_forget_body_memory_only_by_name():
    from tools.forget import build_body, describe

    body = build_body({"dataset_name": "notes", "memory_only": "true"})
    assert body == {"memoryOnly": True, "everything": False, "dataset": "notes"}
    assert describe(body) == "the memory of dataset 'notes'"


def test_forget_body_everything_needs_an_explicit_true():
    from tools.forget import build_body, describe

    assert build_body({"dataset_name": "n"})["everything"] is False
    body = build_body({"everything": "true"})
    assert body["everything"] is True
    assert describe(body) == "all datasets and data owned by this user"


# --- remember entry --------------------------------------------------------------


def test_remember_entry_body_is_snake_case_qa():
    from tools.remember_entry import build_body

    body = build_body(
        {
            "session_id": "conv-1",
            "question": "Who?",
            "answer": "Einstein.",
            "context": "notes",
            "dataset_name": "main_dataset",
        }
    )
    assert body == {
        "entry": {"type": "qa", "question": "Who?", "answer": "Einstein.", "context": "notes"},
        "session_id": "conv-1",
        "dataset_name": "main_dataset",
    }


def test_remember_entry_body_omits_empty_optionals():
    from tools.remember_entry import build_body

    body = build_body({"session_id": "s", "question": "q", "answer": "a", "dataset_id": "3f2b"})
    assert body == {
        "entry": {"type": "qa", "question": "q", "answer": "a"},
        "session_id": "s",
        "dataset_id": "3f2b",
    }


# --- improve ---------------------------------------------------------------------


def test_improve_body_and_stage_summary():
    from tools.improve import build_body, summarize_stages

    body = build_body(
        {
            "dataset_name": "notes",
            "session_ids": "s1, s2",
            "run_in_background": "true",
            "build_global_context_index": "true",
        }
    )
    assert body == {
        "runInBackground": True,
        "buildGlobalContextIndex": True,
        "datasetName": "notes",
        "sessionIds": ["s1", "s2"],
    }

    result = {
        "status": "completed",
        "stages": [
            {"name": "feedback_weights", "status": "skipped", "reason": "no_session_ids"},
            {"name": "triplet_enrichment", "status": "completed"},
            {"name": "global_context_index", "status": "already_completed"},
        ],
    }
    assert summarize_stages(result) == (2, 3)
    assert summarize_stages({"status": "running"}) == (0, 0)


# --- end to end through the Tool class -------------------------------------------


class _Runtime:
    credentials = {
        "base_url": "http://localhost:8000/",
        "user_email": "default_user@example.com",
        "user_password": "default_password",
    }


class _FakeClient:
    """Stands in for httpx.Client: records the recall request and answers it."""

    def __init__(self, log: list, reply: httpx.Response):
        self._log = log
        self._reply = reply

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self._log.append((url, kwargs))
        request = httpx.Request("POST", url)
        if url.endswith("/api/v1/auth/login"):
            return httpx.Response(200, json={"access_token": "tok"}, request=request)
        self._reply.request = request
        return self._reply


def test_recall_tool_end_to_end(monkeypatch):
    import tools.recall as recall_module
    from dify_plugin.entities.tool import ToolInvokeMessage

    log: list = []
    reply = httpx.Response(
        200,
        json=[
            {
                "source": "graph",
                "text": "Ulm.",
                "kind": "completion",
                "search_type": "HYBRID_COMPLETION",
            }
        ],
    )
    monkeypatch.setattr(recall_module.httpx, "Client", lambda *a, **k: _FakeClient(log, reply))

    tool = recall_module.RecallTool.__new__(recall_module.RecallTool)
    tool.runtime = _Runtime()
    tool.response_type = ToolInvokeMessage
    messages = list(tool._invoke({"query": "Where?", "session_id": "conv-1"}))

    url, kwargs = log[-1]
    assert url == "http://localhost:8000/api/v1/recall"
    assert kwargs["json"]["sessionId"] == "conv-1"
    assert kwargs["json"]["stream"] is False
    assert kwargs["headers"]["Accept"] == "application/json"

    variables = {
        m.message.variable_name: m.message.variable_value
        for m in messages
        if m.type == ToolInvokeMessage.MessageType.VARIABLE
    }
    assert variables["results_count"] == 1
    assert variables["answer"] == "Ulm."
    assert "[graph]" not in variables["results_text"]
    assert "1. Ulm." in variables["results_text"]
