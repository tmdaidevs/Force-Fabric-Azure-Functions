"""
Semantic Model tools — Python port of semanticModel.ts.

Provides listing, optimization recommendations (BPA), fixes (XMLA + BIM/TMDL fallback),
and auto-optimize for Fabric Semantic Models.
"""

import re
import json
import math
import base64
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from clients.fabric_client import (
    list_semantic_models,
    execute_semantic_model_query,
    get_workspace,
    get_semantic_model_definition,
    update_semantic_model_definition,
)
from clients.xmla_client import run_xmla_dmv_queries, execute_xmla_command_by_id
from tools.rule_engine import render_rule_report, RuleResult

# ---------------------------------------------------------------------------
# Constants — DAX / DMV / MDSCHEMA queries
# ---------------------------------------------------------------------------

DAX_DIAGNOSTICS: Dict[str, str] = {
    "columnStats": "EVALUATE COLUMNSTATISTICS()",
}

INFO_QUERIES: Dict[str, str] = {
    "tables": "EVALUATE INFO.TABLES()",
    "columns": "EVALUATE INFO.COLUMNS()",
    "measures": "EVALUATE INFO.MEASURES()",
    "relationships": "EVALUATE INFO.RELATIONSHIPS()",
    "partitions": "EVALUATE INFO.PARTITIONS()",
}

MDSCHEMA_COLUMN_QUERIES: Dict[str, str] = {
    "columns": (
        "SELECT [DIMENSION_UNIQUE_NAME],[HIERARCHY_UNIQUE_NAME],"
        "[HIERARCHY_CAPTION],[HIERARCHY_IS_VISIBLE] "
        "FROM $SYSTEM.MDSCHEMA_HIERARCHIES "
        "WHERE [CUBE_NAME]='Model' AND [HIERARCHY_ORIGIN]=2"
    ),
}

DMV_QUERIES: Dict[str, str] = {
    "measureGroupDimensions": (
        "SELECT [MEASUREGROUP_NAME],[DIMENSION_UNIQUE_NAME],"
        "[DIMENSION_CARDINALITY],[DIMENSION_IS_VISIBLE] "
        "FROM $SYSTEM.MDSCHEMA_MEASUREGROUP_DIMENSIONS "
        "WHERE [CUBE_NAME]='Model'"
    ),
    "dimensions": (
        "SELECT [DIMENSION_UNIQUE_NAME],[DIMENSION_CARDINALITY],"
        "[DIMENSION_IS_VISIBLE],[DESCRIPTION] "
        "FROM $SYSTEM.MDSCHEMA_DIMENSIONS "
        "WHERE [CUBE_NAME]='Model'"
    ),
    "hierarchies": (
        "SELECT [DIMENSION_UNIQUE_NAME],[HIERARCHY_UNIQUE_NAME],"
        "[HIERARCHY_CARDINALITY],[HIERARCHY_IS_VISIBLE] "
        "FROM $SYSTEM.MDSCHEMA_HIERARCHIES "
        "WHERE [CUBE_NAME]='Model' AND [HIERARCHY_ORIGIN]=2"
    ),
    "measures": (
        "SELECT [MEASUREGROUP_NAME],[MEASURE_NAME],[EXPRESSION],"
        "[MEASURE_IS_VISIBLE],[DEFAULT_FORMAT_STRING] "
        "FROM $SYSTEM.MDSCHEMA_MEASURES "
        "WHERE [CUBE_NAME]='Model'"
    ),
}

FIX_DMV_QUERIES: Dict[str, str] = {
    "measures": (
        "SELECT [MEASUREGROUP_NAME],[MEASURE_NAME],[EXPRESSION],"
        "[MEASURE_IS_VISIBLE],[DEFAULT_FORMAT_STRING],"
        "[MEASURE_CAPTION],[DESCRIPTION] "
        "FROM $SYSTEM.MDSCHEMA_MEASURES "
        "WHERE [CUBE_NAME]='Model'"
    ),
    "columns": (
        "SELECT [TABLE_NAME],[COLUMN_NAME],[DATA_TYPE],"
        "[IS_NULLABLE],[COLUMN_FLAGS] "
        "FROM $SYSTEM.DBSCHEMA_COLUMNS"
    ),
    "dimensions": (
        "SELECT [DIMENSION_UNIQUE_NAME],[DIMENSION_CARDINALITY],"
        "[DIMENSION_IS_VISIBLE],[DESCRIPTION] "
        "FROM $SYSTEM.MDSCHEMA_DIMENSIONS "
        "WHERE [CUBE_NAME]='Model'"
    ),
}

# ---------------------------------------------------------------------------
# All 19 fix-rule IDs & descriptions (used by auto-optimize dry-run)
# ---------------------------------------------------------------------------

ALL_FIX_RULES: List[Dict[str, str]] = [
    {"id": "SM-FIX-IFERROR", "desc": "Replace IFERROR/ISERROR with DIVIDE or IF+ISBLANK"},
    {"id": "SM-FIX-EVALLOG", "desc": "Remove EVALUATEANDLOG wrappers"},
    {"id": "SM-FIX-ADDZERO", "desc": "Remove unnecessary +0 additions"},
    {"id": "SM-FIX-DIRECTREF", "desc": "Delete duplicate measures that directly reference another measure"},
    {"id": "SM-FIX-SUMX", "desc": "Replace SUMX with simple SUM where possible"},
    {"id": "SM-FIX-FORMAT", "desc": "Add missing format strings to measures based on name/expression heuristics"},
    {"id": "SM-FIX-DESC", "desc": "Add table descriptions from DMV metadata"},
    {"id": "SM-FIX-HIDDEN", "desc": "Hide foreign-key columns used only in relationships"},
    {"id": "SM-FIX-DATE", "desc": "Mark date tables with Time dataCategory"},
    {"id": "SM-FIX-KEY", "desc": "Set IsKey on primary key columns of dimension tables"},
    {"id": "SM-FIX-AUTODATE", "desc": "Remove auto-date/time tables to reduce model size"},
    {"id": "SM-FIX-HIDEDESC", "desc": "Hide description/comment columns that waste memory"},
    {"id": "SM-FIX-HIDEGUID", "desc": "Hide GUID/UUID columns that waste memory"},
    {"id": "SM-FIX-CONSTCOL", "desc": "Remove constant columns (cardinality = 1)"},
    {"id": "SM-FIX-BIDI", "desc": "Review bidirectional cross-filter relationships (manual)"},
    {"id": "SM-FIX-SUMMARIZE", "desc": "Set SummarizeBy=None on non-additive columns (manual)"},
    {"id": "SM-FIX-REMOVEFILTERS", "desc": "Replace ALL() with REMOVEFILTERS() in measures"},
    {"id": "SM-FIX-MEASUREDESC", "desc": "Add auto-generated descriptions to measures"},
    {"id": "SM-FIX-MEASURENAME", "desc": "Clean whitespace/tabs from measure names"},
]

# ---------------------------------------------------------------------------
# 1. semantic_model_list
# ---------------------------------------------------------------------------


def semantic_model_list(args: dict) -> str:
    """List all semantic models in a workspace."""
    workspace_id = args.get("workspaceId", "")
    if not workspace_id:
        return "❌ workspaceId is required."
    try:
        models = list_semantic_models(workspace_id)
        if not models:
            return "No semantic models found in this workspace."
        lines: List[str] = []
        for m in models:
            lines.append(
                f"- **{m.get('displayName', 'N/A')}** (ID: `{m.get('id', 'N/A')}`)"
            )
        return f"## Semantic Models ({len(models)})\n\n" + "\n".join(lines)
    except Exception as e:
        return f"❌ Failed to list semantic models: {e}"


# ---------------------------------------------------------------------------
# 2. Parsing helpers
# ---------------------------------------------------------------------------


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    return str(val)


