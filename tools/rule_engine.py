"""Shared Rule Engine — Unified rule evaluation & rendering."""

from dataclasses import dataclass
from typing import List, Literal, Optional

RuleSeverity = Literal["HIGH", "MEDIUM", "LOW", "INFO"]
RuleStatus = Literal["PASS", "FAIL", "WARN", "N/A", "ERROR"]


@dataclass
class RuleResult:
    id: str
    rule: str
    category: str
    severity: RuleSeverity
    status: RuleStatus
    details: str
    recommendation: Optional[str] = None


STATUS_ICON = {
    "PASS": "✅",
    "FAIL": "🔴",
    "WARN": "🟡",
    "N/A": "⚪",
    "ERROR": "⚠️",
}

SEVERITY_ICON = {
    "HIGH": "🔴",
    "MEDIUM": "🟡",
    "LOW": "🔵",
    "INFO": "ℹ️",
}

_SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
_STATUS_ORDER = {"FAIL": 0, "ERROR": 1, "WARN": 2, "PASS": 3, "N/A": 4}


def render_rule_report(
    title: str,
    scan_time: str,
    header_sections: List[str],
    rules: List[RuleResult],
) -> str:
    """Render a complete rule results report.

    1. Summary counts
    2. Full rule results table (all rules)
    3. Issues table (FAIL/WARN only, with details + recommendation)
    """
    lines: List[str] = [
        f"# 🔍 {title}",
        "",
        f"_Live scan at {scan_time}_",
        "",
    ]

    if header_sections:
        lines.extend(header_sections)
        lines.append("")

    # Summary
    pass_count = sum(1 for r in rules if r.status == "PASS")
    fail_count = sum(1 for r in rules if r.status == "FAIL")
    warn_count = sum(1 for r in rules if r.status == "WARN")
    na_count = sum(1 for r in rules if r.status == "N/A")
    err_count = sum(1 for r in rules if r.status == "ERROR")

    err_part = f" | ⚠️ {err_count} error" if err_count > 0 else ""
    lines.append(
        f"**{len(rules)} rules** — ✅ {pass_count} passed | 🔴 {fail_count} failed "
        f"| 🟡 {warn_count} warning | ⚪ {na_count} n/a{err_part}"
    )
    lines.append("")

    # Issues table
    issues = [r for r in rules if r.status in ("FAIL", "WARN", "ERROR")]

    if not issues:
        lines.append("✅ **All rules passed — no issues found!**")
        return "\n".join(lines)

    sorted_issues = sorted(
        issues,
        key=lambda r: (_STATUS_ORDER.get(r.status, 99), _SEV_ORDER.get(r.severity, 99), r.id),
    )

    lines.append("| Rule | Status | Finding | Recommendation |")
    lines.append("|------|--------|---------|----------------|")

    for r in sorted_issues:
        finding = r.details.replace("|", "\\|").replace("\n", " ")
        rec = (r.recommendation or "—").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {r.id} {r.rule} | {STATUS_ICON[r.status]} {SEVERITY_ICON[r.severity]} "
            f"| {finding} | {rec} |"
        )

    return "\n".join(lines)
