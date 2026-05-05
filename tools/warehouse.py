"""Warehouse tools — ported from warehouse.ts."""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from clients.fabric_client import (
    get_warehouse,
    list_warehouses,
)
from clients.sql_client import execute_sql_query, run_diagnostic_queries
from tools.rule_engine import RuleResult, render_rule_report

# ──────────────────────────────────────────────
# Input validation — prevent SQL injection in auto-fix
# ──────────────────────────────────────────────

SAFE_SQL_NAME = re.compile(r"^[a-zA-Z0-9_\[\].\- ]+$")


def _validate_sql_name(value: str, label: str) -> None:
    if not SAFE_SQL_NAME.match(value):
        raise ValueError(
            f"Invalid {label}: must be alphanumeric/underscore/bracket/dot only."
        )


def _quote_sql_id(name: str) -> str:
    """Bracket-quote a SQL identifier (schema.table → [schema].[table])."""
    if name.startswith("[") and name.endswith("]"):
        return name
    parts = name.split(".")
    return ".".join(f"[{p.strip('[]')}]" for p in parts)


# ──────────────────────────────────────────────
# Tool: warehouse_list
# ──────────────────────────────────────────────


def warehouse_list(args: dict) -> str:
    try:
        warehouses = list_warehouses(args["workspaceId"])

        if not warehouses:
            return "No warehouses found in this workspace."

        lines: List[str] = []
        for wh in warehouses:
            props = wh.get("properties") or {}
            parts = [f"- **{wh['displayName']}** (ID: {wh['id']})"]
            conn = props.get("connectionString")
            if conn:
                parts.append(f"  Connection: {conn}")
            created = props.get("createdDate")
            if created:
                parts.append(f"  Created: {created}")
            lines.append("\n".join(parts))

        return f"## Warehouses in workspace {args['workspaceId']}\n\n" + "\n\n".join(lines)
    except Exception as e:
        return f"Error listing warehouses: {e}"


# ──────────────────────────────────────────────
# SQL Diagnostic Queries
# ──────────────────────────────────────────────

