"""XMLA client — connects to Fabric/Power BI Analysis Services XMLA endpoint for DMV/TMSL."""

import json
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional
from html import escape as html_escape

import requests

from auth.fabric_auth import get_token_for_scope
from clients.fabric_client import get_workspace

ANALYSIS_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


def _get_xmla_url(workspace_name: str) -> str:
    """Get the correct XMLA endpoint URL for a workspace."""
    from urllib.parse import quote
    return f"https://analysis.windows.net/powerbi/api/v1.0/myorg/{quote(workspace_name, safe='')}"


def _escape_xml(s: str) -> str:
    """Escape special XML characters."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _parse_xmla_response(xml_text: str) -> List[Dict[str, Any]]:
    """Parse XMLA SOAP response and extract row data."""
    # Remove namespace prefixes for easier parsing
    # ElementTree has limited namespace support, so we strip them
    cleaned = xml_text
    # Register common namespaces to avoid ns0/ns1 prefixes
    namespaces = {
        "soap": "http://schemas.xmlsoap.org/soap/envelope/",
        "xmla": "urn:schemas-microsoft-com:xml-analysis",
        "rowset": "urn:schemas-microsoft-com:xml-analysis:rowset",
    }

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RuntimeError(f"Failed to parse XMLA response XML: {exc}") from exc

    # Check for SOAP Fault
    fault = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")
    if fault is not None:
        fault_string_el = fault.find("faultstring")
        fault_string = fault_string_el.text if fault_string_el is not None else "Unknown SOAP fault"
        raise RuntimeError(f"XMLA SOAP Fault: {fault_string}")

    # Check for Analysis Services errors
    exception_el = root.find(".//{urn:schemas-microsoft-com:xml-analysis:exception}Exception")
    if exception_el is not None:
        # Try to find error description
        messages = root.find(".//{urn:schemas-microsoft-com:xml-analysis:exception}Messages")
        if messages is not None:
            error_el = messages.find(".//{urn:schemas-microsoft-com:xml-analysis:exception}Error")
            if error_el is not None:
                desc = error_el.get("Description", "Unknown error")
                raise RuntimeError(f"XMLA Error: {desc}")
        raise RuntimeError("XMLA Error: Unknown error")

    # Extract rows from the rowset namespace
    rows: List[Dict[str, Any]] = []
    rowset_ns = "urn:schemas-microsoft-com:xml-analysis:rowset"

    for row_el in root.iter(f"{{{rowset_ns}}}row"):
        row_dict: Dict[str, Any] = {}
        for child in row_el:
            # Strip namespace from tag name
            tag = child.tag
            if "}" in tag:
                tag = tag.split("}", 1)[1]
            # Get text content
            row_dict[tag] = child.text
        rows.append(row_dict)

    return rows


def _parse_xmla_command_response(xml_text: str) -> None:
    """Parse XMLA SOAP response for commands — checks for faults/errors only."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RuntimeError(f"Failed to parse XMLA response XML: {exc}") from exc

    # Check for SOAP Fault
    fault = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")
    if fault is not None:
        fault_string_el = fault.find("faultstring")
        fault_string = fault_string_el.text if fault_string_el is not None else "Unknown SOAP fault"
        raise RuntimeError(f"XMLA SOAP Fault: {fault_string}")

    # Check for Analysis Services errors in various namespace patterns
    for ns in [
        "urn:schemas-microsoft-com:xml-analysis:exception",
        "urn:schemas-microsoft-com:xml-analysis",
    ]:
        exception_el = root.find(f".//{{{ns}}}Exception")
        if exception_el is not None:
            messages = root.find(f".//{{{ns}}}Messages")
            if messages is not None:
                error_el = messages.find(f".//{{{ns}}}Error")
                if error_el is not None:
                    desc = error_el.get("Description", "Unknown error")
                    raise RuntimeError(f"XMLA Error: {desc}")
            raise RuntimeError("XMLA Error: Unknown error")


def execute_xmla_query(
    workspace_name: str,
    dataset_name: str,
    query: str,
) -> List[Dict[str, Any]]:
    """Execute a DMV query via XMLA SOAP over HTTP."""
    token = get_token_for_scope(ANALYSIS_SCOPE)
    xmla_url = _get_xmla_url(workspace_name)

    soap_envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/">
  <Header>
    <BeginSession xmlns="urn:schemas-microsoft-com:xml-analysis" mustUnderstand="1"/>
  </Header>
  <Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>{_escape_xml(query)}</Statement>
      </Command>
      <Properties>
        <PropertyList>
          <Catalog>{_escape_xml(dataset_name)}</Catalog>
          <Format>Tabular</Format>
          <Content>Data</Content>
        </PropertyList>
      </Properties>
    </Execute>
  </Body>
</Envelope>"""

    resp = requests.post(
        xmla_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/xml",
        },
        data=soap_envelope.encode("utf-8"),
    )

    if not resp.ok:
        raise RuntimeError(f"XMLA query failed ({resp.status_code}): {resp.text[:500]}")

    return _parse_xmla_response(resp.text)


def run_xmla_dmv_queries(
    workspace_name: str,
    dataset_name: str,
    queries: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """Run multiple DMV queries and return named results."""
    results: Dict[str, Dict[str, Any]] = {}

    for name, query in queries.items():
        try:
            rows = execute_xmla_query(workspace_name, dataset_name, query)
            results[name] = {"rows": rows}
        except Exception as exc:
            results[name] = {"error": str(exc)}

    return results


def execute_xmla_command(
    workspace_name: str,
    dataset_name: str,
    tmsl_command: Any,
) -> None:
    """Execute a TMSL command (createOrReplace, alter, delete, etc.) via XMLA SOAP."""
    token = get_token_for_scope(ANALYSIS_SCOPE)
    xmla_url = _get_xmla_url(workspace_name)
    command_json = json.dumps(tmsl_command)

    soap_envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/">
  <Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>{_escape_xml(command_json)}</Statement>
      </Command>
      <Properties>
        <PropertyList>
          <Catalog>{_escape_xml(dataset_name)}</Catalog>
        </PropertyList>
      </Properties>
    </Execute>
  </Body>
</Envelope>"""

    resp = requests.post(
        xmla_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/xml",
        },
        data=soap_envelope.encode("utf-8"),
    )

    if not resp.ok:
        raise RuntimeError(f"XMLA command failed ({resp.status_code}): {resp.text[:500]}")

    _parse_xmla_command_response(resp.text)


def execute_xmla_command_by_id(
    workspace_id: str,
    dataset_name: str,
    tmsl_command: Any,
) -> None:
    """Execute a TMSL command by workspace ID (resolves display name automatically)."""
    workspace = get_workspace(workspace_id)
    execute_xmla_command(workspace["displayName"], dataset_name, tmsl_command)
