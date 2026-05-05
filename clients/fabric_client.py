"""Fabric REST API client — handles auth, retries (429), pagination, and all resource operations."""

import base64
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from auth.fabric_auth import get_access_token

logger = logging.getLogger(__name__)

FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
POWERBI_API_BASE = "https://api.powerbi.com/v1.0/myorg"


# ──────────────────────────────────────────────
# Data classes (mirror TypeScript interfaces)
# ──────────────────────────────────────────────

@dataclass
class FabricWorkspace:
    id: str
    displayName: str
    type: str
    description: Optional[str] = None
    capacityId: Optional[str] = None


@dataclass
class FabricItem:
    id: str
    displayName: str
    type: str
    workspaceId: str
    description: Optional[str] = None


@dataclass
class FabricLakehouse(FabricItem):
    properties: Optional[Dict[str, Any]] = None


@dataclass
class FabricWarehouse(FabricItem):
    properties: Optional[Dict[str, Any]] = None


@dataclass
class FabricEventhouse(FabricItem):
    properties: Optional[Dict[str, Any]] = None


@dataclass
class LakehouseTable:
    name: str
    type: str
    location: str
    format: str


@dataclass
class JobInstance:
    id: str
    itemId: str
    jobType: str
    invokeType: str
    status: str
    startTimeUtc: Optional[str] = None
    endTimeUtc: Optional[str] = None
    failureReason: Optional[Dict[str, str]] = None


@dataclass
class SemanticModelDefinitionPart:
    path: str
    payload: str
    payloadType: str = "InlineBase64"


@dataclass
class DaxQueryResponse:
    results: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class FabricGateway:
    id: str
    type: str
    displayName: str
    publicKey: Optional[Dict[str, str]] = None
    version: Optional[str] = None
    gatewayStatus: Optional[str] = None
    virtualNetworkAzureResource: Optional[Dict[str, str]] = None


@dataclass
class FabricConnection:
    id: str
    connectivityType: str
    displayName: Optional[str] = None
    gatewayId: Optional[str] = None
    connectionDetails: Optional[Dict[str, Any]] = None
    credentialDetails: Optional[Dict[str, Any]] = None
    privacyLevel: Optional[str] = None


@dataclass
class GatewayDatasource:
    id: str
    gatewayId: str
    datasourceType: str
    connectionDetails: str
    credentialType: str
    datasourceName: Optional[str] = None


@dataclass
class GatewayDatasourceUser:
    emailAddress: str
    displayName: str
    datasourceAccessRight: str
    principalType: str


# ──────────────────────────────────────────────
# Core fetch helpers
# ──────────────────────────────────────────────

def _sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000.0)


def fabric_fetch(path: str, method: str = "GET", body: Any = None,
                 params: Optional[Dict[str, str]] = None) -> Any:
    """Make an authenticated request to the Fabric REST API with 429 retry logic."""
    token = get_access_token()
    url = f"{FABRIC_API_BASE}{path}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    max_retries = 5
    for attempt in range(max_retries + 1):
        resp = requests.request(
            method,
            url,
            headers=headers,
            json=body if body is not None else None,
            params=params,
        )

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "0"))
            delay_ms = (
                retry_after * 1000
                if retry_after > 0
                else min(1000 * (2 ** attempt), 30000)
            )
            logger.info(
                "Rate limited on %s, retrying in %dms (attempt %d/%d)",
                path, delay_ms, attempt + 1, max_retries,
            )
            _sleep_ms(delay_ms)
            continue

        if resp.status_code == 202:
            location = resp.headers.get("Location")
            retry_after_hdr = resp.headers.get("Retry-After")
            return {"location": location, "retryAfter": retry_after_hdr, "status": 202}

        if resp.status_code == 204:
            return {}

        if not resp.ok:
            raise RuntimeError(f"Fabric API error ({resp.status_code}): {resp.text}")

        return resp.json()

    raise RuntimeError(f"Fabric API rate limit exceeded after {max_retries} retries on {path}")


