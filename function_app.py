import logging
import os
import re

import azure.functions as func

from mcp_server import mcp

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# --- FastMCP ASGI adapter ---
_asgi_app = mcp.http_app(path="/")


async def _run_asgi(req: func.HttpRequest, route: str = "") -> func.HttpResponse:
    """Forward an Azure Functions request to the FastMCP ASGI app."""
    import asyncio
    from io import BytesIO

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": req.method,
        "path": f"/mcp/{route}",
        "query_string": (req.url.split("?", 1)[1] if "?" in req.url else "").encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in req.headers.items()],
        "root_path": "",
    }

    body = req.get_body()
    body_sent = False
    response_started = False
    status_code = 200
    response_headers = []
    response_body = BytesIO()

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        # Keep connection alive for SSE
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal response_started, status_code, response_headers
        if message["type"] == "http.response.start":
            response_started = True
            status_code = message["status"]
            response_headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            response_body.write(message.get("body", b""))

    await _asgi_app(scope, receive, send)

    headers = {k.decode(): v.decode() for k, v in response_headers}
    return func.HttpResponse(
        body=response_body.getvalue(),
        status_code=status_code,
        headers=headers,
    )


@app.route(route="mcp/{*route}", methods=["GET", "POST", "DELETE", "OPTIONS"])
async def mcp_handler(req: func.HttpRequest) -> func.HttpResponse:
    """Catch-all route forwarding to FastMCP."""
    route = req.route_params.get("route", "")
    return await _run_asgi(req, route)


# --- Health endpoint ---
@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse('{"status":"ok"}', mimetype="application/json")


# --- Teams Bot endpoint (lazy imports for compatibility) ---
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


# --- Daily scan timer trigger (lazy import to avoid startup failures) ---
try:
    from orchestration.daily_scan import register_daily_scan
    register_daily_scan(app)
except ImportError as e:
    logging.warning(f"Daily scan not registered (missing dep): {e}")
