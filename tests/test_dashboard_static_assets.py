"""Regression checks for static dashboard assets."""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_HTML_FILES = (
    REPO_ROOT / "src/reflect/data/index.html",
    REPO_ROOT / "docs/report.html",
)


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_uses_larger_font_baseline(path: Path):
    text = path.read_text(encoding="utf-8")

    assert re.search(r"body\s*\{[^}]*font-size\s*:\s*16px\s*;", text), (
        f"Expected body font-size: 16px in {path}"
    )
    assert re.search(r"body\s*\{[^}]*line-height\s*:\s*1\.6\s*;", text), (
        f"Expected body line-height: 1.6 in {path}"
    )
    assert re.search(r"\.header-meta\s*\{[^}]*font-size\s*:\s*13px\s*;", text), (
        f"Expected .header-meta font-size: 13px in {path}"
    )
    assert re.search(r"\.tab\s*\{[^}]*font-size\s*:\s*14px\s*;", text), (
        f"Expected .tab font-size: 14px in {path}"
    )


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_surfaces_cost_controls(path: Path):
    text = path.read_text(encoding="utf-8")

    assert '<option value="cost">Most cost</option>' in text
    assert 'id="cost-stats"' in text
    assert 'id="model-cost-share"' in text
    assert "fmtCost" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_links_bad_report_state_to_public_home(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'href="https://reflect.o11y.dev/"' in text
    assert "window.location.replace(publicHome)" in text
    assert "currentPublicUrl !== publicHome" in text
    assert 'href="showcase.html"' not in text
    assert "window.location.replace('showcase.html')" not in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_hides_demo_badge_for_local_api_reports(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'id="demo-badge"' in text
    assert "isLocalApiReport" in text
    assert "demoBadge.hidden = true" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_shows_branded_loader_during_report_fetch(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'id="report-loader"' in text
    assert 'class="loader-mark"' in text
    assert "loader-orbit" in text
    assert "function showReportLoader(message)" in text
    assert "function hideReportLoader()" in text
    assert "showReportLoader();" in text
    assert "hideReportLoader();" in text
    assert "animation:loader-float" in text
    assert "animation:loader-spin" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_uses_persistent_session_and_filter_rails(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'id="report-shell"' in text
    assert 'id="session-rail"' in text
    assert 'id="filter-rail"' in text
    assert 'id="session-rail-toggle"' in text
    assert 'id="session-rail-open"' in text
    assert 'id="filter-sheet-open"' in text
    assert "body.sessions-rail-collapsed .report-shell" in text
    assert re.search(r"main\s*\{[^}]*max-width\s*:\s*none\s*;", text), (
        f"Expected full-width report shell in {path}"
    )
    assert ".filter-rail{\n  display:block;" in text
    assert ".shell-filter-toggle{display:none}" in text
    assert ".shell-filter-toggle{display:inline-flex}" in text
    assert ".shell-left-toggle{display:none!important}" in text
    assert text.index('id="sb-list"') < text.index('id="tab-sessions"')


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_session_filters_reload_sql_reports(path: Path):
    text = path.read_text(encoding="utf-8")
    match = re.search(r"function scheduleDashboardReload\(\)\{\s*(.*?)\s*\}", text, re.S)

    assert match is not None
    assert "if (!reportSupportsServerFiltering()) return false;" in match.group(1)
    assert "showReportLoader('Filtering sessions...');" in match.group(1)
    assert "window.location.reload()" in match.group(1)
    assert "return true;" in match.group(1)


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_session_filters_redraw_tab_aggregates(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "function currentScope()" in text
    assert "function useLocalFilterAggregates()" in text
    assert "function renderFilteredDashboardSurfaces()" in text
    assert "renderStatRow(scope);" in text
    assert "renderMetricRow(scope);" in text
    assert "renderCharts(scope);" in text
    assert "renderCostPanels(scope);" in text
    assert "renderActivityPanels(scope);" in text
    assert "renderToolsPanels(scope);" in text
    assert "renderSubagentPanel(scope);" in text
    assert "dashboardFilterActive = Boolean(event.detail?.hasFilters || dashboardSelectedSessionId);" in text
    assert "dashboardFilterActive = urlHasDashboardFilters();" in text
    assert "if (useLocalFilterAggregates()) renderFilteredDashboardSurfaces();" in text
    assert "if (!useLocalFilterAggregates()) return null;" in text
    assert "let dashboardSelectedSessionId = currentParams().get('session') || '';" in text
    assert "selectedSessionId: dashboardSelectedSessionId" in text
    assert "event.detail?.hasFilters || dashboardSelectedSessionId" in text
    assert "(params.get('session') || '').trim()" in text
    assert "currentScope()?.graph_dep" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_builds_agent_filters_from_data_without_allowlist(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "const agentNames = [...agentSet].sort((a,b) => a.localeCompare(b));" in text
    assert "preferredAgents" not in text
    assert "AGENT_COLORS" not in text
    assert "function colorForAgent(agent)" in text
    assert "function formatAgentLabel(agent)" in text
    assert "function agentIconSvg(agent)" in text
    assert "D.agent_comparison || []" in text
    assert "Object.keys(D.models_by_count || {})" in text
    assert "D.unique_sessions || sessions.length" in text
    assert "fetch(reportUrlWithCurrentFilters()" in text
    assert "const hasScopeFilter = Boolean" in text
    assert "urlParams.delete('session');" in text
    assert "selectedIdx = filtered[0]._idx;" in text
    assert "pill.innerHTML =" in text
    assert "const agentName = formatAgentLabel(s.agent);" in text
    assert "const agentName = formatAgentLabel(session.agent);" in text
    assert "${escHtml(agentName)}" in text
    assert "s.agent.charAt(0).toUpperCase()" not in text
    assert "session.agent.charAt(0).toUpperCase()" not in text
    assert "['claude','copilot','gemini','cursor'].filter" not in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_tools_tab_spaces_event_distribution_from_top_widgets(path: Path):
    text = path.read_text(encoding="utf-8")

    assert ".tools-summary-grid{" in text
    assert "margin-bottom:18px" in text
    assert '<div class="tools-summary-grid">' in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_wires_sql_data_tab_surfaces(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'data-tab="data">Data</button>' in text
    assert 'id="tab-data"' in text
    assert 'id="sql-specs-panel"' in text
    assert 'id="sql-memory-panel"' in text
    assert 'id="sql-privacy-panel"' in text
    assert 'id="sql-exports-panel"' in text
    assert "function renderSqlTabPayloads()" in text
    assert "const tabs = (D.sqlite && D.sqlite.tabs) || {};" in text
    assert "tabs.specs" in text
    assert "tabs.memory" in text
    assert "tabs.privacy" in text
    assert "tabs.exports" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_prefers_sql_tab_payloads_for_existing_tabs(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "function sqlTab(name)" in text
    assert "const activityTab = sqlTab('activity');" in text
    assert "activityTab.activity_by_day || D.activity_by_day || {}" in text
    assert "sqlTab('activity').tool_percentiles" not in text
    assert "toolsTab.tools_by_count || D.tools_by_count || {}" in text
    assert "mcpTab.mcp_server_before || D.mcp_server_before || {}" in text
    assert "sqlTab('graphs').graph_tool_transitions || D.graph_tool_transitions || []" in text
    assert "sqlTab('graphs').graph_dep || D.graph_dep" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_conversation_rail_aligns_prompt_response_markers(path: Path):
    text = path.read_text(encoding="utf-8")

    assert ".chat-msg .ev-rail{" in text
    assert "display:flex" in text
    assert "align-items:center" in text
    assert ".chat-msg .ev-dot{" in text
    assert "position:static" in text
    assert "width:9px" in text
    assert ".chat-msg .ev-ts{font-size:11px;line-height:1}" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_telemetry_tree_only_infers_parents_inside_real_traces(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "const traceKey = span.trace_id || '';" in text
    assert "if (!visualParent && traceKey && stack.length)" in text
    assert "if (traceKey) {" in text
    assert "const traceKey = span.trace_id || '__trace__';" not in text


def test_public_showcase_demo_includes_codex_agent():
    data = json.loads((REPO_ROOT / "docs/reports/showcase.json").read_text(encoding="utf-8"))

    assert "codex" in data["agents"]
    assert any(agent["name"] == "codex" for agent in data["agent_comparison"])
    assert any(session["agent"] == "codex" for session in data["sessions"])


def test_development_docs_use_poetry_commands():
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "poetry install --extras test" in agents
    assert "poetry run pytest" in agents
    assert "poetry install --extras test" in readme
    assert "poetry run reflect doctor" in readme
    assert "pip install -e .[test]" not in agents
    assert "python3 -m pytest" not in agents
