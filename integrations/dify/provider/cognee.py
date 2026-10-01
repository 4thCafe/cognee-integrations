import logging
from typing import Any

import httpx
from dify_plugin import ToolProvider
from dify_plugin.config.logger_format import plugin_logger_handler
from dify_plugin.errors.tool import ToolProviderCredentialValidationError
from tools.cognee_client import normalize_base_url

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(plugin_logger_handler)

NO_PASSWORD_MARKER = "does not have a password"
BAD_CREDENTIALS_MARKER = "LOGIN_BAD_CREDENTIALS"
TIMEOUT = 60


def login_error_message(status_code: int, body: str) -> str:
    """Turn a failed ``POST /api/v1/auth/login`` response into an actionable message.

    Cognee 1.6+ only gives the default user a password when the server is started
    with ``DEFAULT_USER_PASSWORD``; logging in to a password-less account returns
    HTTP 400 with a distinct detail, which is not a wrong-password situation.
    """
    if status_code == 400:
        if NO_PASSWORD_MARKER in body:
            return (
                "This Cognee user has no password. Either configure an API key instead, or start "
                "the Cognee server with DEFAULT_USER_PASSWORD (and DEFAULT_USER_EMAIL) set to the "
                "same values as the User Password and User Email configured here, then retry. "
                "The bundled docker-compose.yml already does this."
            )
        if BAD_CREDENTIALS_MARKER in body:
            return (
                "Invalid email or password. Check your Cognee server credentials "
                "(the defaults are default_user@example.com / default_password, "
                "matching DEFAULT_USER_EMAIL / DEFAULT_USER_PASSWORD on the server)."
            )
        return "Invalid email or password. Check your Cognee server credentials."
    return f"Cognee login failed with status {status_code}: {body}"


def api_key_error_message(status_code: int, body: str) -> str:
    if status_code in (401, 403):
        return (
            "The API key was rejected by the Cognee server. Check the key, or for a self-hosted "
            "server create one with POST /api/v1/auth/api-keys."
        )
    return f"Cognee API key check failed with status {status_code}: {body}"


class CogneeProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        base_url = normalize_base_url(credentials.get("base_url", ""))
        api_key = (credentials.get("api_key") or "").strip()
        user_email = (credentials.get("user_email") or "").strip()
        user_password = credentials.get("user_password") or ""

        logger.info(f"Validating credentials for base_url={base_url}")

        if not base_url:
            raise ToolProviderCredentialValidationError("Cognee Server URL is required")
        if not api_key and not (user_email and user_password):
            raise ToolProviderCredentialValidationError(
                "Provide either an API Key (Cognee Cloud, or a self-hosted server with API keys) "
                "or both User Email and User Password (self-hosted server)."
            )

        # 1. Health check
        try:
            with httpx.Client(trust_env=False, follow_redirects=True) as client:
                response = client.get(f"{base_url}/health", timeout=TIMEOUT)
            logger.info(f"Health check response: {response.status_code}")
            response.raise_for_status()
        except httpx.ConnectError:
            raise ToolProviderCredentialValidationError(
                f"Cannot connect to Cognee server at {base_url}. "
                "Is the server running? For a local server start it with: docker compose up -d"
            )
        except httpx.HTTPStatusError as e:
            raise ToolProviderCredentialValidationError(
                f"Cognee health check failed with status {e.response.status_code}: "
                f"{e.response.text}"
            )
        except Exception as e:
            raise ToolProviderCredentialValidationError(
                f"Failed to connect to Cognee server: {str(e)}"
            )

        # 2. Verify the credentials against an authenticated route
        try:
            with httpx.Client(trust_env=False, follow_redirects=True) as client:
                if api_key:
                    response = client.get(
                        f"{base_url}/api/v1/datasets",
                        headers={"X-Api-Key": api_key},
                        timeout=TIMEOUT,
                    )
                    logger.info(f"API key check response: {response.status_code}")
                    if response.is_error:
                        raise ToolProviderCredentialValidationError(
                            api_key_error_message(response.status_code, response.text)
                        )
                else:
                    response = client.post(
                        f"{base_url}/api/v1/auth/login",
                        data={"username": user_email, "password": user_password},
                        timeout=TIMEOUT,
                    )
                    logger.info(f"Login response: {response.status_code}")
                    if response.is_error:
                        raise ToolProviderCredentialValidationError(
                            login_error_message(response.status_code, response.text)
                        )
                    if "access_token" not in response.json():
                        raise ToolProviderCredentialValidationError(
                            "Login succeeded but no access token was returned"
                        )
        except ToolProviderCredentialValidationError:
            raise
        except Exception as e:
            logger.error(f"Credential validation failed: {e}")
            raise ToolProviderCredentialValidationError(
                f"Failed to authenticate with Cognee server: {str(e)}"
            )
