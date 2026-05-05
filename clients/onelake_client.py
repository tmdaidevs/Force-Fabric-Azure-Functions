"""OneLake client — ADLS Gen2 REST API for reading files, listing directories, and Delta log analysis."""

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from auth.fabric_auth import get_token_for_scope

ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
STORAGE_SCOPE = "https://storage.azure.com/.default"


# ──────────────────────────────────────────────
# Path safety — prevent path traversal attacks
# ──────────────────────────────────────────────

def _validate_onelake_path(path: str, label: str) -> None:
    if ".." in path or path.startswith("/") or "://" in path:
        raise ValueError(f"Invalid {label}: path traversal not allowed.")


# ──────────────────────────────────────────────
# OneLake ADLS Gen2 REST API Client
# ──────────────────────────────────────────────

def _onelake_fetch(path: str) -> requests.Response:
    token = get_token_for_scope(STORAGE_SCOPE)
    url = f"{ONELAKE_DFS}/{path}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    if not resp.ok:
        raise RuntimeError(f"OneLake API error ({resp.status_code}): {resp.text}")
    return resp


def list_onelake_files(
    workspace_name: str,
    lakehouse_name: str,
    relative_path: str,
) -> List[str]:
    """List files in a OneLake directory (ADLS Gen2 path listing)."""
    _validate_onelake_path(relative_path, "relativePath")
    filesystem = workspace_name
    directory = f"{lakehouse_name}.Lakehouse/{relative_path}"

    token = get_token_for_scope(STORAGE_SCOPE)
    url = (
        f"{ONELAKE_DFS}/{quote(filesystem, safe='')}"
        f"?resource=filesystem"
        f"&directory={quote(directory, safe='')}"
        f"&recursive=false"
    )

    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    if not resp.ok:
        raise RuntimeError(f"OneLake list error ({resp.status_code}): {resp.text}")

    data = resp.json()
    return [p["name"] for p in data.get("paths", [])]


def read_onelake_file(
    workspace_name: str,
    lakehouse_name: str,
    relative_path: str,
) -> str:
    """Read a text file from OneLake."""
    _validate_onelake_path(relative_path, "relativePath")
    full_path = f"{quote(workspace_name, safe='')}/{quote(lakehouse_name, safe='')}.Lakehouse/{relative_path}"
    resp = _onelake_fetch(full_path)
    return resp.text


# ──────────────────────────────────────────────
# Delta Log Types
# ──────────────────────────────────────────────

@dataclass
class DeltaMetadata:
    id: Optional[str] = None
    format: Optional[Dict[str, str]] = None
    schemaString: Optional[str] = None
    partitionColumns: Optional[List[str]] = None
    configuration: Optional[Dict[str, str]] = None
    createdTime: Optional[int] = None


@dataclass
class DeltaCommitInfo:
    timestamp: Optional[int] = None
    operation: Optional[str] = None
    operationParameters: Optional[Dict[str, str]] = None
    operationMetrics: Optional[Dict[str, str]] = None
    engineInfo: Optional[str] = None
    isBlindAppend: Optional[bool] = None


@dataclass
class DeltaAddAction:
    path: str
    size: int
    modificationTime: Optional[int] = None
    partitionValues: Optional[Dict[str, str]] = None
    stats: Optional[str] = None


@dataclass
class DeltaRemoveAction:
    path: str
    deletionTimestamp: Optional[int] = None
    dataChange: Optional[bool] = None


