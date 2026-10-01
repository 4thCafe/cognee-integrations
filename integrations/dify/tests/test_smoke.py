import re
import sys
from pathlib import Path

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

COGNEE_VERSION = "1.6.1"


def test_imports():
    from dify_plugin import DifyPluginEnv, Plugin

    assert Plugin is not None
    assert DifyPluginEnv is not None


# --- docker-compose.yml: the server setup cognee 1.6 needs -------------------


def _compose_text() -> str:
    return (PLUGIN_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")


def test_compose_pins_the_tested_cognee_image():
    assert f"image: cognee/cognee:{COGNEE_VERSION}" in _compose_text()


def test_compose_gives_the_default_user_a_password():
    # Since cognee 1.6 the default user can only log in when the server was
    # started with DEFAULT_USER_PASSWORD; the plugin logs in with a password.
    text = _compose_text()
    assert "DEFAULT_USER_PASSWORD=default_password" in text
    assert "DEFAULT_USER_EMAIL=default_user@example.com" in text


def test_compose_persists_the_1_6_storage_paths():
    text = _compose_text()
    assert ":/cognee-storage/system" in text
    assert ":/cognee-storage/data" in text
    assert "/app/cognee/.cognee_system" not in text


def test_readme_and_manifest_agree_on_versions():
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    manifest = (PLUGIN_ROOT / "manifest.yaml").read_text(encoding="utf-8")
    pyproject = (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert f"Tested with Cognee v{COGNEE_VERSION}" in readme
    assert f"image: cognee/cognee:{COGNEE_VERSION}" in readme
    assert f"pip install cognee=={COGNEE_VERSION}" in readme

    plugin_version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert f"version: {plugin_version}" in manifest
    assert f"**Version:** {plugin_version}" in readme


# --- provider: login failure messages ----------------------------------------


def test_login_error_maps_the_password_less_default_user():
    from provider.cognee import login_error_message

    body = '{"detail":"This user does not have a password. Use API key authentication."}'
    message = login_error_message(400, body)
    assert "DEFAULT_USER_PASSWORD" in message
    assert "Invalid email or password" not in message


def test_login_error_keeps_the_wrong_password_message():
    from provider.cognee import login_error_message

    message = login_error_message(400, '{"detail":"LOGIN_BAD_CREDENTIALS"}')
    assert message.startswith("Invalid email or password")
    assert "DEFAULT_USER_PASSWORD" in message


def test_login_error_reports_other_statuses_verbatim():
    from provider.cognee import login_error_message

    assert login_error_message(503, "down") == "Cognee login failed with status 503: down"


class _FakeClient:
    """Stands in for httpx.Client: /health succeeds, login returns ``login_response``."""

    def __init__(self, login_response: httpx.Response):
        self._login_response = login_response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        return httpx.Response(200, json={"status": "ready"}, request=httpx.Request("GET", url))

    def post(self, url, **kwargs):
        self._login_response.request = httpx.Request("POST", url)
        return self._login_response


def test_validate_credentials_surfaces_the_default_user_hint(monkeypatch):
    import provider.cognee as provider_module
    from dify_plugin.errors.tool import ToolProviderCredentialValidationError

    no_password = httpx.Response(
        400,
        json={"detail": "This user does not have a password. Use API key authentication."},
    )
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: _FakeClient(no_password))

    provider = provider_module.CogneeProvider.__new__(provider_module.CogneeProvider)
    with pytest.raises(ToolProviderCredentialValidationError) as excinfo:
        provider._validate_credentials(
            {
                "base_url": "http://localhost:8000",
                "user_email": "default_user@example.com",
                "user_password": "default_password",
            }
        )
    assert "DEFAULT_USER_PASSWORD" in str(excinfo.value)


# --- search: result formatting -------------------------------------------------


def test_format_results_reads_the_per_dataset_search_result():
    from tools.search import format_results

    body = [
        {
            "dataset_id": "3f2b...",
            "dataset_name": "notes",
            "search_result": "Cognee turns documents into AI memory.",
        },
        {
            "dataset_id": "9a1c...",
            "dataset_name": "empty",
            "search_result": "",
            "error": "no data has been added; add data and run cognify before searching.",
        },
    ]
    count, text = format_results(body)
    assert count == 2
    assert "1. [notes] Cognee turns documents into AI memory." in text
    assert "2. [empty] error: no data has been added" in text
    assert "dataset_id" not in text


def test_format_results_joins_retrieval_only_lists():
    from tools.search import format_results

    body = [{"dataset_name": "notes", "search_result": [{"text": "chunk one"}, "chunk two"]}]
    _, text = format_results(body)
    assert "[notes] chunk one\nchunk two" in text


def test_format_results_handles_bare_results_and_scalars():
    from tools.search import format_results

    count, text = format_results(["a completion string", {"content": "legacy"}])
    assert count == 2
    assert "1. a completion string" in text
    assert "2. legacy" in text

    assert format_results({"unexpected": True}) == (1, "{'unexpected': True}")
