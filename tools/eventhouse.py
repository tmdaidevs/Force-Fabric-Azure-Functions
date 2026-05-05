"""Tool handlers for Fabric Eventhouse operations — diagnostics, optimization, and fixes."""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from clients.fabric_client import list_eventhouses, get_eventhouse, list_kql_databases
from clients.kql_client import run_kql_diagnostics, execute_kql_mgmt, execute_kql_query
from tools.rule_engine import render_rule_report, RuleResult

KqlRow = Dict[str, Any]

# ──────────────────────────────────────────────
# Input validation
# ──────────────────────────────────────────────

SAFE_KQL_NAME = re.compile(r"^[a-zA-Z0-9_\- .]+$")


def _validate_kql_name(value: str, label: str) -> None:
    if not SAFE_KQL_NAME.match(value):
        raise ValueError(f"Invalid {label}: must be alphanumeric/underscore/dash/dot only.")


# ──────────────────────────────────────────────
# Policy parsing helpers
# ──────────────────────────────────────────────

def _policy_string(row: KqlRow, *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v is not None and v != "":
            return v if isinstance(v, str) else json.dumps(v)
    return ""


def _is_truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower() == "true"


def _is_falsy(val: Any) -> bool:
    if isinstance(val, bool):
        return not val
    return str(val).lower() == "false"


# ──────────────────────────────────────────────
# KQL Diagnostic Commands — ALL
# ──────────────────────────────────────────────

KQL_DIAGNOSTICS: Dict[str, Dict[str, Any]] = {
    "tableDetails": {"query": ".show tables details", "isMgmt": True},
    "cachingPolicy": {"query": ".show database policy caching", "isMgmt": True},
    "retentionPolicy": {"query": ".show database policy retention", "isMgmt": True},
    "extentStats": {
        "query": (
            ".show database extents"
            " | summarize ExtentCount=count(), TotalRows=sum(RowCount),"
            " TotalOriginalSizeMB=sum(OriginalSize)/1024/1024,"
            " TotalCompressedSizeMB=sum(CompressedSize)/1024/1024 by TableName"
            " | order by TotalOriginalSizeMB desc"
        ),
        "isMgmt": True,
    },
    "materializedViews": {"query": ".show materialized-views", "isMgmt": True},
    "tableCachingPolicies": {"query": ".show table * policy caching", "isMgmt": True},
    "tableRetentionPolicies": {"query": ".show table * policy retention", "isMgmt": True},
    "ingestionBatching": {"query": ".show table * policy ingestionbatching", "isMgmt": True},
    "streamingIngestion": {"query": ".show table * policy streamingingestion", "isMgmt": True},
    "queryPerformance": {
        "query": (
            '.show commands-and-queries'
            ' | where StartedOn > ago(7d)'
            ' | where State != "InProgress"'
            ' | summarize QueryCount=count(), AvgDurationSec=avg(Duration)/1s,'
            ' MaxDurationSec=max(Duration)/1s, P95DurationSec=percentile(Duration, 95)/1s,'
            ' FailedCount=countif(State == "Failed") by Database, CommandType'
            ' | order by AvgDurationSec desc'
        ),
        "isMgmt": True,
    },
    "slowQueries": {
        "query": (
            '.show commands-and-queries'
            ' | where StartedOn > ago(7d)'
            ' | where State == "Completed"'
            ' | top 15 by Duration desc'
            ' | project StartedOn, Duration, CommandType,'
            ' QueryText=substring(Text, 0, 200), MemoryPeak, TotalCpu,'
            ' User=ClientRequestProperties["x-ms-user-id"]'
        ),
        "isMgmt": True,
    },
    "failedCommands": {
        "query": (
            '.show commands-and-queries'
            ' | where StartedOn > ago(7d)'
            ' | where State == "Failed"'
            ' | top 10 by StartedOn desc'
            ' | project StartedOn, CommandType, FailureReason,'
            ' QueryText=substring(Text, 0, 200)'
        ),
        "isMgmt": True,
    },
    "ingestionFailures": {
        "query": (
            '.show ingestion failures'
            ' | where FailedOn > ago(7d)'
            ' | summarize FailureCount=count(), LastFailure=max(FailedOn)'
            ' by Table, ErrorCode'
            ' | order by FailureCount desc'
        ),
        "isMgmt": True,
    },
    "dataFreshness": {
        "query": (
            '.show tables details'
            ' | project TableName, TotalRowCount, TotalOriginalSize,'
            ' MinExtentsCreationTime, MaxExtentsCreationTime, HotRowCount'
            ' | order by TotalRowCount desc'
        ),
        "isMgmt": True,
    },
    "updatePolicies": {"query": ".show table * policy update", "isMgmt": True},
    "partitioningPolicies": {"query": ".show table * policy partitioning", "isMgmt": True},
    "tableSchemas": {"query": ".show database schema as json | project DatabaseSchema", "isMgmt": True},
    "columnStats": {
        "query": (
            '.show database extents'
            ' | summarize ExtentCount=count(), AvgRowsPerExtent=avg(RowCount),'
            ' MinRows=min(RowCount), MaxRows=max(RowCount),'
            ' TotalCompressedMB=sum(CompressedSize)/1024/1024,'
            ' TotalOriginalMB=sum(OriginalSize)/1024/1024 by TableName'
            ' | extend CompressionRatio=iff(TotalOriginalMB > 0,'
            ' round((1.0 - TotalCompressedMB/TotalOriginalMB) * 100, 1), 0.0)'
            ' | order by TotalOriginalMB desc'
        ),
        "isMgmt": True,
    },
    "mergePolicy": {"query": ".show table * policy merge", "isMgmt": True},
    "encodingPolicy": {"query": ".show table * policy encoding", "isMgmt": True},
    "rowOrderPolicy": {"query": ".show table * policy row_order", "isMgmt": True},
    "continuousExports": {"query": ".show continuous-exports", "isMgmt": True},
    "functions": {"query": ".show functions", "isMgmt": True},
    "journalEntries": {
        "query": (
            '.show journal'
            ' | where EventTimestamp > ago(7d)'
            ' | summarize Count=count() by Event'
            ' | order by Count desc'
        ),
        "isMgmt": True,
    },
    "storageByTable": {
        "query": (
            '.show database extents'
            ' | summarize TotalSizeGB=round(sum(OriginalSize)/1024/1024/1024, 2),'
            ' CompressedSizeGB=round(sum(CompressedSize)/1024/1024/1024, 2) by TableName'
            ' | order by TotalSizeGB desc'
            ' | limit 20'
        ),
        "isMgmt": True,
    },
    "autocompactionPolicy": {"query": ".show database policy autocompaction", "isMgmt": True},
    "extentTagsRetention": {"query": ".show database policy extent_tags_retention", "isMgmt": True},
    "shardingPolicy": {"query": ".show table * policy sharding", "isMgmt": True},
    "materializedViewDetails": {
        "query": (
            '.show materialized-views'
            ' | project Name, SourceTable, Query, IsHealthy, IsEnabled,'
            ' MaterializedTo, LastRun, LastRunResult'
        ),
        "isMgmt": True,
    },
    "reservedColumns": {"query": ".show database schema as json | project DatabaseSchema", "isMgmt": True},
}


# ──────────────────────────────────────────────
# Analysis helpers
# ──────────────────────────────────────────────

def _format_bytes(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.1f} MB"


def _analyze_extent_stats(rows: List[KqlRow]) -> List[str]:
    if not rows:
        return ["No extent data found."]
    lines: List[str] = [
        "| Table | Extents | Rows | Original Size | Compressed Size | Compression |",
        "|-------|---------|------|--------------|----------------|-------------|",
    ]
    fragmented: List[str] = []
    total_orig_mb = 0.0
    total_comp_mb = 0.0
    for r in rows:
        extents = r.get("ExtentCount", 0) or 0
        total_rows = r.get("TotalRows", 0) or 0
        orig_mb = r.get("TotalOriginalSizeMB", 0) or 0
        comp_mb = r.get("TotalCompressedSizeMB", 0) or 0
        ratio = ((1 - comp_mb / orig_mb) * 100) if orig_mb > 0 else 0
        total_orig_mb += orig_mb
        total_comp_mb += comp_mb
        lines.append(
            f"| {r.get('TableName', '')} | {extents} | {total_rows:,} "
            f"| {_format_bytes(orig_mb)} | {_format_bytes(comp_mb)} | {ratio:.0f}% |"
        )
        if extents > 100 and total_rows > 0:
            avg_rows_per_extent = total_rows / extents
            if avg_rows_per_extent < 100000:
                fragmented.append(
                    f"{r.get('TableName', '')} ({extents} extents, avg {round(avg_rows_per_extent)} rows/extent)"
                )
    lines.append("")
    lines.append(f"**Total storage**: {_format_bytes(total_orig_mb)} original → {_format_bytes(total_comp_mb)} compressed")
    if fragmented:
        lines.append("")
        lines.append(f"**🔴 Fragmented tables ({len(fragmented)})** — Too many small extents, run merge:")
        for f in fragmented:
            lines.append(f"- {f}")
        lines.append("→ Run `.merge table <name>` to consolidate extents.")
    return lines


def _analyze_caching_policy(db_policy: List[KqlRow], table_policies: List[KqlRow]) -> List[str]:
    lines: List[str] = ["### Caching Policy"]
    if db_policy:
        for r in db_policy:
            policy_str = _policy_string(r, "Policy", "CachingPolicy")
            lines.append(f"- Database caching policy: `{policy_str}`")
    else:
        lines.append("- No database-level caching policy found.")
    if table_policies:
        lines.append("")
        lines.append("| Table | Caching Policy |")
        lines.append("|-------|---------------|")
        for r in table_policies:
            name = r.get("EntityName", r.get("TableName", ""))
            policy_str = _policy_string(r, "Policy", "CachingPolicy")
            lines.append(f"| {name} | `{policy_str}` |")
    return lines


def _analyze_materialized_views(rows: List[KqlRow]) -> List[str]:
    if not rows:
        return ["No materialized views found."]
    lines: List[str] = [
        "### Materialized Views",
        "",
        "| Name | Source | Healthy | Enabled | Last Run | Result |",
        "|------|--------|---------|---------|----------|--------|",
    ]
    for r in rows:
        name = r.get("Name", r.get("MaterializedViewName", ""))
        source = r.get("SourceTable", "")
        healthy = r.get("IsHealthy", "")
        enabled = r.get("IsEnabled", "")
        last_run = r.get("LastRun", "")
        result = r.get("LastRunResult", "")
        lines.append(f"| {name} | {source} | {healthy} | {enabled} | {last_run} | {result} |")
    return lines


# ──────────────────────────────────────────────
# Tool: eventhouse_list
# ──────────────────────────────────────────────

def eventhouse_list(args: dict) -> str:
    try:
        workspace_id = args["workspaceId"]
        eventhouses = list_eventhouses(workspace_id)
        if not eventhouses:
            return "No eventhouses found in this workspace."

        lines: List[str] = []
        for eh in eventhouses:
            props = eh.get("properties") or {}
            parts = [f"- **{eh.get('displayName', '')}** (ID: {eh.get('id', '')})"]
            query_uri = props.get("queryServiceUri")
            if query_uri:
                parts.append(f"  Query URI: {query_uri}")
            ingestion_uri = props.get("ingestionServiceUri")
            if ingestion_uri:
                parts.append(f"  Ingestion URI: {ingestion_uri}")
            db_ids = props.get("databasesItemIds")
            if db_ids and len(db_ids) > 0:
                parts.append(f"  KQL Databases: {len(db_ids)}")
            lines.append("\n".join(parts))

        return f"## Eventhouses in workspace {workspace_id}\n\n" + "\n\n".join(lines)
    except Exception as e:
        return f"❌ Failed to list eventhouses: {e}"


# ──────────────────────────────────────────────
# Tool: eventhouse_list_kql_databases
# ──────────────────────────────────────────────

def eventhouse_list_kql_databases(args: dict) -> str:
    try:
        workspace_id = args["workspaceId"]
        databases = list_kql_databases(workspace_id)
        if not databases:
            return "No KQL databases found in this workspace."

        lines = [
            f"- **{db.get('displayName', '')}** (ID: {db.get('id', '')}, Type: {db.get('type', '')})"
            for db in databases
        ]
        return f"## KQL Databases in workspace {workspace_id}\n\n" + "\n".join(lines)
    except Exception as e:
        return f"❌ Failed to list KQL databases: {e}"


# ──────────────────────────────────────────────
# Tool: eventhouse_optimization_recommendations
#   ALL 27 rules: EH-001 through EH-027
# ──────────────────────────────────────────────

def _run_eventhouse_rules(
    diag: Dict[str, Dict[str, Any]],
    cluster_uri: str,
    database: str,
) -> List[RuleResult]:
    rules: List[RuleResult] = []

    def _rows(key: str) -> List[KqlRow]:
        entry = diag.get(key, {})
        return entry.get("rows", [])

    def _error(key: str) -> Optional[str]:
        entry = diag.get(key, {})
        return entry.get("error")

    # ── EH-001: Database has tables ──
    table_details = _rows("tableDetails")
    if table_details:
        rules.append(RuleResult(
            id="EH-001", rule="Database has tables", category="Configuration",
            severity="HIGH", status="PASS",
            details=f"Database has {len(table_details)} table(s).",
        ))
    else:
        rules.append(RuleResult(
            id="EH-001", rule="Database has tables", category="Configuration",
            severity="HIGH", status="FAIL",
            details="Database has no tables.",
            recommendation="Create tables and start ingesting data.",
        ))

    # ── EH-002: Extent fragmentation ──
    extent_rows = _rows("extentStats")
    fragmented_tables: List[str] = []
    for r in extent_rows:
        extents = r.get("ExtentCount", 0) or 0
        total_rows = r.get("TotalRows", 0) or 0
        if extents > 100 and total_rows > 0:
            avg = total_rows / extents
            if avg < 100000:
                fragmented_tables.append(r.get("TableName", ""))
    if fragmented_tables:
        rules.append(RuleResult(
            id="EH-002", rule="No extent fragmentation", category="Performance",
            severity="HIGH", status="FAIL",
            details=f"{len(fragmented_tables)} table(s) are fragmented: {', '.join(fragmented_tables[:5])}.",
            recommendation="Run `.merge async <table>` to consolidate extents, or use eventhouse_fix rule EH-002.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-002", rule="No extent fragmentation", category="Performance",
            severity="HIGH", status="PASS",
            details="No fragmented tables detected.",
        ))

    # ── EH-003: Compression ratio ──
    column_stats = _rows("columnStats")
    low_compression: List[str] = []
    for r in column_stats:
        ratio = r.get("CompressionRatio", 0) or 0
        if ratio < 50:
            low_compression.append(f"{r.get('TableName', '')} ({ratio}%)")
    if low_compression:
        rules.append(RuleResult(
            id="EH-003", rule="Good compression ratio", category="Storage",
            severity="MEDIUM", status="WARN",
            details=f"{len(low_compression)} table(s) have low compression (<50%): {', '.join(low_compression[:5])}.",
            recommendation="Review column data types and encoding policies to improve compression.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-003", rule="Good compression ratio", category="Storage",
            severity="MEDIUM", status="PASS",
            details="All tables have good compression ratios (≥50%).",
        ))

    # ── EH-004: Caching policy configured ──
    db_caching = _rows("cachingPolicy")
    table_caching = _rows("tableCachingPolicies")
    has_caching = bool(db_caching) or bool(table_caching)
    tables_no_cache: List[str] = []
    if table_details and table_caching:
        cached_tables = {r.get("EntityName", r.get("TableName", "")) for r in table_caching if _policy_string(r, "Policy", "CachingPolicy")}
        for t in table_details:
            name = t.get("TableName", "")
            if name and name not in cached_tables and not db_caching:
                tables_no_cache.append(name)
    if tables_no_cache:
        rules.append(RuleResult(
            id="EH-004", rule="Caching policy configured", category="Performance",
            severity="MEDIUM", status="WARN",
            details=f"{len(tables_no_cache)} table(s) without caching policy: {', '.join(tables_no_cache[:5])}.",
            recommendation="Set caching policy to control hot cache duration. Use eventhouse_fix rule EH-004.",
        ))
    elif has_caching:
        rules.append(RuleResult(
            id="EH-004", rule="Caching policy configured", category="Performance",
            severity="MEDIUM", status="PASS",
            details="Caching policies are configured.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-004", rule="Caching policy configured", category="Performance",
            severity="MEDIUM", status="WARN",
            details="No caching policy found at database or table level.",
            recommendation="Set a caching policy to optimize query performance. Use eventhouse_fix rule EH-004.",
        ))

    # ── EH-005: Retention policy configured ──
    db_retention = _rows("retentionPolicy")
    table_retention = _rows("tableRetentionPolicies")
    has_retention = bool(db_retention) or bool(table_retention)
    if has_retention:
        rules.append(RuleResult(
            id="EH-005", rule="Retention policy configured", category="Storage",
            severity="MEDIUM", status="PASS",
            details="Retention policies are configured.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-005", rule="Retention policy configured", category="Storage",
            severity="MEDIUM", status="WARN",
            details="No retention policy found at database or table level.",
            recommendation="Set a retention policy to manage data lifecycle. Use eventhouse_fix rule EH-005.",
        ))

    # ── EH-006: Materialized views healthy ──
    mv_rows = _rows("materializedViews")
    unhealthy_mvs: List[str] = []
    disabled_mvs: List[str] = []
    for r in mv_rows:
        name = r.get("Name", r.get("MaterializedViewName", ""))
        if _is_falsy(r.get("IsHealthy", True)):
            unhealthy_mvs.append(name)
        if _is_falsy(r.get("IsEnabled", True)):
            disabled_mvs.append(name)
    if unhealthy_mvs or disabled_mvs:
        parts: List[str] = []
        if unhealthy_mvs:
            parts.append(f"{len(unhealthy_mvs)} unhealthy: {', '.join(unhealthy_mvs[:5])}")
        if disabled_mvs:
            parts.append(f"{len(disabled_mvs)} disabled: {', '.join(disabled_mvs[:5])}")
        rules.append(RuleResult(
            id="EH-006", rule="Materialized views healthy", category="Performance",
            severity="HIGH", status="FAIL",
            details=f"Materialized view issues: {'; '.join(parts)}.",
            recommendation="Re-enable disabled views with eventhouse_fix rule EH-006, or investigate unhealthy views.",
        ))
    elif mv_rows:
        rules.append(RuleResult(
            id="EH-006", rule="Materialized views healthy", category="Performance",
            severity="HIGH", status="PASS",
            details=f"All {len(mv_rows)} materialized view(s) are healthy and enabled.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-006", rule="Materialized views healthy", category="Performance",
            severity="HIGH", status="N/A",
            details="No materialized views found.",
        ))

    # ── EH-007: Query performance ──
    perf_rows = _rows("queryPerformance")
    slow_types: List[str] = []
    for r in perf_rows:
        avg_sec = r.get("AvgDurationSec", 0) or 0
        if avg_sec > 30:
            slow_types.append(f"{r.get('CommandType', '')} (avg {avg_sec:.1f}s)")
    if slow_types:
        rules.append(RuleResult(
            id="EH-007", rule="Query performance acceptable", category="Performance",
            severity="MEDIUM", status="WARN",
            details=f"Slow query types (avg >30s): {', '.join(slow_types[:5])}.",
            recommendation="Review slow queries and optimize KQL. Consider materialized views for repeated patterns.",
        ))
    elif perf_rows:
        rules.append(RuleResult(
            id="EH-007", rule="Query performance acceptable", category="Performance",
            severity="MEDIUM", status="PASS",
            details="All query types have acceptable average duration (<30s).",
        ))
    else:
        rules.append(RuleResult(
            id="EH-007", rule="Query performance acceptable", category="Performance",
            severity="MEDIUM", status="N/A",
            details="No query performance data available.",
        ))

    # ── EH-008: No failed commands ──
    failed_rows = _rows("failedCommands")
    if failed_rows:
        rules.append(RuleResult(
            id="EH-008", rule="No failed commands", category="Reliability",
            severity="MEDIUM", status="WARN",
            details=f"{len(failed_rows)} failed command(s) in the last 7 days.",
            recommendation="Investigate failed commands and fix underlying issues.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-008", rule="No failed commands", category="Reliability",
            severity="MEDIUM", status="PASS",
            details="No failed commands in the last 7 days.",
        ))

    # ── EH-009: No ingestion failures ──
    ing_fail_rows = _rows("ingestionFailures")
    if ing_fail_rows:
        total_failures = sum(r.get("FailureCount", 0) or 0 for r in ing_fail_rows)
        rules.append(RuleResult(
            id="EH-009", rule="No ingestion failures", category="Reliability",
            severity="HIGH", status="FAIL",
            details=f"{total_failures} ingestion failure(s) across {len(ing_fail_rows)} table(s) in the last 7 days.",
            recommendation="Check ingestion pipeline configuration and data format compatibility.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-009", rule="No ingestion failures", category="Reliability",
            severity="HIGH", status="PASS",
            details="No ingestion failures in the last 7 days.",
        ))

    # ── EH-010: Data freshness ──
    freshness_rows = _rows("dataFreshness")
    stale_tables: List[str] = []
    for r in freshness_rows:
        max_time = r.get("MaxExtentsCreationTime")
        if max_time and isinstance(max_time, str):
            try:
                ts = datetime.fromisoformat(max_time.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - ts).days
                if age_days > 7:
                    stale_tables.append(f"{r.get('TableName', '')} ({age_days}d)")
            except (ValueError, TypeError):
                pass
    if stale_tables:
        rules.append(RuleResult(
            id="EH-010", rule="Data freshness", category="Data Quality",
            severity="LOW", status="WARN",
            details=f"{len(stale_tables)} table(s) have stale data (>7 days): {', '.join(stale_tables[:5])}.",
            recommendation="Verify ingestion pipelines are running for these tables.",
        ))
    elif freshness_rows:
        rules.append(RuleResult(
            id="EH-010", rule="Data freshness", category="Data Quality",
            severity="LOW", status="PASS",
            details="All tables have recent data (within 7 days).",
        ))
    else:
        rules.append(RuleResult(
            id="EH-010", rule="Data freshness", category="Data Quality",
            severity="LOW", status="N/A",
            details="No data freshness information available.",
        ))

    # ── EH-011: Update policies ──
    update_rows = _rows("updatePolicies")
    tables_with_update: List[str] = []
    for r in update_rows:
        policy_str = _policy_string(r, "Policy", "UpdatePolicy")
        if policy_str and policy_str != "null" and policy_str != "[]":
            tables_with_update.append(r.get("EntityName", r.get("TableName", "")))
    if tables_with_update:
        rules.append(RuleResult(
            id="EH-011", rule="Update policies reviewed", category="Configuration",
            severity="INFO", status="PASS",
            details=f"{len(tables_with_update)} table(s) have update policies: {', '.join(tables_with_update[:5])}.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-011", rule="Update policies reviewed", category="Configuration",
            severity="INFO", status="N/A",
            details="No update policies configured.",
        ))

    # ── EH-012: Empty tables ──
    empty_tables: List[str] = []
    for r in table_details:
        row_count = r.get("TotalRowCount", 0) or 0
        if row_count == 0:
            empty_tables.append(r.get("TableName", ""))
    if empty_tables:
        rules.append(RuleResult(
            id="EH-012", rule="No empty tables", category="Governance",
            severity="LOW", status="WARN",
            details=f"{len(empty_tables)} empty table(s): {', '.join(empty_tables[:10])}.",
            recommendation="Remove unused empty tables or start ingesting data.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-012", rule="No empty tables", category="Governance",
            severity="LOW", status="PASS",
            details="All tables contain data.",
        ))

    # ── EH-013: Stored functions ──
    func_rows = _rows("functions")
    if func_rows:
        rules.append(RuleResult(
            id="EH-013", rule="Stored functions present", category="Configuration",
            severity="INFO", status="PASS",
            details=f"{len(func_rows)} stored function(s) found.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-013", rule="Stored functions present", category="Configuration",
            severity="INFO", status="N/A",
            details="No stored functions found. Consider using functions for reusable query logic.",
        ))

    # ── EH-014: Ingestion batching ──
    batching_rows = _rows("ingestionBatching")
    tables_no_batching: List[str] = []
    for r in batching_rows:
        policy_str = _policy_string(r, "Policy", "IngestionBatchingPolicy")
        if not policy_str or policy_str == "null":
            tables_no_batching.append(r.get("EntityName", r.get("TableName", "")))
    if tables_no_batching:
        rules.append(RuleResult(
            id="EH-014", rule="Ingestion batching configured", category="Performance",
            severity="MEDIUM", status="WARN",
            details=f"{len(tables_no_batching)} table(s) without batching policy: {', '.join(tables_no_batching[:5])}.",
            recommendation="Configure ingestion batching to optimize ingestion. Use eventhouse_fix rule EH-014.",
        ))
    elif batching_rows:
        rules.append(RuleResult(
            id="EH-014", rule="Ingestion batching configured", category="Performance",
            severity="MEDIUM", status="PASS",
            details="All tables have ingestion batching policies.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-014", rule="Ingestion batching configured", category="Performance",
            severity="MEDIUM", status="N/A",
            details="No ingestion batching data available.",
        ))

    # ── EH-015: Continuous exports ──
    export_rows = _rows("continuousExports")
    unhealthy_exports: List[str] = []
    for r in export_rows:
        if _is_falsy(r.get("IsRunning", True)):
            unhealthy_exports.append(r.get("Name", ""))
    if unhealthy_exports:
        rules.append(RuleResult(
            id="EH-015", rule="Continuous exports healthy", category="Reliability",
            severity="MEDIUM", status="WARN",
            details=f"{len(unhealthy_exports)} continuous export(s) not running: {', '.join(unhealthy_exports[:5])}.",
            recommendation="Investigate and restart stopped continuous exports.",
        ))
    elif export_rows:
        rules.append(RuleResult(
            id="EH-015", rule="Continuous exports healthy", category="Reliability",
            severity="MEDIUM", status="PASS",
            details=f"All {len(export_rows)} continuous export(s) are running.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-015", rule="Continuous exports healthy", category="Reliability",
            severity="MEDIUM", status="N/A",
            details="No continuous exports configured.",
        ))

    # ── EH-016: Partitioning policies ──
    partition_rows = _rows("partitioningPolicies")
    tables_with_partition: List[str] = []
    for r in partition_rows:
        policy_str = _policy_string(r, "Policy", "PartitioningPolicy")
        if policy_str and policy_str != "null":
            tables_with_partition.append(r.get("EntityName", r.get("TableName", "")))
    large_tables_no_partition: List[str] = []
    partitioned_set = set(tables_with_partition)
    for r in extent_rows:
        orig_mb = r.get("TotalOriginalSizeMB", 0) or 0
        table_name = r.get("TableName", "")
        if orig_mb > 1024 and table_name not in partitioned_set:
            large_tables_no_partition.append(f"{table_name} ({_format_bytes(orig_mb)})")
    if large_tables_no_partition:
        rules.append(RuleResult(
            id="EH-016", rule="Large tables partitioned", category="Performance",
            severity="MEDIUM", status="WARN",
            details=f"{len(large_tables_no_partition)} large table(s) without partitioning: {', '.join(large_tables_no_partition[:5])}.",
            recommendation="Add partitioning policy for large tables (>1 GB). Use eventhouse_fix rule EH-016.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-016", rule="Large tables partitioned", category="Performance",
            severity="MEDIUM", status="PASS",
            details="All large tables have partitioning policies or no tables exceed 1 GB.",
        ))

    # ── EH-017: Merge policy ──
    merge_rows = _rows("mergePolicy")
    tables_no_merge: List[str] = []
    for r in merge_rows:
        policy_str = _policy_string(r, "Policy", "MergePolicy")
        if not policy_str or policy_str == "null":
            tables_no_merge.append(r.get("EntityName", r.get("TableName", "")))
    if tables_no_merge:
        rules.append(RuleResult(
            id="EH-017", rule="Merge policy configured", category="Performance",
            severity="LOW", status="WARN",
            details=f"{len(tables_no_merge)} table(s) without merge policy: {', '.join(tables_no_merge[:5])}.",
            recommendation="Configure merge policy to optimize extent management. Use eventhouse_fix rule EH-017.",
        ))
    elif merge_rows:
        rules.append(RuleResult(
            id="EH-017", rule="Merge policy configured", category="Performance",
            severity="LOW", status="PASS",
            details="All tables have merge policies configured.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-017", rule="Merge policy configured", category="Performance",
            severity="LOW", status="N/A",
            details="No merge policy data available.",
        ))

    # ── EH-018: Encoding policy ──
    encoding_rows = _rows("encodingPolicy")
    tables_with_encoding: List[str] = []
    for r in encoding_rows:
        policy_str = _policy_string(r, "Policy", "EncodingPolicy")
        if policy_str and policy_str != "null":
            tables_with_encoding.append(r.get("EntityName", r.get("TableName", "")))
    if tables_with_encoding:
        rules.append(RuleResult(
            id="EH-018", rule="Encoding policy reviewed", category="Storage",
            severity="INFO", status="PASS",
            details=f"{len(tables_with_encoding)} table(s) have custom encoding policies.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-018", rule="Encoding policy reviewed", category="Storage",
            severity="INFO", status="N/A",
            details="No custom encoding policies. Default encoding is used.",
        ))

    # ── EH-019: Row order policy ──
    row_order_rows = _rows("rowOrderPolicy")
    tables_with_row_order: List[str] = []
    for r in row_order_rows:
        policy_str = _policy_string(r, "Policy", "RowOrderPolicy")
        if policy_str and policy_str != "null":
            tables_with_row_order.append(r.get("EntityName", r.get("TableName", "")))
    if tables_with_row_order:
        rules.append(RuleResult(
            id="EH-019", rule="Row order policy reviewed", category="Performance",
            severity="INFO", status="PASS",
            details=f"{len(tables_with_row_order)} table(s) have row order policies.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-019", rule="Row order policy reviewed", category="Performance",
            severity="INFO", status="N/A",
            details="No row order policies configured. Consider adding for query performance.",
        ))

    # ── EH-020: Sharding policy ──
    sharding_rows = _rows("shardingPolicy")
    tables_with_sharding: List[str] = []
    for r in sharding_rows:
        policy_str = _policy_string(r, "Policy", "ShardingPolicy")
        if policy_str and policy_str != "null":
            tables_with_sharding.append(r.get("EntityName", r.get("TableName", "")))
    if tables_with_sharding:
        rules.append(RuleResult(
            id="EH-020", rule="Sharding policy reviewed", category="Performance",
            severity="INFO", status="PASS",
            details=f"{len(tables_with_sharding)} table(s) have custom sharding policies.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-020", rule="Sharding policy reviewed", category="Performance",
            severity="INFO", status="N/A",
            details="No custom sharding policies. Default sharding is used.",
        ))

    # ── EH-021: Autocompaction policy ──
    autocompaction_rows = _rows("autocompactionPolicy")
    has_autocompaction = False
    for r in autocompaction_rows:
        policy_str = _policy_string(r, "Policy", "AutoCompactionPolicy")
        if policy_str and policy_str != "null":
            has_autocompaction = True
            break
    if has_autocompaction:
        rules.append(RuleResult(
            id="EH-021", rule="Autocompaction configured", category="Performance",
            severity="LOW", status="PASS",
            details="Autocompaction policy is configured.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-021", rule="Autocompaction configured", category="Performance",
            severity="LOW", status="WARN",
            details="No autocompaction policy configured.",
            recommendation="Enable autocompaction to reduce storage fragmentation. Use eventhouse_fix rule EH-021.",
        ))

    # ── EH-022: Extent tags retention ──
    tags_rows = _rows("extentTagsRetention")
    has_tags_policy = False
    for r in tags_rows:
        policy_str = _policy_string(r, "Policy", "ExtentTagsRetentionPolicy")
        if policy_str and policy_str != "null" and policy_str != "[]":
            has_tags_policy = True
            break
    if has_tags_policy:
        rules.append(RuleResult(
            id="EH-022", rule="Extent tags retention configured", category="Storage",
            severity="LOW", status="PASS",
            details="Extent tags retention policy is configured.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-022", rule="Extent tags retention configured", category="Storage",
            severity="LOW", status="WARN",
            details="No extent tags retention policy configured.",
            recommendation="Configure extent tags retention to manage tag lifecycle. Use eventhouse_fix rule EH-022.",
        ))

    # ── EH-023: Journal activity ──
    journal_rows = _rows("journalEntries")
    if journal_rows:
        total_events = sum(r.get("Count", 0) or 0 for r in journal_rows)
        rules.append(RuleResult(
            id="EH-023", rule="Journal activity healthy", category="Reliability",
            severity="INFO", status="PASS",
            details=f"{total_events:,} journal event(s) in the last 7 days across {len(journal_rows)} event type(s).",
        ))
    else:
        rules.append(RuleResult(
            id="EH-023", rule="Journal activity healthy", category="Reliability",
            severity="INFO", status="N/A",
            details="No journal entries in the last 7 days.",
        ))

    # ── EH-024: Streaming ingestion ──
    streaming_rows = _rows("streamingIngestion")
    tables_streaming: List[str] = []
    for r in streaming_rows:
        policy_str = _policy_string(r, "Policy", "StreamingIngestionPolicy")
        if policy_str and policy_str != "null":
            try:
                parsed = json.loads(policy_str) if isinstance(policy_str, str) else policy_str
                if isinstance(parsed, dict) and _is_truthy(parsed.get("IsEnabled", False)):
                    tables_streaming.append(r.get("EntityName", r.get("TableName", "")))
            except (json.JSONDecodeError, TypeError):
                pass
    if tables_streaming:
        rules.append(RuleResult(
            id="EH-024", rule="Streaming ingestion reviewed", category="Performance",
            severity="LOW", status="PASS",
            details=f"{len(tables_streaming)} table(s) have streaming ingestion enabled: {', '.join(tables_streaming[:5])}.",
            recommendation="Ensure streaming ingestion is needed — it uses more resources. Use eventhouse_fix rule EH-024 to disable if not needed.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-024", rule="Streaming ingestion reviewed", category="Performance",
            severity="LOW", status="PASS",
            details="No tables have streaming ingestion enabled (batch ingestion is used).",
        ))

    # ── EH-025: Stale materialized views ──
    mv_detail_rows = _rows("materializedViewDetails")
    stale_mvs: List[str] = []
    for r in mv_detail_rows:
        last_run = r.get("LastRun")
        if last_run and isinstance(last_run, str):
            try:
                ts = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - ts).days
                if age_days > 1:
                    stale_mvs.append(f"{r.get('Name', '')} (last run {age_days}d ago)")
            except (ValueError, TypeError):
                pass
    if stale_mvs:
        rules.append(RuleResult(
            id="EH-025", rule="Materialized views fresh", category="Performance",
            severity="MEDIUM", status="WARN",
            details=f"{len(stale_mvs)} materialized view(s) are stale (>1 day): {', '.join(stale_mvs[:5])}.",
            recommendation="Refresh stale materialized views. Use eventhouse_fix rule EH-025.",
        ))
    elif mv_detail_rows:
        rules.append(RuleResult(
            id="EH-025", rule="Materialized views fresh", category="Performance",
            severity="MEDIUM", status="PASS",
            details="All materialized views are fresh (last run within 1 day).",
        ))
    else:
        rules.append(RuleResult(
            id="EH-025", rule="Materialized views fresh", category="Performance",
            severity="MEDIUM", status="N/A",
            details="No materialized views found.",
        ))

    # ── EH-026: Slow queries ──
    slow_rows = _rows("slowQueries")
    if slow_rows:
        rules.append(RuleResult(
            id="EH-026", rule="No excessively slow queries", category="Performance",
            severity="MEDIUM", status="WARN",
            details=f"{len(slow_rows)} slow query/ies found in the last 7 days.",
            recommendation="Review slow queries and optimize KQL patterns. Consider materialized views.",
        ))
    else:
        rules.append(RuleResult(
            id="EH-026", rule="No excessively slow queries", category="Performance",
            severity="MEDIUM", status="PASS",
            details="No excessively slow queries found.",
        ))

    # ── EH-027: Storage distribution ──
    storage_rows = _rows("storageByTable")
    if storage_rows and len(storage_rows) >= 2:
        total_gb = sum(r.get("TotalSizeGB", 0) or 0 for r in storage_rows)
        top_table = storage_rows[0] if storage_rows else {}
        top_gb = top_table.get("TotalSizeGB", 0) or 0
        if total_gb > 0 and (top_gb / total_gb) > 0.8:
            rules.append(RuleResult(
                id="EH-027", rule="Balanced storage distribution", category="Storage",
                severity="LOW", status="WARN",
                details=f"Table \"{top_table.get('TableName', '')}\" holds {top_gb:.2f} GB ({(top_gb / total_gb * 100):.0f}% of total {total_gb:.2f} GB).",
                recommendation="Consider partitioning or archiving data from the dominant table.",
            ))
        else:
            rules.append(RuleResult(
                id="EH-027", rule="Balanced storage distribution", category="Storage",
                severity="LOW", status="PASS",
                details=f"Storage is balanced across {len(storage_rows)} table(s) (total {total_gb:.2f} GB).",
            ))
    else:
        rules.append(RuleResult(
            id="EH-027", rule="Balanced storage distribution", category="Storage",
            severity="LOW", status="N/A",
            details="Not enough tables to assess storage distribution.",
        ))

    return rules


