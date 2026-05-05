"""Tool handlers for Fabric workspace operations."""

from datetime import datetime, timezone
from typing import Dict, List

from clients.fabric_client import (
    list_workspaces,
    get_workspace,
    list_workspace_items,
    list_capacities,
    list_lakehouses,
    list_warehouses,
    list_eventhouses,
    list_semantic_models,
)


# ──────────────────────────────────────────────
# Tool: workspace_list
# ──────────────────────────────────────────────

def workspace_list(args: dict) -> str:
    try:
        workspaces = list_workspaces()

        if not workspaces:
            return "No workspaces found. Ensure you have access to at least one Fabric workspace."

        lines: List[str] = []
        for ws in workspaces:
            parts = [
                f"- **{ws['displayName']}** (ID: {ws['id']})",
                f"  Type: {ws.get('type', 'N/A')}",
            ]
            if ws.get("capacityId"):
                parts.append(f"  Capacity: {ws['capacityId']}")
            if ws.get("description"):
                parts.append(f"  Description: {ws['description']}")
            lines.append("\n".join(parts))

        return f"## Your Fabric Workspaces\n\nTotal: {len(workspaces)}\n\n" + "\n\n".join(lines)
    except Exception as e:
        return f"❌ Failed to list workspaces: {e}"


# ──────────────────────────────────────────────
# Tool: workspace_list_items
# ──────────────────────────────────────────────

def workspace_list_items(args: dict) -> str:
    try:
        workspace_id = args["workspaceId"]
        item_type = args.get("itemType")

        workspace = get_workspace(workspace_id)
        items = list_workspace_items(workspace_id, item_type)

        if not items:
            filter_text = f' of type "{item_type}"' if item_type else ""
            return f'No items{filter_text} found in workspace "{workspace["displayName"]}".'

        # Group items by type
        grouped: Dict[str, list] = {}
        for item in items:
            t = item.get("type", "Unknown")
            grouped.setdefault(t, []).append(item)

        sections: List[str] = []
        for item_type_key, type_items in grouped.items():
            item_lines = [f"  - {it['displayName']} (ID: {it['id']})" for it in type_items]
            sections.append(f"### {item_type_key} ({len(type_items)})\n" + "\n".join(item_lines))

        return "\n".join([
            f'## Items in workspace "{workspace["displayName"]}"',
            "",
            f"Total: {len(items)} item(s)",
            "",
            *sections,
        ])
    except Exception as e:
        return f"❌ Failed to list workspace items: {e}"


# ──────────────────────────────────────────────
# Tool: workspace_capacity_info
# ──────────────────────────────────────────────

def workspace_capacity_info(args: dict) -> str:
    try:
        capacities = list_capacities()

        if not capacities:
            return "No capacities found or you don't have access to view capacity information."

        cap_lines: List[str] = []
        for cap in capacities:
            cap_lines.append("\n".join([
                f"- **{cap['displayName']}** (ID: {cap['id']})",
                f"  SKU: {cap.get('sku', 'N/A')}",
                f"  State: {cap.get('state', 'N/A')}",
                f"  Region: {cap.get('region', 'N/A')}",
            ]))

        return "\n".join([
            "## Fabric Capacities",
            "",
            f"Total: {len(capacities)}",
            "",
            *cap_lines,
            "",
            "### Capacity Optimization Tips",
            "",
            "- **Right-size your capacity**: Monitor CU utilization in the Capacity Metrics app",
            "- **Use autoscale**: Enable capacity autoscale for bursty workloads",
            "- **Pause unused capacities**: Save costs by pausing dev/test capacities when not in use",
            "- **Smoothing**: Fabric smooths CU consumption over 24h windows — short spikes are acceptable",
            "- **Throttling**: If >100% utilization is sustained, background jobs are throttled, then interactive queries",
        ])
    except Exception as e:
        return f"❌ Failed to get capacity info: {e}"


# ──────────────────────────────────────────────
# Tool: fabric_optimization_report
# ──────────────────────────────────────────────

