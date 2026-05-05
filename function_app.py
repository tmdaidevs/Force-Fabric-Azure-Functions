import json
import logging
import os

import azure.functions as func

from mcp_server import mcp

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


# --- MCP JSON-RPC handler (direct, no ASGI) ---
@app.route(route="mcp", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def mcp_handler(req: func.HttpRequest) -> func.HttpResponse:
    """Handle MCP JSON-RPC requests directly via FastMCP."""
    try:
        body = req.get_json()
        method = body.get("method", "")
        params = body.get("params", {})
        req_id = body.get("id")

        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "Fabric Optimize MCP Server",
                    "version": "1.0.0",
                },
            }
        elif method == "tools/list":
            tools = []
            for tool in mcp._tool_manager.list_tools():
                schema = (
                    tool.parameters.model_json_schema()
                    if tool.parameters
                    else {"type": "object", "properties": {}}
                )
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": schema,
                })
            result = {"tools": tools}
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            tool_result = await mcp.call_tool(tool_name, arguments)
            content = []
            for item in tool_result:
                text = item.text if hasattr(item, "text") else str(item)
                content.append({"type": "text", "text": text})
            result = {"content": content, "isError": False}
        elif method == "ping":
            result = {}
        else:
            return func.HttpResponse(
                json.dumps({
                    "jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown method: {method}"},
                }),
                mimetype="application/json",
            )

        return func.HttpResponse(
            json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}),
            mimetype="application/json",
        )
    except Exception as e:
        logging.error(f"MCP error: {e}", exc_info=True)
        return func.HttpResponse(
            json.dumps({
                "jsonrpc": "2.0", "id": body.get("id") if "body" in dir() else None,
                "error": {"code": -32603, "message": str(e)},
            }),
            mimetype="application/json",
        )


# --- Health endpoint ---
@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse('{"status":"ok"}', mimetype="application/json")


# --- Teams Bot endpoint (lazy imports for Python 3.13 compatibility) ---
_bot_adapter = None
_bot_instance = None


def _get_bot():
    global _bot_adapter, _bot_instance
    if _bot_adapter is None:
        from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
        from bot.teams_bot import FabricOptimizerBot
        _bot_adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(
            app_id=os.environ.get("MicrosoftAppId", ""),
            app_password=os.environ.get("MicrosoftAppPassword", ""),
        ))
        _bot_instance = FabricOptimizerBot()
    return _bot_adapter, _bot_instance


@app.route(route="messages", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def messages(req: func.HttpRequest) -> func.HttpResponse:
    """Teams bot messages endpoint."""
    from botbuilder.schema import Activity
    if "application/json" not in (req.headers.get("Content-Type") or ""):
        return func.HttpResponse(status_code=415)
    adapter, bot = _get_bot()
    body = req.get_json()
    activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    async def _turn_callback(turn_context):
        await bot.on_turn(turn_context)

    await adapter.process_activity(activity, auth_header, _turn_callback)
    return func.HttpResponse(status_code=200)


# --- Daily scan timer trigger ---
try:
    from orchestration.daily_scan import register_daily_scan
    register_daily_scan(app)
except ImportError as e:
    logging.warning(f"Daily scan not registered (missing dep): {e}")