def eventhouse_optimization_recommendations(args: dict) -> str:
    try:
        workspace_id = args["workspaceId"]
        eventhouse_id = args["eventhouseId"]

        eh = get_eventhouse(workspace_id, eventhouse_id)
        props = eh.get("properties") or {}
        cluster_uri = props.get("queryServiceUri", "")
        if not cluster_uri:
            return "❌ Eventhouse has no queryServiceUri. Cannot run diagnostics."

        databases = list_kql_databases(workspace_id)
        eh_db_ids = set(props.get("databasesItemIds", []))
        target_dbs = [db for db in databases if db.get("id") in eh_db_ids] if eh_db_ids else databases

        if not target_dbs:
            return "❌ No KQL databases found for this eventhouse."

        now = datetime.now(timezone.utc).isoformat()
        all_rules: List[RuleResult] = []
        header_sections: List[str] = [
            f"**Eventhouse:** {eh.get('displayName', eventhouse_id)}",
            f"**Query URI:** {cluster_uri}",
            f"**KQL Databases:** {len(target_dbs)}",
        ]

        for db in target_dbs:
            db_name = db.get("displayName", "")
            header_sections.append(f"- {db_name} (ID: {db.get('id', '')})")

            diag = run_kql_diagnostics(cluster_uri, db_name, KQL_DIAGNOSTICS)
            db_rules = _run_eventhouse_rules(diag, cluster_uri, db_name)
            for rule in db_rules:
                if len(target_dbs) > 1:
                    rule.details = f"[{db_name}] {rule.details}"
                all_rules.append(rule)

        return render_rule_report(
            "Eventhouse Optimization Report",
            now,
            header_sections,
            all_rules,
        )
    except Exception as e:
        return f"❌ Failed to run eventhouse optimization: {e}"