def fabric_fetch_paginated(path: str) -> List[Any]:
    """Fetch all pages from a paginated Fabric API endpoint with 429 retry."""
    items: List[Any] = []
    continuation_uri: Optional[str] = f"{FABRIC_API_BASE}{path}"

    while continuation_uri:
        token = get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        max_retries = 5
        resp: Optional[requests.Response] = None
        for attempt in range(max_retries + 1):
            resp = requests.get(continuation_uri, headers=headers)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "0"))
                delay_ms = (
                    retry_after * 1000
                    if retry_after > 0
                    else min(1000 * (2 ** attempt), 30000)
                )
                logger.info(
                    "Rate limited (paginated), retrying in %dms (attempt %d/%d)",
                    delay_ms, attempt + 1, max_retries,
                )
                _sleep_ms(delay_ms)
                continue
            break

        if resp is None or not resp.ok:
            error_text = resp.text if resp else "No response after retries"
            status = resp.status_code if resp else "N/A"
            raise RuntimeError(f"Fabric API error ({status}): {error_text}")

        data = resp.json()
        items.extend(data.get("value", []))
        continuation_uri = data.get("continuationUri")

    return items


def _powerbi_fetch(path: str, method: str = "GET", body: Any = None) -> Any:
    """Make an authenticated request to the Power BI REST API."""
    token = get_access_token()
    url = f"{POWERBI_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.request(method, url, headers=headers, json=body)
    if not resp.ok:
        raise RuntimeError(f"Power BI API error ({resp.status_code}): {resp.text}")
    if resp.status_code == 204 or resp.headers.get("content-length") == "0":
        return {}
    return resp.json()


# ──────────────────────────────────────────────
# Workspace operations
# ──────────────────────────────────────────────

def list_workspaces() -> List[Dict[str, Any]]:
    return fabric_fetch_paginated("/workspaces")


def get_workspace(workspace_id: str) -> Dict[str, Any]:
    return fabric_fetch(f"/workspaces/{quote(workspace_id, safe='')}")


def list_workspace_role_assignments(workspace_id: str) -> List[Dict[str, Any]]:
    """List role assignments for a workspace. Returns [{id, principal: {id, type, displayName}, role}]."""
    return fabric_fetch_paginated(f"/workspaces/{quote(workspace_id, safe='')}/roleAssignments")


def get_workspace_admins(workspace_id: str) -> List[Dict[str, Any]]:
    """Return only Admin-role principals for a workspace."""
    assignments = list_workspace_role_assignments(workspace_id)
    return [a for a in assignments if a.get("role") == "Admin"]


def list_workspace_items(workspace_id: str, item_type: Optional[str] = None) -> List[Dict[str, Any]]:
    path = f"/workspaces/{quote(workspace_id, safe='')}/items"
    if item_type:
        path += f"?type={quote(item_type, safe='')}"
    return fabric_fetch_paginated(path)


# ──────────────────────────────────────────────
# Lakehouse operations
# ──────────────────────────────────────────────

def list_lakehouses(workspace_id: str) -> List[Dict[str, Any]]:
    return fabric_fetch_paginated(f"/workspaces/{quote(workspace_id, safe='')}/lakehouses")


def get_lakehouse(workspace_id: str, lakehouse_id: str) -> Dict[str, Any]:
    return fabric_fetch(
        f"/workspaces/{quote(workspace_id, safe='')}/lakehouses/{quote(lakehouse_id, safe='')}"
    )


def list_lakehouse_tables(workspace_id: str, lakehouse_id: str) -> List[Dict[str, Any]]:
    result = fabric_fetch(
        f"/workspaces/{quote(workspace_id, safe='')}/lakehouses/{quote(lakehouse_id, safe='')}/tables"
    )
    return result.get("data", [])


