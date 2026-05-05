"""FastMCP server for Fabric optimization tools."""

import inspect
import logging
import re
from typing import Optional

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from auth.fabric_auth import init_server_auth, require_auth
from clients.fabric_client import list_workspaces
from tools import all_tools, AUTH_TOOL_NAMES

GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_auth_initialized = False

mcp = FastMCP(
    "Fabric Optimize MCP Server",
    instructions=(
        "Fabric optimization server with 30 tools for scanning and fixing "
        "Lakehouses, Warehouses, Eventhouses, Semantic Models, and Gateways."
    ),
)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _ensure_auth():
    global _auth_initialized
    if not _auth_initialized:
        init_server_auth()
        _auth_initialized = True


def _resolve_workspace_id(value: str) -> str:
    if GUID_RE.match(value):
        return value
    workspaces = list_workspaces()
    clean = value.lower().replace("-", "")
    for ws in workspaces:
        if ws["displayName"].lower() in (clean, value.lower()):
            return ws["id"]
    raise ValueError(f'Workspace "{value}" not found.')


def _build_tool_function(tool_def):
    """Build a function with explicit parameters from a tool's input_schema."""
    schema = tool_def.get("input_schema", {})
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    param_names = list(props.keys())

    # Build the function signature dynamically using inspect.Parameter
    params = []
    for name in param_names:
        if name in required:
            params.append(inspect.Parameter(
                name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
            ))
        else:
            params.append(inspect.Parameter(
                name, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=None, annotation=Optional[str]
            ))

    def _execute(tool=tool_def, **kwargs) -> str:
        try:
            _ensure_auth()
            require_auth()
            args = {k: v for k, v in kwargs.items() if v is not None}
            if "workspaceId" in args and not GUID_RE.match(str(args["workspaceId"])):
                args["workspaceId"] = _resolve_workspace_id(str(args["workspaceId"]))
            return tool["handler"](args)
        except Exception as e:
            logging.error(f"Tool {tool['name']} error: {e}")
            return f"Error: {str(e)}"

    # Create wrapper with proper signature
    sig = inspect.Signature(params, return_annotation=str)

    def wrapper(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return _execute(tool=tool_def, **bound.arguments)

    wrapper.__signature__ = sig
    wrapper.__name__ = tool_def["name"]
    wrapper.__doc__ = tool_def["description"]
    return wrapper


# Register all tools with FastMCP
for _tool in all_tools:
    if _tool["name"] in AUTH_TOOL_NAMES:
        continue
    _fn = _build_tool_function(_tool)
    mcp.tool(name=_tool["name"], description=_tool["description"])(_fn)