# ──────────────────────────────────────────────
# Fix Definitions — ALL fixable rules
# ──────────────────────────────────────────────

def _fix_eh002(
    cluster_uri: str, database: str, dry_run: bool,
    diag: Dict[str, Dict[str, Any]],
    table_name: Optional[str] = None, **_kwargs: Any,
) -> List[str]:
    """Merge fragmented extents."""
    results: List[str] = []
    extent_rows = diag.get("extentStats", {}).get("rows", [])
    for r in extent_rows:
        extents = r.get("ExtentCount", 0) or 0
        total_rows = r.get("TotalRows", 0) or 0
        tbl = r.get("TableName", "")
        if table_name and tbl != table_name:
            continue
        if extents > 100 and total_rows > 0:
            avg = total_rows / extents
            if avg < 100000:
                _validate_kql_name(tbl, "table name")
                cmd = f".merge async [{tbl}]"
                if dry_run:
                    results.append(f"🔍 Would run: `{cmd}`")
                else:
                    try:
                        execute_kql_mgmt(cluster_uri, database, cmd)
                        results.append(f"✅ Merge started for table `{tbl}`")
                    except Exception as e:
                        results.append(f"❌ Merge failed for `{tbl}`: {e}")
    if not results:
        results.append("No fragmented tables found to merge.")
    return results


