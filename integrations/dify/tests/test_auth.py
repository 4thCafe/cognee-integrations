"""Authentication helpers, provider validation and the ported dataset tools."""

import sys
from pathlib import Path

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


# --- base url normalization --------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://tenant-xxx.cloud.cognee.ai",
        "https://tenant-xxx.cloud.cognee.ai/",
        "https://tenant-xxx.cloud.cognee.ai/api",
        "https://tenant-xxx.cloud.cognee.ai/api/",
        "https://tenant-xxx.cloud.cognee.ai/api/v1",
        "  https://tenant-xxx.cloud.cognee.ai/api/v1/  ",
    ],
)
def test_normalize_base_url_strips_api_suffixes(raw):
    from tools.cognee_client import normalize_base_url

    assert normalize_base_url(raw) == "https://tenant-xxx.cloud.cognee.ai"


def test_normalize_base_url_keeps_other_paths():
    from tools.cognee_client import normalize_base_url

    assert normalize_base_url("http://localhost:8000") == "http://localhost:8000"
    assert normalize_base_url("http://proxy/cognee/") == "http://proxy/cognee"
    assert normalize_base_url("") == ""


# --- auth headers ------------------------------------------------------------------


class _NoCallClient:
    def post(self, *a, **k):
        raise AssertionError("login must not be called when an API key is set")


def test_api_key_wins_and_skips_login():
    from tools.cognee_client import auth_headers

    headers = auth_headers(
        {"base_url": "http://x", "api_key": " key-1 ", "user_email": "a@b", "user_password": "p"},
        _NoCallClient(),
        json=True,
    )
    assert headers == {
        "X-Api-Key": "key-1",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


class _LoginClient:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return httpx.Response(200, json={"access_token": "tok"}, request=httpx.Request("POST", url))


def test_email_password_logs_in_against_the_normalized_url():
    from tools.cognee_client import auth_headers

    client = _LoginClient()
    headers = auth_headers(
        {
            "base_url": "http://localhost:8000/api",
            "api_key": "",
            "user_email": "u@e",
            "user_password": "pw",
        },
        client,
    )
    assert headers == {"Authorization": "Bearer tok", "Accept": "application/json"}
    url, kwargs = client.calls[0]
    assert url == "http://localhost:8000/api/v1/auth/login"
    assert kwargs["data"] == {"username": "u@e", "password": "pw"}


def test_missing_credentials_raise_a_clear_error():
    from tools.cognee_client import auth_headers

    with pytest.raises(ValueError, match="API key or a user email and password"):
        auth_headers({"base_url": "http://x", "user_email": "u@e"}, _LoginClient())


# --- provider validation -------------------------------------------------------------


class _FakeClient:
    """Health succeeds; authenticated routes answer with ``reply``; records requests."""

    def __init__(self, log, reply):
        self._log = log
        self._reply = reply

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _answer(self, method, url, kwargs):
        self._log.append((method, url, kwargs))
        request = httpx.Request(method, url)
        if url.endswith("/health"):
            return httpx.Response(200, json={"status": "ready"}, request=request)
        self._reply.request = request
        return self._reply

    def get(self, url, **kwargs):
        return self._answer("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._answer("POST", url, kwargs)


def _provider(monkeypatch, reply):
    import provider.cognee as provider_module

    log = []
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: _FakeClient(log, reply))
    return provider_module.CogneeProvider.__new__(provider_module.CogneeProvider), log


def test_provider_accepts_an_api_key_via_the_datasets_route(monkeypatch):
    provider, log = _provider(monkeypatch, httpx.Response(200, json=[]))
    provider._validate_credentials({"base_url": "https://t.cloud.cognee.ai/api", "api_key": "k"})
    method, url, kwargs = log[-1]
    assert (method, url) == ("GET", "https://t.cloud.cognee.ai/api/v1/datasets")
    assert kwargs["headers"] == {"X-Api-Key": "k"}


def test_provider_rejects_a_bad_api_key(monkeypatch):
    from dify_plugin.errors.tool import ToolProviderCredentialValidationError

    provider, _ = _provider(monkeypatch, httpx.Response(401, json={"detail": "Unauthorized"}))
    with pytest.raises(ToolProviderCredentialValidationError, match="API key was rejected"):
        provider._validate_credentials({"base_url": "http://localhost:8000", "api_key": "bad"})


def test_provider_requires_one_credential_method(monkeypatch):
    from dify_plugin.errors.tool import ToolProviderCredentialValidationError

    provider, log = _provider(monkeypatch, httpx.Response(200, json=[]))
    with pytest.raises(ToolProviderCredentialValidationError, match="either an API Key"):
        provider._validate_credentials({"base_url": "http://localhost:8000", "user_email": "u@e"})
    assert log == []  # rejected before any request


def test_provider_falls_back_to_login_without_an_api_key(monkeypatch):
    provider, log = _provider(monkeypatch, httpx.Response(200, json={"access_token": "t"}))
    provider._validate_credentials(
        {
            "base_url": "http://localhost:8000",
            "api_key": "",
            "user_email": "u@e",
            "user_password": "p",
        }
    )
    method, url, _ = log[-1]
    assert (method, url) == ("POST", "http://localhost:8000/api/v1/auth/login")


# --- ported dataset tools ------------------------------------------------------------


def test_get_datasets_formatting():
    from tools.get_datasets import format_datasets

    count, text = format_datasets([{"id": "1", "name": "notes", "createdAt": "2026-09-30"}])
    assert count == 1
    assert "1. notes (id: 1, created: 2026-09-30)" in text
    assert format_datasets([]) == (0, "No datasets found.")


def test_get_dataset_data_formatting():
    from tools.get_dataset_data import format_items

    count, text = format_items(
        [{"id": "d1", "name": "a.txt", "extension": "txt", "createdAt": "x"}]
    )
    assert count == 1
    assert "1. a.txt (id: d1, type: txt, created: x)" in text
    assert format_items({"unexpected": True}) == (0, "No data items found in this dataset.")


def test_add_data_extracts_the_first_data_id():
    from tools.add_data import first_data_id

    body = {
        "status": "PipelineRunCompleted",
        "data_ingestion_info": [{"data_id": "a"}, {"data_id": "b"}],
    }
    assert first_data_id(body) == ("a", 2)
    assert first_data_id([body, {"data_ingestion_info": [{"data_id": "c"}]}]) == ("a", 3)
    assert first_data_id({"status": "ok"}) == ("", 0)


def test_search_body_carries_node_name_and_verbose():
    from tools.search import build_body

    body = build_body({"query": "q", "node_name": "people, places", "verbose": "true", "top_k": 3})
    assert body["nodeName"] == ["people", "places"]
    assert body["verbose"] is True
    assert body["topK"] == 3
    assert body["searchType"] == "GRAPH_COMPLETION"


def test_search_formats_verbose_entries():
    from tools.search import format_results

    body = [
        {
            "dataset_name": "notes",
            "text_result": "answer",
            "context_result": "ctx",
            "objects_result": [],
        }
    ]
    _, text = format_results(body)
    assert "1. [notes] answer" in text
