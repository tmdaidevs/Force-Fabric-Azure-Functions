"""Fabric authentication module — manages Azure credentials and token acquisition."""

import os
import time
from typing import Optional, Literal

from azure.identity import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
    ClientSecretCredential,
    InteractiveBrowserCredential,
    AzureCliCredential,
    VisualStudioCodeCredential,
    DeviceCodeCredential,
)
from azure.core.credentials import TokenCredential, AccessToken

FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
SQL_SCOPE = "https://database.windows.net/.default"
KUSTO_SCOPE = "https://kusto.kusto.windows.net/.default"

AuthMethod = Literal[
    "azure_cli",
    "interactive_browser",
    "device_code",
    "vscode",
    "default",
    "service_principal",
    "managed_identity",
]

_credential: Optional[TokenCredential] = None
_cached_token: Optional[AccessToken] = None
_current_auth_method: Optional[AuthMethod] = None
_is_authenticated: bool = False


def get_auth_status() -> dict:
    """Return current authentication status."""
    return {"authenticated": _is_authenticated, "method": _current_auth_method}


def require_auth() -> None:
    """Raise if not authenticated."""
    if not _is_authenticated or _credential is None:
        raise RuntimeError(
            "Not authenticated. In Azure Functions mode, set FABRIC_AUTH_METHOD "
            "and credentials in Application Settings."
        )


def init_server_auth() -> str:
    """Server-side auto-initialization for Azure Functions.
    Reads auth config from environment variables — no interactive login needed.
    """
    method: AuthMethod = os.environ.get("FABRIC_AUTH_METHOD", "default")  # type: ignore[assignment]
    tenant_id = os.environ.get("FABRIC_TENANT_ID")
    client_id = os.environ.get("FABRIC_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("FABRIC_CLIENT_SECRET")

    return login(method, tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)


def login(
    method: AuthMethod,
    *,
    tenant_id: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> str:
    """Authenticate using the specified method and return a status message."""
    global _credential, _cached_token, _is_authenticated, _current_auth_method

    _credential = None
    _cached_token = None
    _is_authenticated = False
    _current_auth_method = None

    if method == "managed_identity":
        _credential = (
            ManagedIdentityCredential(client_id=client_id)
            if client_id
            else ManagedIdentityCredential()
        )

    elif method == "service_principal":
        if not client_id or not client_secret or not tenant_id:
            raise ValueError("Service Principal requires tenantId, clientId, and clientSecret.")
        _credential = ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )

    elif method == "azure_cli":
        _credential = AzureCliCredential()

    elif method == "interactive_browser":
        kwargs: dict = {}
        if tenant_id:
            kwargs["tenant_id"] = tenant_id
        if client_id:
            kwargs["client_id"] = client_id
        _credential = InteractiveBrowserCredential(**kwargs)

    elif method == "device_code":
        kwargs = {}
        if tenant_id:
            kwargs["tenant_id"] = tenant_id
        if client_id:
            kwargs["client_id"] = client_id
        _credential = DeviceCodeCredential(**kwargs)

    elif method == "vscode":
        kwargs = {}
        if tenant_id:
            kwargs["tenant_id"] = tenant_id
        _credential = VisualStudioCodeCredential(**kwargs)

    elif method == "default":
        _credential = DefaultAzureCredential()

    else:
        raise ValueError(
            f'Unknown auth method "{method}". '
            "Available: managed_identity, service_principal, azure_cli, default"
        )

    try:
        _cached_token = _credential.get_token(FABRIC_SCOPE)
        if not _cached_token:
            raise RuntimeError("No token received")
        _is_authenticated = True
        _current_auth_method = method
        expires_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(_cached_token.expires_on)
        )
        return f'Authenticated via "{method}". Token valid until {expires_iso}.'
    except Exception as exc:
        _credential = None
        raise RuntimeError(f'Authentication failed with method "{method}": {exc}') from exc


def logout() -> str:
    """Clear credentials and return a status message."""
    global _credential, _cached_token, _is_authenticated, _current_auth_method

    _credential = None
    _cached_token = None
    _is_authenticated = False
    prev = _current_auth_method
    _current_auth_method = None
    if prev:
        return f'Logged out (was authenticated via "{prev}").'
    return "Not currently logged in."


def get_access_token() -> str:
    """Return a valid Fabric API access token, refreshing if needed."""
    global _cached_token, _is_authenticated

    require_auth()

    # Reuse cached token if still valid (with 5 min buffer)
    if _cached_token and _cached_token.expires_on > time.time() + 5 * 60:
        return _cached_token.token

    _cached_token = _credential.get_token(FABRIC_SCOPE)  # type: ignore[union-attr]
    if not _cached_token:
        _is_authenticated = False
        raise RuntimeError("Token refresh failed. Please use `auth_login` to re-authenticate.")
    return _cached_token.token


def get_token_for_scope(scope: str) -> str:
    """Acquire a token for an arbitrary scope (e.g. SQL, Kusto)."""
    require_auth()
    token = _credential.get_token(scope)  # type: ignore[union-attr]
    if not token:
        raise RuntimeError(f'Failed to acquire token for scope "{scope}".')
    return token.token