def _fix_eh004(
    cluster_uri: str, database: str, dry_run: bool,
    diag: Dict[str, Dict[str, Any]],
    caching_days: int = 30, **_kwargs: Any,
) -> List[str]:
    """Set caching policy."""
    results: List[str] = []
    cmd = f".alter database ['{database}'] policy caching hot = {caching_days}d"
    if dry_run:
        results.append(f"🔍 Would run: `{cmd}`")
    else:
        try:
            execute_kql_mgmt(cluster_uri, database, cmd)
            results.append(f"✅ Set database caching policy to {caching_days} days.")
        except Exception as e:
            results.append(f"❌ Failed to set caching policy: {e}")
    return results


def _fix_eh005(
    cluster_uri: str, database: str, dry_run: bool,
    diag: Dict[str, Dict[str, Any]],
    retention_days: int = 365, **_kwargs: Any,
) -> List[str]:
    """Set retention policy."""
    results: List[str] = []
    cmd = f".alter database ['{database}'] policy retention softdelete = {retention_days}d recoverability = enabled"
    if dry_run:
        results.append(f"🔍 Would run: `{cmd}`")
    else:
        try:
            execute_kql_mgmt(cluster_uri, database, cmd)
            results.append(f"✅ Set database retention policy to {retention_days} days.")
        except Exception as e:
            results.append(f"❌ Failed to set retention policy: {e}")
    return results


