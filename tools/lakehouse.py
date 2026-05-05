"""Lakehouse tools — ported from lakehouse.ts."""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from clients.fabric_client import (
    get_lakehouse,
    get_workspace,
    list_lakehouse_tables,
    list_lakehouses,
    run_lakehouse_table_maintenance,
    get_lakehouse_job_status,
    run_temporary_notebook,
)
from clients.livy_client import run_spark_fixes_via_livy
from clients.sql_client import run_diagnostic_queries
from clients.onelake_client import (
    read_delta_log,
    get_partition_columns,
    get_table_config,
    get_last_operation,
    count_operations,
    get_file_size_stats,
    days_since_timestamp,
)
from tools.rule_engine import RuleResult, render_rule_report

# ──────────────────────────────────────────────
# Input validation — prevent Spark SQL injection
# ──────────────────────────────────────────────

SAFE_SPARK_NAME = re.compile(r"^[a-zA-Z0-9_\- .]+$")


def _validate_spark_name(value: str, label: str) -> None:
    if not SAFE_SPARK_NAME.match(value):
        raise ValueError(
            f"Invalid {label}: must be alphanumeric/underscore/dash/dot only."
        )


# ──────────────────────────────────────────────
# Tool: lakehouse_list
# ──────────────────────────────────────────────


def lakehouse_list(args: dict) -> str:
    try:
        lakehouses = list_lakehouses(args["workspaceId"])

        if not lakehouses:
            return "No lakehouses found in this workspace."

        lines: List[str] = []
        for lh in lakehouses:
            props = lh.get("properties") or {}
            sql_props = props.get("sqlEndpointProperties") or {}
            sql_status = sql_props.get("provisioningStatus", "unknown")
            parts = [
                f"- **{lh['displayName']}** (ID: {lh['id']})",
                f"  SQL Endpoint: {sql_status}",
            ]
            tables_path = props.get("oneLakeTablesPath")
            if tables_path:
                parts.append(f"  Tables Path: {tables_path}")
            files_path = props.get("oneLakeFilesPath")
            if files_path:
                parts.append(f"  Files Path: {files_path}")
            lines.append("\n".join(parts))

        return f"## Lakehouses in workspace {args['workspaceId']}\n\n" + "\n\n".join(
            lines
        )
    except Exception as e:
        return f"Error listing lakehouses: {e}"


# ──────────────────────────────────────────────
# Tool: lakehouse_list_tables
# ──────────────────────────────────────────────


def lakehouse_list_tables(args: dict) -> str:
    try:
        lakehouse = get_lakehouse(args["workspaceId"], args["lakehouseId"])
        tables = list_lakehouse_tables(args["workspaceId"], args["lakehouseId"])

        if not tables:
            return f'Lakehouse "{lakehouse["displayName"]}" has no tables.'

        rows = [
            f"| {t['name']} | {t.get('type', '')} | {t.get('format', '')} | {t.get('location', '')} |"
            for t in tables
        ]

        return "\n".join(
            [
                f'## Tables in Lakehouse "{lakehouse["displayName"]}"',
                "",
                f"Total: {len(tables)} table(s)",
                "",
                "| Name | Type | Format | Location |",
                "|------|------|--------|----------|",
                *rows,
            ]
        )
    except Exception as e:
        return f"Error listing tables: {e}"


# ──────────────────────────────────────────────
# Tool: lakehouse_run_table_maintenance
# ──────────────────────────────────────────────


def lakehouse_run_table_maintenance(args: dict) -> str:
    try:
        execution_data: Dict[str, Any] = {}
        table_name = args.get("tableName")
        optimize_settings = args.get("optimizeSettings")
        vacuum_settings = args.get("vacuumSettings")

        if table_name:
            table_config: Dict[str, Any] = {}

            if optimize_settings:
                opt: Dict[str, Any] = {
                    "vOrder": optimize_settings.get("vOrder", True)
                }
                z_cols = optimize_settings.get("zOrderColumns")
                if z_cols and len(z_cols) > 0:
                    opt["zOrderBy"] = z_cols
                table_config["optimizeSettings"] = opt

            if vacuum_settings:
                table_config["vacuumSettings"] = {
                    "retentionPeriod": vacuum_settings.get(
                        "retentionPeriod", "7.00:00:00"
                    )
                }

            # Default: both optimize with vOrder and vacuum
            if not optimize_settings and not vacuum_settings:
                table_config["optimizeSettings"] = {"vOrder": True}
                table_config["vacuumSettings"] = {"retentionPeriod": "7.00:00:00"}

            execution_data["tablesToProcess"] = [
                {"tableName": table_name, **table_config}
            ]

        result = run_lakehouse_table_maintenance(
            args["workspaceId"],
            args["lakehouseId"],
            "TableMaintenance",
            execution_data if execution_data else None,
        )

        parts = [
            "## Table Maintenance Job Started",
            "",
            f"- **Job ID**: {result.get('id', 'N/A')}",
            f"- **Status**: {result.get('status', 'Accepted')}",
            f"- **Table**: {table_name}" if table_name else "- **Scope**: All tables",
        ]
        if optimize_settings and optimize_settings.get("vOrder") is not False:
            parts.append("- **V-Order**: Enabled")
        z_cols = (optimize_settings or {}).get("zOrderColumns")
        if z_cols and len(z_cols) > 0:
            parts.append(f"- **Z-Order Columns**: {', '.join(z_cols)}")
        if vacuum_settings:
            parts.append(
                f"- **Vacuum Retention**: {vacuum_settings.get('retentionPeriod', '7 days')}"
            )
        parts.extend(["", "Use `lakehouse_get_job_status` to check progress."])

        return "\n".join(p for p in parts if p is not None)
    except Exception as e:
        return f"Error starting table maintenance: {e}"


# ──────────────────────────────────────────────
# Tool: lakehouse_get_job_status
# ──────────────────────────────────────────────


def lakehouse_get_job_status(args: dict) -> str:
    try:
        job = get_lakehouse_job_status(
            args["workspaceId"], args["lakehouseId"], args["jobInstanceId"]
        )

        lines: List[Optional[str]] = [
            "## Job Status",
            "",
            f"- **Job ID**: {job.get('id', 'N/A')}",
            f"- **Type**: {job.get('jobType', 'N/A')}",
            f"- **Status**: {job.get('status', 'N/A')}",
            f"- **Started**: {job['startTimeUtc']}" if job.get("startTimeUtc") else None,
            f"- **Completed**: {job['endTimeUtc']}" if job.get("endTimeUtc") else None,
        ]

        failure = job.get("failureReason")
        if failure:
            lines.extend(
                [
                    "",
                    "### Failure Details",
                    f"- **Error**: {failure.get('message', 'N/A')}",
                    f"- **Code**: {failure.get('errorCode', 'N/A')}",
                ]
            )

        return "\n".join(l for l in lines if l is not None)
    except Exception as e:
        return f"Error getting job status: {e}"


# ──────────────────────────────────────────────
# SQL Diagnostics for Lakehouse SQL Endpoint
# ──────────────────────────────────────────────