def fabric_optimization_report(args: dict) -> str:
    try:
        workspace_id = args["workspaceId"]
        workspace = get_workspace(workspace_id)

        # Fetch all item types (catch errors individually)
        try:
            lakehouses = list_lakehouses(workspace_id)
        except Exception:
            lakehouses = []
        try:
            warehouses = list_warehouses(workspace_id)
        except Exception:
            warehouses = []
        try:
            eventhouses = list_eventhouses(workspace_id)
        except Exception:
            eventhouses = []
        try:
            semantic_models = list_semantic_models(workspace_id)
        except Exception:
            semantic_models = []
        try:
            all_items = list_workspace_items(workspace_id)
        except Exception:
            all_items = []

        now = datetime.now(timezone.utc).isoformat()

        report: List[str] = [
            "# Fabric Optimization Report",
            f"## Workspace: {workspace['displayName']}",
            "",
            f"Generated: {now}",
            "",
            "---",
            "",
            "## 📋 Inventory Summary",
            "",
            "| Item Type | Count |",
            "|-----------|-------|",
            f"| Lakehouses | {len(lakehouses)} |",
            f"| Warehouses | {len(warehouses)} |",
            f"| Eventhouses | {len(eventhouses)} |",
            f"| Semantic Models | {len(semantic_models)} |",
            f"| Total Items | {len(all_items)} |",
            "",
        ]

        # Lakehouse section
        if lakehouses:
            report.extend(["---", "", "## 🏠 Lakehouse Optimization", ""])
            for lh in lakehouses:
                report.extend([
                    f"### {lh['displayName']}",
                    "",
                    "**Action Items:**",
                    "- [ ] Run OPTIMIZE with V-Order on all tables (`lakehouse_run_table_maintenance`)",
                    "- [ ] Run VACUUM to clean up old files",
                    "- [ ] Review partition strategy for tables > 1 GB",
                    "- [ ] Check for small files problem (many files < 128 MB)",
                    "- [ ] Verify Z-ORDER on frequently filtered columns",
                    "- [ ] Use `lakehouse_optimization_recommendations` for detailed analysis",
                    "",
                ])

        # Warehouse section
        if warehouses:
            report.extend(["---", "", "## 🏭 Warehouse Optimization", ""])
            for wh in warehouses:
                report.extend([
                    f"### {wh['displayName']}",
                    "",
                    "**Action Items:**",
                    "- [ ] Review Query Insights for slow/frequent queries",
                    "- [ ] Verify statistics are up to date on key columns",
                    "- [ ] Check for proper data types (narrow types preferred)",
                    "- [ ] Review batch loading patterns (avoid small inserts)",
                    "- [ ] Rebuild columnstore indexes after bulk modifications",
                    "- [ ] Use `warehouse_optimization_recommendations` for detailed analysis",
                    "",
                ])

        # Eventhouse section
        if eventhouses:
            report.extend(["---", "", "## ⚡ Eventhouse Optimization", ""])
            for eh in eventhouses:
                report.extend([
                    f"### {eh['displayName']}",
                    "",
                    "**Action Items:**",
                    "- [ ] Review caching policies — hot cache should cover common query ranges",
                    "- [ ] Verify retention policies match data lifecycle requirements",
                    "- [ ] Check ingestion batching configuration",
                    "- [ ] Evaluate materialized views for common aggregation patterns",
                    "- [ ] Review partitioning policy for large tables",
                    "- [ ] Merge small extents if present",
                    "- [ ] Use `eventhouse_optimization_recommendations` for detailed analysis",
                    "",
                ])

        # Semantic Model section
        if semantic_models:
            report.extend(["---", "", "## 📊 Semantic Model Optimization", ""])
            for sm in semantic_models:
                report.extend([
                    f"### {sm['displayName']}",
                    "",
                    "**Action Items:**",
                    "- [ ] Remove unused columns to reduce model size",
                    "- [ ] Verify star schema design (fact + dimension tables)",
                    "- [ ] Consider DirectLake mode for Fabric-native access",
                    "- [ ] Set up incremental refresh for large tables",
                    "- [ ] Review DAX measures for optimization opportunities",
                    "- [ ] Ensure integer surrogate keys for relationships",
                    "- [ ] Use `semantic_model_optimization_recommendations` for detailed analysis",
                    "",
                ])

        # General recommendations
        report.extend([
            "---",
            "",
            "## 🎯 Cross-Cutting Recommendations",
            "",
            "### Capacity Management",
            "- Monitor CU consumption via the Capacity Metrics app",
            "- Schedule heavy jobs (refreshes, maintenance) during off-peak hours",
            "- Consider separate capacities for dev/test vs production",
            "",
            "### Data Architecture",
            "- Use **medallion architecture** (Bronze → Silver → Gold) in Lakehouses",
            "- Leverage **shortcuts** to avoid data duplication across workspaces",
            "- Use the **Warehouse** for complex SQL analytics, **Lakehouse** for data engineering",
            "",
            "### Security & Governance",
            "- Implement workspace-level access control",
            "- Use service principals for automated workloads",
            "- Enable data lineage tracking through Microsoft Purview",
            "",
            "### Cost Optimization",
            "- Right-size capacity SKU based on actual usage patterns",
            "- Pause development capacities after hours",
            "- Clean up unused items and old data to reduce storage costs",
            "- Use OneLake data compaction (V-Order) to reduce storage volume",
        ])

        return "\n".join(report)
    except Exception as e:
        return f"❌ Failed to generate optimization report: {e}"


# ──────────────────────────────────────────────
# Tool definitions for MCP registration
# ──────────────────────────────────────────────

workspace_tools = [
    {
        "name": "workspace_list",
        "description": "List all Fabric workspaces you have access to with their IDs, types, and capacity assignments.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": workspace_list,
    },
    {
        "name": "workspace_list_items",
        "description": (
            "List all items in a Fabric workspace, optionally filtered by type (Lakehouse, Warehouse, "
            "Notebook, Pipeline, SemanticModel, Report, etc.). Items are grouped by type."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace",
                },
                "itemType": {
                    "type": "string",
                    "description": (
                        "Optional: filter by item type (e.g., Lakehouse, Warehouse, Notebook, SemanticModel, "
                        "Pipeline, Report, Eventhouse, KQLDatabase, Dashboard, Dataflow)"
                    ),
                },
            },
            "required": ["workspaceId"],
        },
        "handler": workspace_list_items,
    },
    {
        "name": "workspace_capacity_info",
        "description": "List Fabric capacities with their SKU, state, and region. Includes capacity optimization tips.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": workspace_capacity_info,
    },
    {
        "name": "fabric_optimization_report",
        "description": (
            "Generate a comprehensive optimization report for an entire Fabric workspace. "
            "Scans all Lakehouses, Warehouses, Eventhouses, and Semantic Models and provides "
            "a checklist of optimization action items for each item, plus cross-cutting recommendations "
            "for capacity management, data architecture, security, and cost optimization."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspaceId": {
                    "type": "string",
                    "description": "The ID of the Fabric workspace to analyze",
                },
            },
            "required": ["workspaceId"],
        },
        "handler": fabric_optimization_report,
    },
]