def run_lakehouse_table_maintenance(
    workspace_id: str,
    lakehouse_id: str,
    job_type: str = "TableMaintenance",
    execution_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body = {"executionData": json.dumps(execution_data)} if execution_data else None
    return fabric_fetch(
        f"/workspaces/{quote(workspace_id, safe='')}/lakehouses/{quote(lakehouse_id, safe='')}/jobs/instances",
        method="POST",
        params={"jobType": job_type},
        body=body,
    )


def get_lakehouse_job_status(
    workspace_id: str, lakehouse_id: str, job_instance_id: str
) -> Dict[str, Any]:
    return fabric_fetch(
        f"/workspaces/{quote(workspace_id, safe='')}/lakehouses/{quote(lakehouse_id, safe='')}"
        f"/jobs/instances/{quote(job_instance_id, safe='')}"
    )


# ──────────────────────────────────────────────
# Warehouse operations
# ──────────────────────────────────────────────

def list_warehouses(workspace_id: str) -> List[Dict[str, Any]]:
    return fabric_fetch_paginated(f"/workspaces/{quote(workspace_id, safe='')}/warehouses")


def get_warehouse(workspace_id: str, warehouse_id: str) -> Dict[str, Any]:
    return fabric_fetch(
        f"/workspaces/{quote(workspace_id, safe='')}/warehouses/{quote(warehouse_id, safe='')}"
    )


# ──────────────────────────────────────────────
# Eventhouse operations
# ──────────────────────────────────────────────

def list_eventhouses(workspace_id: str) -> List[Dict[str, Any]]:
    return fabric_fetch_paginated(f"/workspaces/{quote(workspace_id, safe='')}/eventhouses")


def get_eventhouse(workspace_id: str, eventhouse_id: str) -> Dict[str, Any]:
    return fabric_fetch(
        f"/workspaces/{quote(workspace_id, safe='')}/eventhouses/{quote(eventhouse_id, safe='')}"
    )


# ──────────────────────────────────────────────
# KQL Database operations
# ──────────────────────────────────────────────

def list_kql_databases(workspace_id: str) -> List[Dict[str, Any]]:
    return fabric_fetch_paginated(f"/workspaces/{quote(workspace_id, safe='')}/kqlDatabases")


# ──────────────────────────────────────────────
# Semantic Model operations
# ──────────────────────────────────────────────

def list_semantic_models(workspace_id: str) -> List[Dict[str, Any]]:
    return fabric_fetch_paginated(f"/workspaces/{quote(workspace_id, safe='')}/semanticModels")


def execute_semantic_model_query(
    workspace_id: str, semantic_model_id: str, query: str
) -> List[Dict[str, Any]]:
    """Execute a DAX or DMV query against a Semantic Model via the Power BI REST API."""
    token = get_access_token()
    url = (
        f"https://api.fabric.microsoft.com/v1.0/myorg/groups/"
        f"{quote(workspace_id, safe='')}/datasets/"
        f"{quote(semantic_model_id, safe='')}/executeQueries"
    )

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "queries": [{"query": query}],
            "serializerSettings": {"includeNulls": True},
        },
    )

    if not resp.ok:
        raise RuntimeError(f"Query failed ({resp.status_code}): {resp.text}")

    result = resp.json()
    try:
        return result["results"][0]["tables"][0]["rows"]
    except (KeyError, IndexError):
        return []


# Backward compat alias
execute_semantic_model_dax_query = execute_semantic_model_query


# ──────────────────────────────────────────────
# Semantic Model Definition (model.bim) operations
# ──────────────────────────────────────────────

def get_semantic_model_definition(
    workspace_id: str, semantic_model_id: str
) -> List[Dict[str, Any]]:
    """Get the full definition (model.bim) of a Semantic Model."""
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    url = (
        f"{FABRIC_API_BASE}/workspaces/{quote(workspace_id, safe='')}"
        f"/semanticModels/{quote(semantic_model_id, safe='')}/getDefinition"
    )
    resp = requests.post(url, headers=headers)

    if resp.status_code == 200:
        data = resp.json()
        return data.get("definition", {}).get("parts", [])

    if resp.status_code == 202:
        location = resp.headers.get("Location")
        retry_after = int(resp.headers.get("Retry-After", "10"))
        if not location:
            return []

        max_poll_time = 120.0
        start = time.time()
        poll_interval = retry_after

        while time.time() - start < max_poll_time:
            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, 15.0)

            fresh_token = get_access_token()
            poll_resp = requests.get(
                location, headers={"Authorization": f"Bearer {fresh_token}"}
            )
            if not poll_resp.ok:
                continue

            op_status = poll_resp.json()
            if op_status.get("status") == "Succeeded":
                result_resp = requests.get(
                    f"{location}/result",
                    headers={"Authorization": f"Bearer {fresh_token}"},
                )
                if result_resp.ok:
                    return result_resp.json().get("definition", {}).get("parts", [])
                return []

            if op_status.get("status") == "Failed":
                error_detail = op_status.get("error")
                error_msg = json.dumps(error_detail) if error_detail else "no detail"
                raise RuntimeError(f"getDefinition operation failed: {error_msg}")

        raise RuntimeError("getDefinition operation timed out.")

    raise RuntimeError(f"Fabric API error ({resp.status_code}): {resp.text}")