WAREHOUSE_DIAGNOSTICS = {
    "tables": """
    SELECT s.name AS schema_name, t.name AS table_name
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    ORDER BY s.name, t.name""",

    "columns": """
    SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE,
           CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
           IS_NULLABLE
    FROM INFORMATION_SCHEMA.COLUMNS
    ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION""",

    "stats": """
    SELECT s.name AS schema_name, t.name AS table_name,
           st.name AS stat_name, st.auto_created
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    LEFT JOIN sys.stats st ON t.object_id = st.object_id
    ORDER BY s.name, t.name""",

    "slowQueries": """
    SELECT TOP 15
        LEFT(command, 300) AS query_text,
        start_time, end_time,
        total_elapsed_time_ms,
        row_count, status
    FROM queryinsights.exec_requests_history
    WHERE status = 'Succeeded'
    ORDER BY total_elapsed_time_ms DESC""",

    "frequentQueries": """
    SELECT TOP 15
        LEFT(command, 300) AS query_text,
        COUNT(*) AS execution_count,
        AVG(total_elapsed_time_ms) AS avg_duration_ms,
        MAX(total_elapsed_time_ms) AS max_duration_ms
    FROM queryinsights.exec_requests_history
    WHERE status = 'Succeeded'
    GROUP BY LEFT(command, 300)
    ORDER BY execution_count DESC""",

    "failedQueries": """
    SELECT TOP 10
        LEFT(command, 300) AS query_text,
        start_time, status
    FROM queryinsights.exec_requests_history
    WHERE status = 'Failed'
    ORDER BY start_time DESC""",

    "queryVolume": """
    SELECT
        CAST(start_time AS DATE) AS query_date,
        COUNT(*) AS query_count,
        AVG(total_elapsed_time_ms) AS avg_duration_ms
    FROM queryinsights.exec_requests_history
    GROUP BY CAST(start_time AS DATE)
    ORDER BY query_date DESC""",

    "missingPrimaryKeys": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name
    FROM sys.tables t
    LEFT JOIN sys.indexes i ON t.object_id = i.object_id AND i.is_primary_key = 1
    WHERE i.object_id IS NULL AND t.is_ms_shipped = 0
    ORDER BY t.name""",

    "deprecatedTypes": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           c.name AS column_name, typ.name AS data_type
    FROM sys.tables t
    JOIN sys.columns c ON t.object_id = c.object_id
    JOIN sys.types typ ON c.user_type_id = typ.user_type_id
    WHERE typ.name IN ('text', 'ntext', 'image')
    ORDER BY t.name, c.name""",

    "floatingPointColumns": """
    SELECT TABLE_SCHEMA + '.' + TABLE_NAME AS table_name,
           COLUMN_NAME, DATA_TYPE
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
      AND DATA_TYPE IN ('float', 'real')
    ORDER BY TABLE_NAME, COLUMN_NAME""",

    "oversizedColumns": """
    SELECT TABLE_SCHEMA + '.' + TABLE_NAME AS table_name,
           COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH AS max_length
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
      AND DATA_TYPE IN ('char', 'varchar', 'nchar', 'nvarchar')
      AND CHARACTER_MAXIMUM_LENGTH > 500
    ORDER BY CHARACTER_MAXIMUM_LENGTH DESC""",

    "namingIssues": """
    SELECT t.name AS table_name, c.name AS column_name
    FROM sys.tables t
    JOIN sys.columns c ON t.object_id = c.object_id
    WHERE c.name COLLATE Latin1_General_BIN LIKE '%[^a-zA-Z0-9_]%'
      AND t.is_ms_shipped = 0
    ORDER BY t.name, c.name""",

    "viewsWithSelectStar": """
    SELECT SCHEMA_NAME(v.schema_id) + '.' + v.name AS view_name
    FROM sys.views v
    JOIN sys.sql_modules m ON v.object_id = m.object_id
    WHERE m.definition LIKE '%SELECT *%'
      AND SCHEMA_NAME(v.schema_id) NOT IN ('sys', 'queryinsights')
      AND v.name NOT IN ('exec_requests_history', 'long_running_queries',
                         'frequently_run_queries', 'exec_sessions_history')
    ORDER BY v.name""",

    "staleStatistics": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           s.name AS stat_name,
           STATS_DATE(s.object_id, s.stats_id) AS last_updated,
           DATEDIFF(day, STATS_DATE(s.object_id, s.stats_id), GETDATE()) AS days_old
    FROM sys.stats s
    JOIN sys.tables t ON s.object_id = t.object_id
    WHERE s.auto_created = 1
      AND DATEDIFF(day, STATS_DATE(s.object_id, s.stats_id), GETDATE()) > 30
    ORDER BY days_old DESC""",

    "constraintCheck": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           f.name AS constraint_name,
           f.is_disabled, f.is_not_trusted
    FROM sys.foreign_keys f
    JOIN sys.tables t ON f.parent_object_id = t.object_id
    WHERE f.is_disabled = 1 OR f.is_not_trusted = 1""",

    "nullableKeyColumns": """
    SELECT TABLE_SCHEMA + '.' + TABLE_NAME AS table_name,
           COLUMN_NAME, DATA_TYPE
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
      AND IS_NULLABLE = 'YES'
      AND (COLUMN_NAME LIKE '%Id' OR COLUMN_NAME LIKE '%_id'
           OR COLUMN_NAME LIKE '%Key' OR COLUMN_NAME LIKE '%_key'
           OR COLUMN_NAME = 'id')
    ORDER BY TABLE_NAME, COLUMN_NAME""",

    "emptyTables": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           SUM(p.rows) AS row_count
    FROM sys.tables t
    JOIN sys.partitions p ON t.object_id = p.object_id
    WHERE p.index_id IN (0,1) AND t.is_ms_shipped = 0
    GROUP BY t.schema_id, t.name
    HAVING SUM(p.rows) = 0
    ORDER BY t.name""",

    "wideTables": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           COUNT(c.column_id) AS column_count
    FROM sys.tables t
    JOIN sys.columns c ON t.object_id = c.object_id
    WHERE t.is_ms_shipped = 0
    GROUP BY t.schema_id, t.name
    HAVING COUNT(c.column_id) > 50
    ORDER BY COUNT(c.column_id) DESC""",

    "mixedDateTypes": """
    SELECT table_name, date_type_count, date_types_used FROM (
      SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
             COUNT(DISTINCT ty.name) AS date_type_count,
             STRING_AGG(ty.name, ', ') AS date_types_used
      FROM sys.tables t
      JOIN sys.columns c ON t.object_id = c.object_id
      JOIN sys.types ty ON c.user_type_id = ty.user_type_id
      WHERE t.is_ms_shipped = 0
        AND ty.name IN ('date','datetime','datetime2','smalldatetime','datetimeoffset')
      GROUP BY t.schema_id, t.name
    ) sub WHERE date_type_count > 1""",

    "missingForeignKeys": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name
    FROM sys.tables t
    LEFT JOIN sys.foreign_keys fk ON t.object_id = fk.parent_object_id
    WHERE t.is_ms_shipped = 0
    GROUP BY t.schema_id, t.name
    HAVING COUNT(fk.object_id) = 0
    ORDER BY t.name""",

    "blobColumns": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           c.name AS column_name,
           TYPE_NAME(c.user_type_id) AS data_type
    FROM sys.tables t
    JOIN sys.columns c ON t.object_id = c.object_id
    WHERE t.is_ms_shipped = 0
      AND TYPE_NAME(c.user_type_id) IN ('varbinary','varchar','nvarchar')
      AND (c.max_length = -1 OR c.max_length > 8000)
    ORDER BY t.name, c.name""",

    "missingAuditColumns": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           SUM(CASE WHEN c.name IN ('created_at','created_date','CreatedAt','CreatedDate') THEN 1 ELSE 0 END) AS has_created,
           SUM(CASE WHEN c.name IN ('updated_at','updated_date','modified_at','modified_date','UpdatedAt','ModifiedAt','UpdatedDate','ModifiedDate') THEN 1 ELSE 0 END) AS has_updated,
           SUM(CASE WHEN c.name IN ('created_by','CreatedBy') THEN 1 ELSE 0 END) AS has_created_by,
           SUM(CASE WHEN c.name IN ('updated_by','modified_by','UpdatedBy','ModifiedBy') THEN 1 ELSE 0 END) AS has_updated_by
    FROM sys.tables t
    JOIN sys.columns c ON t.object_id = c.object_id
    WHERE t.is_ms_shipped = 0
    GROUP BY t.schema_id, t.name
    HAVING SUM(CASE WHEN c.name IN ('created_at','created_date','CreatedAt','CreatedDate','updated_at','updated_date','modified_at','modified_date','UpdatedAt','ModifiedAt','UpdatedDate','ModifiedDate') THEN 1 ELSE 0 END) = 0""",

    "sensitiveColumns": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           c.name AS column_name
    FROM sys.tables t
    JOIN sys.columns c ON t.object_id = c.object_id
    WHERE t.is_ms_shipped = 0
      AND (c.name LIKE '%credit%' OR c.name LIKE '%ssn%' OR c.name LIKE '%password%'
           OR c.name LIKE '%secret%' OR c.name LIKE '%phone%' OR c.name LIKE '%email%'
           OR c.name LIKE '%IBAN%' OR c.name LIKE '%SWIFT%' OR c.name LIKE '%BIC%'
           OR c.name LIKE '%license%' OR c.name LIKE '%tax%id%')
    ORDER BY t.name, c.name""",

    "dataMaskingCheck": """
    SELECT OBJECT_SCHEMA_NAME(c.object_id) + '.' + OBJECT_NAME(c.object_id) AS table_name,
           c.name AS column_name,
           m.masking_function
    FROM sys.masked_columns m
    JOIN sys.columns c ON m.object_id = c.object_id AND m.column_id = c.column_id""",

    "rlsCheck": """
    SELECT name AS policy_name, is_enabled
    FROM sys.security_policies""",

    "dbOwnerMembers": """
    SELECT p.name AS member_name, r.name AS role_name
    FROM sys.database_role_members rm
    JOIN sys.database_principals p ON rm.member_principal_id = p.principal_id
    JOIN sys.database_principals r ON rm.role_principal_id = r.principal_id
    WHERE r.name = 'db_owner'""",

    "viewDependencies": """
    SELECT SCHEMA_NAME(v.schema_id) + '.' + v.name AS view_name,
           COUNT(d.referenced_id) AS dependency_count
    FROM sys.views v
    LEFT JOIN sys.sql_expression_dependencies d ON v.object_id = d.referencing_id
    WHERE SCHEMA_NAME(v.schema_id) NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
    GROUP BY v.schema_id, v.name
    HAVING COUNT(d.referenced_id) > 10
    ORDER BY COUNT(d.referenced_id) DESC""",

    "crossSchemaDeps": """
    SELECT SCHEMA_NAME(o.schema_id) + '.' + o.name AS referencing_object,
           SCHEMA_NAME(ref.schema_id) + '.' + ref.name AS referenced_object
    FROM sys.sql_expression_dependencies d
    JOIN sys.objects o ON d.referencing_id = o.object_id
    JOIN sys.objects ref ON d.referenced_id = ref.object_id
    WHERE o.schema_id <> ref.schema_id
      AND o.type IN ('V','P','FN','IF','TF')
      AND SCHEMA_NAME(o.schema_id) NOT IN ('sys','INFORMATION_SCHEMA','queryinsights')
      AND SCHEMA_NAME(ref.schema_id) NOT IN ('sys','INFORMATION_SCHEMA','queryinsights')""",

    "circularForeignKeys": """
    SELECT OBJECT_SCHEMA_NAME(fk1.parent_object_id) + '.' + OBJECT_NAME(fk1.parent_object_id) AS table1,
           OBJECT_SCHEMA_NAME(fk1.referenced_object_id) + '.' + OBJECT_NAME(fk1.referenced_object_id) AS table2
    FROM sys.foreign_keys fk1
    JOIN sys.foreign_keys fk2 ON fk1.referenced_object_id = fk2.parent_object_id
    WHERE fk1.parent_object_id = fk2.referenced_object_id""",

    "tableNamingIssues": """
    SELECT name AS table_name
    FROM sys.tables
    WHERE is_ms_shipped = 0
      AND (name LIKE '% %' OR name LIKE '%[^0-9A-Za-z_]%')""",

    "dbSettings": """
    SELECT
      is_auto_update_stats_on,
      is_auto_update_stats_async_on,
      is_result_set_caching_on,
      compatibility_level,
      is_ansi_nulls_on,
      is_ansi_padding_on,
      is_ansi_warnings_on,
      is_arithabort_on,
      is_quoted_identifier_on,
      snapshot_isolation_state,
      is_read_committed_snapshot_on,
      page_verify_option_desc,
      state_desc,
      user_access_desc,
      containment_desc,
      is_fulltext_enabled,
      is_data_retention_enabled
    FROM sys.databases
    WHERE name = DB_NAME()""",

    "rowCounts": """
    SELECT s.name AS schema_name, t.name AS table_name,
           SUM(p.rows) AS row_count
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    JOIN sys.partitions p ON t.object_id = p.object_id
    WHERE p.index_id IN (0,1) AND t.is_ms_shipped = 0
    GROUP BY s.name, t.name
    ORDER BY row_count DESC""",

    "lowRowCountTables": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           SUM(p.rows) AS row_count
    FROM sys.tables t
    JOIN sys.partitions p ON t.object_id = p.object_id
    WHERE p.index_id IN (0,1) AND t.is_ms_shipped = 0
    GROUP BY t.schema_id, t.name
    HAVING SUM(p.rows) BETWEEN 1 AND 10
    ORDER BY SUM(p.rows)""",

    "storedProcedures": """
    SELECT o.name AS proc_name,
           CASE WHEN ep.value IS NULL THEN 0 ELSE 1 END AS has_description
    FROM sys.procedures o
    LEFT JOIN sys.extended_properties ep
      ON o.object_id = ep.major_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
    WHERE o.is_ms_shipped = 0
    ORDER BY o.name""",

    "missingDefaults": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           c.name AS column_name
    FROM sys.tables t
    JOIN sys.columns c ON t.object_id = c.object_id
    LEFT JOIN sys.default_constraints dc ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
    WHERE t.is_ms_shipped = 0
      AND dc.object_id IS NULL
      AND c.is_nullable = 0
      AND c.is_identity = 0
    ORDER BY t.name, c.name""",

    "unicodeMix": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           SUM(CASE WHEN TYPE_NAME(c.user_type_id) IN ('nvarchar','nchar') THEN 1 ELSE 0 END) AS unicode_count,
           SUM(CASE WHEN TYPE_NAME(c.user_type_id) IN ('varchar','char') THEN 1 ELSE 0 END) AS non_unicode_count
    FROM sys.tables t
    JOIN sys.columns c ON t.object_id = c.object_id
    WHERE t.is_ms_shipped = 0
      AND TYPE_NAME(c.user_type_id) IN ('nvarchar','nchar','varchar','char')
    GROUP BY t.schema_id, t.name
    HAVING SUM(CASE WHEN TYPE_NAME(c.user_type_id) IN ('nvarchar','nchar') THEN 1 ELSE 0 END) > 0
      AND SUM(CASE WHEN TYPE_NAME(c.user_type_id) IN ('varchar','char') THEN 1 ELSE 0 END) > 0""",

    "schemaDocumentation": """
    SELECT s.name AS schema_name,
           CASE WHEN ep.value IS NULL THEN 0 ELSE 1 END AS has_description
    FROM sys.schemas s
    LEFT JOIN sys.extended_properties ep
      ON ep.class = 3 AND ep.major_id = s.schema_id AND ep.name = 'MS_Description'
    WHERE s.name NOT IN ('sys','INFORMATION_SCHEMA','guest','db_owner','db_accessadmin',
      'db_securityadmin','db_ddladmin','db_backupoperator','db_datareader',
      'db_datawriter','db_denydatareader','db_denydatawriter','queryinsights')
    ORDER BY s.name""",

    "queryVolumeAvg": """
    SELECT
        AVG(total_elapsed_time_ms) AS avg_duration_ms,
        COUNT(*) AS total_queries
    FROM queryinsights.exec_requests_history
    WHERE start_time > DATEADD(day, -7, GETDATE())
      AND status = 'Succeeded'""",

    "computedColumns": """SELECT c.name AS column_name, SCHEMA_NAME(t.schema_id) AS schema_name, t.name AS table_name
    FROM sys.computed_columns c
    JOIN sys.tables t ON c.object_id = t.object_id""",

    "allColumns": """SELECT SCHEMA_NAME(t.schema_id) AS schema_name, t.name AS table_name, COUNT(*) AS col_count
    FROM sys.columns c JOIN sys.tables t ON c.object_id = t.object_id
    GROUP BY SCHEMA_NAME(t.schema_id), t.name""",

    "queryHints": """SELECT DISTINCT SCHEMA_NAME(o.schema_id) AS schema_name, o.name AS object_name, o.type_desc
    FROM sys.sql_modules m JOIN sys.objects o ON m.object_id = o.object_id
    WHERE m.definition LIKE '%NOLOCK%' OR m.definition LIKE '%FORCESEEK%' OR m.definition LIKE '%FORCESCAN%'""",

    "dbSettingsExtended": """SELECT
    is_auto_create_stats_on, is_query_store_on
    FROM sys.databases WHERE name = DB_NAME()""",

    "fkWithoutIndex": """SELECT fk.name AS fk_name, SCHEMA_NAME(fk.schema_id) AS schema_name,
    OBJECT_NAME(fk.parent_object_id) AS table_name,
    COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS column_name
    FROM sys.foreign_keys fk
    JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
    WHERE NOT EXISTS (
      SELECT 1 FROM sys.index_columns ic
      WHERE ic.object_id = fkc.parent_object_id AND ic.column_id = fkc.parent_column_id
    )""",
}


# ──────────────────────────────────────────────
# Analysis helpers
# ──────────────────────────────────────────────


def _analyze_data_types(columns: List[Dict[str, Any]]) -> List[str]:
    issues: List[str] = []
    wide_varchars: List[str] = []
    text_dates: List[str] = []
    text_numbers: List[str] = []

    for col in columns:
        table = f"{col.get('TABLE_SCHEMA', '')}.{col.get('TABLE_NAME', '')}"
        col_name = str(col.get("COLUMN_NAME", ""))
        dtype = str(col.get("DATA_TYPE", "")).lower()
        max_len = col.get("CHARACTER_MAXIMUM_LENGTH")

        if dtype in ("varchar", "nvarchar") and max_len and int(max_len) > 500:
            wide_varchars.append(f"{table}.{col_name} ({dtype}({max_len}))")

        if dtype in ("varchar", "nvarchar") and re.search(
            r"date|time|created|modified|updated", col_name, re.IGNORECASE
        ):
            text_dates.append(f"{table}.{col_name}")

        if dtype in ("varchar", "nvarchar") and re.match(
            r"^(id|count|amount|price|qty|quantity|total|num|number)",
            col_name,
            re.IGNORECASE,
        ):
            text_numbers.append(f"{table}.{col_name}")

    if wide_varchars:
        items = "\n".join(f"  - {c}" for c in wide_varchars[:5])
        extra = (
            f"\n  - ...and {len(wide_varchars) - 5} more"
            if len(wide_varchars) > 5
            else ""
        )
        issues.append(
            f"**⚠️ Wide VARCHAR columns ({len(wide_varchars)})**: May hurt columnstore compression.\n{items}{extra}"
        )

    if text_dates:
        items = "\n".join(f"  - {c}" for c in text_dates[:5])
        issues.append(
            f"**⚠️ Date-like columns stored as text ({len(text_dates)})**: Use DATE/DATETIME2 for better compression and filtering.\n{items}"
        )

    if text_numbers:
        items = "\n".join(f"  - {c}" for c in text_numbers[:5])
        issues.append(
            f"**⚠️ Numeric-like columns stored as text ({len(text_numbers)})**: Use INT/BIGINT/DECIMAL for better performance.\n{items}"
        )

    return issues


def _analyze_slow_queries(rows: List[Dict[str, Any]]) -> List[str]:
    if not rows:
        return [
            "**✅ No slow query data found** — Warehouse may be newly created or lightly used."
        ]

    lines = [f"**Top {len(rows)} slowest queries:**\n"]
    lines.append("| Duration (s) | Rows | Query |")
    lines.append("|-------------|------|-------|")

    for r in rows[:10]:
        dur = f"{(r.get('total_elapsed_time_ms', 0) or 0) / 1000:.1f}"
        row_count = r.get("row_count", "?")
        query = (
            str(r.get("query_text", ""))[:80]
            .replace("|", "\\|")
            .replace("\n", " ")
        )
        lines.append(f"| {dur}s | {row_count} | {query}... |")

    very_slow = sum(
        1
        for r in rows
        if (r.get("total_elapsed_time_ms", 0) or 0) > 60000
    )
    if very_slow > 0:
        lines.append(
            f"\n**🔴 {very_slow} queries took >60 seconds** — These need optimization."
        )

    return lines


def _analyze_frequent_queries(rows: List[Dict[str, Any]]) -> List[str]:
    if not rows:
        return []

    lines = ["**Most frequently executed queries:**\n"]
    lines.append("| Executions | Avg (s) | Max (s) | Query |")
    lines.append("|-----------|---------|---------|-------|")

    for r in rows[:10]:
        count = r.get("execution_count", 0)
        avg = f"{(r.get('avg_duration_ms', 0) or 0) / 1000:.1f}"
        mx = f"{(r.get('max_duration_ms', 0) or 0) / 1000:.1f}"
        query = (
            str(r.get("query_text", ""))[:60]
            .replace("|", "\\|")
            .replace("\n", " ")
        )
        lines.append(f"| {count} | {avg}s | {mx}s | {query}... |")

    expensive_repeat = [
        r
        for r in rows
        if (r.get("execution_count", 0) or 0) > 10
        and (r.get("avg_duration_ms", 0) or 0) > 10000
    ]
    if expensive_repeat:
        lines.append(
            f"\n**🔴 {len(expensive_repeat)} queries are both frequent AND slow (>10s avg)** — "
            "Prime candidates for optimization or caching."
        )

    return lines


def _analyze_failed_queries(rows: List[Dict[str, Any]]) -> List[str]:
    if not rows:
        return ["**✅ No failed queries found.**"]

    lines = [f"**⚠️ {len(rows)} recent query failures:**\n"]
    for r in rows[:5]:
        query = str(r.get("query_text", ""))[:100].replace("\n", " ")
        lines.append(f"- `{r.get('start_time', '')}`: {query}...")

    return lines


# ──────────────────────────────────────────────
# Tool: warehouse_optimization_recommendations
# ──────────────────────────────────────────────


def warehouse_optimization_recommendations(args: dict) -> str:
    try:
        warehouse = get_warehouse(args["workspaceId"], args["warehouseId"])
        rules: List[RuleResult] = []
        header: List[str] = []

        props = warehouse.get("properties") or {}
        connection_string = props.get("connectionString")
        if not connection_string:
            return render_rule_report(
                f"Warehouse Analysis: {warehouse['displayName']}",
                datetime.now(timezone.utc).isoformat(),
                ["## ❌ No SQL connection string available."],
                [
                    RuleResult(
                        id="WH-001",
                        rule="SQL Connection",
                        category="Availability",
                        severity="HIGH",
                        status="ERROR",
                        details="No connection string available.",
                    )
                ],
            )

        header.extend(
            [
                "## 🔌 Connection Info",
                "",
                f"- **Connection**: `{connection_string}`",
                "",
            ]
        )

        r = run_diagnostic_queries(
            connection_string, warehouse["displayName"], WAREHOUSE_DIAGNOSTICS
        )

        def cnt(key: str) -> int:
            entry = r.get(key) or {}
            return len(entry.get("rows") or [])

        def err(key: str) -> Optional[str]:
            entry = r.get(key) or {}
            return entry.get("error")

        def rows(key: str) -> list:
            entry = r.get(key) or {}
            return entry.get("rows") or []

        # Table inventory header
        if r.get("tables", {}).get("rows"):
            by_schema: Dict[str, List[str]] = {}
            for t in rows("tables"):
                schema = str(t.get("schema_name", ""))
                by_schema.setdefault(schema, []).append(
                    str(t.get("table_name", ""))
                )
            header.extend(["## 📋 Tables", ""])
            for schema, tbls in by_schema.items():
                header.extend(
                    [f"**{schema}** ({len(tbls)}): {', '.join(tbls)}", ""]
                )

        # Query Performance header
        slow_rows = rows("slowQueries")
        if slow_rows:
            header.extend(["## 🐢 Top Slow Queries", ""])
            header.append("| Duration (s) | Rows | Query |")
            header.append("|-------------|------|-------|")
            for q in slow_rows[:10]:
                dur = f"{(q.get('total_elapsed_time_ms', 0) or 0) / 1000:.1f}"
                query = (
                    str(q.get("query_text", ""))[:80]
                    .replace("|", "\\|")
                    .replace("\n", " ")
                )
                header.append(
                    f"| {dur}s | {q.get('row_count', '?')} | {query}... |"
                )
            header.append("")

        # ── RULES ──

        # WH-001: Missing Primary Keys
        rules.append(
            RuleResult(
                id="WH-001", rule="Primary Keys Defined", category="Data Quality", severity="HIGH",
                status="ERROR" if err("missingPrimaryKeys") else "PASS" if cnt("missingPrimaryKeys") == 0 else "FAIL",
                details=err("missingPrimaryKeys") or (
                    "All tables have primary keys." if cnt("missingPrimaryKeys") == 0
                    else f"{cnt('missingPrimaryKeys')} table(s) missing PKs: {', '.join(str(x.get('table_name', '')) for x in rows('missingPrimaryKeys')[:5])}"
                ),
                recommendation="Add PRIMARY KEY NOT ENFORCED constraints for data integrity and query optimization.",
            )
        )

        # WH-002: Deprecated Types
        rules.append(
            RuleResult(
                id="WH-002", rule="No Deprecated Data Types", category="Maintainability", severity="HIGH",
                status="ERROR" if err("deprecatedTypes") else "PASS" if cnt("deprecatedTypes") == 0 else "FAIL",
                details=err("deprecatedTypes") or (
                    "No TEXT/NTEXT/IMAGE columns." if cnt("deprecatedTypes") == 0
                    else f"{cnt('deprecatedTypes')} column(s) use deprecated types: {', '.join(str(x.get('table_name','')) + '.' + str(x.get('column_name','')) + '(' + str(x.get('data_type','')) + ')' for x in rows('deprecatedTypes')[:5])}"
                ),
                recommendation="Migrate TEXT/NTEXT/IMAGE to VARCHAR(MAX)/NVARCHAR(MAX)/VARBINARY(MAX).",
            )
        )

        # WH-003: Floating Point
        rules.append(
            RuleResult(
                id="WH-003", rule="No Float/Real Precision Issues", category="Data Quality", severity="MEDIUM",
                status="ERROR" if err("floatingPointColumns") else "PASS" if cnt("floatingPointColumns") == 0 else "WARN",
                details=err("floatingPointColumns") or (
                    "All numeric columns use fixed precision." if cnt("floatingPointColumns") == 0
                    else f"{cnt('floatingPointColumns')} float/real column(s): {', '.join(str(x.get('table_name','')) + '.' + str(x.get('COLUMN_NAME','')) for x in rows('floatingPointColumns')[:5])}"
                ),
                recommendation="Use DECIMAL/NUMERIC for exact values (monetary, percentages).",
            )
        )

        # WH-004: Oversized Columns
        rules.append(
            RuleResult(
                id="WH-004", rule="No Over-Provisioned Columns", category="Performance", severity="MEDIUM",
                status="ERROR" if err("oversizedColumns") else "PASS" if cnt("oversizedColumns") == 0 else "WARN",
                details=err("oversizedColumns") or (
                    "All string columns have reasonable lengths." if cnt("oversizedColumns") == 0
                    else f"{cnt('oversizedColumns')} column(s) >500 chars: {', '.join(str(x.get('table_name','')) + '.' + str(x.get('COLUMN_NAME','')) + '(' + str(x.get('max_length','')) + ')' for x in rows('oversizedColumns')[:5])}"
                ),
                recommendation="Reduce column lengths for better columnstore compression.",
            )
        )

        # WH-005: Column Naming
        rules.append(
            RuleResult(
                id="WH-005", rule="Column Naming Convention", category="Maintainability", severity="LOW",
                status="ERROR" if err("namingIssues") else "PASS" if cnt("namingIssues") == 0 else "WARN",
                details=err("namingIssues") or (
                    "All columns follow alphanumeric naming." if cnt("namingIssues") == 0
                    else f"{cnt('namingIssues')} column(s) with spaces/special chars: {', '.join(str(x.get('table_name','')) + '.' + str(x.get('column_name','')) for x in rows('namingIssues')[:5])}"
                ),
                recommendation="Use only letters, digits, and underscores.",
            )
        )

        # WH-006: Table Naming
        rules.append(
            RuleResult(
                id="WH-006", rule="Table Naming Convention", category="Maintainability", severity="LOW",
                status="ERROR" if err("tableNamingIssues") else "PASS" if cnt("tableNamingIssues") == 0 else "WARN",
                details=err("tableNamingIssues") or (
                    "All table names follow conventions." if cnt("tableNamingIssues") == 0
                    else f"{cnt('tableNamingIssues')} table(s) with invalid names: {', '.join(str(x.get('table_name','')) for x in rows('tableNamingIssues')[:5])}"
                ),
                recommendation="Use only letters, numbers, and underscores in table names.",
            )
        )

        # WH-007: Views with SELECT *
        rules.append(
            RuleResult(
                id="WH-007", rule="No SELECT * in Views", category="Maintainability", severity="LOW",
                status="ERROR" if err("viewsWithSelectStar") else "PASS" if cnt("viewsWithSelectStar") == 0 else "WARN",
                details=err("viewsWithSelectStar") or (
                    "No views use SELECT *." if cnt("viewsWithSelectStar") == 0
                    else f"{cnt('viewsWithSelectStar')} view(s) use SELECT *: {', '.join(str(x.get('view_name','')) for x in rows('viewsWithSelectStar')[:5])}"
                ),
                recommendation="Explicitly list columns in views to prevent breakage.",
            )
        )

        # WH-008: Stale Statistics
        rules.append(
            RuleResult(
                id="WH-008", rule="Statistics Are Fresh", category="Performance", severity="MEDIUM",
                status="ERROR" if err("staleStatistics") else "PASS" if cnt("staleStatistics") == 0 else "FAIL",
                details=err("staleStatistics") or (
                    "All statistics updated within 30 days." if cnt("staleStatistics") == 0
                    else f"{cnt('staleStatistics')} stale statistic(s) >30 days old: {', '.join(str(x.get('table_name','')) + '.' + str(x.get('stat_name','')) + '(' + str(x.get('days_old','')) + 'd)' for x in rows('staleStatistics')[:5])}"
                ),
                recommendation="Run UPDATE STATISTICS to refresh stale stats.",
            )
        )

        # WH-009: Disabled Constraints
        rules.append(
            RuleResult(
                id="WH-009", rule="No Disabled Constraints", category="Data Quality", severity="MEDIUM",
                status="ERROR" if err("constraintCheck") else "PASS" if cnt("constraintCheck") == 0 else "WARN",
                details=err("constraintCheck") or (
                    "All foreign keys enabled and trusted." if cnt("constraintCheck") == 0
                    else f"{cnt('constraintCheck')} disabled/untrusted constraint(s): {', '.join(str(x.get('table_name','')) + '.' + str(x.get('constraint_name','')) for x in rows('constraintCheck')[:5])}"
                ),
                recommendation="Re-enable constraints: ALTER TABLE [t] WITH CHECK CHECK CONSTRAINT ALL.",
            )
        )

        # WH-010: Nullable Key Columns
        rules.append(
            RuleResult(
                id="WH-010", rule="Key Columns Are NOT NULL", category="Data Quality", severity="HIGH",
                status="ERROR" if err("nullableKeyColumns") else "PASS" if cnt("nullableKeyColumns") == 0 else "FAIL",
                details=err("nullableKeyColumns") or (
                    "All key/ID columns are NOT NULL." if cnt("nullableKeyColumns") == 0
                    else f"{cnt('nullableKeyColumns')} nullable key column(s): {', '.join(str(x.get('table_name','')) + '.' + str(x.get('COLUMN_NAME','')) for x in rows('nullableKeyColumns')[:5])}"
                ),
                recommendation="Add NOT NULL constraints to ID/key columns.",
            )
        )

        # WH-011: Empty Tables
        rules.append(
            RuleResult(
                id="WH-011", rule="No Empty Tables", category="Maintainability", severity="MEDIUM",
                status="ERROR" if err("emptyTables") else "PASS" if cnt("emptyTables") == 0 else "WARN",
                details=err("emptyTables") or (
                    "All tables contain data." if cnt("emptyTables") == 0
                    else f"{cnt('emptyTables')} empty table(s): {', '.join(str(x.get('table_name','')) for x in rows('emptyTables')[:5])}"
                ),
                recommendation="Remove unused tables or fix data pipelines.",
            )
        )

        # WH-012: Wide Tables
        rules.append(
            RuleResult(
                id="WH-012", rule="No Excessively Wide Tables", category="Maintainability", severity="MEDIUM",
                status="ERROR" if err("wideTables") else "PASS" if cnt("wideTables") == 0 else "WARN",
                details=err("wideTables") or (
                    "All tables have ≤50 columns." if cnt("wideTables") == 0
                    else f"{cnt('wideTables')} table(s) with >50 columns: {', '.join(str(x.get('table_name','')) + '(' + str(x.get('column_count','')) + ')' for x in rows('wideTables')[:5])}"
                ),
                recommendation="Split wide tables into related fact/dimension tables.",
            )
        )

        # WH-013: Mixed Date Types
        rules.append(
            RuleResult(
                id="WH-013", rule="Consistent Date Types", category="Data Quality", severity="LOW",
                status="ERROR" if err("mixedDateTypes") else "PASS" if cnt("mixedDateTypes") == 0 else "WARN",
                details=err("mixedDateTypes") or (
                    "Each table uses consistent date types." if cnt("mixedDateTypes") == 0
                    else f"{cnt('mixedDateTypes')} table(s) mix date types: {', '.join(str(x.get('table_name','')) + '(' + str(x.get('date_types_used','')) + ')' for x in rows('mixedDateTypes')[:5])}"
                ),
                recommendation="Standardize on datetime2 across all tables.",
            )
        )

        # WH-014: Missing Foreign Keys
        total_table_count = cnt("tables")
        fk_missing = cnt("missingForeignKeys")
        rules.append(
            RuleResult(
                id="WH-014", rule="Foreign Keys Defined", category="Maintainability", severity="MEDIUM",
                status="ERROR" if err("missingForeignKeys") else "N/A" if total_table_count <= 3 else "PASS" if fk_missing == 0 else "WARN",
                details=err("missingForeignKeys") or (
                    "Too few tables to evaluate." if total_table_count <= 3
                    else "All tables have foreign key relationships." if fk_missing == 0
                    else f"{fk_missing} of {total_table_count} table(s) have no FKs: {', '.join(str(x.get('table_name','')) for x in rows('missingForeignKeys')[:5])}"
                ),
                recommendation="Add FK constraints (NOT ENFORCED) to document relationships.",
            )
        )

        # WH-015: BLOB Columns
        rules.append(
            RuleResult(
                id="WH-015", rule="No Large BLOB Columns", category="Performance", severity="MEDIUM",
                status="ERROR" if err("blobColumns") else "PASS" if cnt("blobColumns") == 0 else "WARN",
                details=err("blobColumns") or (
                    "No MAX-length columns." if cnt("blobColumns") == 0
                    else f"{cnt('blobColumns')} MAX-length column(s): {', '.join(str(x.get('table_name','')) + '.' + str(x.get('column_name','')) + '(' + str(x.get('data_type','')) + ')' for x in rows('blobColumns')[:5])}"
                ),
                recommendation="Use OneLake Files for large unstructured data instead of warehouse columns.",
            )
        )

        # WH-016: Missing Audit Columns
        rules.append(
            RuleResult(
                id="WH-016", rule="Tables Have Audit Columns", category="Maintainability", severity="LOW",
                status="ERROR" if err("missingAuditColumns") else "PASS" if cnt("missingAuditColumns") == 0 else "WARN",
                details=err("missingAuditColumns") or (
                    "All tables have created_at/updated_at." if cnt("missingAuditColumns") == 0
                    else f"{cnt('missingAuditColumns')} table(s) lack audit columns: {', '.join(str(x.get('table_name','')) for x in rows('missingAuditColumns')[:5])}"
                ),
                recommendation="Add created_at, updated_at, created_by columns for tracking.",
            )
        )

        # WH-017: Circular Foreign Keys
        rules.append(
            RuleResult(
                id="WH-017", rule="No Circular Foreign Keys", category="Data Quality", severity="HIGH",
                status="ERROR" if err("circularForeignKeys") else "PASS" if cnt("circularForeignKeys") == 0 else "FAIL",
                details=err("circularForeignKeys") or (
                    "No circular FK relationships." if cnt("circularForeignKeys") == 0
                    else f"{cnt('circularForeignKeys')} circular FK(s): {', '.join(str(x.get('table1','')) + ' ↔ ' + str(x.get('table2','')) for x in rows('circularForeignKeys')[:5])}"
                ),
                recommendation="Refactor to eliminate circular references.",
            )
        )

        # WH-018: Sensitive Columns without Masking
        masked_set = {
            f"{x.get('table_name', '')}.{x.get('column_name', '')}"
            for x in rows("dataMaskingCheck")
        }
        unmasked_sensitive = [
            x
            for x in rows("sensitiveColumns")
            if f"{x.get('table_name', '')}.{x.get('column_name', '')}" not in masked_set
        ]
        rules.append(
            RuleResult(
                id="WH-018", rule="Sensitive Data Protected", category="Security", severity="HIGH",
                status="ERROR" if err("sensitiveColumns") else "PASS" if not unmasked_sensitive else "FAIL",
                details=err("sensitiveColumns") or (
                    "All sensitive columns are masked or none detected." if not unmasked_sensitive
                    else f"{len(unmasked_sensitive)} sensitive column(s) without data masking."
                ),
                recommendation="Apply dynamic data masking to PII columns.",
            )
        )

        # WH-019: RLS
        rls_policies = rows("rlsCheck")
        rules.append(
            RuleResult(
                id="WH-019", rule="Row-Level Security", category="Security", severity="MEDIUM",
                status="ERROR" if err("rlsCheck") else "PASS" if rls_policies else "WARN",
                details=err("rlsCheck") or (
                    f"{len(rls_policies)} RLS policies defined." if rls_policies
                    else "No RLS policies — consider adding if data requires row-level isolation."
                ),
                recommendation="Add RLS security policies for multi-tenant or sensitive data scenarios.",
            )
        )

        # WH-020: db_owner Members
        owner_count = cnt("dbOwnerMembers")
        rules.append(
            RuleResult(
                id="WH-020", rule="Minimal db_owner Privileges", category="Security", severity="MEDIUM",
                status="ERROR" if err("dbOwnerMembers") else "PASS" if owner_count <= 3 else "WARN",
                details=err("dbOwnerMembers") or (
                    f"{owner_count} db_owner member(s) — acceptable." if owner_count <= 3
                    else f"{owner_count} db_owner members: {', '.join(str(x.get('member_name','')) for x in rows('dbOwnerMembers'))}"
                ),
                recommendation="Reduce db_owner membership to minimize security risk.",
            )
        )

        # WH-021: View Dependencies
        rules.append(
            RuleResult(
                id="WH-021", rule="No Over-Complex Views", category="Maintainability", severity="LOW",
                status="ERROR" if err("viewDependencies") else "PASS" if cnt("viewDependencies") == 0 else "WARN",
                details=err("viewDependencies") or (
                    "No views with >10 dependencies." if cnt("viewDependencies") == 0
                    else f"{cnt('viewDependencies')} over-complex view(s): {', '.join(str(x.get('view_name','')) + '(' + str(x.get('dependency_count','')) + ' deps)' for x in rows('viewDependencies')[:5])}"
                ),
                recommendation="Simplify view chains to at most 3 levels of nesting.",
            )
        )

        # WH-022: Cross-Schema Dependencies
        rules.append(
            RuleResult(
                id="WH-022", rule="Minimal Cross-Schema Dependencies", category="Maintainability", severity="LOW",
                status="ERROR" if err("crossSchemaDeps") else "PASS" if cnt("crossSchemaDeps") == 0 else "WARN",
                details=err("crossSchemaDeps") or (
                    "No cross-schema references." if cnt("crossSchemaDeps") == 0
                    else f"{cnt('crossSchemaDeps')} cross-schema reference(s): {', '.join(str(x.get('referencing_object','')) + ' → ' + str(x.get('referenced_object','')) for x in rows('crossSchemaDeps')[:5])}"
                ),
                recommendation="Minimize cross-schema dependencies for cleaner architecture.",
            )
        )

        # WH-023: Slow Queries (>60s)
        very_slow_count = sum(
            1 for q in rows("slowQueries") if (q.get("total_elapsed_time_ms", 0) or 0) > 60000
        )
        rules.append(
            RuleResult(
                id="WH-023", rule="No Very Slow Queries (>60s)", category="Performance", severity="HIGH",
                status="ERROR" if err("slowQueries") else "PASS" if very_slow_count == 0 else "FAIL",
                details=err("slowQueries") or (
                    "No queries exceeding 60 seconds." if very_slow_count == 0
                    else f"{very_slow_count} query/queries took >60 seconds."
                ),
                recommendation="Review and optimize slow queries — see Slow Queries table above.",
            )
        )

        # WH-024: Frequent Slow Queries
        expensive_repeat = [
            q for q in rows("frequentQueries")
            if (q.get("execution_count", 0) or 0) > 10
            and (q.get("avg_duration_ms", 0) or 0) > 10000
        ]
        rules.append(
            RuleResult(
                id="WH-024", rule="No Frequently Slow Queries", category="Performance", severity="HIGH",
                status="ERROR" if err("frequentQueries") else "PASS" if not expensive_repeat else "FAIL",
                details=err("frequentQueries") or (
                    "No recurring slow queries." if not expensive_repeat
                    else f"{len(expensive_repeat)} queries are both frequent (>10x) AND slow (>10s avg)."
                ),
                recommendation="Cache results or optimize these high-impact queries.",
            )
        )

        # WH-025: Failed Queries
        failed_rows = rows("failedQueries")
        if err("failedQueries"):
            failed_details = err("failedQueries") or ""
        elif not failed_rows:
            failed_details = "No recent query failures."
        else:
            categories: Dict[str, int] = {}
            for fr in failed_rows:
                text = str(fr.get("query_text", ""))[:50]
                if re.search(r"timeout", text, re.IGNORECASE):
                    cat = "Timeout"
                elif re.search(r"permission|denied|unauthorized", text, re.IGNORECASE):
                    cat = "Permission"
                elif re.search(r"syntax|parse", text, re.IGNORECASE):
                    cat = "Syntax"
                elif re.search(r"deadlock", text, re.IGNORECASE):
                    cat = "Deadlock"
                else:
                    cat = "Other"
                categories[cat] = categories.get(cat, 0) + 1
            breakdown = ", ".join(f"{k}: {v}" for k, v in categories.items())
            failed_details = f"{len(failed_rows)} failure(s) — {breakdown}"
        rules.append(
            RuleResult(
                id="WH-025", rule="No Recent Query Failures", category="Reliability", severity="MEDIUM",
                status="ERROR" if err("failedQueries") else "PASS" if not failed_rows else "WARN",
                details=failed_details,
                recommendation="Investigate failed queries grouped by error type.",
            )
        )

        # WH-026..WH-031: Database Settings
        db_settings_rows = rows("dbSettings")
        if db_settings_rows:
            db = db_settings_rows[0]

            rules.append(
                RuleResult(
                    id="WH-026", rule="AUTO_UPDATE_STATISTICS Enabled", category="Performance", severity="HIGH",
                    status="PASS" if db.get("is_auto_update_stats_on") else "FAIL",
                    details="Auto-update statistics is enabled." if db.get("is_auto_update_stats_on") else "AUTO_UPDATE_STATISTICS is OFF — stale stats cause bad query plans.",
                    recommendation="ALTER DATABASE SET AUTO_UPDATE_STATISTICS ON.",
                )
            )
            rules.append(
                RuleResult(
                    id="WH-027", rule="Result Set Caching Enabled", category="Performance", severity="MEDIUM",
                    status="PASS" if db.get("is_result_set_caching_on") else "WARN",
                    details="Result set caching is enabled." if db.get("is_result_set_caching_on") else "Result set caching is OFF.",
                    recommendation="ALTER DATABASE SET RESULT_SET_CACHING ON.",
                )
            )
            rules.append(
                RuleResult(
                    id="WH-028", rule="Snapshot Isolation Enabled", category="Concurrency", severity="MEDIUM",
                    status="PASS" if db.get("snapshot_isolation_state") else "WARN",
                    details="Snapshot isolation enabled — readers don't block writers." if db.get("snapshot_isolation_state") else "Snapshot isolation OFF — may cause blocking.",
                    recommendation="ALTER DATABASE SET ALLOW_SNAPSHOT_ISOLATION ON.",
                )
            )
            rules.append(
                RuleResult(
                    id="WH-029", rule="Page Verify CHECKSUM", category="Reliability", severity="MEDIUM",
                    status="PASS" if db.get("page_verify_option_desc") == "CHECKSUM" else "WARN",
                    details=f"PAGE_VERIFY is {db.get('page_verify_option_desc', 'unknown')}.",
                    recommendation="ALTER DATABASE SET PAGE_VERIFY CHECKSUM for I/O corruption detection.",
                )
            )

            ansi_flags = [
                db.get("is_ansi_nulls_on"), db.get("is_ansi_padding_on"),
                db.get("is_ansi_warnings_on"), db.get("is_arithabort_on"),
                db.get("is_quoted_identifier_on"),
            ]
            ansi_off = sum(1 for f in ansi_flags if not f)
            rules.append(
                RuleResult(
                    id="WH-030", rule="ANSI Settings Correct", category="Standards", severity="LOW",
                    status="PASS" if ansi_off == 0 else "WARN",
                    details="All ANSI settings are ON." if ansi_off == 0 else f"{ansi_off} ANSI setting(s) are OFF.",
                    recommendation="Enable all ANSI settings for predictable behavior.",
                )
            )
            rules.append(
                RuleResult(
                    id="WH-031", rule="Database ONLINE", category="Availability", severity="HIGH",
                    status="PASS" if db.get("state_desc") == "ONLINE" else "FAIL",
                    details=f"Database state: {db.get('state_desc', 'unknown')}.",
                    recommendation="Ensure database is ONLINE.",
                )
            )
        else:
            rules.append(
                RuleResult(
                    id="WH-026", rule="Database Settings Check", category="Performance", severity="MEDIUM",
                    status="ERROR",
                    details=f"Could not read database settings: {err('dbSettings') or 'unknown'}.",
                )
            )

        # WH-032: Statistics Coverage
        if r.get("stats", {}).get("rows") and r.get("tables", {}).get("rows"):
            stats_data = rows("stats")
            no_stats_tables = [
                t for t in rows("tables")
                if not any(
                    f"{s.get('schema_name','')}.{s.get('table_name','')}" == f"{t.get('schema_name','')}.{t.get('table_name','')}"
                    and s.get("stat_name")
                    for s in stats_data
                )
            ]
            rules.append(
                RuleResult(
                    id="WH-032", rule="All Tables Have Statistics", category="Performance", severity="MEDIUM",
                    status="PASS" if not no_stats_tables else "WARN",
                    details="All tables have statistics." if not no_stats_tables else f"{len(no_stats_tables)} table(s) without statistics.",
                    recommendation="Query these tables to trigger auto-stats creation.",
                )
            )

        # WH-033: Data Type Issues
        col_rows = rows("columns")
        if col_rows:
            dt_issues = _analyze_data_types(col_rows)
            rules.append(
                RuleResult(
                    id="WH-033", rule="Optimal Data Types", category="Performance", severity="MEDIUM",
                    status="PASS" if not dt_issues else "WARN",
                    details="No data type issues detected." if not dt_issues else f"{len(dt_issues)} data type issue(s) found.",
                    recommendation="Fix wide varchar, text dates, and text numeric columns.",
                )
            )

        # WH-034: Low Row Count Tables
        low_row_tables = rows("lowRowCountTables")
        rules.append(
            RuleResult(
                id="WH-034", rule="No Near-Empty Tables", category="Maintainability", severity="LOW",
                status="ERROR" if err("lowRowCountTables") else "PASS" if not low_row_tables else "WARN",
                details=err("lowRowCountTables") or (
                    "No tables with <10 rows." if not low_row_tables
                    else f"{len(low_row_tables)} table(s) with <10 rows: {', '.join(str(x.get('table_name','')) + '(' + str(x.get('row_count','')) + ')' for x in low_row_tables[:5])}"
                ),
                recommendation="Tables with very few rows may be test/staging tables. Remove if unused.",
            )
        )

        # WH-035: Stored Procedures Documentation
        procs = rows("storedProcedures")
        undoc_procs = [p for p in procs if not p.get("has_description")]
        rules.append(
            RuleResult(
                id="WH-035", rule="Stored Procedures Documented", category="Maintainability", severity="LOW",
                status="ERROR" if err("storedProcedures") else "N/A" if not procs else "PASS" if not undoc_procs else "WARN",
                details=err("storedProcedures") or (
                    "No stored procedures." if not procs
                    else f"All {len(procs)} procedure(s) documented." if not undoc_procs
                    else f"{len(undoc_procs)} procedure(s) undocumented: {', '.join(str(x.get('proc_name','')) for x in undoc_procs[:5])}"
                ),
                recommendation="Add MS_Description extended properties to stored procedures.",
            )
        )

        # WH-036: NOT NULL columns without defaults
        no_defaults = rows("missingDefaults")
        rules.append(
            RuleResult(
                id="WH-036", rule="NOT NULL Columns Have Defaults", category="Data Quality", severity="MEDIUM",
                status="ERROR" if err("missingDefaults") else "PASS" if not no_defaults else "WARN",
                details=err("missingDefaults") or (
                    "All NOT NULL columns have DEFAULT constraints." if not no_defaults
                    else f"{len(no_defaults)} NOT NULL column(s) without defaults: {', '.join(str(x.get('table_name','')) + '.' + str(x.get('column_name','')) for x in no_defaults[:5])}"
                ),
                recommendation="Add DEFAULT constraints to NOT NULL columns to prevent insert failures.",
            )
        )

        # WH-037: Unicode/Non-Unicode Mix
        unicode_mixed = rows("unicodeMix")
        rules.append(
            RuleResult(
                id="WH-037", rule="Consistent String Types", category="Maintainability", severity="LOW",
                status="ERROR" if err("unicodeMix") else "PASS" if not unicode_mixed else "WARN",
                details=err("unicodeMix") or (
                    "All tables use consistent string types." if not unicode_mixed
                    else f"{len(unicode_mixed)} table(s) mix varchar/nvarchar: {', '.join(str(x.get('table_name','')) + '(' + str(x.get('unicode_count','')) + 'n + ' + str(x.get('non_unicode_count','')) + 'v)' for x in unicode_mixed[:5])}"
                ),
                recommendation="Standardize on nvarchar (Unicode) or varchar (non-Unicode) within each table.",
            )
        )

        # WH-038: Schema Documentation
        schemas = rows("schemaDocumentation")
        undoc_schemas = [s for s in schemas if not s.get("has_description")]
        rules.append(
            RuleResult(
                id="WH-038", rule="Schemas Are Documented", category="Maintainability", severity="LOW",
                status="ERROR" if err("schemaDocumentation") else "N/A" if not schemas else "PASS" if not undoc_schemas else "WARN",
                details=err("schemaDocumentation") or (
                    "No user schemas." if not schemas
                    else f"All {len(schemas)} schema(s) documented." if not undoc_schemas
                    else f"{len(undoc_schemas)} schema(s) undocumented: {', '.join(str(x.get('schema_name','')) for x in undoc_schemas[:5])}"
                ),
                recommendation="Add MS_Description extended properties to schemas for documentation.",
            )
        )

        # WH-039: Query Performance Average
        qv_avg = rows("queryVolumeAvg")
        if qv_avg and qv_avg[0].get("avg_duration_ms") is not None:
            avg_ms = qv_avg[0].get("avg_duration_ms", 0) or 0
            total_queries = qv_avg[0].get("total_queries", 0) or 0
            rules.append(
                RuleResult(
                    id="WH-039", rule="Query Performance Healthy", category="Performance", severity="MEDIUM",
                    status="PASS" if avg_ms < 5000 else "WARN" if avg_ms < 30000 else "FAIL",
                    details=f"Average query duration: {avg_ms / 1000:.1f}s over {total_queries} queries (last 7 days).",
                    recommendation="Investigate slow query patterns — average exceeds 5s." if avg_ms >= 5000 else None,
                )
            )

        # WH-040: AUTO_CREATE_STATISTICS enabled
        auto_create_stats = rows("dbSettingsExtended")
        auto_create_enabled = (
            len(auto_create_stats) > 0
            and auto_create_stats[0].get("is_auto_create_stats_on") is True
        )
        rules.append(
            RuleResult(
                id="WH-040", rule="AUTO_CREATE_STATISTICS Enabled", category="Performance", severity="HIGH",
                status="ERROR" if err("dbSettingsExtended") else "PASS" if auto_create_enabled else "FAIL",
                details=err("dbSettingsExtended") or (
                    "Auto-create statistics is enabled." if auto_create_enabled
                    else "Auto-create statistics is disabled."
                ),
                recommendation="Enable with: ALTER DATABASE SET AUTO_CREATE_STATISTICS ON",
            )
        )

        # WH-041: QUERY_STORE enabled
        query_store_on = (
            len(auto_create_stats) > 0
            and auto_create_stats[0].get("is_query_store_on") is True
        )
        rules.append(
            RuleResult(
                id="WH-041", rule="Query Store Enabled", category="Performance", severity="MEDIUM",
                status="ERROR" if err("dbSettingsExtended") else "PASS" if query_store_on else "WARN",
                details=err("dbSettingsExtended") or (
                    "Query Store is enabled for performance monitoring." if query_store_on
                    else "Query Store is not enabled."
                ),
                recommendation="Enable with: ALTER DATABASE SET QUERY_STORE = ON",
            )
        )

        # WH-042: Excessive computed columns
        computed_cols = rows("computedColumns")
        all_col_counts = rows("allColumns")
        tables_excessive_computed: List[str] = []
        for tc in all_col_counts:
            tbl = f"{tc.get('schema_name', '')}.{tc.get('table_name', '')}"
            total_cols = tc.get("col_count", 0) or 0
            comp_count = sum(
                1
                for c in computed_cols
                if f"{c.get('schema_name', '')}.{c.get('table_name', '')}" == tbl
            )
            if total_cols > 0 and comp_count / total_cols > 0.3:
                tables_excessive_computed.append(f"{tbl} ({comp_count}/{total_cols})")
        rules.append(
            RuleResult(
                id="WH-042", rule="No Excessive Computed Columns", category="Maintainability", severity="LOW",
                status="ERROR" if err("computedColumns") else "PASS" if not tables_excessive_computed else "WARN",
                details=err("computedColumns") or (
                    "No tables with >30% computed columns." if not tables_excessive_computed
                    else f"{len(tables_excessive_computed)} table(s): {', '.join(tables_excessive_computed[:3])}"
                ),
                recommendation="Review computed columns — consider materializing in source or using views.",
            )
        )

        # WH-043: Query hints audit
        hint_objects = rows("queryHints")
        rules.append(
            RuleResult(
                id="WH-043", rule="No Forced Query Hints", category="Performance", severity="LOW",
                status="ERROR" if err("queryHints") else "PASS" if not hint_objects else "WARN",
                details=err("queryHints") or (
                    "No objects using query hints." if not hint_objects
                    else f"{len(hint_objects)} object(s) using hints: {', '.join(str(h.get('object_name','')) for h in hint_objects[:3])}"
                ),
                recommendation="Review NOLOCK/FORCESEEK hints — they may mask optimizer issues.",
            )
        )

        # WH-044: Missing indexes on FK columns
        missing_fk_idx = rows("fkWithoutIndex")
        rules.append(
            RuleResult(
                id="WH-044", rule="FK Columns Have Indexes", category="Performance", severity="MEDIUM",
                status="ERROR" if err("fkWithoutIndex") else "PASS" if not missing_fk_idx else "WARN",
                details=err("fkWithoutIndex") or (
                    "All FK columns have supporting indexes." if not missing_fk_idx
                    else f"{len(missing_fk_idx)} FK column(s) missing indexes: {', '.join(str(x.get('table_name','')) + '.' + str(x.get('column_name','')) for x in missing_fk_idx[:3])}"
                ),
                recommendation="Create indexes on FK columns for better join performance.",
            )
        )

        return render_rule_report(
            f"Warehouse Analysis: {warehouse['displayName']}",
            datetime.now(timezone.utc).isoformat(),
            header,
            rules,
        )
    except Exception as e:
        return f"Error analyzing warehouse: {e}"


# ──────────────────────────────────────────────
# Tool: warehouse_analyze_query_patterns
# ──────────────────────────────────────────────


def warehouse_analyze_query_patterns(args: dict) -> str:
    try:
        warehouse = get_warehouse(args["workspaceId"], args["warehouseId"])
        props = warehouse.get("properties") or {}
        connection_string = props.get("connectionString")

        if not connection_string:
            return f'Warehouse "{warehouse["displayName"]}" has no SQL connection string available. Cannot analyze queries.'

        results = run_diagnostic_queries(
            connection_string,
            warehouse["displayName"],
            {
                "slowQueries": WAREHOUSE_DIAGNOSTICS["slowQueries"],
                "frequentQueries": WAREHOUSE_DIAGNOSTICS["frequentQueries"],
                "failedQueries": WAREHOUSE_DIAGNOSTICS["failedQueries"],
                "queryVolume": WAREHOUSE_DIAGNOSTICS["queryVolume"],
            },
        )

        report = [
            f"# 📊 Query Pattern Analysis: {warehouse['displayName']}",
            "",
            f"_Live analysis at {datetime.now(timezone.utc).isoformat()}_",
            "",
        ]

        slow = results.get("slowQueries", {}).get("rows")
        if slow is not None:
            report.extend(["## 🐢 Slowest Queries", ""])
            report.extend(_analyze_slow_queries(slow))
            report.append("")

        freq = results.get("frequentQueries", {}).get("rows")
        if freq is not None:
            report.extend(["## 🔄 Most Frequent Queries", ""])
            report.extend(_analyze_frequent_queries(freq))
            report.append("")

        failed = results.get("failedQueries", {}).get("rows")
        if failed is not None:
            report.extend(["## ❌ Recent Failures", ""])
            report.extend(_analyze_failed_queries(failed))
            report.append("")

        vol = results.get("queryVolume", {}).get("rows")
        if vol:
            report.extend(["## 📈 Daily Volume", ""])
            report.append("| Date | Queries | Avg Duration |")
            report.append("|------|---------|-------------|")
            for rv in vol[:14]:
                avg = f"{(rv.get('avg_duration_ms', 0) or 0) / 1000:.1f}"
                report.append(
                    f"| {rv.get('query_date', '')} | {rv.get('query_count', '')} | {avg}s |"
                )
            report.append("")

        return "\n".join(report)
    except Exception as e:
        return f"Error analyzing query patterns: {e}"


# ──────────────────────────────────────────────
# Warehouse fix definitions
# ──────────────────────────────────────────────


def _wh_fix_001(fix_args: dict, diag: dict) -> List[str]:
    """Add PRIMARY KEY NOT ENFORCED constraints."""
    tables = (diag.get("missingPrimaryKeys") or {}).get("rows") or []
    result = []
    for t in tables:
        tbl = _quote_sql_id(str(t.get("table_name", "")))
        tbl_safe = re.sub(r"[^a-zA-Z0-9_]", "_", str(t.get("table_name", "")))
        cols = [
            c for c in ((diag.get("nullableKeyColumns") or {}).get("rows") or [])
            if str(c.get("table_name", "")) == str(t.get("table_name", ""))
        ]
        pk_col = f"[{cols[0].get('COLUMN_NAME', 'id')}]" if cols else "[id]"
        result.append(
            f"ALTER TABLE {tbl} ADD CONSTRAINT [PK_{tbl_safe}] PRIMARY KEY NONCLUSTERED ({pk_col}) NOT ENFORCED"
        )
    return result


def _wh_fix_008(fix_args: dict, diag: dict) -> List[str]:
    """Refresh stale statistics."""
    stale = (diag.get("staleStatistics") or {}).get("rows") or []
    tables = list({str(s.get("table_name", "")) for s in stale})
    return [f"UPDATE STATISTICS {_quote_sql_id(t)}" for t in tables]


def _wh_fix_009(fix_args: dict, diag: dict) -> List[str]:
    """Re-enable disabled/untrusted constraints."""
    constraints = (diag.get("constraintCheck") or {}).get("rows") or []
    return [
        f"ALTER TABLE {_quote_sql_id(str(c.get('table_name', '')))} WITH CHECK CHECK CONSTRAINT [{c.get('constraint_name', '')}]"
        for c in constraints
    ]


def _wh_fix_016(fix_args: dict, diag: dict) -> List[str]:
    """Add audit columns."""
    tables = (diag.get("missingAuditColumns") or {}).get("rows") or []
    return [
        f"ALTER TABLE {_quote_sql_id(str(t.get('table_name', '')))} ADD [created_at] DATETIME2 NULL DEFAULT GETDATE(), [updated_at] DATETIME2 NULL DEFAULT GETDATE()"
        for t in tables
    ]


def _wh_fix_018(fix_args: dict, diag: dict) -> List[str]:
    """Apply dynamic data masking to sensitive/PII columns."""
    sensitive = (diag.get("sensitiveColumns") or {}).get("rows") or []
    masked = {
        f"{r.get('table_name','')}.{r.get('column_name','')}"
        for r in ((diag.get("dataMaskingCheck") or {}).get("rows") or [])
    }
    result = []
    for c in sensitive:
        key = f"{c.get('table_name','')}.{c.get('column_name','')}"
        if key in masked:
            continue
        col = str(c.get("column_name") or c.get("COLUMN_NAME", ""))
        col_lower = col.lower()
        if "email" in col_lower:
            fn = "email()"
        elif "phone" in col_lower or "mobile" in col_lower:
            fn = 'partial(0,"XXX-XXX-",4)'
        else:
            fn = "default()"
        result.append(
            f"ALTER TABLE {_quote_sql_id(str(c.get('table_name', '')))} ALTER COLUMN [{col}] ADD MASKED WITH (FUNCTION = '{fn}')"
        )
    return result


def _wh_fix_db_setting(setting_sql: str):
    """Factory for simple database setting fixes."""
    def _fix(fix_args: dict, _diag: dict) -> List[str]:
        db_name = fix_args.get("warehouseName", "current")
        return [setting_sql.format(db=db_name)]
    return _fix


def _wh_fix_030(fix_args: dict, _diag: dict) -> List[str]:
    """Enable all ANSI settings."""
    db = fix_args.get("warehouseName", "current")
    return [
        f"ALTER DATABASE [{db}] SET ANSI_NULLS ON",
        f"ALTER DATABASE [{db}] SET ANSI_PADDING ON",
        f"ALTER DATABASE [{db}] SET ANSI_WARNINGS ON",
        f"ALTER DATABASE [{db}] SET ARITHABORT ON",
        f"ALTER DATABASE [{db}] SET QUOTED_IDENTIFIER ON",
    ]


def _wh_fix_032(fix_args: dict, diag: dict) -> List[str]:
    """Create statistics on tables without any."""
    tables = (diag.get("tables") or {}).get("rows") or []
    stats_data = (diag.get("stats") or {}).get("rows") or []
    no_stats = [
        t for t in tables
        if not any(
            f"{s.get('schema_name','')}.{s.get('table_name','')}" == f"{t.get('schema_name','')}.{t.get('table_name','')}"
            and s.get("stat_name")
            for s in stats_data
        )
    ]
    return [f"UPDATE STATISTICS [{t.get('schema_name','')}].[{t.get('table_name','')}]" for t in no_stats]


def _wh_fix_036(fix_args: dict, diag: dict) -> List[str]:
    """Add DEFAULT constraints to NOT NULL columns without them."""
    missing = (diag.get("missingDefaults") or {}).get("rows") or []
    return [
        f"ALTER TABLE {_quote_sql_id(str(r.get('table_name', '')))} ADD DEFAULT '' FOR [{r.get('column_name', '')}]"
        for r in missing[:20]
    ]


def _wh_fix_044(fix_args: dict, diag: dict) -> List[str]:
    """Create indexes on FK columns missing them."""
    missing = (diag.get("fkWithoutIndex") or {}).get("rows") or []
    result = []
    for r_item in missing[:10]:
        schema = str(r_item.get("schema_name", ""))
        table = str(r_item.get("table_name", ""))
        col = str(r_item.get("column_name", ""))
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", f"{schema}_{table}_{col}")
        result.append(
            f"CREATE NONCLUSTERED INDEX [IX_FK_{safe_name}] ON [{schema}].[{table}] ([{col}])"
        )
    return result


WAREHOUSE_FIXES: Dict[str, Dict[str, Any]] = {
    "WH-001": {"description": "Add PRIMARY KEY NOT ENFORCED constraints to tables missing PKs", "get_sql": _wh_fix_001},
    "WH-008": {"description": "Refresh stale statistics (>30 days old)", "get_sql": _wh_fix_008},
    "WH-009": {"description": "Re-enable disabled/untrusted constraints", "get_sql": _wh_fix_009},
    "WH-016": {"description": "Add audit columns (created_at, updated_at) to tables", "get_sql": _wh_fix_016},
    "WH-018": {"description": "Apply dynamic data masking to sensitive/PII columns", "get_sql": _wh_fix_018},
    "WH-026": {"description": "Enable AUTO_UPDATE_STATISTICS", "get_sql": _wh_fix_db_setting("ALTER DATABASE [{db}] SET AUTO_UPDATE_STATISTICS ON")},
    "WH-027": {"description": "Enable result set caching", "get_sql": _wh_fix_db_setting("ALTER DATABASE [{db}] SET RESULT_SET_CACHING ON")},
    "WH-028": {"description": "Enable snapshot isolation", "get_sql": _wh_fix_db_setting("ALTER DATABASE [{db}] SET ALLOW_SNAPSHOT_ISOLATION ON")},
    "WH-029": {"description": "Set PAGE_VERIFY to CHECKSUM", "get_sql": _wh_fix_db_setting("ALTER DATABASE [{db}] SET PAGE_VERIFY CHECKSUM")},
    "WH-030": {"description": "Enable all ANSI settings", "get_sql": _wh_fix_030},
    "WH-032": {"description": "Create statistics on tables without any", "get_sql": _wh_fix_032},
    "WH-036": {"description": "Add DEFAULT constraints to NOT NULL columns without them", "get_sql": _wh_fix_036},
    "WH-040": {"description": "Enable AUTO_CREATE_STATISTICS", "get_sql": _wh_fix_db_setting("ALTER DATABASE [{db}] SET AUTO_CREATE_STATISTICS ON")},
    "WH-041": {"description": "Enable Query Store", "get_sql": _wh_fix_db_setting("ALTER DATABASE [{db}] SET QUERY_STORE = ON")},
    "WH-044": {"description": "Create indexes on FK columns missing them", "get_sql": _wh_fix_044},
}


# ──────────────────────────────────────────────
# Tool: warehouse_fix
# ──────────────────────────────────────────────


def warehouse_fix(args: dict) -> str:
    try:
        warehouse = get_warehouse(args["workspaceId"], args["warehouseId"])
        props = warehouse.get("properties") or {}
        connection_string = props.get("connectionString")

        if not connection_string:
            return "❌ No SQL connection string available. Cannot apply fixes."

        _validate_sql_name(warehouse["displayName"], "warehouse name")

        is_dry_run = args.get("dryRun", False)

        # Run diagnostics
        needed_queries = [
            "missingPrimaryKeys", "nullableKeyColumns", "staleStatistics",
            "constraintCheck", "missingAuditColumns", "sensitiveColumns",
            "dataMaskingCheck", "tables", "stats", "missingDefaults",
            "dbSettings", "fkWithoutIndex", "dbSettingsExtended",
        ]
        diag_queries = {k: WAREHOUSE_DIAGNOSTICS[k] for k in needed_queries if k in WAREHOUSE_DIAGNOSTICS}
        diagnostics = run_diagnostic_queries(
            connection_string, warehouse["displayName"], diag_queries
        )

        fixable_rule_ids = list(WAREHOUSE_FIXES.keys())
        rule_ids = (
            [rid for rid in (args.get("ruleIds") or []) if rid in fixable_rule_ids]
            if args.get("ruleIds")
            else fixable_rule_ids
        )

        results: List[str] = []
        total_fixed = 0
        total_failed = 0
        total_skipped = 0

        for rule_id in rule_ids:
            fix = WAREHOUSE_FIXES.get(rule_id)
            if not fix:
                continue

            sqls = fix["get_sql"](
                {"warehouseName": warehouse["displayName"]},
                diagnostics,
            )

            if not sqls:
                results.append(f"| {rule_id} | ⚪ | No action needed | — |")
                total_skipped += 1
                continue

            for sql_cmd in sqls:
                if is_dry_run:
                    results.append(
                        f"| {rule_id} | 🔍 | {fix['description']} | `{sql_cmd[:80]}...` |"
                    )
                    total_skipped += 1
                else:
                    try:
                        execute_sql_query(
                            connection_string,
                            warehouse["displayName"],
                            sql_cmd,
                        )
                        results.append(
                            f"| {rule_id} | ✅ | {fix['description']} | `{sql_cmd[:80]}...` |"
                        )
                        total_fixed += 1
                    except Exception as e:
                        msg = str(e)[:80]
                        results.append(
                            f"| {rule_id} | ❌ | Failed: {msg} | `{sql_cmd[:60]}...` |"
                        )
                        total_failed += 1

        mode = "DRY RUN (preview only)" if is_dry_run else "Applying fixes"
        summary = (
            f"**{total_skipped} command(s) previewed** — re-run without dryRun to apply."
            if is_dry_run
            else f"**{total_fixed} fixed, {total_failed} failed"
            + (f", {total_skipped} skipped" if total_skipped > 0 else "")
            + "**"
        )

        lines = [
            f"# 🔧 Warehouse Fix: {warehouse['displayName']}",
            "",
            f"_{mode} at {datetime.now(timezone.utc).isoformat()}_",
            "",
            summary,
            "",
            "| Rule | Status | Action | SQL |",
            "|------|--------|--------|-----|",
            *results,
            "",
        ]
        if is_dry_run:
            lines.append("> 💡 Set `dryRun: false` to execute these commands.")

        return "\n".join(lines)
    except Exception as e:
        return f"Error applying warehouse fix: {e}"


# ──────────────────────────────────────────────
# Tool: warehouse_auto_optimize
# ──────────────────────────────────────────────


def warehouse_auto_optimize(args: dict) -> str:
    return warehouse_fix(
        {
            "workspaceId": args["workspaceId"],
            "warehouseId": args["warehouseId"],
            "ruleIds": None,
            "dryRun": args.get("dryRun"),
        }
    )


# ──────────────────────────────────────────────
# Tool definitions for MCP registration
# ──────────────────────────────────────────────

warehouse_tools = [
    {
        "name": "warehouse_list",
        "description": "List all warehouses in a Fabric workspace with their metadata and connection details.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
            },
            "required": ["workspaceId"],
        },
        "handler": warehouse_list,
    },
    {
        "name": "warehouse_optimization_recommendations",
        "description": (
            "LIVE SCAN: Connects to a Fabric Warehouse SQL endpoint and runs real diagnostic queries. "
            "Analyzes table schemas, data types, statistics coverage, slow queries, frequent queries, "
            "failed queries, and query volume trends. Returns findings with prioritized action items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "warehouseId": {
                    "type": "string",
                    "description": "The ID of the warehouse to analyze",
                },
            },
            "required": ["workspaceId", "warehouseId"],
        },
        "handler": warehouse_optimization_recommendations,
    },
    {
        "name": "warehouse_analyze_query_patterns",
        "description": (
            "LIVE SCAN: Connects to a Fabric Warehouse SQL endpoint and analyzes real query execution "
            "history. Returns top slow queries, most frequent queries, recent failures, and daily "
            "query volume trends with actual data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "warehouseId": {
                    "type": "string",
                    "description": "The ID of the warehouse",
                },
            },
            "required": ["workspaceId", "warehouseId"],
        },
        "handler": warehouse_analyze_query_patterns,
    },
    {
        "name": "warehouse_fix",
        "description": (
            "AUTO-FIX: Connects to a Fabric Warehouse and applies fixes for detected issues. "
            "Can fix: stale statistics, missing PKs, disabled constraints, missing audit columns, "
            "sensitive data masking, database settings (AUTO_UPDATE_STATISTICS, AUTO_CREATE_STATISTICS, "
            "result set caching, snapshot isolation, ANSI settings), Query Store, and missing FK indexes. "
            "Fixable rule IDs: WH-001, WH-008, WH-026, WH-027, WH-028, WH-029, WH-030, WH-032, "
            "WH-036, WH-040, WH-041, WH-044. "
            "Specify ruleIds to fix specific issues, or omit to fix all auto-fixable issues. "
            "Use dryRun=true to preview SQL commands without executing them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "warehouseId": {
                    "type": "string",
                    "description": "The ID of the warehouse to fix",
                },
                "ruleIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: specific rule IDs to fix (e.g. ['WH-008', 'WH-026']). If omitted, all auto-fixable rules are applied.",
                },
                "dryRun": {
                    "type": "boolean",
                    "description": "If true, preview SQL commands without executing them (default: false)",
                },
            },
            "required": ["workspaceId", "warehouseId"],
        },
        "handler": warehouse_fix,
    },
    {
        "name": "warehouse_auto_optimize",
        "description": (
            "AUTO-OPTIMIZE: Scans a Fabric Warehouse for all fixable issues and applies all safe fixes automatically. "
            "Runs diagnostics first, then applies: stale statistics refresh, PK constraints, ANSI settings, "
            "result set caching, snapshot isolation, AUTO_CREATE_STATISTICS, Query Store, FK indexes, and more. "
            "Fixable rule IDs: WH-001, WH-008, WH-026, WH-027, WH-028, WH-029, WH-030, WH-032, "
            "WH-036, WH-040, WH-041, WH-044. Use dryRun=true to preview."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "warehouseId": {
                    "type": "string",
                    "description": "The ID of the warehouse to optimize",
                },
                "dryRun": {
                    "type": "boolean",
                    "description": "If true, preview SQL commands without executing (default: false)",
                },
            },
            "required": ["workspaceId", "warehouseId"],
        },
        "handler": warehouse_auto_optimize,
    },
]
