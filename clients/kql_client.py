"""KQL client — execute queries and management commands against Eventhouse/Kusto endpoints."""

import re
from typing import Any, Dict, List, Optional

import requests

from auth.fabric_auth import get_token_for_scope, KUSTO_SCOPE

KqlRow = Dict[str, Any]

DEFAULT_QUERY_TIMEOUT_S = 120


def _parse_kql_table(table: Dict[str, Any]) -> List[KqlRow]:
    """Convert a KQL result table (columns + rows) into a list of dicts."""
    columns = table.get("Columns", [])
    rows_data = table.get("Rows", [])
    result: List[KqlRow] = []
    for row in rows_data:
        obj: KqlRow = {}
        for i, col in enumerate(columns):
            obj[col["ColumnName"]] = row[i] if i < len(row) else None
        result.append(obj)
    return result


def execute_kql_query(
    cluster_uri: str,
    database: str,
    query: str,
    timeout_s: int = DEFAULT_QUERY_TIMEOUT_S,
) -> List[KqlRow]:
    """Execute a KQL query against an Eventhouse/Kusto endpoint."""
    token = get_token_for_scope(KUSTO_SCOPE)
    base_uri = cluster_uri.rstrip("/")
    url = f"{base_uri}/v1/rest/query"

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={"db": database, "csl": query},
        timeout=timeout_s,
    )

    if not resp.ok:
        raise RuntimeError(f"KQL query failed ({resp.status_code}): {resp.text}")

    result = resp.json()
    tables = result.get("Tables", [])
    if tables:
        return _parse_kql_table(tables[0])
    return []


def execute_kql_mgmt(
    cluster_uri: str,
    database: str,
    command: str,
    timeout_s: int = DEFAULT_QUERY_TIMEOUT_S,
) -> List[KqlRow]:
    """Execute a KQL management command (.show, .alter, etc.)."""
    token = get_token_for_scope(KUSTO_SCOPE)
    base_uri = cluster_uri.rstrip("/")
    url = f"{base_uri}/v1/rest/mgmt"

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={"db": database, "csl": command},
        timeout=timeout_s,
    )

    if not resp.ok:
        raise RuntimeError(f"KQL mgmt command failed ({resp.status_code}): {resp.text}")

    result = resp.json()
    tables = result.get("Tables", [])
    if tables:
        return _parse_kql_table(tables[0])
    return []


def run_kql_diagnostics(
    cluster_uri: str,
    database: str,
    commands: Dict[str, Dict[str, Any]],
    concurrency: int = 5,
) -> Dict[str, Dict[str, Any]]:
    """Run multiple KQL diagnostic commands and return named results.
    Processes sequentially (Python synchronous model).
    Each entry in commands: {"query": str, "isMgmt": bool}.
    """
    results: Dict[str, Dict[str, Any]] = {}

    for name, cmd in commands.items():
        try:
            if cmd.get("isMgmt"):
                rows = execute_kql_mgmt(cluster_uri, database, cmd["query"])
            else:
                rows = execute_kql_query(cluster_uri, database, cmd["query"])
            results[name] = {"rows": rows}
        except Exception as exc:
            results[name] = {"error": str(exc)}

    return results
