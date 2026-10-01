"""Shared helpers for the tools that call the Cognee API.

Kept deliberately small: credential lookup, authentication, header
construction, parameter parsing and error formatting. Each tool still owns
its request.

Authentication works against both deployments of the same ``/api/v1`` API:

* an **API key** (Cognee Cloud, or a self-hosted server with a key) is sent
  as ``X-Api-Key`` on every request;
* **email and password** (self-hosted default user) are exchanged for a
  bearer token at ``POST /api/v1/auth/login`` once per tool call.

The API key wins when both are configured.
"""

from typing import Any

import httpx

LOGIN_TIMEOUT = 60
API_PREFIX = "/api/v1"


def normalize_base_url(raw: str) -> str:
    """Server root the ``/api/v1`` routes hang off.

    Accepts ``https://host``, ``https://host/``, ``https://host/api`` and
    ``https://host/api/v1`` and returns ``https://host``. The ``/api`` form
    is what earlier versions of this plugin asked Cognee Cloud users to
    enter, so it keeps working after an upgrade.
    """
    url = (raw or "").strip().rstrip("/")
    for suffix in (API_PREFIX, "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


def base_url_of(runtime: Any) -> str:
    return normalize_base_url(runtime.credentials.get("base_url", ""))


def login(client: httpx.Client, base_url: str, email: str, password: str) -> str:
    """Log in with the OAuth2 password form and return the bearer token."""
    response = client.post(
        f"{base_url}{API_PREFIX}/auth/login",
        data={"username": email, "password": password},
        timeout=LOGIN_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def auth_headers(
    credentials: dict[str, Any], client: httpx.Client, *, json: bool = False
) -> dict[str, str]:
    """Headers that authenticate a request for the given provider credentials."""
    api_key = (credentials.get("api_key") or "").strip()
    if api_key:
        headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    else:
        email = credentials.get("user_email") or ""
        password = credentials.get("user_password") or ""
        if not email or not password:
            raise ValueError("Configure either an API key or a user email and password for Cognee")
        token = login(client, normalize_base_url(credentials.get("base_url", "")), email, password)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if json:
        headers["Content-Type"] = "application/json"
    return headers


def authorize(client: httpx.Client, runtime: Any, *, json: bool = False) -> dict[str, str]:
    """``auth_headers`` for a tool's runtime credentials."""
    return auth_headers(runtime.credentials, client, json=json)


def split_csv(value: Any) -> list[str]:
    """Split a comma-separated tool parameter into a list of non-empty items."""
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def as_bool(value: Any) -> bool:
    """Interpret a Dify select/boolean parameter (``"true"``/``True``) as a bool."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def parse_json(response: httpx.Response, default: Any = None) -> Any:
    try:
        return response.json()
    except Exception:
        return default if default is not None else {"status": "ok"}


def error_text(exc: Exception, action: str) -> str:
    """One-line description of a failed call, with the server body when there is one."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Cognee API error {exc.response.status_code}: {exc.response.text}"
    return f"{action}: {exc}"
