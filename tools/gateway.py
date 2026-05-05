"""Tool handlers for Fabric gateway and connection operations."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from clients.fabric_client import (
    list_gateways,
    list_connections,
    list_gateway_datasources,
    get_gateway_datasource_status,
    list_gateway_datasource_users,
    delete_gateway_datasource,
    delete_gateway_datasource_user,
    delete_connection,
)
from tools.rule_engine import RuleResult, render_rule_report


# ──────────────────────────────────────────────
# Tool: gateway_list
# ──────────────────────────────────────────────

def gateway_list(args: dict) -> str:
    try:
        gateways = list_gateways()

        if not gateways:
            return "No gateways found."

        lines: List[str] = []
        for gw in gateways:
            parts = [
                f"- **{gw['displayName']}** (ID: {gw['id']})",
                f"  Type: {gw.get('type', 'N/A')}",
            ]
            if gw.get("gatewayStatus"):
                parts.append(f"  Status: {gw['gatewayStatus']}")
            if gw.get("version"):
                parts.append(f"  Version: {gw['version']}")
            vnet = gw.get("virtualNetworkAzureResource")
            if vnet:
                parts.append(
                    f"  VNet: {vnet.get('virtualNetworkName', '')}/{vnet.get('subnetName', '')}"
                )
            lines.append("\n".join(parts))

        return f"## Gateways\n\n" + "\n\n".join(lines)
    except Exception as e:
        return f"❌ Failed to list gateways: {e}"


# ──────────────────────────────────────────────
# Tool: gateway_list_connections
# ──────────────────────────────────────────────

def gateway_list_connections(args: dict) -> str:
    try:
        connections = list_connections()

        if not connections:
            return "No connections found."

        lines: List[str] = []
        for conn in connections:
            display = conn.get("displayName") or "(unnamed)"
            parts = [
                f"- **{display}** (ID: {conn['id']})",
                f"  Connectivity Type: {conn.get('connectivityType', 'N/A')}",
            ]
            if conn.get("gatewayId"):
                parts.append(f"  Gateway ID: {conn['gatewayId']}")
            if conn.get("privacyLevel"):
                parts.append(f"  Privacy Level: {conn['privacyLevel']}")
            lines.append("\n".join(parts))

        return f"## Connections\n\n" + "\n\n".join(lines)
    except Exception as e:
        return f"❌ Failed to list connections: {e}"


# ──────────────────────────────────────────────
# Diagnostics collection
# ──────────────────────────────────────────────

class _GatewayDiagnostics:
    """Container for collected gateway diagnostic data."""
    def __init__(self) -> None:
        self.gateways: List[dict] = []
        self.connections: List[dict] = []
        self.datasources_by_gateway: Dict[str, List[dict]] = {}
        self.users_by_datasource: Dict[str, List[dict]] = {}
        self.status_by_datasource: Dict[str, str] = {}


def _collect_diagnostics() -> _GatewayDiagnostics:
    diag = _GatewayDiagnostics()
    diag.gateways = list_gateways()
    diag.connections = list_connections()

    # Only fetch datasources for non-Personal gateways
    eligible = [gw for gw in diag.gateways if gw.get("type") != "Personal"]

    for gw in eligible:
        gw_id = gw["id"]
        try:
            datasources = list_gateway_datasources(gw_id)
        except Exception:
            datasources = []
        diag.datasources_by_gateway[gw_id] = datasources

        for ds in datasources:
            ds_key = f"{gw_id}|{ds['id']}"
            try:
                status = get_gateway_datasource_status(gw_id, ds["id"])
                diag.status_by_datasource[ds_key] = status
            except Exception:
                diag.status_by_datasource[ds_key] = "ERROR"
            try:
                users = list_gateway_datasource_users(gw_id, ds["id"])
                diag.users_by_datasource[ds_key] = users
            except Exception:
                diag.users_by_datasource[ds_key] = []

    return diag


# ──────────────────────────────────────────────
# Rule checks
# ──────────────────────────────────────────────

def _run_gateway_rules(diag: _GatewayDiagnostics) -> List[RuleResult]:
    rules: List[RuleResult] = []

    # GW-001: Gateway online
    for gw in diag.gateways:
        if gw.get("type") == "Personal":
            continue
        status = (gw.get("gatewayStatus") or "").lower()
        is_online = status in ("live", "online", "")
        rules.append(RuleResult(
            id="GW-001",
            rule="Gateway online",
            category="Availability",
            severity="HIGH",
            status="PASS" if is_online else "FAIL",
            details=(
                f'Gateway "{gw["displayName"]}" is online.'
                if is_online
                else f'Gateway "{gw["displayName"]}" status: {gw.get("gatewayStatus", "unknown")}.'
            ),
            recommendation=None if is_online else "Check gateway service and network connectivity.",
        ))

    # GW-002: Gateway version current
    for gw in diag.gateways:
        if gw.get("type") == "Personal" or not gw.get("version"):
            continue
        version_str = gw["version"]
        parts = [int(p) for p in version_str.split(".") if p.isdigit()]
        major = parts[0] if parts else 0
        is_old = major > 0 and major < 3000
        version_num = int(version_str.replace(".", "") or "0")
        warn_threshold = is_old or version_num < 300000
        rules.append(RuleResult(
            id="GW-002",
            rule="Gateway version current",
            category="Maintenance",
            severity="MEDIUM",
            status="WARN" if warn_threshold else "PASS",
            details=f'Gateway "{gw["displayName"]}" version: {version_str}.',
            recommendation="Update gateway to latest version from https://aka.ms/gateway." if warn_threshold else None,
        ))

    # GW-003: No unused gateways (0 datasources)
    for gw in diag.gateways:
        if gw.get("type") == "Personal":
            continue
        datasources = diag.datasources_by_gateway.get(gw["id"], [])
        has_ds = len(datasources) > 0
        rules.append(RuleResult(
            id="GW-003",
            rule="No unused gateways",
            category="Governance",
            severity="MEDIUM",
            status="PASS" if has_ds else "WARN",
            details=(
                f'Gateway "{gw["displayName"]}" has {len(datasources)} datasource(s).'
                if has_ds
                else f'Gateway "{gw["displayName"]}" has no datasources.'
            ),
            recommendation=None if has_ds else "Consider removing unused gateways to reduce management overhead.",
        ))

    # GW-004: No unused datasources — cross-ref with connections
    connected_gw_ids = {c["gatewayId"] for c in diag.connections if c.get("gatewayId")}
    for gw in diag.gateways:
        if gw.get("type") == "Personal":
            continue
        datasources = diag.datasources_by_gateway.get(gw["id"], [])
        for ds in datasources:
            has_conn = gw["id"] in connected_gw_ids
            ds_name = ds.get("datasourceName") or ds["id"]
            rules.append(RuleResult(
                id="GW-004",
                rule="No unused datasources",
                category="Governance",
                severity="LOW",
                status="PASS" if has_conn else "WARN",
                details=(
                    f'Datasource "{ds_name}" on "{gw["displayName"]}" is referenced.'
                    if has_conn
                    else f'Datasource "{ds_name}" on "{gw["displayName"]}" has no matching connections.'
                ),
                recommendation=None if has_conn else "Delete unused datasources with gateway_fix rule GW-004.",
            ))

    # GW-005: Datasource connectivity healthy
    for ds_key, status in diag.status_by_datasource.items():
        gateway_id, datasource_id = ds_key.split("|")
        gw = next((g for g in diag.gateways if g["id"] == gateway_id), None)
        ds = next(
            (d for d in diag.datasources_by_gateway.get(gateway_id, []) if d["id"] == datasource_id),
            None,
        )
        gw_name = gw["displayName"] if gw else gateway_id
        ds_name = (ds.get("datasourceName") if ds else None) or datasource_id
        is_ok = status == "OK"
        rules.append(RuleResult(
            id="GW-005",
            rule="Datasource connectivity healthy",
            category="Availability",
            severity="HIGH",
            status="PASS" if is_ok else "FAIL",
            details=(
                f'Datasource "{ds_name}" on "{gw_name}" is reachable.'
                if is_ok
                else f'Datasource "{ds_name}" on "{gw_name}": {status}.'
            ),
            recommendation=None if is_ok else "Check datasource credentials and network connectivity.",
        ))

    # GW-006: No excessive admins (>5 per datasource)
    for ds_key, users in diag.users_by_datasource.items():
        gateway_id, datasource_id = ds_key.split("|")
        gw = next((g for g in diag.gateways if g["id"] == gateway_id), None)
        ds = next(
            (d for d in diag.datasources_by_gateway.get(gateway_id, []) if d["id"] == datasource_id),
            None,
        )
        gw_name = gw["displayName"] if gw else gateway_id
        ds_name = (ds.get("datasourceName") if ds else None) or datasource_id
        admin_count = sum(
            1 for u in users
            if u.get("datasourceAccessRight") in ("Admin", "ReadOverrideEffectiveIdentity")
        )
        ok = admin_count <= 5
        rules.append(RuleResult(
            id="GW-006",
            rule="No excessive admins",
            category="Security",
            severity="MEDIUM",
            status="PASS" if ok else "WARN",
            details=(
                f'Datasource "{ds_name}" on "{gw_name}" has {admin_count} admin(s).'
                if ok
                else f'Datasource "{ds_name}" on "{gw_name}" has {admin_count} admins (>5).'
            ),
            recommendation=None if ok else "Reduce admin users via gateway_fix rule GW-006.",
        ))

    # GW-007: Connection credentials check
    for conn in diag.connections:
        creds = conn.get("credentialDetails")
        has_creds = bool(creds and isinstance(creds, dict) and len(creds) > 0)
        display = conn.get("displayName") or conn["id"]
        rules.append(RuleResult(
            id="GW-007",
            rule="Connection credentials configured",
            category="Security",
            severity="MEDIUM",
            status="PASS" if has_creds else "WARN",
            details=(
                f'Connection "{display}" has credentials configured.'
                if has_creds
                else f'Connection "{display}" has no credential details.'
            ),
            recommendation=None if has_creds else "Update connection to configure valid credentials.",
        ))

    # GW-008: No orphaned cloud connections
    gateway_ids = {g["id"] for g in diag.gateways}
    for conn in diag.connections:
        if conn.get("connectivityType") != "ShareableCloud":
            continue
        is_orphaned = conn.get("gatewayId") and conn["gatewayId"] not in gateway_ids
        display = conn.get("displayName") or conn["id"]
        rules.append(RuleResult(
            id="GW-008",
            rule="No orphaned cloud connections",
            category="Governance",
            severity="LOW",
            status="WARN" if is_orphaned else "PASS",
            details=(
                f'Connection "{display}" references missing gateway {conn["gatewayId"]}.'
                if is_orphaned
                else f'Connection "{display}" is properly bound.'
            ),
            recommendation="Delete orphaned connection with gateway_fix rule GW-008." if is_orphaned else None,
        ))

    # GW-009: VNet gateway properly configured
    for gw in diag.gateways:
        if gw.get("type") != "VirtualNetwork":
            continue
        vnet = gw.get("virtualNetworkAzureResource") or {}
        fully_configured = all([
            vnet.get("subscriptionId"),
            vnet.get("resourceGroupName"),
            vnet.get("virtualNetworkName"),
            vnet.get("subnetName"),
        ])
        rules.append(RuleResult(
            id="GW-009",
            rule="VNet gateway configured",
            category="Configuration",
            severity="HIGH",
            status="PASS" if fully_configured else "FAIL",
            details=(
                f'VNet gateway "{gw["displayName"]}" is fully configured '
                f'({vnet.get("virtualNetworkName", "")}/{vnet.get("subnetName", "")}).'
                if fully_configured
                else f'VNet gateway "{gw["displayName"]}" is missing Azure resource configuration fields.'
            ),
            recommendation=(
                None if fully_configured
                else "Complete VNet gateway configuration with subscription, resource group, VNet, and subnet."
            ),
        ))

    # GW-010: No duplicate datasources
    for gw in diag.gateways:
        if gw.get("type") == "Personal":
            continue
        datasources = diag.datasources_by_gateway.get(gw["id"], [])
        seen: Dict[str, List[dict]] = {}
        for ds in datasources:
            key = f"{ds.get('datasourceType', '')}|{ds.get('connectionDetails', '')}"
            seen.setdefault(key, []).append(ds)
        for group in seen.values():
            if len(group) > 1:
                rules.append(RuleResult(
                    id="GW-010",
                    rule="No duplicate datasources",
                    category="Governance",
                    severity="LOW",
                    status="WARN",
                    details=f'Gateway "{gw["displayName"]}" has {len(group)} duplicate {group[0].get("datasourceType", "unknown")} datasources.',
                    recommendation="Remove duplicates with gateway_fix rule GW-010.",
                ))

    # GW-011: Privacy level configured
    for conn in diag.connections:
        privacy = conn.get("privacyLevel") or ""
        has_privacy = privacy != "" and privacy.lower() != "none"
        display = conn.get("displayName") or conn["id"]
        rules.append(RuleResult(
            id="GW-011",
            rule="Privacy level configured",
            category="Security",
            severity="LOW",
            status="PASS" if has_privacy else "WARN",
            details=(
                f'Connection "{display}" privacy level: {privacy}.'
                if has_privacy
                else f'Connection "{display}" has no privacy level set.'
            ),
            recommendation=(
                None if has_privacy
                else "Set an appropriate privacy level (Organizational, Private, or Public) on the connection."
            ),
        ))

    # GW-012: All connections have display names
    for conn in diag.connections:
        name = (conn.get("displayName") or "").strip()
        has_name = name != ""
        rules.append(RuleResult(
            id="GW-012",
            rule="Connection has display name",
            category="Governance",
            severity="LOW",
            status="PASS" if has_name else "WARN",
            details=(
                f'Connection "{name}" ({conn["id"]}) is named.'
                if has_name
                else f'Connection {conn["id"]} has no display name.'
            ),
            recommendation=None if has_name else "Add a descriptive display name to the connection for easier management.",
        ))

    return rules


# ──────────────────────────────────────────────
# Tool: gateway_optimization_recommendations
# ──────────────────────────────────────────────

def gateway_optimization_recommendations(args: dict) -> str:
    try:
        diag = _collect_diagnostics()

        header_sections = [
            f"**Gateways scanned:** {len(diag.gateways)}",
            f"**Connections scanned:** {len(diag.connections)}",
        ]

        rules = _run_gateway_rules(diag)
        now = datetime.now(timezone.utc).isoformat()

        return render_rule_report(
            "Gateway & Connection Optimization Report",
            now,
            header_sections,
            rules,
        )
    except Exception as e:
        return f"❌ Failed to run gateway optimization: {e}"


# ──────────────────────────────────────────────
# Structured Fix Definitions
# ──────────────────────────────────────────────

def _fix_gw004(diag: _GatewayDiagnostics, dry_run: bool) -> List[str]:
    """Delete unused datasources (no matching connections)."""
    results: List[str] = []
    connected_gw_ids = {c["gatewayId"] for c in diag.connections if c.get("gatewayId")}

    for gw in diag.gateways:
        if gw.get("type") == "Personal":
            continue
        datasources = diag.datasources_by_gateway.get(gw["id"], [])
        for ds in datasources:
            if gw["id"] not in connected_gw_ids:
                ds_name = ds.get("datasourceName") or ds["id"]
                if dry_run:
                    results.append(f'🔍 Would delete datasource "{ds_name}" from gateway "{gw["displayName"]}"')
                else:
                    try:
                        delete_gateway_datasource(gw["id"], ds["id"])
                        results.append(f'✅ Deleted datasource "{ds_name}" from gateway "{gw["displayName"]}"')
                    except Exception as e:
                        results.append(f'❌ Failed to delete datasource "{ds_name}": {e}')

    if not results:
        results.append("No unused datasources found.")
    return results


def _fix_gw006(diag: _GatewayDiagnostics, dry_run: bool) -> List[str]:
    """Remove excess admin users (keep first 5)."""
    results: List[str] = []

    for ds_key, users in diag.users_by_datasource.items():
        gateway_id, datasource_id = ds_key.split("|")
        admins = [
            u for u in users
            if u.get("datasourceAccessRight") in ("Admin", "ReadOverrideEffectiveIdentity")
        ]
        if len(admins) <= 5:
            continue

        to_remove = admins[5:]
        for user in to_remove:
            email = user.get("emailAddress", "unknown")
            if dry_run:
                results.append(f'🔍 Would remove admin "{email}" from datasource {datasource_id}')
            else:
                try:
                    delete_gateway_datasource_user(gateway_id, datasource_id, email)
                    results.append(f'✅ Removed admin "{email}" from datasource {datasource_id}')
                except Exception as e:
                    results.append(f'❌ Failed to remove "{email}": {e}')

    if not results:
        results.append("No excessive admins found.")
    return results


def _fix_gw008(diag: _GatewayDiagnostics, dry_run: bool) -> List[str]:
    """Delete orphaned cloud connections (referencing missing gateways)."""
    results: List[str] = []
    gateway_ids = {g["id"] for g in diag.gateways}

    for conn in diag.connections:
        if conn.get("connectivityType") != "ShareableCloud":
            continue
        if conn.get("gatewayId") and conn["gatewayId"] not in gateway_ids:
            display = conn.get("displayName") or conn["id"]
            if dry_run:
                results.append(
                    f'🔍 Would delete orphaned connection "{display}" '
                    f'(references missing gateway {conn["gatewayId"]})'
                )
            else:
                try:
                    delete_connection(conn["id"])
                    results.append(f'✅ Deleted orphaned connection "{display}"')
                except Exception as e:
                    results.append(f'❌ Failed to delete connection "{display}": {e}')

    if not results:
        results.append("No orphaned connections found.")
    return results


def _fix_gw010(diag: _GatewayDiagnostics, dry_run: bool) -> List[str]:
    """Delete duplicate datasources (keep first, remove rest)."""
    results: List[str] = []

    for gw in diag.gateways:
        if gw.get("type") == "Personal":
            continue
        datasources = diag.datasources_by_gateway.get(gw["id"], [])
        seen: Dict[str, List[dict]] = {}
        for ds in datasources:
            key = f"{ds.get('datasourceType', '')}|{ds.get('connectionDetails', '')}"
            seen.setdefault(key, []).append(ds)

        for group in seen.values():
            if len(group) <= 1:
                continue
            duplicates = group[1:]
            for dup in duplicates:
                dup_name = dup.get("datasourceName") or dup["id"]
                if dry_run:
                    results.append(
                        f'🔍 Would delete duplicate datasource "{dup_name}" '
                        f'from gateway "{gw["displayName"]}"'
                    )
                else:
                    try:
                        delete_gateway_datasource(gw["id"], dup["id"])
                        results.append(
                            f'✅ Deleted duplicate datasource "{dup_name}" '
                            f'from gateway "{gw["displayName"]}"'
                        )
                    except Exception as e:
                        results.append(f"❌ Failed to delete duplicate: {e}")

    if not results:
        results.append("No duplicate datasources found.")
    return results


_GATEWAY_FIXES: Dict[str, dict] = {
    "GW-004": {
        "description": "Delete unused datasources (no matching connections)",
        "apply": _fix_gw004,
    },
    "GW-006": {
        "description": "Remove excess admin users (keep first 5)",
        "apply": _fix_gw006,
    },
    "GW-008": {
        "description": "Delete orphaned cloud connections (referencing missing gateways)",
        "apply": _fix_gw008,
    },
    "GW-010": {
        "description": "Delete duplicate datasources (keep first, remove rest)",
        "apply": _fix_gw010,
    },
}

FIXABLE_RULE_IDS = list(_GATEWAY_FIXES.keys())


# ──────────────────────────────────────────────
# Tool: gateway_fix — Auto-fix detected issues
# ──────────────────────────────────────────────

def gateway_fix(args: dict) -> str:
    try:
        is_dry_run = args.get("dryRun", False)
        requested_rules = args.get("ruleIds") or FIXABLE_RULE_IDS

        # Validate rule IDs
        invalid_rules = [r for r in requested_rules if r not in _GATEWAY_FIXES]
        if invalid_rules:
            return (
                f"❌ Unknown rule IDs: {', '.join(invalid_rules)}. "
                f"Fixable rules: {', '.join(FIXABLE_RULE_IDS)}"
            )

        diag = _collect_diagnostics()
        now = datetime.now(timezone.utc).isoformat()

        lines: List[str] = [
            f"# 🔧 Gateway Fix: {'DRY RUN' if is_dry_run else 'Executing'}",
            "",
            f"_{now}_",
            "",
            f"**Rules to fix:** {', '.join(requested_rules)}",
            "",
        ]

        for rule_id in requested_rules:
            fix = _GATEWAY_FIXES[rule_id]
            lines.extend([f"## {rule_id}: {fix['description']}", ""])
            results = fix["apply"](diag, is_dry_run)
            lines.extend([f"- {r}" for r in results])
            lines.append("")

        return "\n".join(lines)
    except Exception as e:
        return f"❌ Failed to run gateway fix: {e}"


# ──────────────────────────────────────────────
# Tool definitions for MCP registration
# ──────────────────────────────────────────────

gateway_tools = [
    {
        "name": "gateway_list",
        "description": "List all gateways with their status, version, type, and VNet configuration.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": gateway_list,
    },
    {
        "name": "gateway_list_connections",
        "description": "List all connections with their connectivity type, gateway binding, and privacy level.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": gateway_list_connections,
    },
    {
        "name": "gateway_optimization_recommendations",
        "description": (
            "LIVE SCAN: Scans all gateways and connections with 12 rules covering availability "
            "(online status, connectivity), security (credentials, excessive admins, privacy levels), "
            "governance (unused gateways/datasources, orphaned connections, duplicates, display names), "
            "and configuration (VNet setup, version currency). Returns findings with prioritized action items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": gateway_optimization_recommendations,
    },
    {
        "name": "gateway_fix",
        "description": (
            "AUTO-FIX: Applies fixes to gateway and connection issues. "
            "Fixable rules: GW-004 (delete unused datasources), GW-006 (remove excess admins), "
            "GW-008 (delete orphaned connections), GW-010 (delete duplicate datasources). "
            "Use dryRun=true to preview changes without executing them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ruleIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Rule IDs to fix: GW-004, GW-006, GW-008, GW-010",
                },
                "dryRun": {
                    "type": "boolean",
                    "description": "If true, preview changes without executing them (default: false)",
                },
            },
            "required": [],
        },
        "handler": gateway_fix,
    },
]
