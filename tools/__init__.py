from tools.rule_engine import RuleResult, RuleSeverity, RuleStatus, render_rule_report
from tools.auth_tools import auth_tools
from tools.workspace import workspace_tools
from tools.lakehouse import lakehouse_tools
from tools.warehouse import warehouse_tools
from tools.eventhouse import eventhouse_tools
from tools.semantic_model import semantic_model_tools
from tools.gateway import gateway_tools

AUTH_TOOL_NAMES = {t["name"] for t in auth_tools}
all_tools = (
    auth_tools
    + workspace_tools
    + lakehouse_tools
    + warehouse_tools
    + eventhouse_tools
    + semantic_model_tools
    + gateway_tools
)


def get_tool_by_name(name):
    return next((t for t in all_tools if t["name"] == name), None)
