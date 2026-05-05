"""Livy API client — run Spark SQL directly via Fabric Livy sessions (no notebooks needed)."""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from auth.fabric_auth import get_access_token

FABRIC_API_BASE = "https://api.fabric.microsoft.com"
LIVY_API_VERSION = "2023-12-01"


@dataclass
class LivyStatementResult:
    status: str  # "ok" | "error"
    output: Optional[str] = None
    error: Optional[str] = None
    traceback: Optional[List[str]] = None


@dataclass
class LivyJobResult:
    table: str
    fixId: str
    description: str
    status: str  # "ok" | "error"
    output: Optional[str] = None
    error: Optional[str] = None


def _livy_fetch(url: str, method: str = "GET", body: Any = None) -> Dict[str, Any]:
    """Authenticated fetch against the Livy API."""
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    resp = requests.request(method, url, headers=headers, json=body)
    status = resp.status_code

    if status in (200, 202, 204):
        text = resp.text
        return json.loads(text) if text.strip() else {}

    raise RuntimeError(f"Livy API error ({status}): {resp.text}")


def _build_session_base_url(workspace_id: str, lakehouse_id: str) -> str:
    return (
        f"{FABRIC_API_BASE}/v1/workspaces/{quote(workspace_id, safe='')}"
        f"/lakehouses/{quote(lakehouse_id, safe='')}"
        f"/livyapi/versions/{LIVY_API_VERSION}/sessions"
    )


def _create_session(
    workspace_id: str, lakehouse_id: str, max_retries: int = 3
) -> Dict[str, str]:
    """Create a Livy Spark session with retry/backoff for cold-start scenarios."""
    base_url = _build_session_base_url(workspace_id, lakehouse_id)

    for attempt in range(max_retries):
        try:
            data = _livy_fetch(base_url, method="POST", body={})
            return {"sessionId": str(data.get("id", "")), "baseUrl": base_url}
        except Exception as exc:
            msg = str(exc)
            if re.search(r"429|500|502|503|504", msg) and attempt < max_retries - 1:
                wait = 10 * (attempt + 1)
                time.sleep(wait)
                continue
            raise

    raise RuntimeError("Failed to create Livy session after retries.")


def _wait_for_session(session_url: str, timeout_s: float = 300) -> None:
    """Wait for a Livy session to reach 'idle' state."""
    start = time.time()
    poll_interval = 5.0

    while time.time() - start < timeout_s:
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.3, 15.0)

        data = _livy_fetch(session_url)
        state = data.get("state", "")

        if state == "idle":
            return
        if state in ("dead", "error", "killed", "shutting_down"):
            raise RuntimeError(f"Livy session entered terminal state: {state}")

    raise RuntimeError(f"Livy session did not become idle within {timeout_s}s.")


def _execute_statement(
    session_url: str, code: str, timeout_s: float = 300
) -> LivyStatementResult:
    """Submit a PySpark statement and wait for its result."""
    statements_url = f"{session_url}/statements"

    stmt_info = _livy_fetch(statements_url, method="POST", body={"code": code, "kind": "pyspark"})
    stmt_url = f"{statements_url}/{stmt_info.get('id', 0)}"
    start = time.time()

    while time.time() - start < timeout_s:
        time.sleep(3)
        stmt = _livy_fetch(stmt_url)

        if stmt.get("state") == "available":
            output = stmt.get("output")
            if not output:
                return LivyStatementResult(status="error", error="No output from statement")
            if output.get("status") == "ok":
                return LivyStatementResult(
                    status="ok",
                    output=(output.get("data") or {}).get("text/plain", ""),
                )
            return LivyStatementResult(
                status="error",
                error=output.get("evalue", "Unknown error"),
                traceback=output.get("traceback"),
            )

        if stmt.get("state") in ("error", "cancelled"):
            return LivyStatementResult(status="error", error=f"Statement {stmt['state']}")

    return LivyStatementResult(status="error", error="Statement execution timeout")


def _delete_session(session_url: str) -> None:
    """Delete a Livy session (best-effort cleanup)."""
    try:
        _livy_fetch(session_url, method="DELETE")
    except Exception:
        pass


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def run_spark_fixes_via_livy(
    workspace_id: str,
    lakehouse_id: str,
    commands: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Run Spark SQL fixes on lakehouse tables via Livy API.
    Creates a session, executes one statement per table, cleans up.

    commands: list of {"table": str, "fixId": str, "description": str, "code": str}
    Returns: {"results": [...], "sessionCleanedUp": True}
    """
    results: List[Dict[str, Any]] = []

    session_info = _create_session(workspace_id, lakehouse_id)
    session_url = f"{session_info['baseUrl']}/{session_info['sessionId']}"

    try:
        _wait_for_session(session_url)

        for cmd in commands:
            result = _execute_statement(session_url, cmd["code"])
            results.append({
                "table": cmd["table"],
                "fixId": cmd["fixId"],
                "description": cmd["description"],
                "status": result.status,
                "output": result.output,
                "error": result.error,
            })
    finally:
        _delete_session(session_url)

    return {"results": results, "sessionCleanedUp": True}
