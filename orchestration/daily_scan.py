import os
import logging
import azure.functions as func

from auth.fabric_auth import init_server_auth
from clients.fabric_client import (
    list_workspaces,
    list_workspace_items,
    get_workspace_admins,
)
from clients.graph_client import send_proactive_message
from tools.lakehouse import lakehouse_optimization_recommendations
from tools.warehouse import warehouse_optimization_recommendations
from tools.eventhouse import eventhouse_optimization_recommendations
from tools.semantic_model import semantic_model_optimization_recommendations

logger = logging.getLogger(__name__)

EXCLUDED = set(
    os.environ.get("SCAN_EXCLUDED_WORKSPACES", "").split(",")
)


def register_daily_scan(app):
    """Register the daily scan timer trigger on the given app."""

    @app.timer_trigger(schedule="0 0 7 * * *", arg_name="timer", run_on_startup=False)
    def daily_scan_timer(timer: func.TimerRequest) -> None:
        logger.info("Daily scan started")
        init_server_auth()

        workspaces = list_workspaces()
        total_issues = 0
        total_notified = 0

        for ws in workspaces:
            if ws["id"] in EXCLUDED or ws.get("displayName", "") in EXCLUDED:
                continue
            if ws.get("type") == "Personal":
                continue

            try:
                result = scan_and_notify(ws)
                total_issues += result.get("issues", 0)
                total_notified += result.get("notified", 0)
            except Exception as e:
                logger.error(f"Failed to scan {ws.get('displayName', ws['id'])}: {e}")

        logger.info(f"Daily scan complete: {len(workspaces)} workspaces, {total_issues} issues, {total_notified} notified")


def scan_and_notify(workspace: dict) -> dict:
    ws_id = workspace["id"]
    ws_name = workspace.get("displayName", ws_id)
    logger.info(f"Scanning workspace: {ws_name}")

    items = list_workspace_items(ws_id)
    findings = []

    for item in items:
        item_type = item.get("type", "")
        item_id = item["id"]
        item_name = item.get("displayName", item_id)

        try:
            result = None
            if item_type == "Lakehouse":
                result = lakehouse_optimization_recommendations({"workspaceId": ws_id, "lakehouseId": item_id})
            elif item_type == "Warehouse":
                result = warehouse_optimization_recommendations({"workspaceId": ws_id, "warehouseId": item_id})
            elif item_type == "Eventhouse":
                result = eventhouse_optimization_recommendations({"workspaceId": ws_id, "eventhouseId": item_id})
            elif item_type == "SemanticModel":
                result = semantic_model_optimization_recommendations({"workspaceId": ws_id, "semanticModelId": item_id})

            if result and ("\U0001f534" in result or "\U0001f7e1" in result):
                findings.append({"item_name": item_name, "item_type": item_type, "result": result})
        except Exception as e:
            logger.warning(f"Scan failed for {item_name}: {e}")

    if not findings:
        logger.info(f"No issues in {ws_name}")
        return {"workspace": ws_name, "issues": 0}

    admins = get_workspace_admins(ws_id)
    if not admins:
        logger.warning(f"No admins found for {ws_name}")
        return {"workspace": ws_name, "issues": len(findings), "notified": 0}

    message = format_scan_message(ws_name, ws_id, findings)
    notified = 0
    for admin in admins:
        principal = admin.get("principal", {})
        principal_id = principal.get("id")
        if principal_id and principal.get("type") in ("User", "user"):
            try:
                send_proactive_message(principal_id, message)
                notified += 1
                logger.info(f"Notified {principal.get('displayName', principal_id)} for {ws_name}")
            except Exception as e:
                logger.warning(f"Failed to notify {principal_id}: {e}")

    return {"workspace": ws_name, "issues": len(findings), "notified": notified}


def format_scan_message(ws_name: str, ws_id: str, findings: list) -> str:
    portal_url = f"https://app.fabric.microsoft.com/groups/{ws_id}"
    html = (
        f'<h2>\U0001f50d Fabric Optimizer \u2014 Daily Scan</h2>'
        f'<p><strong>Workspace:</strong> <a href="{portal_url}">{ws_name}</a></p>'
        f'<p><strong>Issues found:</strong> {len(findings)} item(s) need attention</p><hr/>'
    )
    for f in findings:
        red = f["result"].count("\U0001f534")
        yellow = f["result"].count("\U0001f7e1")
        severity = "\U0001f534 " * min(red, 3) + "\U0001f7e1 " * min(yellow, 3)
        html += f"<p><strong>{f['item_type']}: {f['item_name']}</strong> {severity}</p>"
    html += (
        '<hr/><p>Reply to discuss findings with the Fabric Optimizer agent.</p>'
        '<p>Say <strong>"approve"</strong> to preview and apply fixes (dry run first).</p>'
    )
    return html
