"""SQL client — execute queries against Fabric SQL endpoints via pytds with AAD token auth."""

import struct
from typing import Any, Dict, List, Optional

import pytds

from auth.fabric_auth import get_token_for_scope, SQL_SCOPE

SqlRow = Dict[str, Any]


def _make_token_bytes(token: str) -> bytes:
    """Convert a JWT string into the TDS token struct expected by pytds."""
    token_bytes = token.encode("UTF-16-LE")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


def execute_sql_query(server: str, database: str, sql: str) -> List[SqlRow]:
    """Execute a single SQL query. Creates a new connection each time."""
    token = get_token_for_scope(SQL_SCOPE)

    rows: List[SqlRow] = []
    conn = pytds.connect(
        dsn=server,
        database=database,
        port=1433,
        login_timeout=30,
        timeout=60,
        as_dict=True,
        use_tz=None,
        auth=pytds.login.AzureAuth(token),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description:
                rows = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
    return rows


def _execute_on_connection(conn: Any, sql: str) -> List[SqlRow]:
    """Execute a SQL query on an existing open connection."""
    with conn.cursor() as cur:
        cur.execute(sql)
        if cur.description:
            return [dict(row) for row in cur.fetchall()]
    return []


def _create_connection(server: str, database: str, token: str) -> Any:
    """Create a reusable SQL connection with AAD token auth."""
    return pytds.connect(
        dsn=server,
        database=database,
        port=1433,
        login_timeout=30,
        timeout=60,
        as_dict=True,
        use_tz=None,
        auth=pytds.login.AzureAuth(token),
    )


def run_diagnostic_queries(
    server: str,
    database: str,
    queries: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """Run multiple diagnostic queries on a single reusable connection.
    Opens one connection, runs all queries sequentially, then closes.
    """
    results: Dict[str, Dict[str, Any]] = {}
    token = get_token_for_scope(SQL_SCOPE)

    try:
        conn = _create_connection(server, database, token)
    except Exception as exc:
        msg = str(exc)
        for name in queries:
            results[name] = {"error": msg}
        return results

    try:
        for name, sql in queries.items():
            try:
                rows = _execute_on_connection(conn, sql)
                results[name] = {"rows": rows}
            except Exception as exc:
                results[name] = {"error": str(exc)}
    finally:
        conn.close()

    return results