def _fix_eh006(
    cluster_uri: str, database: str, dry_run: bool,
    diag: Dict[str, Dict[str, Any]], **_kwargs: Any,
) -> List[str]:
    """Re-enable disabled materialized views."""
    results: List[str] = []
    mv_rows = diag.get("materializedViews", {}).get("rows", [])
    for r in mv_rows:
        name = r.get("Name", r.get("MaterializedViewName", ""))
        if _is_falsy(r.get("IsEnabled", True)):
            _validate_kql_name(name, "materialized view name")
            cmd = f".enable materialized-view [{name}]"
            if dry_run:
                results.append(f"🔍 Would run: `{cmd}`")
            else:
                try:
                    execute_kql_mgmt(cluster_uri, database, cmd)
                    results.append(f"✅ Re-enabled materialized view `{name}`")
                except Exception as e:
                    results.append(f"❌ Failed to re-enable `{name}`: {e}")
    if not results:
        results.append("No disabled materialized views found.")
    return results


def _fix_eh014(
    cluster_uri: str, database: str, dry_run: bool,
    diag: Dict[str, Dict[str, Any]],
    table_name: Optional[str] = None, **_kwargs: Any,
) -> List[str]:
    """Configure ingestion batching policy."""
    results: List[str] = []
    batching_rows = diag.get("ingestionBatching", {}).get("rows", [])
    for r in batching_rows:
        policy_str = _policy_string(r, "Policy", "IngestionBatchingPolicy")
        tbl = r.get("EntityName", r.get("TableName", ""))
        if table_name and tbl != table_name:
            continue
        if not policy_str or policy_str == "null":
            _validate_kql_name(tbl, "table name")
            policy = json.dumps({
                "MaximumBatchingTimeSpan": "00:05:00",
                "MaximumNumberOfItems": 500,
                "MaximumRawDataSizeMB": 1024,
            })
            cmd = f".alter table [{tbl}] policy ingestionbatching @'{policy}'"
            if dry_run:
                results.append(f"🔍 Would run: `{cmd}`")
            else:
                try:
                    execute_kql_mgmt(cluster_uri, database, cmd)
                    results.append(f"✅ Set ingestion batching policy for `{tbl}`")
                except Exception as e:
                    results.append(f"❌ Failed to set batching policy for `{tbl}`: {e}")
    if not results:
        results.append("All tables already have ingestion batching policies.")
    return results