def update_semantic_model_definition(
    workspace_id: str,
    semantic_model_id: str,
    parts: List[Dict[str, Any]],
) -> None:
    """Update the definition (model.bim / TMDL) of a Semantic Model."""
    token = get_access_token()
    url = (
        f"{FABRIC_API_BASE}/workspaces/{quote(workspace_id, safe='')}"
        f"/semanticModels/{quote(semantic_model_id, safe='')}/updateDefinition"
    )

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"definition": {"parts": parts}},
    )

    if resp.status_code in (200, 204):
        return

    if resp.status_code == 202:
        location = resp.headers.get("Location")
        retry_after = int(resp.headers.get("Retry-After", "10"))
        if not location:
            return

        max_poll_time = 120.0
        start = time.time()
        poll_interval = retry_after

        while time.time() - start < max_poll_time:
            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, 15.0)

            fresh_token = get_access_token()
            poll_resp = requests.get(
                location, headers={"Authorization": f"Bearer {fresh_token}"}
            )
            if not poll_resp.ok:
                continue

            op_status = poll_resp.json()
            if op_status.get("status") == "Succeeded":
                return
            if op_status.get("status") == "Failed":
                error_detail = op_status.get("error")
                error_msg = json.dumps(error_detail) if error_detail else "no detail"
                raise RuntimeError(f"updateDefinition operation failed: {error_msg}")

        raise RuntimeError("updateDefinition operation timed out.")

    if not resp.ok:
        raise RuntimeError(f"Fabric API error ({resp.status_code}): {resp.text}")


# ──────────────────────────────────────────────
# Notebook operations (create, run, poll, delete)
# ──────────────────────────────────────────────