def parse_column_statistics(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse COLUMNSTATISTICS() DAX output into ColumnStat dicts."""
    results: List[Dict[str, Any]] = []
    for r in rows:
        table_name = _safe_str(
            r.get("Table Name")
            or r.get("[Table Name]")
            or r.get("TableName")
            or r.get("[TableName]")
        )
        col_name = _safe_str(
            r.get("Column Name")
            or r.get("[Column Name]")
            or r.get("ColumnName")
            or r.get("[ColumnName]")
        )
        min_val = r.get("Min") or r.get("[Min]")
        max_val = r.get("Max") or r.get("[Max]")
        cardinality = _safe_int(
            r.get("Max Length")
            or r.get("[Max Length]")
            or r.get("Cardinality")
            or r.get("[Cardinality]"),
            0,
        )
        max_length = _safe_int(
            r.get("Max Length")
            or r.get("[Max Length]"),
            0,
        )
        # Distinguish cardinality from maxLength — TS uses column order
        # COLUMNSTATISTICS returns: Table Name, Column Name, Min, Max, Cardinality, Max Length
        card_val = r.get("Cardinality") or r.get("[Cardinality]")
        if card_val is not None:
            cardinality = _safe_int(card_val, 0)
        ml_val = r.get("Max Length") or r.get("[Max Length]")
        if ml_val is not None:
            max_length = _safe_int(ml_val, 0)

        results.append(
            {
                "tableName": table_name,
                "columnName": col_name,
                "min": min_val,
                "max": max_val,
                "cardinality": cardinality,
                "maxLength": max_length,
            }
        )
    return results


def parse_dmv_measures(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse MDSCHEMA_MEASURES DMV rows into XmlaMeasureInfo dicts."""
    results: List[Dict[str, Any]] = []
    for r in rows:
        results.append(
            {
                "measureGroupName": _safe_str(r.get("MEASUREGROUP_NAME") or r.get("[MEASUREGROUP_NAME]")),
                "measureName": _safe_str(r.get("MEASURE_NAME") or r.get("[MEASURE_NAME]")),
                "expression": _safe_str(r.get("EXPRESSION") or r.get("[EXPRESSION]")),
                "isVisible": r.get("MEASURE_IS_VISIBLE", r.get("[MEASURE_IS_VISIBLE]", True)),
                "formatString": _safe_str(r.get("DEFAULT_FORMAT_STRING") or r.get("[DEFAULT_FORMAT_STRING]")),
                "caption": _safe_str(r.get("MEASURE_CAPTION") or r.get("[MEASURE_CAPTION]")),
                "description": _safe_str(r.get("DESCRIPTION") or r.get("[DESCRIPTION]")),
            }
        )
    return results


def parse_dmv_columns(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse DBSCHEMA_COLUMNS DMV rows into XmlaColumnInfo dicts."""
    results: List[Dict[str, Any]] = []
    for r in rows:
        results.append(
            {
                "tableName": _safe_str(r.get("TABLE_NAME") or r.get("[TABLE_NAME]")),
                "columnName": _safe_str(r.get("COLUMN_NAME") or r.get("[COLUMN_NAME]")),
                "dataType": _safe_str(r.get("DATA_TYPE") or r.get("[DATA_TYPE]")),
                "isNullable": r.get("IS_NULLABLE", r.get("[IS_NULLABLE]", True)),
                "columnFlags": _safe_int(r.get("COLUMN_FLAGS") or r.get("[COLUMN_FLAGS]"), 0),
            }
        )
    return results


def parse_dmv_dimensions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse MDSCHEMA_DIMENSIONS DMV rows into XmlaDimensionInfo dicts."""
    results: List[Dict[str, Any]] = []
    for r in rows:
        results.append(
            {
                "dimensionUniqueName": _safe_str(r.get("DIMENSION_UNIQUE_NAME") or r.get("[DIMENSION_UNIQUE_NAME]")),
                "cardinality": _safe_int(r.get("DIMENSION_CARDINALITY") or r.get("[DIMENSION_CARDINALITY]"), 0),
                "isVisible": r.get("DIMENSION_IS_VISIBLE", r.get("[DIMENSION_IS_VISIBLE]", True)),
                "description": _safe_str(r.get("DESCRIPTION") or r.get("[DESCRIPTION]")),
            }
        )
    return results


# ---------------------------------------------------------------------------
# 3. BPA rules engine
# ---------------------------------------------------------------------------

_DATE_PATTERN = re.compile(
    r"(date|time|day|month|year|week|quarter|period|dt_|_dt$|_date$|created|modified|updated|timestamp)",
    re.IGNORECASE,
)
_NUMERIC_PATTERN = re.compile(
    r"(amount|price|cost|qty|quantity|total|sum|count|avg|average|rate|percent|pct|num|number|value|revenue|sales|profit|margin|budget|forecast|target|actual|variance|balance|score|rank|index|weight|ratio|factor|multiplier|coefficient|id$|_id$|key$|_key$|code$|_code$|no$|_no$)",
    re.IGNORECASE,
)
_DESC_PATTERN = re.compile(
    r"(description|comment|note|remark|memo|narrative|detail|text|body|content|summary|abstract|observation)",
    re.IGNORECASE,
)
_GUID_PATTERN = re.compile(r"(guid|uuid|uniqueidentifier)", re.IGNORECASE)
_TIMESTAMP_PATTERN = re.compile(
    r"(timestamp|datetime|created_at|updated_at|modified_at|inserted_at|_ts$|_timestamp$)",
    re.IGNORECASE,
)
_BOOL_VALUES = {"yes", "no", "true", "false", "y", "n", "1", "0", "on", "off"}


def run_bpa_rules(stats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run Best-Practice Analyzer rules on COLUMNSTATISTICS data."""
    findings: List[Dict[str, Any]] = []
    if not stats:
        return findings

    # Group stats by table
    tables: Dict[str, List[Dict[str, Any]]] = {}
    for s in stats:
        tn = s.get("tableName", "")
        tables.setdefault(tn, []).append(s)

    # Track column names across tables for repeated-name rule
    col_name_tables: Dict[str, List[str]] = {}
    for s in stats:
        cn = s.get("columnName", "")
        tn = s.get("tableName", "")
        if cn:
            col_name_tables.setdefault(cn, []).append(tn)

    for table_name, cols in tables.items():
        col_count = len(cols)
        max_card = max((c.get("cardinality", 0) for c in cols), default=0)

        # --- Wide Table (>30 cols) ---
        if col_count > 30:
            findings.append(
                {
                    "rule": "Wide Table",
                    "severity": "MEDIUM",
                    "table": table_name,
                    "column": "",
                    "detail": f"Table has {col_count} columns. Consider splitting into a star schema.",
                }
            )

        # --- Extremely Wide Table (>100 cols) ---
        if col_count > 100:
            findings.append(
                {
                    "rule": "Extremely Wide Table",
                    "severity": "HIGH",
                    "table": table_name,
                    "column": "",
                    "detail": f"Table has {col_count} columns — strongly consider normalization.",
                }
            )

        # --- Single Column Table ---
        if col_count == 1:
            findings.append(
                {
                    "rule": "Single Column Table",
                    "severity": "LOW",
                    "table": table_name,
                    "column": "",
                    "detail": "Table has only 1 column. May be unnecessary unless used for disconnected slicing.",
                }
            )

        # Count high-card columns for multi-high-card rule
        high_card_cols = [c for c in cols if c.get("cardinality", 0) > 50000]
        if len(high_card_cols) > 5:
            findings.append(
                {
                    "rule": "Multiple High-Cardinality Columns",
                    "severity": "HIGH",
                    "table": table_name,
                    "column": "",
                    "detail": (
                        f"{len(high_card_cols)} columns have cardinality > 50,000. "
                        "This significantly increases model memory."
                    ),
                }
            )

        # Check for integer surrogate key
        has_int_key = any(
            re.search(r"(id$|_id$|key$|_key$)", c.get("columnName", ""), re.IGNORECASE)
            and isinstance(c.get("min"), (int, float))
            for c in cols
        )
        has_string_key = any(
            re.search(r"(id$|_id$|key$|_key$)", c.get("columnName", ""), re.IGNORECASE)
            and isinstance(c.get("min"), str)
            for c in cols
        )
        if has_string_key and not has_int_key and max_card > 1000:
            findings.append(
                {
                    "rule": "No Integer Surrogate Key",
                    "severity": "MEDIUM",
                    "table": table_name,
                    "column": "",
                    "detail": "Table uses string keys with no integer surrogate — adds memory pressure at scale.",
                }
            )

        # --- Per-column rules ---
        for col in cols:
            cn = col.get("columnName", "")
            card = col.get("cardinality", 0)
            ml = col.get("maxLength", 0)
            min_v = col.get("min")
            max_v = col.get("max")

            # High Cardinality Text Column
            if ml > 100 and card > 10000 and max_card > 0 and card > 0.7 * max_card:
                findings.append(
                    {
                        "rule": "High Cardinality Text Column",
                        "severity": "HIGH",
                        "table": table_name,
                        "column": cn,
                        "detail": (
                            f"Cardinality {card:,}, maxLength {ml}. "
                            "Consider removing or hashing."
                        ),
                    }
                )

            # Constant Column
            if card == 1:
                findings.append(
                    {
                        "rule": "Constant Column",
                        "severity": "LOW",
                        "table": table_name,
                        "column": cn,
                        "detail": "Column has a single unique value — can likely be removed.",
                    }
                )

            # Boolean Stored as Text
            if card == 2 and isinstance(min_v, str) and isinstance(max_v, str):
                vals = {str(min_v).lower(), str(max_v).lower()}
                if vals.issubset(_BOOL_VALUES):
                    findings.append(
                        {
                            "rule": "Boolean Stored as Text",
                            "severity": "MEDIUM",
                            "table": table_name,
                            "column": cn,
                            "detail": f"Values ({min_v}, {max_v}) look boolean. Use TRUE/FALSE data type.",
                        }
                    )

            # Date Stored as Text
            if _DATE_PATTERN.search(cn) and isinstance(min_v, str) and ml > 5:
                findings.append(
                    {
                        "rule": "Date Stored as Text",
                        "severity": "MEDIUM",
                        "table": table_name,
                        "column": cn,
                        "detail": "Column name implies a date but is stored as text. Use Date/DateTime type.",
                    }
                )

            # Numeric Column Stored as Text
            if (
                _NUMERIC_PATTERN.search(cn)
                and isinstance(min_v, str)
                and isinstance(max_v, str)
            ):
                try:
                    float(min_v)
                    float(max_v)
                    findings.append(
                        {
                            "rule": "Numeric Column Stored as Text",
                            "severity": "MEDIUM",
                            "table": table_name,
                            "column": cn,
                            "detail": "Column name implies numeric data stored as text. Convert to number type.",
                        }
                    )
                except (ValueError, TypeError):
                    pass

            # Low Cardinality Column in Fact Table
            if 2 < card <= 20 and max_card > 10000:
                findings.append(
                    {
                        "rule": "Low Cardinality Column in Fact Table",
                        "severity": "LOW",
                        "table": table_name,
                        "column": cn,
                        "detail": (
                            f"Only {card} unique values in a high-cardinality table ({max_card:,} max). "
                            "Consider moving to a dimension."
                        ),
                    }
                )

            # Description / Comment Column
            if ml > 200 and card > 100 and _DESC_PATTERN.search(cn):
                findings.append(
                    {
                        "rule": "Description/Comment Column",
                        "severity": "MEDIUM",
                        "table": table_name,
                        "column": cn,
                        "detail": f"Long text column (maxLen {ml}, card {card:,}). Hide or remove if not user-facing.",
                    }
                )

            # Column Name Starts with Underscore
            if cn.startswith("_"):
                findings.append(
                    {
                        "rule": "Column Name Starts with Underscore",
                        "severity": "LOW",
                        "table": table_name,
                        "column": cn,
                        "detail": "Leading underscore often indicates a staging/internal column. Consider hiding.",
                    }
                )

            # Nearly Unique Numeric Column
            if card > 0 and max_card > 0 and card > 0.95 * max_card and isinstance(min_v, (int, float)):
                findings.append(
                    {
                        "rule": "Nearly Unique Numeric Column",
                        "severity": "LOW",
                        "table": table_name,
                        "column": cn,
                        "detail": f"Cardinality {card:,} is >95% of table max ({max_card:,}). May be a natural key.",
                    }
                )

            # GUID / UUID Column
            if 32 <= ml <= 40 and _GUID_PATTERN.search(cn):
                findings.append(
                    {
                        "rule": "GUID/UUID Column",
                        "severity": "MEDIUM",
                        "table": table_name,
                        "column": cn,
                        "detail": "GUID columns consume significant memory. Replace with integer surrogates if possible.",
                    }
                )

            # High-Precision Timestamp
            if card > 10000 and _TIMESTAMP_PATTERN.search(cn):
                findings.append(
                    {
                        "rule": "High-Precision Timestamp",
                        "severity": "LOW",
                        "table": table_name,
                        "column": cn,
                        "detail": f"Timestamp with {card:,} unique values. Truncate to date if time precision isn't needed.",
                    }
                )

            # Binary-like Column with Non-Standard Values
            if card == 2 and isinstance(min_v, (int, float)) and isinstance(max_v, (int, float)):
                if not ({int(min_v), int(max_v)} == {0, 1}):
                    findings.append(
                        {
                            "rule": "Binary-like Column with Non-Standard Values",
                            "severity": "LOW",
                            "table": table_name,
                            "column": cn,
                            "detail": f"Two numeric values ({min_v}, {max_v}) that aren't 0/1. Standardize to boolean.",
                        }
                    )

    # --- Column Name Repeated Across Many Tables ---
    for cn, tbl_list in col_name_tables.items():
        unique_tables = list(set(tbl_list))
        if len(unique_tables) > 3:
            findings.append(
                {
                    "rule": "Column Name Repeated Across Many Tables",
                    "severity": "LOW",
                    "table": ", ".join(unique_tables[:5]),
                    "column": cn,
                    "detail": f"Column '{cn}' appears in {len(unique_tables)} tables. Consider a shared dimension.",
                }
            )

    return findings


# ---------------------------------------------------------------------------
# 4. Report builders
# ---------------------------------------------------------------------------


def build_table_overview(stats: List[Dict[str, Any]]) -> str:
    """Build markdown table overview from column stats."""
    tables: Dict[str, List[Dict[str, Any]]] = {}
    for s in stats:
        tables.setdefault(s.get("tableName", ""), []).append(s)

    lines = ["| Table | Columns | Max Cardinality | Avg MaxLength |", "| --- | ---:| ---:| ---:|"]
    for tn in sorted(tables.keys()):
        cols = tables[tn]
        max_card = max((c.get("cardinality", 0) for c in cols), default=0)
        avg_ml = sum(c.get("maxLength", 0) for c in cols) / max(len(cols), 1)
        lines.append(f"| {tn} | {len(cols)} | {max_card:,} | {avg_ml:,.0f} |")
    return "\n".join(lines)


def build_memory_hotspots_table(stats: List[Dict[str, Any]]) -> str:
    """Top 20 columns by cardinality × maxLength."""
    scored = []
    for s in stats:
        score = s.get("cardinality", 0) * s.get("maxLength", 0)
        scored.append({**s, "score": score})
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:20]
    lines = [
        "| Table | Column | Cardinality | MaxLength | Score |",
        "| --- | --- | ---:| ---:| ---:|",
    ]
    for s in top:
        lines.append(
            f"| {s.get('tableName','')} | {s.get('columnName','')} "
            f"| {s.get('cardinality',0):,} | {s.get('maxLength',0):,} "
            f"| {s.get('score',0):,} |"
        )
    return "\n".join(lines)


def build_cardinality_distribution(stats: List[Dict[str, Any]]) -> str:
    """Bucket table of cardinality distribution."""
    buckets = {
        "1 (constant)": 0,
        "2-10": 0,
        "11-100": 0,
        "101-1K": 0,
        "1K-10K": 0,
        "10K-100K": 0,
        "100K-1M": 0,
        ">1M": 0,
    }
    for s in stats:
        c = s.get("cardinality", 0)
        if c <= 1:
            buckets["1 (constant)"] += 1
        elif c <= 10:
            buckets["2-10"] += 1
        elif c <= 100:
            buckets["11-100"] += 1
        elif c <= 1000:
            buckets["101-1K"] += 1
        elif c <= 10000:
            buckets["1K-10K"] += 1
        elif c <= 100000:
            buckets["10K-100K"] += 1
        elif c <= 1000000:
            buckets["100K-1M"] += 1
        else:
            buckets[">1M"] += 1

    lines = ["| Cardinality Range | Column Count |", "| --- | ---:|"]
    for k, v in buckets.items():
        if v > 0:
            lines.append(f"| {k} | {v} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Optimization recommendations — all rules SM-001..SM-029, SM-B01..SM-B14
# ---------------------------------------------------------------------------


def _run_query(workspace_name: str, model_name: str, query: str) -> List[Dict[str, Any]]:
    """Helper: run DMV/DAX against the model, returning rows or [] on error."""
    try:
        result = run_xmla_dmv_queries(workspace_name, model_name, {"q": query})
        q_result = result.get("q", {})
        if isinstance(q_result, dict):
            return q_result.get("rows", [])
        return []
    except Exception:
        return []


def _count_pattern_in_measures(measures: List[Dict[str, Any]], pattern: re.Pattern) -> List[Dict[str, Any]]:
    """Return measures whose expression matches the pattern."""
    hits: List[Dict[str, Any]] = []
    for m in measures:
        expr = m.get("expression", "") or ""
        if pattern.search(expr):
            hits.append(m)
    return hits


def semantic_model_optimization_recommendations(args: dict) -> str:
    """Run full BPA + DMV analysis on a Semantic Model."""
    workspace_id = args.get("workspaceId", "")
    model_id = args.get("semanticModelId", "")
    if not workspace_id or not model_id:
        return "❌ workspaceId and semanticModelId are required."

    try:
        workspace = get_workspace(workspace_id)
        workspace_name = workspace.get("displayName", workspace_id)
    except Exception:
        workspace_name = workspace_id

    # Resolve model name
    try:
        models = list_semantic_models(workspace_id)
        model_info = next((m for m in models if m.get("id") == model_id), None)
        model_name = model_info.get("displayName", model_id) if model_info else model_id
    except Exception:
        model_name = model_id

    scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rules: List[RuleResult] = []
    header_sections: List[str] = []

    # ---- Run DMV queries ----
    try:
        dmv_data = run_xmla_dmv_queries(workspace_name, model_name, DMV_QUERIES)
    except Exception as e:
        dmv_data = {}
        header_sections.append(f"⚠️ DMV query failed: {e}")

    dmv_measures_rows = (dmv_data.get("measures", {}) or {}).get("rows", [])
    dmv_dimensions_rows = (dmv_data.get("dimensions", {}) or {}).get("rows", [])
    dmv_hierarchies_rows = (dmv_data.get("hierarchies", {}) or {}).get("rows", [])
    dmv_mgd_rows = (dmv_data.get("measureGroupDimensions", {}) or {}).get("rows", [])

    measures = parse_dmv_measures(dmv_measures_rows)
    dimensions = parse_dmv_dimensions(dmv_dimensions_rows)

    # ---- Run COLUMNSTATISTICS ----
    col_stats: List[Dict[str, Any]] = []
    try:
        cs_result = execute_semantic_model_query(
            workspace_id, model_id, DAX_DIAGNOSTICS["columnStats"]
        )
        if isinstance(cs_result, list):
            col_stats = parse_column_statistics(cs_result)
    except Exception:
        pass

    # ---- Run INFO queries ----
    info_tables: List[Dict[str, Any]] = []
    info_columns: List[Dict[str, Any]] = []
    info_measures: List[Dict[str, Any]] = []
    info_relationships: List[Dict[str, Any]] = []
    info_partitions: List[Dict[str, Any]] = []
    try:
        for qname, qtext in INFO_QUERIES.items():
            try:
                rows = execute_semantic_model_query(workspace_id, model_id, qtext)
                if not isinstance(rows, list):
                    rows = []
            except Exception:
                rows = []
            if qname == "tables":
                info_tables = rows
            elif qname == "columns":
                info_columns = rows
            elif qname == "measures":
                info_measures = rows
            elif qname == "relationships":
                info_relationships = rows
            elif qname == "partitions":
                info_partitions = rows
    except Exception:
        pass

    # ---- Run MDSCHEMA columns ----
    mdschema_cols: List[Dict[str, Any]] = []
    try:
        md_data = run_xmla_dmv_queries(workspace_name, model_name, MDSCHEMA_COLUMN_QUERIES)
        mdschema_cols = (md_data.get("columns", {}) or {}).get("rows", [])
    except Exception:
        pass

    # ---- Header sections ----
    header_sections.append(f"**Model:** {model_name}")
    header_sections.append(f"**Workspace:** {workspace_name}")
    header_sections.append(f"**Tables (DMV):** {len(dimensions)}")
    header_sections.append(f"**Measures (DMV):** {len(measures)}")
    if col_stats:
        header_sections.append(f"**Columns (COLUMNSTATISTICS):** {len(col_stats)}")

    # ====================================================================
    # DMV-based rules SM-001 .. SM-018
    # ====================================================================

    # SM-001: IFERROR usage
    iferror_re = re.compile(r"\bI[FS]ERROR\b", re.IGNORECASE)
    iferror_hits = _count_pattern_in_measures(measures, iferror_re)
    if iferror_hits:
        names = ", ".join(m["measureName"] for m in iferror_hits[:5])
        rules.append(
            RuleResult(
                id="SM-001",
                rule="Avoid IFERROR / ISERROR",
                category="DAX",
                severity="MEDIUM",
                status="FAIL",
                details=f"{len(iferror_hits)} measure(s) use IFERROR/ISERROR: {names}",
                recommendation="Replace with DIVIDE() or IF(ISBLANK(...)).",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-001",
                rule="Avoid IFERROR / ISERROR",
                category="DAX",
                severity="MEDIUM",
                status="PASS",
                details="No IFERROR/ISERROR usage found.",
            )
        )

    # SM-002: Use DIVIDE not /
    divide_re = re.compile(r"(?<![A-Za-z])/(?![/*])", re.IGNORECASE)
    divide_hits = _count_pattern_in_measures(measures, divide_re)
    if divide_hits:
        names = ", ".join(m["measureName"] for m in divide_hits[:5])
        rules.append(
            RuleResult(
                id="SM-002",
                rule="Use DIVIDE() instead of /",
                category="DAX",
                severity="LOW",
                status="WARN",
                details=f"{len(divide_hits)} measure(s) use '/' operator: {names}",
                recommendation="DIVIDE() handles division by zero safely.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-002",
                rule="Use DIVIDE() instead of /",
                category="DAX",
                severity="LOW",
                status="PASS",
                details="All measures use DIVIDE() or avoid division.",
            )
        )

    # SM-003: No EVALUATEANDLOG
    evallog_re = re.compile(r"\bEVALUATEANDLOG\b", re.IGNORECASE)
    evallog_hits = _count_pattern_in_measures(measures, evallog_re)
    if evallog_hits:
        names = ", ".join(m["measureName"] for m in evallog_hits[:5])
        rules.append(
            RuleResult(
                id="SM-003",
                rule="Remove EVALUATEANDLOG",
                category="DAX",
                severity="HIGH",
                status="FAIL",
                details=f"{len(evallog_hits)} measure(s) contain EVALUATEANDLOG: {names}",
                recommendation="EVALUATEANDLOG is for debugging only — remove before production.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-003",
                rule="Remove EVALUATEANDLOG",
                category="DAX",
                severity="HIGH",
                status="PASS",
                details="No EVALUATEANDLOG usage found.",
            )
        )

    # SM-004: TREATAS not INTERSECT
    intersect_re = re.compile(r"\bINTERSECT\b", re.IGNORECASE)
    intersect_hits = _count_pattern_in_measures(measures, intersect_re)
    if intersect_hits:
        names = ", ".join(m["measureName"] for m in intersect_hits[:5])
        rules.append(
            RuleResult(
                id="SM-004",
                rule="Use TREATAS instead of INTERSECT",
                category="DAX",
                severity="LOW",
                status="WARN",
                details=f"{len(intersect_hits)} measure(s) use INTERSECT: {names}",
                recommendation="TREATAS is more efficient for virtual relationships.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-004",
                rule="Use TREATAS instead of INTERSECT",
                category="DAX",
                severity="LOW",
                status="PASS",
                details="No INTERSECT usage found.",
            )
        )

    # SM-005: No duplicate measures
    expr_map: Dict[str, List[str]] = {}
    for m in measures:
        expr = (m.get("expression") or "").strip()
        if expr:
            expr_map.setdefault(expr, []).append(m["measureName"])
    duplicates = {k: v for k, v in expr_map.items() if len(v) > 1}
    if duplicates:
        dup_info = "; ".join(f"{', '.join(v)}" for v in list(duplicates.values())[:3])
        rules.append(
            RuleResult(
                id="SM-005",
                rule="No duplicate measure expressions",
                category="DAX",
                severity="MEDIUM",
                status="FAIL",
                details=f"{len(duplicates)} duplicate expression(s): {dup_info}",
                recommendation="Consolidate duplicate measures; have one reference the other.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-005",
                rule="No duplicate measure expressions",
                category="DAX",
                severity="MEDIUM",
                status="PASS",
                details="No duplicate measure expressions found.",
            )
        )

    # SM-006: Filter by columns not tables
    filter_table_re = re.compile(r"\bFILTER\s*\(\s*['\"]?\w+['\"]?\s*,", re.IGNORECASE)
    filter_hits = _count_pattern_in_measures(measures, filter_table_re)
    if filter_hits:
        names = ", ".join(m["measureName"] for m in filter_hits[:5])
        rules.append(
            RuleResult(
                id="SM-006",
                rule="Filter by columns, not tables",
                category="DAX",
                severity="MEDIUM",
                status="WARN",
                details=f"{len(filter_hits)} measure(s) may filter entire tables: {names}",
                recommendation="Use column-level filters (e.g., KEEPFILTERS) for performance.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-006",
                rule="Filter by columns, not tables",
                category="DAX",
                severity="MEDIUM",
                status="PASS",
                details="No table-level FILTER usage detected.",
            )
        )

    # SM-007: Avoid adding 0
    add_zero_re = re.compile(r"\+\s*0(?:\b|$)", re.IGNORECASE)
    add_zero_hits = _count_pattern_in_measures(measures, add_zero_re)
    if add_zero_hits:
        names = ", ".join(m["measureName"] for m in add_zero_hits[:5])
        rules.append(
            RuleResult(
                id="SM-007",
                rule="Avoid adding +0",
                category="DAX",
                severity="LOW",
                status="WARN",
                details=f"{len(add_zero_hits)} measure(s) add +0: {names}",
                recommendation="Use FORMAT or IF(ISBLANK(...)) instead of +0 to force evaluation.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-007",
                rule="Avoid adding +0",
                category="DAX",
                severity="LOW",
                status="PASS",
                details="No +0 additions found.",
            )
        )

    # SM-008: Measures documentation
    undocumented = [m for m in measures if not (m.get("description") or "").strip()]
    if len(undocumented) > len(measures) * 0.5 and len(measures) > 0:
        rules.append(
            RuleResult(
                id="SM-008",
                rule="Measures should have descriptions",
                category="Documentation",
                severity="LOW",
                status="WARN",
                details=f"{len(undocumented)} of {len(measures)} measures lack descriptions.",
                recommendation="Add descriptions to improve maintainability.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-008",
                rule="Measures should have descriptions",
                category="Documentation",
                severity="LOW",
                status="PASS",
                details="Majority of measures have descriptions.",
            )
        )

    # SM-009: Model has tables
    if not dimensions:
        rules.append(
            RuleResult(
                id="SM-009",
                rule="Model should have tables",
                category="Model",
                severity="HIGH",
                status="FAIL",
                details="No tables found in the model.",
                recommendation="Add data tables to the semantic model.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-009",
                rule="Model should have tables",
                category="Model",
                severity="HIGH",
                status="PASS",
                details=f"Model contains {len(dimensions)} table(s).",
            )
        )

    # SM-010: Date table check
    date_table_re = re.compile(r"(date|calendar|time|period)", re.IGNORECASE)
    has_date_table = any(
        date_table_re.search(d.get("dimensionUniqueName", "")) for d in dimensions
    )
    if not has_date_table and len(dimensions) > 0:
        rules.append(
            RuleResult(
                id="SM-010",
                rule="Model should have a date table",
                category="Model",
                severity="MEDIUM",
                status="WARN",
                details="No explicit date/calendar table detected.",
                recommendation="Create a dedicated date dimension for time intelligence.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-010",
                rule="Model should have a date table",
                category="Model",
                severity="MEDIUM",
                status="PASS",
                details="Date/calendar table detected.",
            )
        )

    # SM-011: Avoid 1-(x/y)
    one_minus_re = re.compile(r"1\s*-\s*\(", re.IGNORECASE)
    one_minus_hits = _count_pattern_in_measures(measures, one_minus_re)
    if one_minus_hits:
        names = ", ".join(m["measureName"] for m in one_minus_hits[:5])
        rules.append(
            RuleResult(
                id="SM-011",
                rule="Avoid 1-(x/y) pattern",
                category="DAX",
                severity="LOW",
                status="WARN",
                details=f"{len(one_minus_hits)} measure(s) use 1-(...): {names}",
                recommendation="Rewrite for clarity: DIVIDE(y-x, y).",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-011",
                rule="Avoid 1-(x/y) pattern",
                category="DAX",
                severity="LOW",
                status="PASS",
                details="No 1-(...) patterns found.",
            )
        )

    # SM-012: Direct measure references
    # Measures that are just references to another measure (no logic)
    ref_re = re.compile(r"^\s*\[[\w\s]+\]\s*$")
    ref_hits = [m for m in measures if ref_re.match(m.get("expression") or "")]
    if ref_hits:
        names = ", ".join(m["measureName"] for m in ref_hits[:5])
        rules.append(
            RuleResult(
                id="SM-012",
                rule="Avoid direct measure references",
                category="DAX",
                severity="LOW",
                status="WARN",
                details=f"{len(ref_hits)} measure(s) directly reference another: {names}",
                recommendation="Delete the wrapper and use the original measure directly.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-012",
                rule="Avoid direct measure references",
                category="DAX",
                severity="LOW",
                status="PASS",
                details="No direct measure references found.",
            )
        )

    # SM-013: Nested CALCULATE
    nested_calc_re = re.compile(r"CALCULATE\s*\([^)]*CALCULATE", re.IGNORECASE | re.DOTALL)
    nested_calc_hits = _count_pattern_in_measures(measures, nested_calc_re)
    if nested_calc_hits:
        names = ", ".join(m["measureName"] for m in nested_calc_hits[:5])
        rules.append(
            RuleResult(
                id="SM-013",
                rule="Avoid nested CALCULATE",
                category="DAX",
                severity="MEDIUM",
                status="WARN",
                details=f"{len(nested_calc_hits)} measure(s) with nested CALCULATE: {names}",
                recommendation="Flatten nested CALCULATE for clarity and performance.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-013",
                rule="Avoid nested CALCULATE",
                category="DAX",
                severity="MEDIUM",
                status="PASS",
                details="No nested CALCULATE patterns found.",
            )
        )

    # SM-014: SUMX for simple aggregation
    sumx_re = re.compile(r"\bSUMX\s*\(\s*['\"]?\w+['\"]?\s*,\s*['\"]?\w+['\"]?\s*\[[\w\s]+\]\s*\)", re.IGNORECASE)
    sumx_hits = _count_pattern_in_measures(measures, sumx_re)
    if sumx_hits:
        names = ", ".join(m["measureName"] for m in sumx_hits[:5])
        rules.append(
            RuleResult(
                id="SM-014",
                rule="Use SUM instead of SUMX for simple aggregation",
                category="DAX",
                severity="LOW",
                status="WARN",
                details=f"{len(sumx_hits)} measure(s) use SUMX for simple column sum: {names}",
                recommendation="Replace SUMX(Table, Table[Col]) with SUM(Table[Col]).",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-014",
                rule="Use SUM instead of SUMX for simple aggregation",
                category="DAX",
                severity="LOW",
                status="PASS",
                details="No simple SUMX patterns found.",
            )
        )

    # SM-015: Format strings
    no_fmt = [
        m
        for m in measures
        if not (m.get("formatString") or "").strip()
        and (m.get("expression") or "").strip()
    ]
    if no_fmt:
        names = ", ".join(m["measureName"] for m in no_fmt[:5])
        rules.append(
            RuleResult(
                id="SM-015",
                rule="Measures should have format strings",
                category="Model",
                severity="LOW",
                status="WARN",
                details=f"{len(no_fmt)} measure(s) lack format strings: {names}",
                recommendation="Add format strings for consistent display (e.g. #,0, 0.00%, $#,0).",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-015",
                rule="Measures should have format strings",
                category="Model",
                severity="LOW",
                status="PASS",
                details="All measures have format strings.",
            )
        )

    # SM-016: FILTER(ALL(...))
    filter_all_re = re.compile(r"\bFILTER\s*\(\s*ALL\s*\(", re.IGNORECASE)
    filter_all_hits = _count_pattern_in_measures(measures, filter_all_re)
    if filter_all_hits:
        names = ", ".join(m["measureName"] for m in filter_all_hits[:5])
        rules.append(
            RuleResult(
                id="SM-016",
                rule="Avoid FILTER(ALL(...))",
                category="DAX",
                severity="MEDIUM",
                status="WARN",
                details=f"{len(filter_all_hits)} measure(s) use FILTER(ALL(...)): {names}",
                recommendation="Use CALCULATE with filter arguments instead of FILTER(ALL(...)).",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-016",
                rule="Avoid FILTER(ALL(...))",
                category="DAX",
                severity="MEDIUM",
                status="PASS",
                details="No FILTER(ALL(...)) usage found.",
            )
        )

    # SM-017: Measure naming convention
    bad_name_re = re.compile(r"[\t]|^\s+|\s+$|  +")
    bad_name_hits = [m for m in measures if bad_name_re.search(m.get("measureName", ""))]
    if bad_name_hits:
        names = ", ".join(m["measureName"][:30] for m in bad_name_hits[:5])
        rules.append(
            RuleResult(
                id="SM-017",
                rule="Measure naming convention",
                category="Documentation",
                severity="LOW",
                status="WARN",
                details=f"{len(bad_name_hits)} measure(s) have irregular names: {names}",
                recommendation="Remove leading/trailing spaces, tabs, and double spaces from measure names.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-017",
                rule="Measure naming convention",
                category="Documentation",
                severity="LOW",
                status="PASS",
                details="All measure names follow conventions.",
            )
        )

    # SM-018: Table count check
    if len(dimensions) > 50:
        rules.append(
            RuleResult(
                id="SM-018",
                rule="Model table count",
                category="Model",
                severity="MEDIUM",
                status="WARN",
                details=f"Model has {len(dimensions)} tables. Large models can degrade performance.",
                recommendation="Consider splitting into multiple models or reducing table count.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-018",
                rule="Model table count",
                category="Model",
                severity="MEDIUM",
                status="PASS",
                details=f"Model has {len(dimensions)} tables — within normal range.",
            )
        )

    # ====================================================================
    # SM-021 .. SM-029 — new rules using INFO + DMV data
    # ====================================================================

    # SM-021: Bidirectional cross-filter overuse
    bidi_rels = [
        r
        for r in info_relationships
        if str(r.get("CrossFilteringBehavior", r.get("[CrossFilteringBehavior]", ""))).lower()
        in ("2", "bothways", "bidirectional", "bothdirections")
    ]
    if len(bidi_rels) > 2:
        rules.append(
            RuleResult(
                id="SM-021",
                rule="Bidirectional cross-filter overuse",
                category="Model",
                severity="MEDIUM",
                status="WARN",
                details=f"{len(bidi_rels)} bidirectional relationships detected.",
                recommendation="Bidirectional filters can cause ambiguity and performance issues. Use sparingly.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-021",
                rule="Bidirectional cross-filter overuse",
                category="Model",
                severity="MEDIUM",
                status="PASS",
                details=f"{len(bidi_rels)} bidirectional relationship(s) — acceptable.",
            )
        )

    # SM-022: Implicit measures (SummarizeBy != None)
    implicit_cols = [
        c
        for c in info_columns
        if str(c.get("SummarizeBy", c.get("[SummarizeBy]", ""))).lower()
        not in ("", "none", "0")
    ]
    if implicit_cols:
        rules.append(
            RuleResult(
                id="SM-022",
                rule="Implicit measures (SummarizeBy != None)",
                category="Model",
                severity="MEDIUM",
                status="WARN",
                details=f"{len(implicit_cols)} column(s) have implicit aggregation set.",
                recommendation="Set SummarizeBy = None and create explicit measures instead.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-022",
                rule="Implicit measures (SummarizeBy != None)",
                category="Model",
                severity="MEDIUM",
                status="PASS",
                details="All columns have SummarizeBy = None.",
            )
        )

    # SM-023: Disconnected tables
    connected_tables: set = set()
    for r in info_relationships:
        from_table = r.get("FromTableID", r.get("[FromTableID]", ""))
        to_table = r.get("ToTableID", r.get("[ToTableID]", ""))
        if from_table:
            connected_tables.add(str(from_table))
        if to_table:
            connected_tables.add(str(to_table))

    all_table_ids = set()
    table_id_to_name: Dict[str, str] = {}
    for t in info_tables:
        tid = str(t.get("ID", t.get("[ID]", "")))
        tname = t.get("Name", t.get("[Name]", ""))
        if tid:
            all_table_ids.add(tid)
            table_id_to_name[tid] = tname

    disconnected = all_table_ids - connected_tables
    # Filter out measure tables (no columns, just measures)
    disc_names = [table_id_to_name.get(tid, tid) for tid in disconnected]
    if disc_names:
        rules.append(
            RuleResult(
                id="SM-023",
                rule="Disconnected tables",
                category="Model",
                severity="LOW",
                status="WARN",
                details=f"{len(disc_names)} disconnected table(s): {', '.join(disc_names[:5])}",
                recommendation="Disconnected tables are okay for parameter/slicer tables; otherwise connect via relationships.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-023",
                rule="Disconnected tables",
                category="Model",
                severity="LOW",
                status="PASS",
                details="All tables are connected via relationships.",
            )
        )

    # SM-024: ALL() vs REMOVEFILTERS()
    all_re = re.compile(r"\bALL\s*\(", re.IGNORECASE)
    all_hits = _count_pattern_in_measures(measures, all_re)
    removefilters_re = re.compile(r"\bREMOVEFILTERS\s*\(", re.IGNORECASE)
    removefilters_hits = _count_pattern_in_measures(measures, removefilters_re)
    if all_hits and not removefilters_hits:
        names = ", ".join(m["measureName"] for m in all_hits[:5])
        rules.append(
            RuleResult(
                id="SM-024",
                rule="Consider REMOVEFILTERS() over ALL()",
                category="DAX",
                severity="LOW",
                status="WARN",
                details=f"{len(all_hits)} measure(s) use ALL(): {names}",
                recommendation="REMOVEFILTERS() is semantically clearer when used to remove filters in CALCULATE.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-024",
                rule="Consider REMOVEFILTERS() over ALL()",
                category="DAX",
                severity="LOW",
                status="PASS",
                details="ALL/REMOVEFILTERS usage looks fine.",
            )
        )

    # SM-025: Excessive USERELATIONSHIP
    userel_re = re.compile(r"\bUSERELATIONSHIP\s*\(", re.IGNORECASE)
    userel_hits = _count_pattern_in_measures(measures, userel_re)
    if len(userel_hits) > 5:
        rules.append(
            RuleResult(
                id="SM-025",
                rule="Excessive USERELATIONSHIP usage",
                category="DAX",
                severity="MEDIUM",
                status="WARN",
                details=f"{len(userel_hits)} measures use USERELATIONSHIP.",
                recommendation="Many USERELATIONSHIP calls may indicate a role-playing dimension issue. Consider separate date tables.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-025",
                rule="Excessive USERELATIONSHIP usage",
                category="DAX",
                severity="MEDIUM",
                status="PASS",
                details=f"{len(userel_hits)} USERELATIONSHIP usage(s) — acceptable.",
            )
        )

    # SM-026: Complex relationship web
    if len(info_relationships) > len(info_tables) * 2 and len(info_tables) > 0:
        rules.append(
            RuleResult(
                id="SM-026",
                rule="Complex relationship web",
                category="Model",
                severity="MEDIUM",
                status="WARN",
                details=f"{len(info_relationships)} relationships for {len(info_tables)} tables (ratio > 2:1).",
                recommendation="Simplify the relationship web to improve query performance and maintainability.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-026",
                rule="Complex relationship web",
                category="Model",
                severity="MEDIUM",
                status="PASS",
                details=f"{len(info_relationships)} relationships for {len(info_tables)} tables.",
            )
        )

    # SM-027: Inactive relationships
    inactive_rels = [
        r
        for r in info_relationships
        if str(r.get("IsActive", r.get("[IsActive]", "true"))).lower() in ("false", "0")
    ]
    if len(inactive_rels) > 3:
        rules.append(
            RuleResult(
                id="SM-027",
                rule="Many inactive relationships",
                category="Model",
                severity="LOW",
                status="WARN",
                details=f"{len(inactive_rels)} inactive relationships.",
                recommendation="Review whether all inactive relationships are needed (activated via USERELATIONSHIP).",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-027",
                rule="Many inactive relationships",
                category="Model",
                severity="LOW",
                status="PASS",
                details=f"{len(inactive_rels)} inactive relationship(s) — acceptable.",
            )
        )

    # SM-028: Format strings (from DMV measures)
    no_fmt_dmv = [
        m
        for m in measures
        if not (m.get("formatString") or "").strip()
        and (m.get("expression") or "").strip()
    ]
    if no_fmt_dmv:
        rules.append(
            RuleResult(
                id="SM-028",
                rule="Format strings (DMV)",
                category="Model",
                severity="LOW",
                status="WARN",
                details=f"{len(no_fmt_dmv)} measure(s) without format strings in DMV.",
                recommendation="Add format strings for consistent user experience.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-028",
                rule="Format strings (DMV)",
                category="Model",
                severity="LOW",
                status="PASS",
                details="All DMV measures have format strings.",
            )
        )

    # SM-029: Pseudo-hierarchies
    # Detect columns that look like hierarchical levels in the same table
    hierarchy_patterns = [
        re.compile(r"(level|lvl)\s*[_\s]*\d", re.IGNORECASE),
        re.compile(r"(category|subcategory|sub_category)", re.IGNORECASE),
        re.compile(r"(group|subgroup|sub_group)", re.IGNORECASE),
        re.compile(r"(region|country|state|city)", re.IGNORECASE),
    ]
    tables_with_hierarchies: List[str] = []
    for d in dimensions:
        dim_name = d.get("dimensionUniqueName", "")
        matching_hier_cols = 0
        for h in dmv_hierarchies_rows:
            h_dim = h.get("DIMENSION_UNIQUE_NAME", h.get("[DIMENSION_UNIQUE_NAME]", ""))
            if h_dim == dim_name:
                h_name = h.get("HIERARCHY_UNIQUE_NAME", h.get("[HIERARCHY_UNIQUE_NAME]", ""))
                for p in hierarchy_patterns:
                    if p.search(h_name):
                        matching_hier_cols += 1
                        break
        if matching_hier_cols >= 3:
            tables_with_hierarchies.append(dim_name)

    if tables_with_hierarchies:
        rules.append(
            RuleResult(
                id="SM-029",
                rule="Pseudo-hierarchies detected",
                category="Model",
                severity="LOW",
                status="WARN",
                details=f"Tables with potential hierarchies: {', '.join(tables_with_hierarchies[:3])}",
                recommendation="Define explicit hierarchies in the model for better drill-down UX.",
            )
        )
    else:
        rules.append(
            RuleResult(
                id="SM-029",
                rule="Pseudo-hierarchies detected",
                category="Model",
                severity="LOW",
                status="PASS",
                details="No pseudo-hierarchies detected.",
            )
        )

    # ====================================================================
    # COLUMNSTATISTICS-based BPA — SM-B01..SM-B14
    # ====================================================================

    bpa_to_rule: Dict[str, Dict[str, str]] = {
        "Wide Table": {"id": "SM-B01", "cat": "BPA-Schema"},
        "High Cardinality Text Column": {"id": "SM-B02", "cat": "BPA-Memory"},
        "Constant Column": {"id": "SM-B03", "cat": "BPA-Bloat"},
        "Boolean Stored as Text": {"id": "SM-B04", "cat": "BPA-DataType"},
        "Date Stored as Text": {"id": "SM-B05", "cat": "BPA-DataType"},
        "Numeric Column Stored as Text": {"id": "SM-B06", "cat": "BPA-DataType"},
        "Low Cardinality Column in Fact Table": {"id": "SM-B07", "cat": "BPA-Schema"},
        "Description/Comment Column": {"id": "SM-B08", "cat": "BPA-Memory"},
        "No Integer Surrogate Key": {"id": "SM-B09", "cat": "BPA-Schema"},
        "Extremely Wide Table": {"id": "SM-B10", "cat": "BPA-Schema"},
        "Single Column Table": {"id": "SM-B11", "cat": "BPA-Schema"},
        "Multiple High-Cardinality Columns": {"id": "SM-B12", "cat": "BPA-Memory"},
        "Column Name Starts with Underscore": {"id": "SM-B13", "cat": "BPA-Naming"},
        "Nearly Unique Numeric Column": {"id": "SM-B14", "cat": "BPA-Schema"},
    }

    if col_stats:
        bpa_findings = run_bpa_rules(col_stats)
        # Group by rule name
        bpa_grouped: Dict[str, List[Dict[str, Any]]] = {}
        for f in bpa_findings:
            bpa_grouped.setdefault(f["rule"], []).append(f)

        used_ids: set = set()
        for rule_name, findings_list in bpa_grouped.items():
            mapping = bpa_to_rule.get(rule_name)
            if mapping:
                rid = mapping["id"]
                cat = mapping["cat"]
                used_ids.add(rid)
                severity = findings_list[0].get("severity", "MEDIUM")
                detail_lines = [
                    f"{f.get('table','')}.{f.get('column','')}: {f.get('detail','')}"
                    for f in findings_list[:5]
                ]
                extra = f" (+{len(findings_list)-5} more)" if len(findings_list) > 5 else ""
                rules.append(
                    RuleResult(
                        id=rid,
                        rule=rule_name,
                        category=cat,
                        severity=severity,
                        status="FAIL" if severity == "HIGH" else "WARN",
                        details="; ".join(detail_lines) + extra,
                        recommendation=findings_list[0].get("detail", ""),
                    )
                )

        # PASS rules not hit
        for rule_name, mapping in bpa_to_rule.items():
            if mapping["id"] not in used_ids:
                rules.append(
                    RuleResult(
                        id=mapping["id"],
                        rule=rule_name,
                        category=mapping["cat"],
                        severity="LOW",
                        status="PASS",
                        details=f"No {rule_name.lower()} issues detected.",
                    )
                )

        # Add overview sections
        header_sections.append("\n### Table Overview\n" + build_table_overview(col_stats))
        header_sections.append("\n### Memory Hotspots (Top 20)\n" + build_memory_hotspots_table(col_stats))
        header_sections.append("\n### Cardinality Distribution\n" + build_cardinality_distribution(col_stats))
    else:
        # ---- METADATA-based BPA (fallback when COLUMNSTATISTICS unavailable) ----
        header_sections.append("⚠️ COLUMNSTATISTICS unavailable — running metadata-only BPA.")

        # SM-B02 from MDSCHEMA hierarchies (high cardinality)
        high_card_hierarchies = [
            h
            for h in mdschema_cols
            if _safe_int(h.get("HIERARCHY_CARDINALITY", h.get("[HIERARCHY_CARDINALITY]", 0))) > 10000
        ]
        if high_card_hierarchies:
            names = ", ".join(
                _safe_str(h.get("HIERARCHY_CAPTION") or h.get("[HIERARCHY_CAPTION]"))
                for h in high_card_hierarchies[:5]
            )
            rules.append(
                RuleResult(
                    id="SM-B02",
                    rule="High Cardinality Text Column",
                    category="BPA-Memory",
                    severity="HIGH",
                    status="WARN",
                    details=f"{len(high_card_hierarchies)} high-cardinality column(s): {names}",
                    recommendation="Consider removing or hashing high-cardinality text columns.",
                )
            )
        else:
            rules.append(
                RuleResult(
                    id="SM-B02",
                    rule="High Cardinality Text Column",
                    category="BPA-Memory",
                    severity="HIGH",
                    status="PASS",
                    details="No high-cardinality text columns detected from metadata.",
                )
            )

        # SM-B03 constant columns — can't detect from metadata alone
        rules.append(
            RuleResult(
                id="SM-B03",
                rule="Constant Column",
                category="BPA-Bloat",
                severity="LOW",
                status="N/A",
                details="Cannot detect constant columns without COLUMNSTATISTICS.",
            )
        )

        # SM-B05 date stored as text — check INFO.COLUMNS for string columns with date names
        date_text_cols = [
            c
            for c in info_columns
            if _DATE_PATTERN.search(_safe_str(c.get("ExplicitName", c.get("[ExplicitName]", ""))))
            and str(c.get("ExplicitDataType", c.get("[ExplicitDataType]", ""))).lower() in ("string", "6", "text", "wstr")
        ]
        if date_text_cols:
            rules.append(
                RuleResult(
                    id="SM-B05",
                    rule="Date Stored as Text",
                    category="BPA-DataType",
                    severity="MEDIUM",
                    status="WARN",
                    details=f"{len(date_text_cols)} date-named columns stored as text.",
                    recommendation="Convert to proper Date/DateTime type.",
                )
            )
        else:
            rules.append(
                RuleResult(
                    id="SM-B05",
                    rule="Date Stored as Text",
                    category="BPA-DataType",
                    severity="MEDIUM",
                    status="PASS",
                    details="No date-as-text issues detected from metadata.",
                )
            )

        # SM-B06 numeric stored as text
        num_text_cols = [
            c
            for c in info_columns
            if _NUMERIC_PATTERN.search(_safe_str(c.get("ExplicitName", c.get("[ExplicitName]", ""))))
            and str(c.get("ExplicitDataType", c.get("[ExplicitDataType]", ""))).lower() in ("string", "6", "text", "wstr")
        ]
        if num_text_cols:
            rules.append(
                RuleResult(
                    id="SM-B06",
                    rule="Numeric Column Stored as Text",
                    category="BPA-DataType",
                    severity="MEDIUM",
                    status="WARN",
                    details=f"{len(num_text_cols)} numeric-named columns stored as text.",
                    recommendation="Convert to proper numeric type.",
                )
            )
        else:
            rules.append(
                RuleResult(
                    id="SM-B06",
                    rule="Numeric Column Stored as Text",
                    category="BPA-DataType",
                    severity="MEDIUM",
                    status="PASS",
                    details="No numeric-as-text issues detected from metadata.",
                )
            )

        # SM-B07 low cardinality (N/A without COLUMNSTATISTICS)
        rules.append(
            RuleResult(
                id="SM-B07",
                rule="Low Cardinality Column in Fact Table",
                category="BPA-Schema",
                severity="LOW",
                status="N/A",
                details="Cannot detect without COLUMNSTATISTICS.",
            )
        )

        # SM-B08 description columns
        desc_cols = [
            c
            for c in info_columns
            if _DESC_PATTERN.search(_safe_str(c.get("ExplicitName", c.get("[ExplicitName]", ""))))
        ]
        if desc_cols:
            rules.append(
                RuleResult(
                    id="SM-B08",
                    rule="Description/Comment Column",
                    category="BPA-Memory",
                    severity="MEDIUM",
                    status="WARN",
                    details=f"{len(desc_cols)} description/comment column(s) found. Consider hiding.",
                    recommendation="Hide or remove description columns to reduce model memory.",
                )
            )
        else:
            rules.append(
                RuleResult(
                    id="SM-B08",
                    rule="Description/Comment Column",
                    category="BPA-Memory",
                    severity="MEDIUM",
                    status="PASS",
                    details="No description/comment columns detected.",
                )
            )

        # SM-B09 no integer surrogate key (from INFO.COLUMNS)
        rules.append(
            RuleResult(
                id="SM-B09",
                rule="No Integer Surrogate Key",
                category="BPA-Schema",
                severity="MEDIUM",
                status="N/A",
                details="Cannot accurately detect without COLUMNSTATISTICS.",
            )
        )

        # SM-B10 extremely wide table — from dimensions cardinality proxy (not exact)
        wide_dims = [d for d in dimensions if d.get("cardinality", 0) > 100]
        if wide_dims:
            rules.append(
                RuleResult(
                    id="SM-B10",
                    rule="Extremely Wide Table",
                    category="BPA-Schema",
                    severity="HIGH",
                    status="WARN",
                    details=f"{len(wide_dims)} potentially large table(s).",
                    recommendation="Consider normalization for very wide tables.",
                )
            )
        else:
            rules.append(
                RuleResult(
                    id="SM-B10",
                    rule="Extremely Wide Table",
                    category="BPA-Schema",
                    severity="HIGH",
                    status="PASS",
                    details="No extremely wide tables detected.",
                )
            )

        # SM-B12 multiple high-card columns
        rules.append(
            RuleResult(
                id="SM-B12",
                rule="Multiple High-Cardinality Columns",
                category="BPA-Memory",
                severity="HIGH",
                status="N/A",
                details="Cannot detect without COLUMNSTATISTICS.",
            )
        )

        # SM-B13 underscore columns from INFO
        underscore_cols = [
            c
            for c in info_columns
            if _safe_str(c.get("ExplicitName", c.get("[ExplicitName]", ""))).startswith("_")
        ]
        if underscore_cols:
            rules.append(
                RuleResult(
                    id="SM-B13",
                    rule="Column Name Starts with Underscore",
                    category="BPA-Naming",
                    severity="LOW",
                    status="WARN",
                    details=f"{len(underscore_cols)} column(s) start with underscore.",
                    recommendation="Consider hiding or renaming internal columns.",
                )
            )
        else:
            rules.append(
                RuleResult(
                    id="SM-B13",
                    rule="Column Name Starts with Underscore",
                    category="BPA-Naming",
                    severity="LOW",
                    status="PASS",
                    details="No underscore-prefixed columns found.",
                )
            )

    # ---- Render final report ----
    return render_rule_report(
        title=f"Semantic Model Optimization — {model_name}",
        scan_time=scan_time,
        header_sections=header_sections,
        rules=rules,
    )


# ---------------------------------------------------------------------------
# 6. Fix infrastructure
# ---------------------------------------------------------------------------


def infer_format_string(name: str, expr: str) -> str:
    """Infer a DAX format string from measure name/expression patterns."""
    name_lower = name.lower()
    expr_lower = (expr or "").lower()

    # Percentage
    if any(
        kw in name_lower
        for kw in ("pct", "percent", "rate", "ratio", "margin", "%", "share", "growth")
    ):
        return "0.00%"
    if "%" in expr_lower or "divide" in expr_lower:
        return "0.00%"

    # Currency
    if any(
        kw in name_lower
        for kw in ("revenue", "sales", "cost", "price", "amount", "budget", "profit", "value", "dollar", "eur", "usd", "gbp")
    ):
        return "$#,0.00"

    # Count / integer
    if any(kw in name_lower for kw in ("count", "cnt", "qty", "quantity", "num", "number", "total")):
        return "#,0"

    # Average / decimal
    if any(kw in name_lower for kw in ("avg", "average", "mean", "score", "rating", "index")):
        return "#,0.00"

    # Default numeric
    return "#,0"


def apply_dax_fix(expr: str, rule_id: str) -> Optional[str]:
    """Apply a regex-based DAX fix to an expression. Returns modified expr or None if no change."""
    if not expr:
        return None

    original = expr

    if rule_id == "SM-FIX-IFERROR":
        # Replace IFERROR(x, y) → IF(ISERROR(x), y, x) — simplistic
        # More practical: IFERROR(x, BLANK()) → just x with DIVIDE wrapping
        expr = re.sub(
            r"\bIFERROR\s*\(\s*(.+?)\s*,\s*BLANK\s*\(\s*\)\s*\)",
            r"\1",
            expr,
            flags=re.IGNORECASE,
        )
        # IFERROR(x, 0) → IF(ISERROR(x), 0, x)
        expr = re.sub(
            r"\bIFERROR\s*\(\s*(.+?)\s*,\s*0\s*\)",
            r"IF(ISERROR(\1), 0, \1)",
            expr,
            flags=re.IGNORECASE,
        )

    elif rule_id == "SM-FIX-EVALLOG":
        # Remove EVALUATEANDLOG wrapper: EVALUATEANDLOG(x) → x
        expr = re.sub(
            r"\bEVALUATEANDLOG\s*\(\s*(.+?)\s*\)",
            r"\1",
            expr,
            flags=re.IGNORECASE,
        )

    elif rule_id == "SM-FIX-ADDZERO":
        # Remove + 0
        expr = re.sub(r"\+\s*0\b", "", expr)

    elif rule_id == "SM-FIX-SUMX":
        # SUMX(Table, Table[Col]) → SUM(Table[Col])
        expr = re.sub(
            r"\bSUMX\s*\(\s*(['\"]?\w+['\"]?)\s*,\s*\1\[([^\]]+)\]\s*\)",
            r"SUM(\1[\2])",
            expr,
            flags=re.IGNORECASE,
        )

    elif rule_id == "SM-FIX-REMOVEFILTERS":
        # Replace ALL(...) with REMOVEFILTERS(...) in CALCULATE context
        expr = re.sub(
            r"\bALL\s*\(",
            "REMOVEFILTERS(",
            expr,
            flags=re.IGNORECASE,
        )

    if expr != original:
        return expr
    return None


def _build_create_or_replace_measure(table_name: str, measure_name: str,
                                      expression: str,
                                      format_string: str = "",
                                      description: str = "") -> dict:
    """Build a TMSL createOrReplace command for a measure."""
    measure_def: Dict[str, Any] = {
        "name": measure_name,
        "expression": expression,
    }
    if format_string:
        measure_def["formatString"] = format_string
    if description:
        measure_def["description"] = description
    return {
        "createOrReplace": {
            "object": {
                "database": "",  # filled by caller
                "table": table_name,
                "measure": measure_name,
            },
            "measure": measure_def,
        }
    }


def apply_xmla_fixes(workspace_id: str, model_name: str, rule_ids: List[str]) -> str:
    """Apply fixes via XMLA/TMSL. Returns markdown result."""
    try:
        workspace = get_workspace(workspace_id)
        workspace_name = workspace.get("displayName", workspace_id)
    except Exception:
        workspace_name = workspace_id

    # Fetch DMV data
    try:
        dmv_data = run_xmla_dmv_queries(workspace_name, model_name, FIX_DMV_QUERIES)
    except Exception as e:
        return f"❌ Failed to query DMV: {e}"

    dmv_measures_rows = (dmv_data.get("measures", {}) or {}).get("rows", [])
    dmv_columns_rows = (dmv_data.get("columns", {}) or {}).get("rows", [])
    dmv_dimensions_rows = (dmv_data.get("dimensions", {}) or {}).get("rows", [])

    measures = parse_dmv_measures(dmv_measures_rows)
    columns = parse_dmv_columns(dmv_columns_rows)
    dimensions = parse_dmv_dimensions(dmv_dimensions_rows)

    results: List[str] = []
    commands_executed = 0
    errors: List[str] = []

    rule_set = set(rule_ids) if rule_ids else set()

    def _should_fix(rid: str) -> bool:
        return not rule_set or rid in rule_set

    # -- DAX measure fixes: IFERROR, EVALLOG, ADDZERO, SUMX, REMOVEFILTERS --
    dax_fix_rules = [
        ("SM-FIX-IFERROR", re.compile(r"\bI[FS]ERROR\b", re.IGNORECASE)),
        ("SM-FIX-EVALLOG", re.compile(r"\bEVALUATEANDLOG\b", re.IGNORECASE)),
        ("SM-FIX-ADDZERO", re.compile(r"\+\s*0(?:\b|$)")),
        ("SM-FIX-SUMX", re.compile(r"\bSUMX\s*\(\s*['\"]?\w+['\"]?\s*,\s*['\"]?\w+['\"]?\s*\[", re.IGNORECASE)),
        ("SM-FIX-REMOVEFILTERS", re.compile(r"\bALL\s*\(", re.IGNORECASE)),
    ]

    for rule_id, pattern in dax_fix_rules:
        if not _should_fix(rule_id):
            continue
        for m in measures:
            expr = m.get("expression", "") or ""
            if not pattern.search(expr):
                continue
            fixed = apply_dax_fix(expr, rule_id)
            if fixed is not None:
                cmd = {
                    "createOrReplace": {
                        "object": {
                            "database": model_name,
                            "table": m["measureGroupName"],
                            "measure": m["measureName"],
                        },
                        "measure": {
                            "name": m["measureName"],
                            "expression": fixed,
                            "formatString": m.get("formatString", ""),
                            "description": m.get("description", ""),
                        },
                    }
                }
                try:
                    execute_xmla_command_by_id(workspace_id, model_name, cmd)
                    commands_executed += 1
                    results.append(f"✅ {rule_id}: Fixed measure `{m['measureName']}`")
                except Exception as e:
                    errors.append(f"❌ {rule_id}: Failed to fix `{m['measureName']}`: {e}")

    # -- SM-FIX-FORMAT: Add format strings --
    if _should_fix("SM-FIX-FORMAT"):
        for m in measures:
            if (m.get("formatString") or "").strip():
                continue
            expr = m.get("expression", "") or ""
            if not expr.strip():
                continue
            fmt = infer_format_string(m["measureName"], expr)
            cmd = {
                "createOrReplace": {
                    "object": {
                        "database": model_name,
                        "table": m["measureGroupName"],
                        "measure": m["measureName"],
                    },
                    "measure": {
                        "name": m["measureName"],
                        "expression": expr,
                        "formatString": fmt,
                        "description": m.get("description", ""),
                    },
                }
            }
            try:
                execute_xmla_command_by_id(workspace_id, model_name, cmd)
                commands_executed += 1
                results.append(f"✅ SM-FIX-FORMAT: Added format `{fmt}` to `{m['measureName']}`")
            except Exception as e:
                errors.append(f"❌ SM-FIX-FORMAT: Failed for `{m['measureName']}`: {e}")

    # -- SM-FIX-MEASUREDESC: Add descriptions --
    if _should_fix("SM-FIX-MEASUREDESC"):
        for m in measures:
            if (m.get("description") or "").strip():
                continue
            expr = m.get("expression", "") or ""
            if not expr.strip():
                continue
            desc = f"Auto-generated: {m['measureName']} = {expr[:80]}{'...' if len(expr) > 80 else ''}"
            cmd = {
                "createOrReplace": {
                    "object": {
                        "database": model_name,
                        "table": m["measureGroupName"],
                        "measure": m["measureName"],
                    },
                    "measure": {
                        "name": m["measureName"],
                        "expression": expr,
                        "formatString": m.get("formatString", ""),
                        "description": desc,
                    },
                }
            }
            try:
                execute_xmla_command_by_id(workspace_id, model_name, cmd)
                commands_executed += 1
                results.append(f"✅ SM-FIX-MEASUREDESC: Added description to `{m['measureName']}`")
            except Exception as e:
                errors.append(f"❌ SM-FIX-MEASUREDESC: Failed for `{m['measureName']}`: {e}")

    # -- SM-FIX-MEASURENAME: Clean whitespace/tabs --
    if _should_fix("SM-FIX-MEASURENAME"):
        bad_name_re = re.compile(r"[\t]|^\s+|\s+$|  +")
        for m in measures:
            mn = m.get("measureName", "")
            if not bad_name_re.search(mn):
                continue
            clean_name = re.sub(r"\t", " ", mn)
            clean_name = re.sub(r"  +", " ", clean_name).strip()
            if clean_name == mn:
                continue
            cmd = {
                "createOrReplace": {
                    "object": {
                        "database": model_name,
                        "table": m["measureGroupName"],
                        "measure": mn,
                    },
                    "measure": {
                        "name": clean_name,
                        "expression": m.get("expression", ""),
                        "formatString": m.get("formatString", ""),
                        "description": m.get("description", ""),
                    },
                }
            }
            try:
                execute_xmla_command_by_id(workspace_id, model_name, cmd)
                commands_executed += 1
                results.append(f"✅ SM-FIX-MEASURENAME: Renamed `{mn}` → `{clean_name}`")
            except Exception as e:
                errors.append(f"❌ SM-FIX-MEASURENAME: Failed for `{mn}`: {e}")

    # -- SM-FIX-DIRECTREF: Delete duplicate measures --
    if _should_fix("SM-FIX-DIRECTREF"):
        ref_re = re.compile(r"^\s*\[[\w\s]+\]\s*$")
        for m in measures:
            expr = m.get("expression", "") or ""
            if ref_re.match(expr):
                cmd = {
                    "delete": {
                        "object": {
                            "database": model_name,
                            "table": m["measureGroupName"],
                            "measure": m["measureName"],
                        }
                    }
                }
                try:
                    execute_xmla_command_by_id(workspace_id, model_name, cmd)
                    commands_executed += 1
                    results.append(f"✅ SM-FIX-DIRECTREF: Deleted duplicate `{m['measureName']}`")
                except Exception as e:
                    errors.append(f"❌ SM-FIX-DIRECTREF: Failed for `{m['measureName']}`: {e}")

    # -- SM-FIX-DESC: Add table descriptions from DMV --
    if _should_fix("SM-FIX-DESC"):
        for d in dimensions:
            dim_name = d.get("dimensionUniqueName", "")
            if not dim_name:
                continue
            desc = d.get("description", "")
            if desc.strip():
                continue
            # Cannot easily set table descriptions via TMSL createOrReplace on measures
            # This would need an alter table command
            table_name = dim_name.replace("[", "").replace("]", "")
            auto_desc = f"Dimension table: {table_name} ({d.get('cardinality', 0):,} rows)"
            cmd = {
                "alter": {
                    "object": {
                        "database": model_name,
                        "table": table_name,
                    },
                    "table": {
                        "name": table_name,
                        "description": auto_desc,
                    },
                }
            }
            try:
                execute_xmla_command_by_id(workspace_id, model_name, cmd)
                commands_executed += 1
                results.append(f"✅ SM-FIX-DESC: Added description to table `{table_name}`")
            except Exception as e:
                errors.append(f"❌ SM-FIX-DESC: Failed for table `{table_name}`: {e}")

    # -- SM-FIX-DATE: Mark date tables --
    if _should_fix("SM-FIX-DATE"):
        date_re = re.compile(r"(date|calendar|time|period)", re.IGNORECASE)
        for d in dimensions:
            dim_name = d.get("dimensionUniqueName", "")
            if not date_re.search(dim_name):
                continue
            table_name = dim_name.replace("[", "").replace("]", "")
            cmd = {
                "alter": {
                    "object": {
                        "database": model_name,
                        "table": table_name,
                    },
                    "table": {
                        "name": table_name,
                        "dataCategory": "Time",
                    },
                }
            }
            try:
                execute_xmla_command_by_id(workspace_id, model_name, cmd)
                commands_executed += 1
                results.append(f"✅ SM-FIX-DATE: Marked `{table_name}` as Time table")
            except Exception as e:
                errors.append(f"❌ SM-FIX-DATE: Failed for `{table_name}`: {e}")

    # -- SM-FIX-HIDDEN: Hide FK columns (skipped — needs relationship metadata) --
    if _should_fix("SM-FIX-HIDDEN"):
        results.append("ℹ️ SM-FIX-HIDDEN: Skipped — requires relationship metadata via BIM/TMDL.")

    # -- SM-FIX-HIDEDESC: Hide description columns --
    if _should_fix("SM-FIX-HIDEDESC"):
        for c in columns:
            cn = c.get("columnName", "")
            if _DESC_PATTERN.search(cn):
                table_name = c.get("tableName", "")
                cmd = {
                    "alter": {
                        "object": {
                            "database": model_name,
                            "table": table_name,
                            "column": cn,
                        },
                        "column": {
                            "name": cn,
                            "isHidden": True,
                        },
                    }
                }
                try:
                    execute_xmla_command_by_id(workspace_id, model_name, cmd)
                    commands_executed += 1
                    results.append(f"✅ SM-FIX-HIDEDESC: Hid `{table_name}.{cn}`")
                except Exception as e:
                    errors.append(f"❌ SM-FIX-HIDEDESC: Failed for `{table_name}.{cn}`: {e}")

    # -- SM-FIX-HIDEGUID: Hide GUID/UUID columns --
    if _should_fix("SM-FIX-HIDEGUID"):
        for c in columns:
            cn = c.get("columnName", "")
            if _GUID_PATTERN.search(cn):
                table_name = c.get("tableName", "")
                cmd = {
                    "alter": {
                        "object": {
                            "database": model_name,
                            "table": table_name,
                            "column": cn,
                        },
                        "column": {
                            "name": cn,
                            "isHidden": True,
                        },
                    }
                }
                try:
                    execute_xmla_command_by_id(workspace_id, model_name, cmd)
                    commands_executed += 1
                    results.append(f"✅ SM-FIX-HIDEGUID: Hid `{table_name}.{cn}`")
                except Exception as e:
                    errors.append(f"❌ SM-FIX-HIDEGUID: Failed for `{table_name}.{cn}`: {e}")

    # -- SM-FIX-KEY: Skipped for XMLA (needs BIM) --
    if _should_fix("SM-FIX-KEY"):
        results.append("ℹ️ SM-FIX-KEY: Skipped — needs BIM/TMDL fallback to set IsKey.")

    # -- SM-FIX-AUTODATE: Remove auto-date tables --
    if _should_fix("SM-FIX-AUTODATE"):
        autodate_re = re.compile(r"^(DateTableTemplate|LocalDateTable|AutoDate)", re.IGNORECASE)
        for d in dimensions:
            dim_name = d.get("dimensionUniqueName", "").replace("[", "").replace("]", "")
            if autodate_re.match(dim_name):
                cmd = {
                    "delete": {
                        "object": {
                            "database": model_name,
                            "table": dim_name,
                        }
                    }
                }
                try:
                    execute_xmla_command_by_id(workspace_id, model_name, cmd)
                    commands_executed += 1
                    results.append(f"✅ SM-FIX-AUTODATE: Deleted auto-date table `{dim_name}`")
                except Exception as e:
                    errors.append(f"❌ SM-FIX-AUTODATE: Failed for `{dim_name}`: {e}")

    # -- SM-FIX-CONSTCOL: Skipped for XMLA (needs COLUMNSTATISTICS + BIM) --
    if _should_fix("SM-FIX-CONSTCOL"):
        results.append("ℹ️ SM-FIX-CONSTCOL: Skipped — needs BIM/TMDL fallback with COLUMNSTATISTICS data.")

    # -- SM-FIX-BIDI: Skipped (needs Tabular Editor) --
    if _should_fix("SM-FIX-BIDI"):
        results.append("ℹ️ SM-FIX-BIDI: Skipped — bidirectional relationship changes need Tabular Editor.")

    # -- SM-FIX-SUMMARIZE: Skipped (needs column-level TMSL) --
    if _should_fix("SM-FIX-SUMMARIZE"):
        results.append("ℹ️ SM-FIX-SUMMARIZE: Skipped — SummarizeBy changes need column-level TMSL not available via this path.")

    # Build summary
    lines = [f"## Semantic Model Fix Results — {model_name}"]
    lines.append(f"\n**Commands executed:** {commands_executed}")
    if errors:
        lines.append(f"**Errors:** {len(errors)}")
    lines.append("")
    lines.extend(results)
    if errors:
        lines.append("\n### Errors")
        lines.extend(errors)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. BIM / TMDL fallback fix
# ---------------------------------------------------------------------------


def _apply_bim_fixes(bim: Dict[str, Any], rule_ids: List[str]) -> int:
    """Apply fixes to a BIM (model.bim) JSON in-place. Returns count of changes."""
    changes = 0
    rule_set = set(rule_ids) if rule_ids else set()

    def _should(rid: str) -> bool:
        return not rule_set or rid in rule_set

    model = bim.get("model", bim)
    tables = model.get("tables", [])

    for table in tables:
        table_name = table.get("name", "")

        # SM-FIX-DESC: Add table descriptions
        if _should("SM-FIX-DESC"):
            if not table.get("description"):
                table["description"] = f"Table: {table_name}"
                changes += 1

        # SM-FIX-DATE: Mark date tables
        if _should("SM-FIX-DATE"):
            if re.search(r"(date|calendar|time|period)", table_name, re.IGNORECASE):
                if table.get("dataCategory") != "Time":
                    table["dataCategory"] = "Time"
                    changes += 1

        # SM-FIX-AUTODATE: Remove auto-date tables (mark for removal)
        # Done after the loop

        # Column-level fixes
        columns_list = table.get("columns", [])
        for col in columns_list:
            col_name = col.get("name", "")

            # SM-FIX-HIDDEN: Hide FK columns
            if _should("SM-FIX-HIDDEN"):
                if re.search(r"(id$|_id$|key$|_key$|fk_|_fk$)", col_name, re.IGNORECASE):
                    if not col.get("isHidden"):
                        col["isHidden"] = True
                        changes += 1

            # SM-FIX-KEY: Set IsKey
            if _should("SM-FIX-KEY"):
                if re.search(r"(^id$|_id$|key$|_key$)", col_name, re.IGNORECASE):
                    if not col.get("isKey"):
                        col["isKey"] = True
                        changes += 1

            # SM-FIX-HIDEDESC: Hide description columns
            if _should("SM-FIX-HIDEDESC"):
                if _DESC_PATTERN.search(col_name):
                    if not col.get("isHidden"):
                        col["isHidden"] = True
                        changes += 1

            # SM-FIX-HIDEGUID: Hide GUID columns
            if _should("SM-FIX-HIDEGUID"):
                if _GUID_PATTERN.search(col_name):
                    if not col.get("isHidden"):
                        col["isHidden"] = True
                        changes += 1

        # SM-FIX-CONSTCOL: Remove constant columns (heuristic — remove columns named 'constant' etc.)
        if _should("SM-FIX-CONSTCOL"):
            original_len = len(columns_list)
            table["columns"] = [
                c
                for c in columns_list
                if not re.search(r"^(constant|dummy|placeholder)$", c.get("name", ""), re.IGNORECASE)
            ]
            changes += original_len - len(table["columns"])

        # Measure-level fixes
        measure_list = table.get("measures", [])
        measures_to_remove: List[int] = []
        for idx, measure in enumerate(measure_list):
            m_name = measure.get("name", "")
            m_expr = measure.get("expression", "")
            if isinstance(m_expr, list):
                m_expr = "\n".join(m_expr)

            # SM-FIX-FORMAT: Add format strings
            if _should("SM-FIX-FORMAT"):
                if not measure.get("formatString"):
                    fmt = infer_format_string(m_name, m_expr)
                    measure["formatString"] = fmt
                    changes += 1

            # SM-FIX-MEASUREDESC: Add descriptions
            if _should("SM-FIX-MEASUREDESC"):
                if not measure.get("description"):
                    desc = f"Auto-generated: {m_name}"
                    measure["description"] = desc
                    changes += 1

            # SM-FIX-MEASURENAME: Clean names
            if _should("SM-FIX-MEASURENAME"):
                clean = re.sub(r"\t", " ", m_name)
                clean = re.sub(r"  +", " ", clean).strip()
                if clean != m_name:
                    measure["name"] = clean
                    changes += 1

            # SM-FIX-IFERROR
            if _should("SM-FIX-IFERROR"):
                fixed = apply_dax_fix(m_expr, "SM-FIX-IFERROR")
                if fixed is not None:
                    measure["expression"] = fixed
                    changes += 1

            # SM-FIX-EVALLOG
            if _should("SM-FIX-EVALLOG"):
                fixed = apply_dax_fix(m_expr if isinstance(m_expr, str) else measure.get("expression", ""), "SM-FIX-EVALLOG")
                if fixed is not None:
                    measure["expression"] = fixed
                    changes += 1

            # SM-FIX-ADDZERO
            if _should("SM-FIX-ADDZERO"):
                cur_expr = measure.get("expression", "")
                if isinstance(cur_expr, list):
                    cur_expr = "\n".join(cur_expr)
                fixed = apply_dax_fix(cur_expr, "SM-FIX-ADDZERO")
                if fixed is not None:
                    measure["expression"] = fixed
                    changes += 1

            # SM-FIX-SUMX
            if _should("SM-FIX-SUMX"):
                cur_expr = measure.get("expression", "")
                if isinstance(cur_expr, list):
                    cur_expr = "\n".join(cur_expr)
                fixed = apply_dax_fix(cur_expr, "SM-FIX-SUMX")
                if fixed is not None:
                    measure["expression"] = fixed
                    changes += 1

            # SM-FIX-DIRECTREF: Delete duplicate measures
            if _should("SM-FIX-DIRECTREF"):
                cur_expr = measure.get("expression", "")
                if isinstance(cur_expr, list):
                    cur_expr = "\n".join(cur_expr)
                ref_re = re.compile(r"^\s*\[[\w\s]+\]\s*$")
                if ref_re.match(cur_expr):
                    measures_to_remove.append(idx)
                    changes += 1

        # Remove duplicate measures (in reverse to preserve indices)
        for idx in sorted(measures_to_remove, reverse=True):
            measure_list.pop(idx)

    # SM-FIX-AUTODATE: Remove auto-date tables
    if _should("SM-FIX-AUTODATE"):
        autodate_re = re.compile(r"^(DateTableTemplate|LocalDateTable|AutoDate)", re.IGNORECASE)
        original_count = len(tables)
        model["tables"] = [t for t in tables if not autodate_re.match(t.get("name", ""))]
        changes += original_count - len(model["tables"])

    return changes


def _apply_tmdl_fixes(parts: List[Dict[str, Any]], rule_ids: List[str]) -> int:
    """Apply fixes to TMDL definition parts in-place. Returns count of changes."""
    changes = 0
    rule_set = set(rule_ids) if rule_ids else set()

    def _should(rid: str) -> bool:
        return not rule_set or rid in rule_set

    for part in parts:
        path = part.get("path", "")
        payload = part.get("payload", "")
        if not payload:
            continue

        try:
            content = base64.b64decode(payload).decode("utf-8")
        except Exception:
            continue

        original = content

        # SM-FIX-FORMAT: Add format strings to measures in TMDL
        if _should("SM-FIX-FORMAT"):
            # TMDL measures: "measure 'Name' = expression"
            def _add_format(match: re.Match) -> str:
                full = match.group(0)
                name = match.group(1)
                if "formatString" not in full:
                    fmt = infer_format_string(name, "")
                    return full.rstrip() + f"\n\t\tformatString: {fmt}"
                return full

            content = re.sub(
                r"measure\s+'([^']+)'\s*=\s*[^\n]+(?:\n\t\t[^\n]+)*",
                _add_format,
                content,
            )

        # SM-FIX-HIDEDESC: Hide description columns
        if _should("SM-FIX-HIDEDESC"):
            content = re.sub(
                r"(column\s+'(?:description|comment|note|remark|memo)[^']*'[^\n]*)",
                lambda m: m.group(0) + "\n\t\tisHidden",
                content,
                flags=re.IGNORECASE,
            )

        # SM-FIX-HIDEGUID: Hide GUID columns
        if _should("SM-FIX-HIDEGUID"):
            content = re.sub(
                r"(column\s+'(?:guid|uuid|uniqueidentifier)[^']*'[^\n]*)",
                lambda m: m.group(0) + "\n\t\tisHidden",
                content,
                flags=re.IGNORECASE,
            )

        if content != original:
            part["payload"] = base64.b64encode(content.encode("utf-8")).decode("ascii")
            changes += 1

    return changes


def semantic_model_fix_fallback(workspace_id: str, model_id: str, model_name: str,
                                 rule_ids: List[str]) -> str:
    """BIM/TMDL download → fix → upload fallback."""
    try:
        parts = get_semantic_model_definition(workspace_id, model_id)
    except Exception as e:
        return f"❌ Failed to download model definition: {e}"

    if not parts:
        return "❌ Model definition is empty."

    # Detect format: BIM vs TMDL
    bim_part = None
    for p in parts:
        path = (p.get("path") or "").lower()
        if path.endswith(".bim") or path.endswith("model.bim"):
            bim_part = p
            break

    changes = 0

    if bim_part:
        # BIM format
        try:
            payload = bim_part.get("payload", "")
            bim_json = json.loads(base64.b64decode(payload).decode("utf-8"))
        except Exception as e:
            return f"❌ Failed to parse BIM JSON: {e}"

        changes = _apply_bim_fixes(bim_json, rule_ids)

        if changes > 0:
            new_payload = base64.b64encode(
                json.dumps(bim_json, indent=2).encode("utf-8")
            ).decode("ascii")
            bim_part["payload"] = new_payload
            try:
                update_semantic_model_definition(workspace_id, model_id, parts)
            except Exception as e:
                return f"❌ Failed to upload fixed BIM: {e}"
    else:
        # TMDL format
        changes = _apply_tmdl_fixes(parts, rule_ids)
        if changes > 0:
            try:
                update_semantic_model_definition(workspace_id, model_id, parts)
            except Exception as e:
                return f"❌ Failed to upload fixed TMDL: {e}"

    fmt = "BIM" if bim_part else "TMDL"
    if changes == 0:
        return f"ℹ️ No changes needed ({fmt} format)."
    return f"✅ Applied {changes} fix(es) via {fmt} download/upload to **{model_name}**."


# ---------------------------------------------------------------------------
# 8. semantic_model_fix handler
# ---------------------------------------------------------------------------


def semantic_model_fix(args: dict) -> str:
    """Fix semantic model issues — tries XMLA first, falls back to BIM/TMDL."""
    workspace_id = args.get("workspaceId", "")
    model_id = args.get("semanticModelId", "")
    rule_ids = args.get("ruleIds", [])
    if not workspace_id or not model_id:
        return "❌ workspaceId and semanticModelId are required."

    # Resolve model name
    try:
        models = list_semantic_models(workspace_id)
        model_info = next((m for m in models if m.get("id") == model_id), None)
        model_name = model_info.get("displayName", model_id) if model_info else model_id
    except Exception:
        model_name = model_id

    # Try XMLA first
    try:
        result = apply_xmla_fixes(workspace_id, model_name, rule_ids)
        if "❌" not in result.split("\n")[0]:
            return result
    except Exception:
        pass

    # Fallback to BIM/TMDL
    return semantic_model_fix_fallback(workspace_id, model_id, model_name, rule_ids)


# ---------------------------------------------------------------------------
# 9. semantic_model_auto_optimize handler
# ---------------------------------------------------------------------------


def semantic_model_auto_optimize(args: dict) -> str:
    """Auto-optimize: preview (dryRun) or apply all 19 fix rules."""
    workspace_id = args.get("workspaceId", "")
    model_id = args.get("semanticModelId", "")
    dry_run = args.get("dryRun", False)

    if not workspace_id or not model_id:
        return "❌ workspaceId and semanticModelId are required."

    if dry_run:
        lines = ["## Auto-Optimize — Dry Run (19 rules)\n"]
        lines.append("| # | Rule ID | Description |")
        lines.append("| ---:| --- | --- |")
        for i, r in enumerate(ALL_FIX_RULES, 1):
            lines.append(f"| {i} | `{r['id']}` | {r['desc']} |")
        lines.append(
            "\nSet `dryRun: false` to apply all fixes."
        )
        return "\n".join(lines)

    all_ids = [r["id"] for r in ALL_FIX_RULES]
    return semantic_model_fix({
        "workspaceId": workspace_id,
        "semanticModelId": model_id,
        "ruleIds": all_ids,
    })


# ---------------------------------------------------------------------------
# 10. Tool exports
# ---------------------------------------------------------------------------

semantic_model_tools = [
    {
        "name": "semantic_model_list",
        "description": "List all semantic models in a Fabric workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace.",
                },
            },
            "required": ["workspaceId"],
        },
        "handler": semantic_model_list,
    },
    {
        "name": "semantic_model_optimization_recommendations",
        "description": (
            "LIVE SCAN: Connects to a Fabric Semantic Model and executes DAX queries "
            "(COLUMNSTATISTICS) to analyze the actual model. Runs Best Practice Analyzer "
            "rules to detect high-cardinality text columns, constant columns, booleans/"
            "dates/numbers stored as text, wide tables, string keys, description columns, "
            "bidirectional relationships, implicit measures, disconnected tables, ALL() vs "
            "REMOVEFILTERS(), excessive USERELATIONSHIP, complex relationship webs, inactive "
            "relationships, missing format strings, and pseudo-hierarchies. Returns memory "
            "hotspots and prioritized fixes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace.",
                },
                "semanticModelId": {
                    "type": "string",
                    "description": "The ID of the semantic model to analyze.",
                },
            },
            "required": ["workspaceId", "semanticModelId"],
        },
        "handler": semantic_model_optimization_recommendations,
    },
    {
        "name": "semantic_model_fix",
        "description": (
            "AUTO-FIX: Uses XMLA/TMSL commands for atomic per-object fixes (measures, columns, "
            "tables). Falls back to download/upload (BIM/TMDL) if XMLA endpoint is unavailable. "
            "19 fix rules: SM-FIX-FORMAT, SM-FIX-DESC, SM-FIX-HIDDEN, SM-FIX-DATE, SM-FIX-KEY, "
            "SM-FIX-AUTODATE, SM-FIX-IFERROR, SM-FIX-EVALLOG, SM-FIX-ADDZERO, SM-FIX-DIRECTREF, "
            "SM-FIX-SUMX, SM-FIX-MEASUREDESC, SM-FIX-MEASURENAME, SM-FIX-HIDEDESC, SM-FIX-HIDEGUID, "
            "SM-FIX-CONSTCOL, SM-FIX-BIDI, SM-FIX-SUMMARIZE, SM-FIX-REMOVEFILTERS."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace.",
                },
                "semanticModelId": {
                    "type": "string",
                    "description": "The ID of the semantic model to fix.",
                },
                "ruleIds": {
                    "type": "array",
                    "description": (
                        "Optional: specific fix IDs to apply. If omitted, all safe fixes are applied."
                    ),
                },
            },
            "required": ["workspaceId", "semanticModelId"],
        },
        "handler": semantic_model_fix,
    },
    {
        "name": "semantic_model_auto_optimize",
        "description": (
            "AUTO-OPTIMIZE: Applies all 19 safe fixes to a Semantic Model using XMLA/TMSL "
            "commands (falls back to download/upload if XMLA is unavailable). Covers: DAX fixes "
            "(IFERROR, EVALUATEANDLOG, +0, direct refs, SUMX→SUM, ALL→REMOVEFILTERS), model fixes "
            "(format strings, descriptions, date tables, IsKey, hidden MDX, auto-date tables, "
            "bidirectional relationships, SummarizeBy), and bloat fixes (hide description/GUID "
            "columns, remove constants, clean measure names). Use dryRun=true to preview."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace.",
                },
                "semanticModelId": {
                    "type": "string",
                    "description": "The ID of the semantic model to optimize.",
                },
                "dryRun": {
                    "type": "boolean",
                    "description": "If true, preview fixes without applying (default: false).",
                },
            },
            "required": ["workspaceId", "semanticModelId"],
        },
        "handler": semantic_model_auto_optimize,
    },
]