def _fix_eh016(
    cluster_uri: str, database: str, dry_run: bool,
    diag: Dict[str, Dict[str, Any]],
    table_name: Optional[str] = None, **_kwargs: Any,
) -> List[str]:
    """Add partitioning policy for large tables."""
    results: List[str] = []
    extent_rows = diag.get("extentStats", {}).get("rows", [])
    partition_rows = diag.get("partitioningPolicies", {}).get("rows", [])
    partitioned = set()
    for r in partition_rows:
        policy_str = _policy_string(r, "Policy", "PartitioningPolicy")
        if policy_str and policy_str != "null":
            partitioned.add(r.get("EntityName", r.get("TableName", "")))

    for r in extent_rows:
        orig_mb = r.get("TotalOriginalSizeMB", 0) or 0
        tbl = r.get("TableName", "")
        if table_name and tbl != table_name:
            continue
        if orig_mb > 1024 and tbl not in partitioned:
            _validate_kql_name(tbl, "table name")
            # Use ingestion_time as a safe default partition key
            policy = json.dumps({
                "PartitionKeys": [
                    {
                        "ColumnName": "ingestion_time()",
                        "Kind": "UniformRange",
                        "Properties": {
                            "Reference": "2020-01-01T00:00:00",
                            "RangeSize": "1.00:00:00",
                            "OverrideCreationTime": False,
                        },
                    }
                ],
            })
            cmd = f".alter table [{tbl}] policy partitioning @'{policy}'"
            if dry_run:
                results.append(f"🔍 Would run: `{cmd}` for table `{tbl}` ({_format_bytes(orig_mb)})")
            else:
                try:
                    execute_kql_mgmt(cluster_uri, database, cmd)
                    results.append(f"✅ Set partitioning policy for `{tbl}` ({_format_bytes(orig_mb)})")
                except Exception as e:
                    results.append(f"❌ Failed to set partitioning for `{tbl}`: {e}")
    if not results:
        results.append("No large un-partitioned tables found.")
    return results


def _fix_eh017(
    cluster_uri: str, database: str, dry_run: bool,
    diag: Dict[str, Dict[str, Any]],
    table_name: Optional[str] = None, **_kwargs: Any,
) -> List[str]:
    """Configure merge policy."""
    results: List[str] = []
    merge_rows = diag.get("mergePolicy", {}).get("rows", [])
    for r in merge_rows:
        policy_str = _policy_string(r, "Policy", "MergePolicy")
        tbl = r.get("EntityName", r.get("TableName", ""))
        if table_name and tbl != table_name:
            continue
        if not policy_str or policy_str == "null":
            _validate_kql_name(tbl, "table name")
            policy = json.dumps({
                "RowCountUpperBoundForMerge": 16777216,
                "OriginalSizeMBUpperBoundForMerge": 2048,
                "MaxRangeInHours": 24,
                "MaxExtentsToMerge": 100,
                "MinExtentsToMerge": 2,
            })
            cmd = f".alter table [{tbl}] policy merge @'{policy}'"
            if dry_run:
                results.append(f"🔍 Would run: `{cmd}`")
            else:
                try:
                    execute_kql_mgmt(cluster_uri, database, cmd)
                    results.append(f"✅ Set merge policy for `{tbl}`")
                except Exception as e:
                    results.append(f"❌ Failed to set merge policy for `{tbl}`: {e}")
    if not results:
        results.append("All tables already have merge policies configured.")
    return results


def _fix_eh021(
    cluster_uri: str, database: str, dry_run: bool,
    diag: Dict[str, Dict[str, Any]], **_kwargs: Any,
) -> List[str]:
    """Enable autocompaction policy."""
    results: List[str] = []
    policy = json.dumps({"Enabled": True})
    cmd = f".alter database ['{database}'] policy autocompaction @'{policy}'"
    if dry_run:
        results.append(f"🔍 Would run: `{cmd}`")
    else:
        try:
            execute_kql_mgmt(cluster_uri, database, cmd)
            results.append("✅ Enabled autocompaction policy.")
        except Exception as e:
            results.append(f"❌ Failed to enable autocompaction: {e}")
    return results


def _fix_eh022(
    cluster_uri: str, database: str, dry_run: bool,
    diag: Dict[str, Dict[str, Any]], **_kwargs: Any,
) -> List[str]:
    """Configure extent tags retention."""
    results: List[str] = []
    policy = json.dumps([{"TagPrefix": "drop-by:", "RetentionPeriod": "7.00:00:00"}])
    cmd = f".alter database ['{database}'] policy extent_tags_retention @'{policy}'"
    if dry_run:
        results.append(f"🔍 Would run: `{cmd}`")
    else:
        try:
            execute_kql_mgmt(cluster_uri, database, cmd)
            results.append("✅ Set extent tags retention policy (7-day drop-by retention).")
        except Exception as e:
            results.append(f"❌ Failed to set extent tags retention: {e}")
    return results


def _fix_eh024(
    cluster_uri: str, database: str, dry_run: bool,
    diag: Dict[str, Dict[str, Any]],
    table_name: Optional[str] = None, **_kwargs: Any,
) -> List[str]:
    """Disable streaming ingestion where enabled."""
    results: List[str] = []
    streaming_rows = diag.get("streamingIngestion", {}).get("rows", [])
    for r in streaming_rows:
        policy_str = _policy_string(r, "Policy", "StreamingIngestionPolicy")
        tbl = r.get("EntityName", r.get("TableName", ""))
        if table_name and tbl != table_name:
            continue
        if policy_str and policy_str != "null":
            try:
                parsed = json.loads(policy_str) if isinstance(policy_str, str) else policy_str
                if isinstance(parsed, dict) and _is_truthy(parsed.get("IsEnabled", False)):
                    _validate_kql_name(tbl, "table name")
                    policy = json.dumps({"IsEnabled": False})
                    cmd = f".alter table [{tbl}] policy streamingingestion @'{policy}'"
                    if dry_run:
                        results.append(f"🔍 Would run: `{cmd}`")
                    else:
                        try:
                            execute_kql_mgmt(cluster_uri, database, cmd)
                            results.append(f"✅ Disabled streaming ingestion for `{tbl}`")
                        except Exception as e:
                            results.append(f"❌ Failed to disable streaming for `{tbl}`: {e}")
            except (json.JSONDecodeError, TypeError):
                pass
    if not results:
        results.append("No tables have streaming ingestion enabled.")
    return results


def _fix_eh025(
    cluster_uri: str, database: str, dry_run: bool,
    diag: Dict[str, Dict[str, Any]], **_kwargs: Any,
) -> List[str]:
    """Refresh stale materialized views."""
    results: List[str] = []
    mv_rows = diag.get("materializedViewDetails", {}).get("rows", [])
    for r in mv_rows:
        name = r.get("Name", "")
        last_run = r.get("LastRun")
        if last_run and isinstance(last_run, str):
            try:
                ts = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - ts).days
                if age_days > 1:
                    _validate_kql_name(name, "materialized view name")
                    cmd = f".refresh materialized-view [{name}]"
                    if dry_run:
                        results.append(f"🔍 Would run: `{cmd}` (last run {age_days}d ago)")
                    else:
                        try:
                            execute_kql_mgmt(cluster_uri, database, cmd)
                            results.append(f"✅ Refreshed materialized view `{name}`")
                        except Exception as e:
                            results.append(f"❌ Failed to refresh `{name}`: {e}")
            except (ValueError, TypeError):
                pass
    if not results:
        results.append("No stale materialized views found.")
    return results