@dataclass
class DeltaLogAnalysis:
    metadata: Optional[DeltaMetadata] = None
    commits: List[DeltaCommitInfo] = field(default_factory=list)
    activeFiles: List[DeltaAddAction] = field(default_factory=list)
    totalVersions: int = 0
    errors: List[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# Delta Log Reader
# ──────────────────────────────────────────────

def read_delta_log(
    workspace_name: str,
    lakehouse_name: str,
    table_name: str,
) -> DeltaLogAnalysis:
    """Read and parse the Delta log for a table."""
    result = DeltaLogAnalysis()
    log_dir = f"Tables/{table_name}/_delta_log"

    try:
        files = list_onelake_files(workspace_name, lakehouse_name, log_dir)

        json_files = sorted([f for f in files if f.endswith(".json")])[-50:]
        result.totalVersions = len(json_files)

        for file_path in json_files:
            try:
                relative_path = (
                    f"Tables/{table_name}/_delta_log/{file_path.split('_delta_log/')[-1]}"
                    if "_delta_log" in file_path
                    else file_path
                )

                content = read_onelake_file(workspace_name, lakehouse_name, relative_path)

                for line in content.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)

                        if "metaData" in entry:
                            md = entry["metaData"]
                            result.metadata = DeltaMetadata(
                                id=md.get("id"),
                                format=md.get("format"),
                                schemaString=md.get("schemaString"),
                                partitionColumns=md.get("partitionColumns"),
                                configuration=md.get("configuration"),
                                createdTime=md.get("createdTime"),
                            )
                        if "commitInfo" in entry:
                            ci = entry["commitInfo"]
                            result.commits.append(DeltaCommitInfo(
                                timestamp=ci.get("timestamp"),
                                operation=ci.get("operation"),
                                operationParameters=ci.get("operationParameters"),
                                operationMetrics=ci.get("operationMetrics"),
                                engineInfo=ci.get("engineInfo"),
                                isBlindAppend=ci.get("isBlindAppend"),
                            ))
                        if "add" in entry:
                            a = entry["add"]
                            result.activeFiles.append(DeltaAddAction(
                                path=a["path"],
                                size=a.get("size", 0),
                                modificationTime=a.get("modificationTime"),
                                partitionValues=a.get("partitionValues"),
                                stats=a.get("stats"),
                            ))
                    except (json.JSONDecodeError, KeyError):
                        pass
            except Exception as exc:
                result.errors.append(f"Failed to read {file_path}: {exc}")
    except Exception as exc:
        result.errors.append(f"Failed to list delta log: {exc}")

    return result


# ──────────────────────────────────────────────
# Delta Log Analysis Helpers
# ──────────────────────────────────────────────

def get_partition_columns(log: DeltaLogAnalysis) -> List[str]:
    if log.metadata and log.metadata.partitionColumns:
        return log.metadata.partitionColumns
    return []


def get_table_config(log: DeltaLogAnalysis) -> Dict[str, str]:
    if log.metadata and log.metadata.configuration:
        return log.metadata.configuration
    return {}


def get_last_operation(log: DeltaLogAnalysis, operation: str) -> Optional[DeltaCommitInfo]:
    for commit in reversed(log.commits):
        if commit.operation == operation:
            return commit
    return None


def count_operations(log: DeltaLogAnalysis) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for c in log.commits:
        op = c.operation or "UNKNOWN"
        counts[op] = counts.get(op, 0) + 1
    return counts


def get_file_size_stats(log: DeltaLogAnalysis) -> Dict[str, Any]:
    files = log.activeFiles
    total_files = len(files)
    total_size_bytes = sum(f.size for f in files)
    avg_file_size_mb = total_size_bytes / total_files / (1024 * 1024) if total_files > 0 else 0.0
    small_file_count = sum(1 for f in files if f.size < 25 * 1024 * 1024)  # <25MB
    large_file_count = sum(1 for f in files if f.size > 1024 * 1024 * 1024)  # >1GB

    return {
        "totalFiles": total_files,
        "totalSizeBytes": total_size_bytes,
        "avgFileSizeMB": avg_file_size_mb,
        "smallFileCount": small_file_count,
        "largeFileCount": large_file_count,
    }


def days_since_timestamp(timestamp_ms: int) -> int:
    """Return the number of days since a given Unix timestamp (milliseconds)."""
    return int((time.time() * 1000 - timestamp_ms) / (86400 * 1000))
