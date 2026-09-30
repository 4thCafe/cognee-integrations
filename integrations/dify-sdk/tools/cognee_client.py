"""Shared helpers for the tools that call the Cognee 1.6 memory API.

Kept deliberately small: credential lookup, login, header construction,
parameter parsing and error formatting. Each tool still owns its request.
"""

from typing import Any

import httpx

LOGIN_TIMEOUT = 60


def credentials(runtime: Any) -> tuple[str, str, str]:
    """Return ``(base_url, user_email, user_password)`` from the provider credentials."""
    creds = runtime.credentials
    return (
        creds["base_url"].rstrip("/"),
        creds["user_email"],
        creds["user_password"],
    )


def login(client: httpx.Client, base_url: str, email: str, password: str) -> str:
    """Log in with the OAuth2 password form and return the bearer token."""
    response = client.post(
        f"{base_url}/api/v1/auth/login",
        data={"username": email, "password": password},
        timeout=LOGIN_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def auth_headers(token: str, *, json: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if json:
        headers["Content-Type"] = "application/json"
    return headers


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