EVENTHOUSE_FIXES: Dict[str, Dict[str, Any]] = {
    "EH-002": {"description": "Merge fragmented extents", "apply": _fix_eh002},
    "EH-004": {"description": "Set caching policy", "apply": _fix_eh004},
    "EH-005": {"description": "Set retention policy", "apply": _fix_eh005},
    "EH-006": {"description": "Re-enable disabled materialized views", "apply": _fix_eh006},
    "EH-014": {"description": "Configure ingestion batching policy", "apply": _fix_eh014},
    "EH-016": {"description": "Add partitioning policy for large tables", "apply": _fix_eh016},
    "EH-017": {"description": "Configure merge policy", "apply": _fix_eh017},
    "EH-021": {"description": "Enable autocompaction policy", "apply": _fix_eh021},
    "EH-022": {"description": "Configure extent tags retention", "apply": _fix_eh022},
    "EH-024": {"description": "Disable streaming ingestion", "apply": _fix_eh024},
    "EH-025": {"description": "Refresh stale materialized views", "apply": _fix_eh025},
}

FIXABLE_RULE_IDS = list(EVENTHOUSE_FIXES.keys())


# ──────────────────────────────────────────────
# Tool: eventhouse_fix
# ──────────────────────────────────────────────

def eventhouse_fix(args: dict) -> str:
    try:
        workspace_id = args["workspaceId"]
        eventhouse_id = args["eventhouseId"]
        is_dry_run = args.get("dryRun", False)
        requested_rules = args.get("ruleIds") or FIXABLE_RULE_IDS
        kql_database_name = args.get("kqlDatabaseName")
        table_name = args.get("tableName")
        caching_days = args.get("cachingDays", 30)
        retention_days = args.get("retentionDays", 365)

        # Validate rule IDs
        invalid_rules = [r for r in requested_rules if r not in EVENTHOUSE_FIXES]
        if invalid_rules:
            return (
                f"❌ Unknown rule IDs: {', '.join(invalid_rules)}. "
                f"Fixable rules: {', '.join(FIXABLE_RULE_IDS)}"
            )

        eh = get_eventhouse(workspace_id, eventhouse_id)
        props = eh.get("properties") or {}
        cluster_uri = props.get("queryServiceUri", "")
        if not cluster_uri:
            return "❌ Eventhouse has no queryServiceUri. Cannot run fixes."

        databases = list_kql_databases(workspace_id)
        eh_db_ids = set(props.get("databasesItemIds", []))
        target_dbs = [db for db in databases if db.get("id") in eh_db_ids] if eh_db_ids else databases

        if kql_database_name:
            target_dbs = [db for db in target_dbs if db.get("displayName") == kql_database_name]
            if not target_dbs:
                return f"❌ KQL database '{kql_database_name}' not found."

        if not target_dbs:
            return "❌ No KQL databases found for this eventhouse."

        now = datetime.now(timezone.utc).isoformat()
        lines: List[str] = [
            f"# 🔧 Eventhouse Fix: {'DRY RUN' if is_dry_run else 'Executing'}",
            "",
            f"_{now}_",
            "",
            f"**Eventhouse:** {eh.get('displayName', eventhouse_id)}",
            f"**Rules to fix:** {', '.join(requested_rules)}",
            "",
        ]

        for db in target_dbs:
            db_name = db.get("displayName", "")
            lines.append(f"## Database: {db_name}")
            lines.append("")

            # Run diagnostics for this database
            diag = run_kql_diagnostics(cluster_uri, db_name, KQL_DIAGNOSTICS)

            for rule_id in requested_rules:
                fix = EVENTHOUSE_FIXES[rule_id]
                lines.append(f"### {rule_id}: {fix['description']}")
                lines.append("")
                fix_results = fix["apply"](
                    cluster_uri, db_name, is_dry_run, diag,
                    table_name=table_name,
                    caching_days=caching_days,
                    retention_days=retention_days,
                )
                lines.extend([f"- {r}" for r in fix_results])
                lines.append("")

        return "\n".join(lines)
    except Exception as e:
        return f"❌ Failed to run eventhouse fix: {e}"


# ──────────────────────────────────────────────
# Tool: eventhouse_auto_optimize
# ──────────────────────────────────────────────

def eventhouse_auto_optimize(args: dict) -> str:
    """Delegates to eventhouse_fix with all fixable rules."""
    fix_args = {
        "workspaceId": args["workspaceId"],
        "eventhouseId": args["eventhouseId"],
        "dryRun": args.get("dryRun", False),
        "ruleIds": FIXABLE_RULE_IDS,
        "cachingDays": args.get("cachingDays", 30),
        "retentionDays": args.get("retentionDays", 365),
    }
    return eventhouse_fix(fix_args)


# ──────────────────────────────────────────────
# Helpers for materialized view repair
# ──────────────────────────────────────────────

def _extract_columns_from_query(query: str) -> List[str]:
    """Extract column names from a materialized view KQL query (best effort)."""
    columns: List[str] = []
    # Match 'summarize X=func(...), Y=func(...)' or 'project A, B, C' patterns
    summarize_match = re.search(r'\bsummarize\b\s+(.+?)(?:\bby\b|$)', query, re.IGNORECASE)
    if summarize_match:
        expr = summarize_match.group(1)
        for part in expr.split(","):
            part = part.strip()
            eq_match = re.match(r'(\w+)\s*=', part)
            if eq_match:
                columns.append(eq_match.group(1))
    project_match = re.search(r'\bproject\b\s+(.+?)(?:\||$)', query, re.IGNORECASE)
    if project_match:
        expr = project_match.group(1)
        for part in expr.split(","):
            part = part.strip()
            eq_match = re.match(r'(\w+)\s*=', part)
            if eq_match:
                columns.append(eq_match.group(1))
            elif re.match(r'^\w+$', part):
                columns.append(part)
    return columns


def _escape_regex(text: str) -> str:
    """Escape special regex characters."""
    return re.escape(text)


# ──────────────────────────────────────────────
# Tool: eventhouse_fix_materialized_views
# ──────────────────────────────────────────────