def run_temporary_notebook(
    workspace_id: str,
    code: str,
    notebook_name: Optional[str] = None,
    default_lakehouse_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a temporary notebook with PySpark code, run it, wait for completion, delete it."""
    import re

    name = notebook_name or f"_force_temp_fix_{int(time.time() * 1000)}"

    py_content = (
        "# Fabric notebook source\n\n"
        "# METADATA ********************\n\n"
        '# META {\n# META   "kernel_info": {\n# META     "name": "synapse_pyspark"\n# META   }\n# META }\n\n'
        "# CELL ********************\n\n"
        f"{code}\n\n"
        "# METADATA ********************\n\n"
        '# META {\n# META   "language": "python",\n# META   "language_group": "synapse_pyspark"\n# META }\n'
    )

    payload_base64 = base64.b64encode(py_content.encode("utf-8")).decode("ascii")

    create_body: Dict[str, Any] = {
        "displayName": name,
        "definition": {
            "format": "ipynb",
            "parts": [
                {
                    "path": "notebook-content.py",
                    "payload": payload_base64,
                    "payloadType": "InlineBase64",
                },
            ],
        },
    }

    # 1. Create notebook
    try:
        created = fabric_fetch(
            f"/workspaces/{quote(workspace_id, safe='')}/notebooks",
            method="POST",
            body=create_body,
        )
        notebook_id = created["id"]
    except Exception as exc:
        return {"status": "Failed", "error": f"Failed to create notebook: {exc}"}

    # 2. Run notebook job
    job_instance_id: Optional[str] = None
    try:
        run_result = fabric_fetch(
            f"/workspaces/{quote(workspace_id, safe='')}/items/{quote(notebook_id, safe='')}/jobs/instances",
            method="POST",
            params={"jobType": "RunNotebook"},
        )
        job_instance_id = run_result.get("id")

        if run_result.get("location"):
            match = re.search(r"instances/([^/?]+)", str(run_result["location"]))
            if match:
                job_instance_id = match.group(1)
    except Exception as exc:
        try:
            fabric_fetch(
                f"/workspaces/{quote(workspace_id, safe='')}/notebooks/{quote(notebook_id, safe='')}",
                method="DELETE",
            )
        except Exception:
            pass
        return {"status": "Failed", "error": f"Failed to run notebook: {exc}"}

    # 3. Poll for completion
    final_status = "Unknown"
    error_msg: Optional[str] = None
    max_poll_time = 5 * 60  # 5 minutes
    start_time = time.time()
    poll_interval = 2.0

    while time.time() - start_time < max_poll_time:
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.5, 15.0)

        try:
            status = fabric_fetch(
                f"/workspaces/{quote(workspace_id, safe='')}/items/{quote(notebook_id, safe='')}"
                f"/jobs/instances/{quote(job_instance_id or 'latest', safe='')}"
            )

            if status.get("status") == "Completed":
                final_status = "Completed"
                break
            elif status.get("status") in ("Failed", "Cancelled", "Deduped"):
                final_status = status["status"]
                error_msg = (status.get("failureReason") or {}).get("message")
                break
        except Exception:
            pass

    if final_status == "Unknown":
        final_status = "Timeout"
        error_msg = "Notebook did not complete within 5 minutes."

    # 4. Delete temp notebook
    try:
        fabric_fetch(
            f"/workspaces/{quote(workspace_id, safe='')}/notebooks/{quote(notebook_id, safe='')}",
            method="DELETE",
        )
    except Exception:
        pass

    result: Dict[str, Any] = {"status": final_status}
    if error_msg:
        result["error"] = error_msg
    return result


# ──────────────────────────────────────────────
# Capacity operations
# ──────────────────────────────────────────────

def list_capacities() -> List[Dict[str, Any]]:
    return fabric_fetch_paginated("/capacities")


# ──────────────────────────────────────────────
# Gateway & Connection operations
# ──────────────────────────────────────────────

def list_gateways() -> List[Dict[str, Any]]:
    return fabric_fetch_paginated("/gateways")


def get_gateway(gateway_id: str) -> Dict[str, Any]:
    return fabric_fetch(f"/gateways/{quote(gateway_id, safe='')}")


def list_connections() -> List[Dict[str, Any]]:
    return fabric_fetch_paginated("/connections")


def delete_connection(connection_id: str) -> None:
    fabric_fetch(f"/connections/{quote(connection_id, safe='')}", method="DELETE")


def list_gateway_datasources(gateway_id: str) -> List[Dict[str, Any]]:
    result = _powerbi_fetch(f"/gateways/{quote(gateway_id, safe='')}/datasources")
    return result.get("value", [])


def get_gateway_datasource_status(gateway_id: str, datasource_id: str) -> str:
    try:
        _powerbi_fetch(
            f"/gateways/{quote(gateway_id, safe='')}/datasources/{quote(datasource_id, safe='')}/status"
        )
        return "OK"
    except Exception as exc:
        return str(exc)


def list_gateway_datasource_users(
    gateway_id: str, datasource_id: str
) -> List[Dict[str, Any]]:
    result = _powerbi_fetch(
        f"/gateways/{quote(gateway_id, safe='')}/datasources/{quote(datasource_id, safe='')}/users"
    )
    return result.get("value", [])


def delete_gateway_datasource(gateway_id: str, datasource_id: str) -> None:
    _powerbi_fetch(
        f"/gateways/{quote(gateway_id, safe='')}/datasources/{quote(datasource_id, safe='')}",
        method="DELETE",
    )


def delete_gateway_datasource_user(
    gateway_id: str, datasource_id: str, email_address: str
) -> None:
    _powerbi_fetch(
        f"/gateways/{quote(gateway_id, safe='')}/datasources/{quote(datasource_id, safe='')}"
        f"/users/{quote(email_address, safe='')}",
        method="DELETE",
    )