LAKEHOUSE_SQL_DIAGNOSTICS = {
    "tableInfo": """
    SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
    FROM INFORMATION_SCHEMA.TABLES
    ORDER BY TABLE_SCHEMA, TABLE_NAME""",
    "columnInfo": """
    SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE,
           CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, IS_NULLABLE
    FROM INFORMATION_SCHEMA.COLUMNS
    ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION""",
    "tableRowCounts": """
    SELECT s.name AS schema_name, t.name AS table_name,
           SUM(p.rows) AS row_count
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    JOIN sys.partitions p ON t.object_id = p.object_id
    WHERE p.index_id IN (0,1)
    GROUP BY s.name, t.name
    ORDER BY row_count DESC""",
    "nullableColumnsRatio": """
    SELECT TABLE_SCHEMA, TABLE_NAME,
           COUNT(*) AS total_columns,
           SUM(CASE WHEN IS_NULLABLE = 'YES' THEN 1 ELSE 0 END) AS nullable_count
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
    GROUP BY TABLE_SCHEMA, TABLE_NAME
    ORDER BY total_columns DESC""",
    "dataTypeDistribution": """
    SELECT DATA_TYPE, COUNT(*) AS column_count
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
    GROUP BY DATA_TYPE
    ORDER BY column_count DESC""",
    "wideStringColumns": """
    SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE,
           CHARACTER_MAXIMUM_LENGTH
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
      AND DATA_TYPE IN ('varchar', 'nvarchar', 'char', 'nchar')
      AND CHARACTER_MAXIMUM_LENGTH > 500
    ORDER BY CHARACTER_MAXIMUM_LENGTH DESC""",
    "nullableKeyColumns": """
    SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
      AND IS_NULLABLE = 'YES'
      AND (COLUMN_NAME LIKE '%Id' OR COLUMN_NAME LIKE '%_id'
           OR COLUMN_NAME LIKE '%Key' OR COLUMN_NAME LIKE '%_key'
           OR COLUMN_NAME = 'id' OR COLUMN_NAME = 'pk')
    ORDER BY TABLE_NAME, COLUMN_NAME""",
    "floatingPointColumns": """
    SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
      AND DATA_TYPE IN ('float', 'real')
    ORDER BY TABLE_NAME, COLUMN_NAME""",
    "columnNamingIssues": """
    SELECT t.name AS table_name, c.name AS column_name
    FROM sys.tables t
    JOIN sys.columns c ON t.object_id = c.object_id
    WHERE c.name COLLATE Latin1_General_BIN LIKE '%[^a-zA-Z0-9_]%'
      AND t.is_ms_shipped = 0
    ORDER BY t.name, c.name""",
    "missingAuditColumns": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           SUM(CASE WHEN c.name IN ('created_at','created_date','CreatedAt','CreatedDate','__created_at') THEN 1 ELSE 0 END) AS has_created,
           SUM(CASE WHEN c.name IN ('updated_at','updated_date','modified_at','ModifiedAt','__updated_at') THEN 1 ELSE 0 END) AS has_updated
    FROM sys.tables t
    JOIN sys.columns c ON t.object_id = c.object_id
    WHERE t.is_ms_shipped = 0
    GROUP BY t.schema_id, t.name
    HAVING SUM(CASE WHEN c.name IN ('created_at','created_date','CreatedAt','CreatedDate','updated_at','updated_date','modified_at','ModifiedAt','__created_at','__updated_at') THEN 1 ELSE 0 END) = 0""",
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
    "emptyTables": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name
    FROM sys.tables t
    JOIN sys.partitions p ON t.object_id = p.object_id
    WHERE p.index_id IN (0,1) AND t.is_ms_shipped = 0
    GROUP BY t.schema_id, t.name
    HAVING SUM(p.rows) = 0""",
    "textDateColumns": """
    SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
      AND DATA_TYPE IN ('varchar', 'nvarchar')
      AND (COLUMN_NAME LIKE '%date%' OR COLUMN_NAME LIKE '%time%'
           OR COLUMN_NAME LIKE '%created%' OR COLUMN_NAME LIKE '%modified%'
           OR COLUMN_NAME LIKE '%updated%' OR COLUMN_NAME LIKE '%_dt' OR COLUMN_NAME LIKE '%_ts')
    ORDER BY TABLE_NAME, COLUMN_NAME""",
    "textNumericColumns": """
    SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
      AND DATA_TYPE IN ('varchar', 'nvarchar', 'char')
      AND CHARACTER_MAXIMUM_LENGTH <= 20
      AND (COLUMN_NAME LIKE '%id' OR COLUMN_NAME LIKE '%num%' OR COLUMN_NAME LIKE '%code%'
           OR COLUMN_NAME LIKE '%amount%' OR COLUMN_NAME LIKE '%price%' OR COLUMN_NAME LIKE '%qty%')
      AND COLUMN_NAME NOT LIKE '%guid%' AND COLUMN_NAME NOT LIKE '%uuid%'
    ORDER BY TABLE_NAME, COLUMN_NAME""",
    "sensitiveColumns": """
    SELECT TABLE_SCHEMA + '.' + TABLE_NAME AS table_name, COLUMN_NAME
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')
      AND (COLUMN_NAME LIKE '%credit%' OR COLUMN_NAME LIKE '%ssn%' OR COLUMN_NAME LIKE '%password%'
           OR COLUMN_NAME LIKE '%secret%' OR COLUMN_NAME LIKE '%phone%' OR COLUMN_NAME LIKE '%email%'
           OR COLUMN_NAME LIKE '%IBAN%' OR COLUMN_NAME LIKE '%SWIFT%' OR COLUMN_NAME LIKE '%BIC%'
           OR COLUMN_NAME LIKE '%license%' OR COLUMN_NAME LIKE '%tax%id%')
    ORDER BY TABLE_NAME, COLUMN_NAME""",
    "largeTables": """
    SELECT s.name AS schema_name, t.name AS table_name,
           SUM(p.rows) AS row_count
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    JOIN sys.partitions p ON t.object_id = p.object_id
    WHERE p.index_id IN (0,1) AND t.is_ms_shipped = 0
    GROUP BY s.name, t.name
    HAVING SUM(p.rows) > 1000000
    ORDER BY SUM(p.rows) DESC""",
    "deprecatedTypes": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name,
           c.name AS column_name, TYPE_NAME(c.user_type_id) AS data_type
    FROM sys.tables t
    JOIN sys.columns c ON t.object_id = c.object_id
    WHERE t.is_ms_shipped = 0
      AND TYPE_NAME(c.user_type_id) IN ('text', 'ntext', 'image')
    ORDER BY t.name, c.name""",
    "tablesWithoutKeys": """
    SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name AS table_name
    FROM sys.tables t
    WHERE t.is_ms_shipped = 0
      AND NOT EXISTS (
        SELECT 1 FROM sys.columns c
        WHERE c.object_id = t.object_id
          AND (c.name LIKE '%Id' OR c.name LIKE '%_id' OR c.name LIKE '%Key'
               OR c.name LIKE '%_key' OR c.name = 'id' OR c.name = 'pk')
      )
    ORDER BY t.name""",
}


# ──────────────────────────────────────────────
# Tool: lakehouse_optimization_recommendations
# ──────────────────────────────────────────────


def lakehouse_optimization_recommendations(args: dict) -> str:
    try:
        lakehouse = get_lakehouse(args["workspaceId"], args["lakehouseId"])
        tables = list_lakehouse_tables(args["workspaceId"], args["lakehouseId"])

        rules: List[RuleResult] = []
        header: List[str] = []

        props = lakehouse.get("properties") or {}
        sql_ep = props.get("sqlEndpointProperties") or {}
        sql_status = sql_ep.get("provisioningStatus")
        sql_connection = sql_ep.get("connectionString")

        # Header: Endpoints
        sql_label = (
            f"✅ Active (`{sql_connection}`)"
            if sql_status == "Success"
            else f"⚠️ {sql_status or 'unknown'}"
        )
        delta_count = sum(
            1 for t in tables if (t.get("format") or "").lower() == "delta"
        )
        header.extend(
            [
                "## 🔌 Connection Info",
                "",
                f"- **SQL Endpoint**: {sql_label}",
                f"- **Tables**: {len(tables)} ({delta_count} Delta)",
                "",
            ]
        )

        # RULE LH-001: SQL Endpoint Status
        rules.append(
            RuleResult(
                id="LH-001",
                rule="SQL Endpoint Active",
                category="Availability",
                severity="HIGH",
                status="PASS" if sql_status == "Success" else "FAIL",
                details=(
                    "SQL Analytics Endpoint is provisioned and active."
                    if sql_status == "Success"
                    else f"SQL Endpoint status: {sql_status or 'unknown'}. Deep analysis not possible."
                ),
                recommendation="Provision the SQL Analytics Endpoint to enable cross-query analytics and deep analysis.",
            )
        )

        # RULE LH-002: Medallion Architecture Naming
        lh_name = lakehouse["displayName"].lower()
        bronze_kw = ["bronze", "raw", "landing", "ingest", "stage"]
        silver_kw = ["silver", "refined", "intermediate", "cleansed", "enriched"]
        gold_kw = ["gold", "curated", "consumption", "serving", "analytics"]
        has_layer = (
            any(k in lh_name for k in bronze_kw)
            or any(k in lh_name for k in silver_kw)
            or any(k in lh_name for k in gold_kw)
        )
        detected_layer = (
            "Bronze"
            if any(k in lh_name for k in bronze_kw)
            else "Silver"
            if any(k in lh_name for k in silver_kw)
            else "Gold"
            if any(k in lh_name for k in gold_kw)
            else None
        )
        rules.append(
            RuleResult(
                id="LH-002",
                rule="Medallion Architecture Naming",
                category="Maintainability",
                severity="LOW",
                status="PASS" if has_layer else "WARN",
                details=(
                    f"Lakehouse follows {detected_layer} layer naming convention."
                    if has_layer
                    else f'Name "{lakehouse["displayName"]}" doesn\'t follow bronze/silver/gold pattern.'
                ),
                recommendation="Prefix lakehouse names with bronze_/silver_/gold_ for clear data architecture.",
            )
        )

        # RULE LH-003: Non-Delta Tables
        non_delta = [
            t for t in tables if (t.get("format") or "").lower() != "delta"
        ]
        rules.append(
            RuleResult(
                id="LH-003",
                rule="All Tables Use Delta Format",
                category="Performance",
                severity="HIGH",
                status="N/A"
                if not tables
                else "PASS"
                if not non_delta
                else "FAIL",
                details=(
                    "No tables in lakehouse."
                    if not tables
                    else f"All {len(tables)} tables use Delta format."
                    if not non_delta
                    else f"{len(non_delta)} table(s) not using Delta: {', '.join(t['name'] for t in non_delta)}"
                ),
                recommendation="Convert non-Delta tables to Delta format for OPTIMIZE/VACUUM/V-Order support.",
            )
        )

        # RULE LH-004: Table Maintenance
        delta_tables = [
            t for t in tables if (t.get("format") or "").lower() == "delta"
        ]
        rules.append(
            RuleResult(
                id="LH-004",
                rule="Table Maintenance Recommended",
                category="Performance",
                severity="MEDIUM",
                status="N/A" if not delta_tables else "WARN",
                details=(
                    "No Delta tables to maintain."
                    if not delta_tables
                    else f"{len(delta_tables)} Delta table(s) should have regular OPTIMIZE + VACUUM: {', '.join(t['name'] for t in delta_tables)}"
                ),
                recommendation="Run lakehouse_run_table_maintenance regularly (OPTIMIZE with V-Order + VACUUM).",
            )
        )

        # ── SQL Endpoint Analysis ──
        if sql_connection and sql_status == "Success":
            sql = run_diagnostic_queries(
                sql_connection,
                lakehouse["displayName"],
                LAKEHOUSE_SQL_DIAGNOSTICS,
            )

            def _cnt(key: str) -> int:
                entry = sql.get(key) or {}
                return len(entry.get("rows") or [])

            def _err(key: str) -> Optional[str]:
                entry = sql.get(key) or {}
                return entry.get("error")

            def _rows(key: str) -> list:
                entry = sql.get(key) or {}
                return entry.get("rows") or []

            # LH-005: Empty Tables
            empty_count = _cnt("emptyTables")
            rules.append(
                RuleResult(
                    id="LH-005",
                    rule="No Empty Tables",
                    category="Data Quality",
                    severity="MEDIUM",
                    status="ERROR"
                    if _err("emptyTables")
                    else "PASS"
                    if empty_count == 0
                    else "WARN",
                    details=(
                        f"Could not check: {_err('emptyTables')}"
                        if _err("emptyTables")
                        else "All tables contain data."
                        if empty_count == 0
                        else f"{empty_count} empty table(s): {', '.join(r['table_name'] for r in _rows('emptyTables')[:5])}"
                    ),
                    recommendation="Remove unused tables or verify data pipelines are running.",
                )
            )

            # LH-006: Wide String Columns
            wide_count = _cnt("wideStringColumns")
            rules.append(
                RuleResult(
                    id="LH-006",
                    rule="No Over-Provisioned String Columns",
                    category="Performance",
                    severity="MEDIUM",
                    status="ERROR"
                    if _err("wideStringColumns")
                    else "PASS"
                    if wide_count == 0
                    else "WARN",
                    details=(
                        f"Could not check: {_err('wideStringColumns')}"
                        if _err("wideStringColumns")
                        else "All string columns have reasonable lengths."
                        if wide_count == 0
                        else f"{wide_count} column(s) with length >500: {', '.join(str(r['TABLE_NAME']) + '.' + str(r['COLUMN_NAME']) + '(' + str(r['CHARACTER_MAXIMUM_LENGTH']) + ')' for r in _rows('wideStringColumns')[:3])}"
                    ),
                    recommendation="Reduce column lengths in source pipeline for better Delta/V-Order compression.",
                )
            )

            # LH-007: Nullable Key Columns
            null_key_count = _cnt("nullableKeyColumns")
            rules.append(
                RuleResult(
                    id="LH-007",
                    rule="Key Columns Are NOT NULL",
                    category="Data Quality",
                    severity="HIGH",
                    status="ERROR"
                    if _err("nullableKeyColumns")
                    else "PASS"
                    if null_key_count == 0
                    else "FAIL",
                    details=(
                        f"Could not check: {_err('nullableKeyColumns')}"
                        if _err("nullableKeyColumns")
                        else "All key/ID columns are NOT NULL."
                        if null_key_count == 0
                        else f"{null_key_count} key column(s) allow NULL: {', '.join(str(r['TABLE_NAME']) + '.' + str(r['COLUMN_NAME']) for r in _rows('nullableKeyColumns')[:5])}"
                    ),
                    recommendation="Add NOT NULL constraints to ID/key columns in the source pipeline.",
                )
            )

            # LH-008: Floating Point Columns
            float_count = _cnt("floatingPointColumns")
            rules.append(
                RuleResult(
                    id="LH-008",
                    rule="No Float/Real Precision Issues",
                    category="Data Quality",
                    severity="MEDIUM",
                    status="ERROR"
                    if _err("floatingPointColumns")
                    else "PASS"
                    if float_count == 0
                    else "WARN",
                    details=(
                        f"Could not check: {_err('floatingPointColumns')}"
                        if _err("floatingPointColumns")
                        else "No float/real columns found. All numeric types use fixed precision."
                        if float_count == 0
                        else f"{float_count} float/real column(s): {', '.join(str(r['TABLE_NAME']) + '.' + str(r['COLUMN_NAME']) for r in _rows('floatingPointColumns')[:5])}"
                    ),
                    recommendation="Use DECIMAL/NUMERIC for exact values (monetary, percentages).",
                )
            )

            # LH-009: Column Naming Issues
            naming_count = _cnt("columnNamingIssues")
            rules.append(
                RuleResult(
                    id="LH-009",
                    rule="Column Naming Convention",
                    category="Maintainability",
                    severity="LOW",
                    status="ERROR"
                    if _err("columnNamingIssues")
                    else "PASS"
                    if naming_count == 0
                    else "WARN",
                    details=(
                        f"Could not check: {_err('columnNamingIssues')}"
                        if _err("columnNamingIssues")
                        else "All columns follow alphanumeric + underscore naming."
                        if naming_count == 0
                        else f"{naming_count} column(s) with spaces or special characters: {', '.join(str(r['table_name']) + '.' + str(r['column_name']) for r in _rows('columnNamingIssues')[:5])}"
                    ),
                    recommendation="Use only letters, digits, and underscores (snake_case preferred).",
                )
            )

            # LH-010: Date Columns Stored as Text
            text_date_count = _cnt("textDateColumns")
            rules.append(
                RuleResult(
                    id="LH-010",
                    rule="Date Columns Use Proper Types",
                    category="Data Quality",
                    severity="MEDIUM",
                    status="ERROR"
                    if _err("textDateColumns")
                    else "PASS"
                    if text_date_count == 0
                    else "FAIL",
                    details=(
                        f"Could not check: {_err('textDateColumns')}"
                        if _err("textDateColumns")
                        else "All date-like columns use proper DATE/DATETIME2 types."
                        if text_date_count == 0
                        else f"{text_date_count} date column(s) stored as text: {', '.join(str(r['TABLE_NAME']) + '.' + str(r['COLUMN_NAME']) for r in _rows('textDateColumns')[:3])}"
                    ),
                    recommendation="Convert to DATE/DATETIME2 for time intelligence, sorting, filtering.",
                )
            )

            # LH-011: Numeric Columns Stored as Text
            text_num_count = _cnt("textNumericColumns")
            rules.append(
                RuleResult(
                    id="LH-011",
                    rule="Numeric Columns Use Proper Types",
                    category="Data Quality",
                    severity="MEDIUM",
                    status="ERROR"
                    if _err("textNumericColumns")
                    else "PASS"
                    if text_num_count == 0
                    else "FAIL",
                    details=(
                        f"Could not check: {_err('textNumericColumns')}"
                        if _err("textNumericColumns")
                        else "All numeric-like columns use proper numeric types."
                        if text_num_count == 0
                        else f"{text_num_count} numeric column(s) stored as text: {', '.join(str(r['TABLE_NAME']) + '.' + str(r['COLUMN_NAME']) for r in _rows('textNumericColumns')[:3])}"
                    ),
                    recommendation="Convert to INT/BIGINT/DECIMAL in source pipeline for proper aggregation.",
                )
            )

            # LH-012: Wide Tables (>30 columns)
            wide_tables = [
                r
                for r in _rows("nullableColumnsRatio")
                if (r.get("total_columns") or 0) > 30
            ]
            rules.append(
                RuleResult(
                    id="LH-012",
                    rule="No Excessively Wide Tables",
                    category="Maintainability",
                    severity="LOW",
                    status="ERROR"
                    if _err("nullableColumnsRatio")
                    else "PASS"
                    if not wide_tables
                    else "WARN",
                    details=(
                        f"Could not check: {_err('nullableColumnsRatio')}"
                        if _err("nullableColumnsRatio")
                        else "All tables have ≤30 columns."
                        if not wide_tables
                        else f"{len(wide_tables)} table(s) with >30 columns: {', '.join(str(r['TABLE_NAME']) + '(' + str(r['total_columns']) + ')' for r in wide_tables[:3])}"
                    ),
                    recommendation="Consider normalizing into fact + dimension tables.",
                )
            )

            # LH-013: Highly Nullable Tables
            high_nullable = [
                r
                for r in _rows("nullableColumnsRatio")
                if (r.get("total_columns") or 0) > 5
                and (r.get("nullable_count") or 0)
                / max((r.get("total_columns") or 1), 1)
                > 0.9
            ]
            rules.append(
                RuleResult(
                    id="LH-013",
                    rule="Schema Has NOT NULL Constraints",
                    category="Data Quality",
                    severity="MEDIUM",
                    status="ERROR"
                    if _err("nullableColumnsRatio")
                    else "PASS"
                    if not high_nullable
                    else "WARN",
                    details=(
                        f"Could not check: {_err('nullableColumnsRatio')}"
                        if _err("nullableColumnsRatio")
                        else "No tables with >90% nullable columns."
                        if not high_nullable
                        else f"{len(high_nullable)} table(s) are >90% nullable: {', '.join(str(r['TABLE_NAME']) + '(' + str(r['nullable_count']) + '/' + str(r['total_columns']) + ')' for r in high_nullable[:3])}"
                    ),
                    recommendation="Add NOT NULL constraints where data should always be present.",
                )
            )

            # LH-014: Missing Audit Columns
            no_audit_count = _cnt("missingAuditColumns")
            rules.append(
                RuleResult(
                    id="LH-014",
                    rule="Tables Have Audit Columns",
                    category="Maintainability",
                    severity="LOW",
                    status="ERROR"
                    if _err("missingAuditColumns")
                    else "PASS"
                    if no_audit_count == 0
                    else "WARN",
                    details=(
                        f"Could not check: {_err('missingAuditColumns')}"
                        if _err("missingAuditColumns")
                        else "All tables have created_at/updated_at audit columns."
                        if no_audit_count == 0
                        else f"{no_audit_count} table(s) lack audit columns: {', '.join(r['table_name'] for r in _rows('missingAuditColumns')[:5])}"
                    ),
                    recommendation="Add created_at/updated_at columns for data lineage tracking.",
                )
            )

            # LH-015: Mixed Date Types
            mixed_date_count = _cnt("mixedDateTypes")
            rules.append(
                RuleResult(
                    id="LH-015",
                    rule="Consistent Date Types Per Table",
                    category="Data Quality",
                    severity="LOW",
                    status="ERROR"
                    if _err("mixedDateTypes")
                    else "PASS"
                    if mixed_date_count == 0
                    else "WARN",
                    details=(
                        f"Could not check: {_err('mixedDateTypes')}"
                        if _err("mixedDateTypes")
                        else "Each table uses a single consistent date/time type."
                        if mixed_date_count == 0
                        else f"{mixed_date_count} table(s) mix date types: {', '.join(str(r['table_name']) + '(' + str(r['date_types_used']) + ')' for r in _rows('mixedDateTypes')[:3])}"
                    ),
                    recommendation="Standardize on datetime2 across all tables.",
                )
            )

            # LH-S01: Sensitive/PII Columns
            sensitive_count = _cnt("sensitiveColumns")
            rules.append(
                RuleResult(
                    id="LH-S01",
                    rule="No Unprotected Sensitive Data",
                    category="Security",
                    severity="HIGH",
                    status="ERROR"
                    if _err("sensitiveColumns")
                    else "PASS"
                    if sensitive_count == 0
                    else "WARN",
                    details=(
                        f"Could not check: {_err('sensitiveColumns')}"
                        if _err("sensitiveColumns")
                        else "No sensitive column patterns (PII) detected."
                        if sensitive_count == 0
                        else f"{sensitive_count} sensitive column(s) found: {', '.join(str(r['table_name']) + '.' + str(r['COLUMN_NAME']) for r in _rows('sensitiveColumns')[:5])}"
                    ),
                    recommendation="Review PII columns and apply data masking or move to a secure layer.",
                )
            )

            # LH-S02: Large Tables
            large_count = _cnt("largeTables")
            rules.append(
                RuleResult(
                    id="LH-S02",
                    rule="Large Tables Identified",
                    category="Performance",
                    severity="INFO",
                    status="ERROR" if _err("largeTables") else "PASS",
                    details=(
                        f"Could not check: {_err('largeTables')}"
                        if _err("largeTables")
                        else "No tables exceed 1M rows."
                        if large_count == 0
                        else f"{large_count} table(s) >1M rows: {', '.join(str(r.get('table_name', r.get('schema_name', '') + '.' + r.get('table_name', ''))) + '(' + format(r.get('row_count') or 0, ',') + ' rows)' for r in _rows('largeTables')[:5])}"
                    ),
                )
            )

            # LH-S03: Deprecated Data Types
            deprecated_count = _cnt("deprecatedTypes")
            rules.append(
                RuleResult(
                    id="LH-S03",
                    rule="No Deprecated Data Types",
                    category="Maintainability",
                    severity="HIGH",
                    status="ERROR"
                    if _err("deprecatedTypes")
                    else "PASS"
                    if deprecated_count == 0
                    else "FAIL",
                    details=(
                        f"Could not check: {_err('deprecatedTypes')}"
                        if _err("deprecatedTypes")
                        else "No TEXT/NTEXT/IMAGE columns found."
                        if deprecated_count == 0
                        else f"{deprecated_count} column(s) with deprecated types: {', '.join(str(r['table_name']) + '.' + str(r['column_name']) + '(' + str(r['data_type']) + ')' for r in _rows('deprecatedTypes')[:5])}"
                    ),
                    recommendation="Migrate TEXT/NTEXT/IMAGE to VARCHAR(MAX)/NVARCHAR(MAX)/VARBINARY(MAX).",
                )
            )

            # LH-S04: Tables Without Any Key Column
            no_key_count = _cnt("tablesWithoutKeys")
            rules.append(
                RuleResult(
                    id="LH-S04",
                    rule="All Tables Have Key Columns",
                    category="Data Quality",
                    severity="MEDIUM",
                    status="ERROR"
                    if _err("tablesWithoutKeys")
                    else "PASS"
                    if no_key_count == 0
                    else "WARN",
                    details=(
                        f"Could not check: {_err('tablesWithoutKeys')}"
                        if _err("tablesWithoutKeys")
                        else "All tables have at least one ID/Key column."
                        if no_key_count == 0
                        else f"{no_key_count} table(s) without any key column: {', '.join(r['table_name'] for r in _rows('tablesWithoutKeys')[:5])}"
                    ),
                    recommendation="Add a unique identifier column (ID/Key) for row identification and joins.",
                )
            )

            # LH-031: Tables with Nested/Complex Types
            nested_cols = [
                c
                for c in _rows("columnInfo")
                if (c.get("DATA_TYPE") or "").lower()
                in ("array", "struct", "map")
                or "row(" in (c.get("DATA_TYPE") or "").lower()
            ]
            rules.append(
                RuleResult(
                    id="LH-031",
                    rule="No Deeply Nested Types",
                    category="Performance",
                    severity="LOW",
                    status="ERROR"
                    if _err("columnInfo")
                    else "PASS"
                    if not nested_cols
                    else "WARN",
                    details=(
                        f"Could not check: {_err('columnInfo')}"
                        if _err("columnInfo")
                        else "No nested complex type columns found."
                        if not nested_cols
                        else f"{len(nested_cols)} column(s) with nested types (STRUCT/ARRAY/MAP)."
                    ),
                    recommendation="Consider flattening nested types for better query performance and Direct Lake compatibility.",
                )
            )
        else:
            # SQL endpoint not available
            sql_rule_ids = [
                "LH-005", "LH-006", "LH-007", "LH-008", "LH-009",
                "LH-010", "LH-011", "LH-012", "LH-013", "LH-014", "LH-015",
            ]
            sql_rule_names = [
                "No Empty Tables", "No Over-Provisioned String Columns",
                "Key Columns Are NOT NULL", "No Float/Real Precision Issues",
                "Column Naming Convention", "Date Columns Use Proper Types",
                "Numeric Columns Use Proper Types", "No Excessively Wide Tables",
                "Schema Has NOT NULL Constraints", "Tables Have Audit Columns",
                "Consistent Date Types Per Table",
            ]
            for i, rid in enumerate(sql_rule_ids):
                rules.append(
                    RuleResult(
                        id=rid,
                        rule=sql_rule_names[i],
                        category="Data Quality",
                        severity="MEDIUM",
                        status="N/A",
                        details="SQL Endpoint not available — cannot perform deep analysis.",
                    )
                )

        # ── Delta Log Analysis (via OneLake ADLS Gen2) ──
        if delta_tables:
            workspace = None
            try:
                workspace = get_workspace(args["workspaceId"])
            except Exception:
                pass

            if workspace:
                delta_log_results: List[Dict[str, Any]] = []
                delta_table_limit = 20
                skipped = max(0, len(delta_tables) - delta_table_limit)

                for t in delta_tables[:delta_table_limit]:
                    try:
                        log = read_delta_log(
                            workspace["displayName"],
                            lakehouse["displayName"],
                            t["name"],
                        )
                        delta_log_results.append({"table": t["name"], "log": log})
                    except Exception:
                        pass

                if skipped > 0:
                    header.append(
                        f"> ⚠️ Delta Log analysis limited to {delta_table_limit} tables. {skipped} table(s) skipped."
                    )
                    header.append("")

                if delta_log_results:
                    # LH-016: Partitioning Check
                    unpartitioned: List[str] = []
                    partitioned: List[str] = []
                    for item in delta_log_results:
                        parts = get_partition_columns(item["log"])
                        if parts:
                            partitioned.append(
                                f"{item['table']}({','.join(parts)})"
                            )
                        else:
                            stats = get_file_size_stats(item["log"])
                            if stats.get("totalSizeBytes", 0) > 10 * 1024 * 1024 * 1024:
                                unpartitioned.append(item["table"])
                    rules.append(
                        RuleResult(
                            id="LH-016",
                            rule="Large Tables Are Partitioned",
                            category="Performance",
                            severity="MEDIUM",
                            status="PASS" if not unpartitioned else "WARN",
                            details=(
                                f"Partitioned tables: {', '.join(partitioned) if partitioned else 'No tables >10GB need partitioning.'}"
                                if not unpartitioned
                                else f"{len(unpartitioned)} large table(s) >10GB without partitioning: {', '.join(unpartitioned)}"
                            ),
                            recommendation="Partition large tables by frequently filtered columns (date, region).",
                        )
                    )

                    # LH-017: VACUUM History
                    no_vacuum: List[str] = []
                    stale_vacuum: List[str] = []
                    for item in delta_log_results:
                        last_vac = get_last_operation(item["log"], "VACUUM")
                        if not last_vac:
                            no_vacuum.append(item["table"])
                        elif last_vac.get("timestamp") and days_since_timestamp(last_vac["timestamp"]) > 7:
                            stale_vacuum.append(
                                f"{item['table']}({days_since_timestamp(last_vac['timestamp'])}d ago)"
                            )
                    vacuum_issues = no_vacuum + stale_vacuum
                    rules.append(
                        RuleResult(
                            id="LH-017",
                            rule="Regular VACUUM Executed",
                            category="Maintenance",
                            severity="MEDIUM",
                            status="PASS" if not vacuum_issues else "WARN",
                            details=(
                                "All tables have recent VACUUM operations."
                                if not vacuum_issues
                                else f"{len(vacuum_issues)} table(s) need VACUUM: {', '.join(vacuum_issues[:5])}"
                            ),
                            recommendation="Run VACUUM weekly to remove stale files and reduce storage costs.",
                        )
                    )

                    # LH-018: OPTIMIZE History
                    no_optimize: List[str] = []
                    for item in delta_log_results:
                        last_opt = get_last_operation(item["log"], "OPTIMIZE")
                        if not last_opt:
                            no_optimize.append(item["table"])
                    rules.append(
                        RuleResult(
                            id="LH-018",
                            rule="Regular OPTIMIZE Executed",
                            category="Performance",
                            severity="MEDIUM",
                            status="PASS" if not no_optimize else "WARN",
                            details=(
                                "All tables have OPTIMIZE operations in history."
                                if not no_optimize
                                else f"{len(no_optimize)} table(s) never optimized: {', '.join(no_optimize[:5])}"
                            ),
                            recommendation="Run OPTIMIZE regularly for file compaction and V-Order.",
                        )
                    )

                    # LH-019: Small File Problem
                    small_file_issues: List[str] = []
                    for item in delta_log_results:
                        stats = get_file_size_stats(item["log"])
                        total_files = stats.get("totalFiles", 0)
                        small_count = stats.get("smallFileCount", 0)
                        if total_files > 10 and small_count > total_files * 0.5:
                            avg_mb = stats.get("avgFileSizeMB", 0)
                            small_file_issues.append(
                                f"{item['table']}({small_count}/{total_files} files <25MB, avg {avg_mb:.1f}MB)"
                            )
                    rules.append(
                        RuleResult(
                            id="LH-019",
                            rule="No Small File Problem",
                            category="Performance",
                            severity="HIGH",
                            status="PASS" if not small_file_issues else "FAIL",
                            details=(
                                "File sizes are in optimal range."
                                if not small_file_issues
                                else f"{len(small_file_issues)} table(s) with small file problem: {', '.join(small_file_issues[:3])}"
                            ),
                            recommendation="Run OPTIMIZE to compact small files. Enable autoOptimize for future writes.",
                        )
                    )

                    # LH-020: Auto-Optimize Enabled
                    no_auto_opt: List[str] = []
                    for item in delta_log_results:
                        config = get_table_config(item["log"])
                        auto_opt = config.get("delta.autoOptimize.optimizeWrite", "false")
                        if auto_opt != "true":
                            no_auto_opt.append(item["table"])
                    rules.append(
                        RuleResult(
                            id="LH-020",
                            rule="Auto-Optimize Enabled",
                            category="Performance",
                            severity="MEDIUM",
                            status="PASS" if not no_auto_opt else "WARN",
                            details=(
                                "All tables have autoOptimize.optimizeWrite enabled."
                                if not no_auto_opt
                                else f"{len(no_auto_opt)} table(s) without auto-optimize: {', '.join(no_auto_opt[:5])}"
                            ),
                            recommendation="ALTER TABLE SET TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true').",
                        )
                    )

                    # LH-021: Retention Policy Configured
                    no_retention: List[str] = []
                    for item in delta_log_results:
                        config = get_table_config(item["log"])
                        if (
                            not config.get("delta.logRetentionDuration")
                            and not config.get("delta.deletedFileRetentionDuration")
                        ):
                            no_retention.append(item["table"])
                    rules.append(
                        RuleResult(
                            id="LH-021",
                            rule="Retention Policy Configured",
                            category="Maintenance",
                            severity="LOW",
                            status="PASS" if not no_retention else "WARN",
                            details=(
                                "All tables have retention policies configured."
                                if not no_retention
                                else f"{len(no_retention)} table(s) without retention policy: {', '.join(no_retention[:5])}"
                            ),
                            recommendation="Set logRetentionDuration and deletedFileRetentionDuration to control storage costs.",
                        )
                    )

                    # LH-022: Excessive Delta Log Versions
                    too_many_versions: List[str] = []
                    for item in delta_log_results:
                        log = item["log"]
                        tv = log.get("totalVersions", 0) if isinstance(log, dict) else getattr(log, "total_versions", 0)
                        if tv > 100:
                            too_many_versions.append(f"{item['table']}({tv} versions)")
                    rules.append(
                        RuleResult(
                            id="LH-022",
                            rule="Delta Log Version Count Reasonable",
                            category="Performance",
                            severity="LOW",
                            status="PASS" if not too_many_versions else "WARN",
                            details=(
                                "All tables have reasonable version counts."
                                if not too_many_versions
                                else f"{len(too_many_versions)} table(s) with many versions: {', '.join(too_many_versions[:5])}"
                            ),
                            recommendation="Run VACUUM to trigger checkpoint creation and reduce log replay time.",
                        )
                    )

                    # LH-023: Write Amplification Check
                    high_write_amp: List[str] = []
                    for item in delta_log_results:
                        ops = count_operations(item["log"])
                        total_ops = sum(ops.values())
                        merge_deletes = ops.get("MERGE", 0) + ops.get("DELETE", 0) + ops.get("UPDATE", 0)
                        if total_ops > 10 and merge_deletes / total_ops > 0.5:
                            high_write_amp.append(
                                f"{item['table']}({merge_deletes}/{total_ops} ops are MERGE/UPDATE/DELETE)"
                            )
                    rules.append(
                        RuleResult(
                            id="LH-023",
                            rule="Low Write Amplification",
                            category="Performance",
                            severity="MEDIUM",
                            status="PASS" if not high_write_amp else "WARN",
                            details=(
                                "Write operations are mostly appends — low write amplification."
                                if not high_write_amp
                                else f"{len(high_write_amp)} table(s) with high MERGE/UPDATE/DELETE ratio: {', '.join(high_write_amp[:3])}"
                            ),
                            recommendation="Consider append-only patterns or Liquid Clustering to reduce write amplification.",
                        )
                    )

                    # LH-024: Data Skipping Configured
                    no_data_skipping: List[str] = []
                    for item in delta_log_results:
                        config = get_table_config(item["log"])
                        skip_cols = int(config.get("delta.dataSkippingNumIndexedCols", "0"))
                        if skip_cols == 0:
                            no_data_skipping.append(item["table"])
                    rules.append(
                        RuleResult(
                            id="LH-024",
                            rule="Data Skipping Configured",
                            category="Performance",
                            severity="LOW",
                            status="PASS" if not no_data_skipping else "WARN",
                            details=(
                                "All tables have data skipping configured."
                                if not no_data_skipping
                                else f"{len(no_data_skipping)} table(s) without explicit data skipping: {', '.join(no_data_skipping[:5])}"
                            ),
                            recommendation="SET TBLPROPERTIES ('delta.dataSkippingNumIndexedCols' = '32') for faster queries.",
                        )
                    )

                    # LH-025: Z-Order Applied to Large Tables
                    needs_zorder: List[str] = []
                    for item in delta_log_results:
                        stats = get_file_size_stats(item["log"])
                        if stats.get("totalSizeBytes", 0) > 10 * 1024 * 1024 * 1024:
                            log = item["log"]
                            commits = log.get("commits", []) if isinstance(log, dict) else getattr(log, "commits", [])
                            has_zorder = any(
                                c.get("operation") == "OPTIMIZE"
                                and "zOrderBy" in str(c.get("operationParameters", {}))
                                for c in commits
                            )
                            if not has_zorder:
                                needs_zorder.append(item["table"])
                    rules.append(
                        RuleResult(
                            id="LH-025",
                            rule="Z-Order on Large Tables",
                            category="Performance",
                            severity="MEDIUM",
                            status="PASS" if not needs_zorder else "WARN",
                            details=(
                                "All large tables have Z-Order applied or are <10GB."
                                if not needs_zorder
                                else f"{len(needs_zorder)} large table(s) >10GB without Z-Order: {', '.join(needs_zorder)}"
                            ),
                            recommendation="OPTIMIZE table ZORDER BY (frequently filtered columns) for faster queries.",
                        )
                    )

                    # LH-026: V-Order Enabled
                    no_vorder: List[str] = []
                    for item in delta_log_results:
                        config = get_table_config(item["log"])
                        if config.get("delta.parquet.vorder.enabled") != "true":
                            no_vorder.append(item["table"])
                    rules.append(
                        RuleResult(
                            id="LH-026",
                            rule="V-Order Enabled",
                            category="Performance",
                            severity="MEDIUM",
                            status="PASS" if not no_vorder else "WARN",
                            details=(
                                "All tables have V-Order enabled."
                                if not no_vorder
                                else f"{len(no_vorder)} table(s) without V-Order: {', '.join(no_vorder[:3])}"
                            ),
                            recommendation="Enable V-Order for 30-50% better compression and faster reads. Fix: v-order",
                        )
                    )

                    # LH-027: Change Data Feed on Large Tables
                    large_delta_no_cdf: List[str] = []
                    for item in delta_log_results:
                        config = get_table_config(item["log"])
                        stats = get_file_size_stats(item["log"])
                        if (
                            stats.get("totalFiles", 0) > 100
                            and config.get("delta.enableChangeDataFeed") != "true"
                        ):
                            large_delta_no_cdf.append(item["table"])
                    rules.append(
                        RuleResult(
                            id="LH-027",
                            rule="Change Data Feed on Large Tables",
                            category="Data Management",
                            severity="LOW",
                            status="PASS" if not large_delta_no_cdf else "WARN",
                            details=(
                                "All large tables have CDF enabled."
                                if not large_delta_no_cdf
                                else f"{len(large_delta_no_cdf)} large table(s) without Change Data Feed."
                            ),
                            recommendation="Enable CDF for incremental ETL. Fix: change-data-feed",
                        )
                    )

                    # LH-028: Column Mapping Enabled
                    without_col_mapping: List[str] = []
                    for item in delta_log_results:
                        config = get_table_config(item["log"])
                        mode = config.get("delta.columnMapping.mode")
                        if not mode or mode == "none":
                            without_col_mapping.append(item["table"])
                    rules.append(
                        RuleResult(
                            id="LH-028",
                            rule="Column Mapping Enabled",
                            category="Maintainability",
                            severity="LOW",
                            status="PASS" if not without_col_mapping else "WARN",
                            details=(
                                "All tables have column mapping."
                                if not without_col_mapping
                                else f"{len(without_col_mapping)} table(s) without column mapping mode=name."
                            ),
                            recommendation="Enable column mapping for schema evolution support. Fix: column-mapping",
                        )
                    )

                    # LH-029: Deletion Vectors Enabled
                    without_dv: List[str] = []
                    for item in delta_log_results:
                        config = get_table_config(item["log"])
                        if config.get("delta.enableDeletionVectors") != "true":
                            without_dv.append(item["table"])
                    rules.append(
                        RuleResult(
                            id="LH-029",
                            rule="Deletion Vectors Enabled",
                            category="Performance",
                            severity="LOW",
                            status="PASS" if not without_dv else "WARN",
                            details=(
                                "All tables have deletion vectors."
                                if not without_dv
                                else f"{len(without_dv)} table(s) without deletion vectors."
                            ),
                            recommendation="Enable deletion vectors for faster UPDATE/DELETE/MERGE. Fix: deletion-vectors",
                        )
                    )

                    # LH-030: Checkpoint Interval Check
                    bad_checkpoint: List[str] = []
                    for item in delta_log_results:
                        config = get_table_config(item["log"])
                        interval = int(config.get("delta.checkpointInterval", "10"))
                        if interval > 50:
                            bad_checkpoint.append(item["table"])
                    rules.append(
                        RuleResult(
                            id="LH-030",
                            rule="Checkpoint Interval Appropriate",
                            category="Performance",
                            severity="LOW",
                            status="PASS" if not bad_checkpoint else "WARN",
                            details=(
                                "All tables have reasonable checkpoint intervals."
                                if not bad_checkpoint
                                else f"{len(bad_checkpoint)} table(s) with high checkpoint interval (>50)."
                            ),
                            recommendation="Set checkpoint interval to 10 for faster query startup. Fix: checkpoint-interval",
                        )
                    )

        return render_rule_report(
            f"Lakehouse Analysis: {lakehouse['displayName']}",
            datetime.now(timezone.utc).isoformat(),
            header,
            rules,
        )
    except Exception as e:
        return f"Error analyzing lakehouse: {e}"


# ──────────────────────────────────────────────
# Lakehouse fix commands
# ──────────────────────────────────────────────

LAKEHOUSE_FIX_COMMANDS: Dict[str, Dict[str, Any]] = {
    "auto-optimize": {
        "description": "Enable auto-optimize (optimizeWrite + autoCompact)",
        "get_code": lambda lh, t: (
            f'spark.sql("ALTER TABLE `{lh}`.`{t}` SET TBLPROPERTIES '
            f"('delta.autoOptimize.optimizeWrite' = 'true', 'delta.autoOptimize.autoCompact' = 'true')\")\n"
            f'print("✅ Auto-optimize enabled for {t}")'
        ),
    },
    "retention": {
        "description": "Set log retention (30 days) and deleted file retention (7 days)",
        "get_code": lambda lh, t: (
            f'spark.sql("ALTER TABLE `{lh}`.`{t}` SET TBLPROPERTIES '
            f"('delta.logRetentionDuration' = 'interval 30 days', "
            f"'delta.deletedFileRetentionDuration' = 'interval 7 days')\")\n"
            f'print("✅ Retention policy set for {t}")'
        ),
    },
    "data-skipping": {
        "description": "Enable data skipping with 32 indexed columns",
        "get_code": lambda lh, t: (
            f'spark.sql("ALTER TABLE `{lh}`.`{t}` SET TBLPROPERTIES '
            f"('delta.dataSkippingNumIndexedCols' = '32')\")\n"
            f'print("✅ Data skipping enabled for {t}")'
        ),
    },
    "audit-columns": {
        "description": "Add created_at and updated_at audit columns (idempotent)",
        "get_code": lambda lh, t: (
            f'existing = [c.name.lower() for c in spark.table("`{lh}`.`{t}`").schema]\n'
            f"added = []\n"
            f'if "created_at" not in existing:\n'
            f'    spark.sql("ALTER TABLE `{lh}`.`{t}` ADD COLUMNS (created_at TIMESTAMP)")\n'
            f'    added.append("created_at")\n'
            f'if "updated_at" not in existing:\n'
            f'    spark.sql("ALTER TABLE `{lh}`.`{t}` ADD COLUMNS (updated_at TIMESTAMP)")\n'
            f'    added.append("updated_at")\n'
            f"if added:\n"
            f'    print(f"✅ Added {{\', \'.join(added)}} to {t}")\n'
            f"else:\n"
            f'    print("✅ Audit columns already exist on {t} - skipped")'
        ),
    },
    "v-order": {
        "description": "Enable V-Order compression for better read performance",
        "get_code": lambda lh, t: (
            f'spark.sql("ALTER TABLE `{lh}`.`{t}` SET TBLPROPERTIES '
            f"('delta.parquet.vorder.enabled' = 'true')\")\n"
            f'print("✅ V-Order enabled for {t}")'
        ),
    },
    "change-data-feed": {
        "description": "Enable Change Data Feed for incremental processing",
        "get_code": lambda lh, t: (
            f'spark.sql("ALTER TABLE `{lh}`.`{t}` SET TBLPROPERTIES '
            f"('delta.enableChangeDataFeed' = 'true')\")\n"
            f'print("✅ Change Data Feed enabled for {t}")'
        ),
    },
    "column-mapping": {
        "description": "Enable column mapping mode=name for schema evolution",
        "get_code": lambda lh, t: (
            f'spark.sql("ALTER TABLE `{lh}`.`{t}` SET TBLPROPERTIES '
            f"('delta.columnMapping.mode' = 'name', 'delta.minReaderVersion' = '2', "
            f"'delta.minWriterVersion' = '5')\")\n"
            f'print("✅ Column mapping enabled for {t}")'
        ),
    },
    "checkpoint-interval": {
        "description": "Set optimal checkpoint interval (10 commits)",
        "get_code": lambda lh, t: (
            f'spark.sql("ALTER TABLE `{lh}`.`{t}` SET TBLPROPERTIES '
            f"('delta.checkpointInterval' = '10')\")\n"
            f'print("✅ Checkpoint interval set for {t}")'
        ),
    },
    "deletion-vectors": {
        "description": "Enable deletion vectors for faster deletes/updates",
        "get_code": lambda lh, t: (
            f'spark.sql("ALTER TABLE `{lh}`.`{t}` SET TBLPROPERTIES '
            f"('delta.enableDeletionVectors' = 'true')\")\n"
            f'print("✅ Deletion vectors enabled for {t}")'
        ),
    },
    "compute-stats": {
        "description": "Compute table statistics for query optimization",
        "get_code": lambda lh, t: (
            f'spark.sql("ANALYZE TABLE `{lh}`.`{t}` COMPUTE STATISTICS")\n'
            f'print("✅ Statistics computed for {t}")'
        ),
    },
}


# ──────────────────────────────────────────────
# Tool: lakehouse_auto_optimize
# ──────────────────────────────────────────────


def lakehouse_auto_optimize(args: dict) -> str:
    try:
        lakehouse = get_lakehouse(args["workspaceId"], args["lakehouseId"])
        lh_name = lakehouse["displayName"]
        _validate_spark_name(lh_name, "lakehouse name")
        is_dry_run = args.get("dryRun", False)
        fix_ids = args.get("fixIds") or ["auto-optimize", "retention", "data-skipping"]

        all_tables = list_lakehouse_tables(args["workspaceId"], args["lakehouseId"])
        delta_tbls = [
            t for t in all_tables if (t.get("format") or "").lower() == "delta"
        ]

        if not delta_tbls:
            return f"# 🔧 Lakehouse Auto-Optimize: {lh_name}\n\nNo Delta tables found. Nothing to optimize."

        commands: List[Dict[str, str]] = []
        for t in delta_tbls:
            _validate_spark_name(t["name"], "table name")
            for fix_id in fix_ids:
                fix = LAKEHOUSE_FIX_COMMANDS.get(fix_id)
                if not fix:
                    continue
                commands.append(
                    {
                        "table": t["name"],
                        "fixId": fix_id,
                        "description": fix["description"],
                        "code": fix["get_code"](lh_name, t["name"]),
                    }
                )

        if is_dry_run:
            lines = [
                f"# 🔧 Lakehouse Auto-Optimize: {lh_name}",
                "",
                f"_DRY RUN at {datetime.now(timezone.utc).isoformat()}_",
                "",
                f"**{len(delta_tbls)} tables × {len(fix_ids)} fixes = {len(commands)} commands previewed**",
                "",
                "| Table | Fix | Description |",
                "|-------|-----|-------------|",
            ]
            for cmd in commands:
                lines.append(
                    f"| {cmd['table']} | {cmd['fixId']} | {cmd['description']} |"
                )
            lines.extend(["", "> 💡 Set `dryRun: false` to execute via Livy API."])
            return "\n".join(lines)

        # Execute via single Livy session
        lines = [
            f"# 🔧 Lakehouse Auto-Optimize: {lh_name}",
            "",
            f"_Executed at {datetime.now(timezone.utc).isoformat()} via Livy API_",
            "",
        ]

        try:
            result = run_spark_fixes_via_livy(
                args["workspaceId"], args["lakehouseId"], commands
            )
            results = result.get("results", [])

            passed = sum(1 for r in results if r.get("status") == "ok")
            failed = len(results) - passed

            lines.extend(
                [
                    f"**{passed} succeeded, {failed} failed** across {len(delta_tbls)} tables",
                    "",
                    "| Table | Fix | Status | Detail |",
                    "|-------|-----|--------|--------|",
                ]
            )

            for r in results:
                icon = "✅" if r.get("status") == "ok" else "❌"
                detail = (
                    r.get("output", "OK")
                    if r.get("status") == "ok"
                    else r.get("error", "Failed")
                )
                lines.append(
                    f"| {r.get('table', '')} | {r.get('fixId', '')} | {icon} | {detail} |"
                )
        except Exception as e:
            lines.append(f"**❌ Livy session failed**: {e}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error in auto-optimize: {e}"


# ──────────────────────────────────────────────
# Tool: lakehouse_fix
# ──────────────────────────────────────────────


def lakehouse_fix(args: dict) -> str:
    try:
        table_name = args["tableName"]
        _validate_spark_name(table_name, "tableName")

        lakehouse = get_lakehouse(args["workspaceId"], args["lakehouseId"])
        fix_ids = args.get("fixIds") or list(LAKEHOUSE_FIX_COMMANDS.keys())
        lh_name = lakehouse["displayName"]
        is_dry_run = args.get("dryRun", False)

        _validate_spark_name(lh_name, "lakehouse name")

        fix_descriptions: List[Dict[str, str]] = []
        for fix_id in fix_ids:
            fix = LAKEHOUSE_FIX_COMMANDS.get(fix_id)
            if fix:
                code = fix["get_code"](lh_name, table_name)
                fix_descriptions.append(
                    {"id": fix_id, "description": fix["description"], "code": code}
                )

        if not fix_descriptions:
            return "❌ No valid fix IDs provided. Available: auto-optimize, retention, data-skipping, audit-columns"

        # Dry-run
        if is_dry_run:
            lines = [
                f"# 🔧 Lakehouse Fix: {lh_name}.{table_name}",
                "",
                f"_DRY RUN (preview only) at {datetime.now(timezone.utc).isoformat()}_",
                "",
                f"**{len(fix_descriptions)} command(s) previewed** — re-run without dryRun to apply.",
                "",
                "| Fix | Description | Spark SQL |",
                "|-----|-------------|-----------|",
            ]
            for f in fix_descriptions:
                lines.append(
                    f"| {f['id']} | 🔍 {f['description']} | `{f['code'][:80]}...` |"
                )
            lines.extend(
                ["", "> 💡 Set `dryRun: false` to execute these commands via Livy API."]
            )
            return "\n".join(lines)

        # Build commands for Livy
        commands = [
            {
                "table": table_name,
                "fixId": f["id"],
                "description": f["description"],
                "code": f["code"],
            }
            for f in fix_descriptions
        ]

        used_method = "Livy API"
        fix_results: List[Dict[str, str]] = []

        try:
            result = run_spark_fixes_via_livy(
                args["workspaceId"], args["lakehouseId"], commands
            )
            for r in result.get("results", []):
                fix_results.append(
                    {
                        "id": r.get("fixId", ""),
                        "description": r.get("description", ""),
                        "status": "✅" if r.get("status") == "ok" else "❌",
                        "detail": r.get("output", "OK")
                        if r.get("status") == "ok"
                        else r.get("error", "Failed"),
                    }
                )
        except Exception:
            # Livy failed — fall back to notebook
            used_method = "Notebook (Livy fallback)"
            code = "\n\n".join(f["code"] for f in fix_descriptions)
            nb_result = run_temporary_notebook(args["workspaceId"], code)
            for f in fix_descriptions:
                fix_results.append(
                    {
                        "id": f["id"],
                        "description": f["description"],
                        "status": "✅"
                        if nb_result.get("status") == "Completed"
                        else "❌",
                        "detail": "OK"
                        if nb_result.get("status") == "Completed"
                        else nb_result.get("error", "Failed"),
                    }
                )

        all_ok = all(r["status"] == "✅" for r in fix_results)

        lines = [
            f"# 🔧 Lakehouse Fix: {lh_name}.{table_name}",
            "",
            f"_Executed at {datetime.now(timezone.utc).isoformat()} via {used_method}_",
            "",
            f"**Status**: {'✅ Success' if all_ok else '⚠️ Partial/Failed'}",
            "",
            "| Fix | Status | Detail |",
            "|-----|--------|--------|",
        ]

        for f in fix_results:
            lines.append(
                f"| {f['id']} {f['description']} | {f['status']} | {f['detail']} |"
            )

        return "\n".join(lines)
    except Exception as e:
        return f"Error applying lakehouse fix: {e}"


# ──────────────────────────────────────────────
# Tool definitions for MCP registration
# ──────────────────────────────────────────────

lakehouse_tools = [
    {
        "name": "lakehouse_list",
        "description": (
            "List all lakehouses in a Fabric workspace with their metadata, "
            "SQL endpoint status, and OneLake paths."
        ),
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
        "handler": lakehouse_list,
    },
    {
        "name": "lakehouse_list_tables",
        "description": (
            "List all tables in a Fabric Lakehouse with their type, "
            "format (Delta/Parquet), and location."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "lakehouseId": {
                    "type": "string",
                    "description": "The ID of the lakehouse",
                },
            },
            "required": ["workspaceId", "lakehouseId"],
        },
        "handler": lakehouse_list_tables,
    },
    {
        "name": "lakehouse_run_table_maintenance",
        "description": (
            "Run table maintenance (OPTIMIZE with V-Order, Z-ORDER, VACUUM) on a Fabric Lakehouse. "
            "Can target a specific table or all tables. Compacts small files, applies V-Order compression, "
            "and removes unreferenced old files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "lakehouseId": {
                    "type": "string",
                    "description": "The ID of the lakehouse",
                },
                "tableName": {
                    "type": "string",
                    "description": "Optional: name of a specific table to optimize. If omitted, all tables are processed.",
                },
                "optimizeSettings": {
                    "type": "object",
                    "description": "OPTIMIZE settings",
                    "properties": {
                        "vOrder": {
                            "type": "boolean",
                            "description": "Enable V-Order optimization (default: true)",
                        },
                        "zOrderColumns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Columns to Z-ORDER by for faster filtered reads",
                        },
                    },
                },
                "vacuumSettings": {
                    "type": "object",
                    "description": "VACUUM settings",
                    "properties": {
                        "retentionPeriod": {
                            "type": "string",
                            "description": "Retention period in format 'D.HH:MM:SS' (default: '7.00:00:00' = 7 days)",
                        },
                    },
                },
            },
            "required": ["workspaceId", "lakehouseId"],
        },
        "handler": lakehouse_run_table_maintenance,
    },
    {
        "name": "lakehouse_get_job_status",
        "description": "Check the status of a table maintenance job on a Fabric Lakehouse.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "lakehouseId": {
                    "type": "string",
                    "description": "The ID of the lakehouse",
                },
                "jobInstanceId": {
                    "type": "string",
                    "description": "The ID of the job instance to check",
                },
            },
            "required": ["workspaceId", "lakehouseId", "jobInstanceId"],
        },
        "handler": lakehouse_get_job_status,
    },
    {
        "name": "lakehouse_optimization_recommendations",
        "description": (
            "LIVE SCAN: Analyzes a Fabric Lakehouse by checking table formats (Delta vs non-Delta), "
            "connecting to the SQL Analytics Endpoint to inspect row counts, column data types, "
            "empty tables, and large tables. Returns findings with prioritized action items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "lakehouseId": {
                    "type": "string",
                    "description": "The ID of the lakehouse to analyze",
                },
            },
            "required": ["workspaceId", "lakehouseId"],
        },
        "handler": lakehouse_optimization_recommendations,
    },
    {
        "name": "lakehouse_fix",
        "description": (
            "AUTO-FIX: Applies Spark SQL fixes to a Lakehouse via Livy API (no notebooks needed). "
            "Falls back to temporary Notebook if Livy is unavailable. "
            "Can fix: auto-optimize, retention policy, data skipping, audit columns, "
            "v-order, change-data-feed, column-mapping, checkpoint-interval, deletion-vectors, compute-stats. "
            "Use dryRun=true to preview commands without executing them. "
            "Available fixIds: auto-optimize, retention, data-skipping, audit-columns, v-order, "
            "change-data-feed, column-mapping, checkpoint-interval, deletion-vectors, compute-stats."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "lakehouseId": {
                    "type": "string",
                    "description": "The ID of the lakehouse",
                },
                "tableName": {
                    "type": "string",
                    "description": "The table to fix",
                },
                "fixIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Fix IDs to apply: auto-optimize, retention, data-skipping, audit-columns, "
                        "v-order, change-data-feed, column-mapping, checkpoint-interval, "
                        "deletion-vectors, compute-stats. If omitted, all are applied."
                    ),
                },
                "dryRun": {
                    "type": "boolean",
                    "description": "If true, preview commands without executing them (default: false)",
                },
            },
            "required": ["workspaceId", "lakehouseId", "tableName"],
        },
        "handler": lakehouse_fix,
    },
    {
        "name": "lakehouse_auto_optimize",
        "description": (
            "AUTO-OPTIMIZE: Discovers ALL Delta tables in a Lakehouse and applies fixes to every table "
            "in a single Livy Spark session (no notebooks needed). "
            "Default fixes: auto-optimize, retention, data-skipping. "
            "Use dryRun=true to preview. Use fixIds to select specific fixes. "
            "Additional fixes: v-order, change-data-feed, column-mapping, checkpoint-interval, "
            "deletion-vectors, compute-stats."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "lakehouseId": {
                    "type": "string",
                    "description": "The ID of the lakehouse",
                },
                "fixIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Fix IDs: auto-optimize, retention, data-skipping, audit-columns, "
                        "v-order, change-data-feed, column-mapping, checkpoint-interval, "
                        "deletion-vectors, compute-stats. Default: first three."
                    ),
                },
                "dryRun": {
                    "type": "boolean",
                    "description": "If true, preview commands without executing (default: false)",
                },
            },
            "required": ["workspaceId", "lakehouseId"],
        },
        "handler": lakehouse_auto_optimize,
    },
]