def eventhouse_fix_materialized_views(args: dict) -> str:
    try:
        workspace_id = args["workspaceId"]
        eventhouse_id = args["eventhouseId"]
        is_dry_run = args.get("dryRun", False)
        kql_database_name = args.get("kqlDatabaseName")

        eh = get_eventhouse(workspace_id, eventhouse_id)
        props = eh.get("properties") or {}
        cluster_uri = props.get("queryServiceUri", "")
        if not cluster_uri:
            return "❌ Eventhouse has no queryServiceUri."

        databases = list_kql_databases(workspace_id)
        eh_db_ids = set(props.get("databasesItemIds", []))
        target_dbs = [db for db in databases if db.get("id") in eh_db_ids] if eh_db_ids else databases

        if kql_database_name:
            target_dbs = [db for db in target_dbs if db.get("displayName") == kql_database_name]
            if not target_dbs:
                return f"❌ KQL database '{kql_database_name}' not found."

        if not target_dbs:
            return "❌ No KQL databases found for this eventhouse."

        now = datetime.now(timezone.utc).isoformat()
        lines: List[str] = [
            f"# 🔧 Materialized View Repair: {'DRY RUN' if is_dry_run else 'Executing'}",
            "",
            f"_{now}_",
            "",
        ]

        for db in target_dbs:
            db_name = db.get("displayName", "")
            lines.append(f"## Database: {db_name}")
            lines.append("")

            # Get materialized views
            try:
                mv_rows = execute_kql_mgmt(cluster_uri, db_name, ".show materialized-views")
            except Exception as e:
                lines.append(f"❌ Failed to fetch materialized views: {e}")
                lines.append("")
                continue

            if not mv_rows:
                lines.append("No materialized views found.")
                lines.append("")
                continue

            # Get table list for schema matching
            try:
                table_rows = execute_kql_mgmt(cluster_uri, db_name, ".show tables details")
            except Exception:
                table_rows = []

            table_names = {r.get("TableName", "") for r in table_rows}

            # Get database schema for column matching
            try:
                schema_rows = execute_kql_mgmt(
                    cluster_uri, db_name, ".show database schema as json | project DatabaseSchema"
                )
                db_schema = {}
                if schema_rows and schema_rows[0].get("DatabaseSchema"):
                    raw = schema_rows[0]["DatabaseSchema"]
                    db_schema = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                db_schema = {}

            # Build column signature map for tables
            table_columns: Dict[str, List[str]] = {}
            if db_schema:
                tables_schema = db_schema.get("Tables", db_schema.get("tables", {}))
                if isinstance(tables_schema, dict):
                    for tname, tinfo in tables_schema.items():
                        cols = tinfo.get("OrderedColumns", tinfo.get("Columns", []))
                        if isinstance(cols, list):
                            table_columns[tname] = [
                                c.get("Name", c.get("ColumnName", "")) for c in cols if isinstance(c, dict)
                            ]

            for mv in mv_rows:
                mv_name = mv.get("Name", mv.get("MaterializedViewName", ""))
                source_table = mv.get("SourceTable", "")
                query = mv.get("Query", "")
                is_enabled = not _is_falsy(mv.get("IsEnabled", True))
                is_healthy = not _is_falsy(mv.get("IsHealthy", True))

                issues: List[str] = []
                if not is_enabled:
                    issues.append("disabled")
                if not is_healthy:
                    issues.append("unhealthy")
                if source_table and source_table not in table_names:
                    issues.append(f"source table '{source_table}' missing")

                if not issues:
                    lines.append(f"- ✅ `{mv_name}` — healthy, no issues")
                    continue

                lines.append(f"- 🔴 `{mv_name}` — {', '.join(issues)}")

                # Try to find renamed source table by column matching
                if source_table and source_table not in table_names and query:
                    source_cols = table_columns.get(source_table, [])
                    if not source_cols:
                        # Try to infer from query
                        source_cols = _extract_columns_from_query(query)

                    best_match = ""
                    best_score = 0.0

                    if source_cols:
                        for tname, tcols in table_columns.items():
                            if tname == source_table:
                                continue
                            matching = len(set(source_cols) & set(tcols))
                            total = max(len(source_cols), 1)
                            score = matching / total
                            if score > best_score and score >= 0.5:
                                best_score = score
                                best_match = tname

                    if best_match:
                        lines.append(
                            f"  → Best match: `{best_match}` "
                            f"({best_score * 100:.0f}% column similarity)"
                        )
                        _validate_kql_name(mv_name, "materialized view name")
                        _validate_kql_name(best_match, "table name")

                        # Replace source table in query
                        new_query = re.sub(
                            r'\b' + _escape_regex(source_table) + r'\b',
                            best_match,
                            query,
                        )

                        drop_cmd = f".drop materialized-view [{mv_name}] ifexists"
                        create_cmd = f".create materialized-view [{mv_name}] on table [{best_match}] {{ {new_query} }}"

                        if is_dry_run:
                            lines.append(f"  🔍 Would run: `{drop_cmd}`")
                            lines.append(f"  🔍 Would run: `{create_cmd}`")
                        else:
                            try:
                                execute_kql_mgmt(cluster_uri, db_name, drop_cmd)
                                execute_kql_mgmt(cluster_uri, db_name, create_cmd)
                                lines.append(f"  ✅ Recreated `{mv_name}` on `{best_match}`")
                            except Exception as e:
                                lines.append(f"  ❌ Failed to recreate `{mv_name}`: {e}")
                    else:
                        lines.append("  ⚠️ No matching table found by column schema similarity.")
                elif not is_enabled:
                    _validate_kql_name(mv_name, "materialized view name")
                    cmd = f".enable materialized-view [{mv_name}]"
                    if is_dry_run:
                        lines.append(f"  🔍 Would run: `{cmd}`")
                    else:
                        try:
                            execute_kql_mgmt(cluster_uri, db_name, cmd)
                            lines.append(f"  ✅ Re-enabled `{mv_name}`")
                        except Exception as e:
                            lines.append(f"  ❌ Failed to re-enable `{mv_name}`: {e}")

            lines.append("")

        return "\n".join(lines)
    except Exception as e:
        return f"❌ Failed to fix materialized views: {e}"


# ──────────────────────────────────────────────
# Tool definitions for MCP registration
# ──────────────────────────────────────────────

eventhouse_tools = [
    {
        "name": "eventhouse_list",
        "description": "List all eventhouses in a Fabric workspace with their query/ingestion URIs and KQL database counts.",
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
        "handler": eventhouse_list,
    },
    {
        "name": "eventhouse_list_kql_databases",
        "description": "List all KQL databases in a Fabric workspace.",
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
        "handler": eventhouse_list_kql_databases,
    },
    {
        "name": "eventhouse_optimization_recommendations",
        "description": (
            "LIVE SCAN: Connects to a Fabric Eventhouse KQL endpoint and runs real diagnostic commands. "
            "Analyzes 27 rules: table storage/fragmentation (extent stats), caching policies, retention policies, "
            "materialized views health, ingestion batching, streaming ingestion, partitioning, "
            "merge/encoding/row_order policies, stored functions, and query performance. "
            "Returns findings with prioritized action items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "eventhouseId": {
                    "type": "string",
                    "description": "The ID of the eventhouse to analyze",
                },
            },
            "required": ["workspaceId", "eventhouseId"],
        },
        "handler": eventhouse_optimization_recommendations,
    },
    {
        "name": "eventhouse_fix",
        "description": (
            "AUTO-FIX: Applies fixes to a Fabric Eventhouse. "
            "Fixable rules: EH-002 (merge fragmentation), EH-004 (caching), EH-005 (retention), "
            "EH-006 (re-enable materialized views), EH-014 (ingestion batching), EH-016 (partitioning), "
            "EH-017 (merge policy), EH-021 (autocompaction), EH-022 (extent tags retention), "
            "EH-024 (streaming ingestion), EH-025 (refresh stale materialized views). "
            "Use dryRun=true to preview commands without executing them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "eventhouseId": {
                    "type": "string",
                    "description": "The ID of the eventhouse to fix",
                },
                "ruleIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Rule IDs to fix: EH-002, EH-004, EH-005, EH-006, EH-014, "
                        "EH-016, EH-017, EH-021, EH-022, EH-024, EH-025"
                    ),
                },
                "dryRun": {
                    "type": "boolean",
                    "description": "If true, preview commands without executing them (default: false)",
                },
                "kqlDatabaseName": {
                    "type": "string",
                    "description": "Optional: specific KQL database name",
                },
                "tableName": {
                    "type": "string",
                    "description": "Optional: specific table name",
                },
                "cachingDays": {
                    "type": "number",
                    "description": "Hot cache days (default: 30)",
                },
                "retentionDays": {
                    "type": "number",
                    "description": "Retention days (default: 365)",
                },
            },
            "required": ["workspaceId", "eventhouseId"],
        },
        "handler": eventhouse_fix,
    },
    {
        "name": "eventhouse_auto_optimize",
        "description": (
            "AUTO-OPTIMIZE: Scans a Fabric Eventhouse for all fixable issues across all KQL databases "
            "and applies fixes. Covers: merge fragmentation, caching policies, retention policies, "
            "materialized views, ingestion batching, partitioning, merge policy, autocompaction, "
            "extent tags retention, streaming ingestion, stale materialized view refresh. "
            "Use dryRun=true to preview."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "eventhouseId": {
                    "type": "string",
                    "description": "The ID of the eventhouse to optimize",
                },
                "dryRun": {
                    "type": "boolean",
                    "description": "If true, preview KQL commands without executing (default: false)",
                },
                "cachingDays": {
                    "type": "number",
                    "description": "Hot cache days (default: 30)",
                },
                "retentionDays": {
                    "type": "number",
                    "description": "Retention days (default: 365)",
                },
            },
            "required": ["workspaceId", "eventhouseId"],
        },
        "handler": eventhouse_auto_optimize,
    },
    {
        "name": "eventhouse_fix_materialized_views",
        "description": (
            "AUTO-FIX: Diagnoses and repairs broken materialized views in a Fabric Eventhouse. "
            "Detects: disabled views, missing/renamed source tables. Auto-matches renamed tables "
            "by column schema similarity, then drops and recreates views. "
            "Use dryRun=true to preview changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "eventhouseId": {
                    "type": "string",
                    "description": "The ID of the eventhouse",
                },
                "dryRun": {
                    "type": "boolean",
                    "description": "If true, preview fixes without executing (default: false)",
                },
                "kqlDatabaseName": {
                    "type": "string",
                    "description": "Optional: specific KQL database name",
                },
            },
            "required": ["workspaceId", "eventhouseId"],
        },
        "handler": eventhouse_fix_materialized_views,
    },
]
