"""Tool handlers for Fabric authentication."""

from auth.fabric_auth import login, logout, get_auth_status


def auth_login(args: dict) -> str:
    try:
        result = login(
            args.get("method", "default"),
            tenant_id=args.get("tenantId"),
            client_id=args.get("clientId"),
            client_secret=args.get("clientSecret"),
        )
        return result
    except Exception as e:
        return f"❌ Login failed: {e}"


def auth_status(args: dict) -> str:
    try:
        status = get_auth_status()

        if not status.get("authenticated"):
            return "\n".join([
                "## ❌ Not Authenticated",
                "",
                "You are not logged in to Fabric. Use `auth_login` to connect.",
                "",
                "### Available Login Methods",
                "",
                "| Method | Description |",
                "|--------|-------------|",
                "| `azure_cli` | Use existing Azure CLI session (`az login`) — **recommended for development** |",
                "| `interactive_browser` | Opens a browser window for interactive login |",
                "| `device_code` | Login via device code (useful for headless/remote environments) |",
                "| `vscode` | Use VS Code's Azure account |",
                "| `service_principal` | Use a service principal (requires tenantId, clientId, clientSecret) |",
                "| `default` | Auto-detect (tries CLI, managed identity, env vars, VS Code, etc.) |",
            ])

        return "\n".join([
            "## ✅ Authenticated",
            "",
            f"- **Method**: {status.get('method', 'unknown')}",
            "",
            "You are connected to Fabric and ready to use optimization tools.",
        ])
    except Exception as e:
        return f"❌ Failed to get auth status: {e}"


def auth_logout(args: dict) -> str:
    try:
        return logout()
    except Exception as e:
        return f"❌ Logout failed: {e}"


auth_tools = [
    {
        "name": "auth_login",
        "description": (
            "Login to Microsoft Fabric. MUST be called before using any other tool. "
            "Choose a login method: azure_cli (recommended), interactive_browser, "
            "device_code, vscode, service_principal, or default."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "description": (
                        "Authentication method. Options: "
                        "'azure_cli' (use existing az login session — recommended), "
                        "'interactive_browser' (opens browser for login), "
                        "'device_code' (device code flow for headless environments), "
                        "'vscode' (use VS Code Azure account), "
                        "'service_principal' (requires tenantId, clientId, clientSecret), "
                        "'default' (auto-detect best available method)."
                    ),
                },
                "tenantId": {
                    "type": "string",
                    "description": "Azure Tenant ID (optional, needed for interactive_browser, device_code, service_principal)",
                },
                "clientId": {
                    "type": "string",
                    "description": "Azure App Registration Client ID (optional, needed for interactive_browser, service_principal)",
                },
                "clientSecret": {
                    "type": "string",
                    "description": "Client secret (only for service_principal method)",
                },
            },
            "required": ["method"],
        },
        "handler": auth_login,
    },
    {
        "name": "auth_status",
        "description": (
            "Check if you are currently authenticated to Microsoft Fabric. "
            "Shows the login method and available authentication options."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": auth_status,
    },
    {
        "name": "auth_logout",
        "description": "Logout from Microsoft Fabric and clear cached credentials.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": auth_logout,
    },
]
