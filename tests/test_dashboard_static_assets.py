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
    assert "animation:loader-spin" not in text
    assert "filter:drop-shadow(0 0 28px rgba(242,138,26,.34))" in text
    assert "box-shadow:0 0 28px rgba(242,138,26,.62)" in text
    assert "top:60%" in text


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
def test_dashboard_session_filters_use_server_scoped_sql_payloads(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "function currentScope()" not in text
    assert "function useLocalFilterAggregates()" not in text
    assert "function renderFilteredDashboardSurfaces()" not in text
    assert "visibleSessions()" not in text
    assert "function incrementCounter(" not in text
    assert "function sortedCounter(" not in text
    assert "dashboardFilterActive = Boolean(event.detail?.hasFilters || dashboardSelectedSessionId);" in text
    assert "dashboardFilterActive = urlHasDashboardFilters();" in text
    assert "let dashboardSelectedSessionId = currentParams().get('session') || '';" in text
    assert "selectedSessionId: dashboardSelectedSessionId" in text
    assert "event.detail?.hasFilters || dashboardSelectedSessionId" in text
    assert "(params.get('session') || '').trim()" in text
    assert "const compareTab = sqlTab('compare');" in text
    assert "const comparison = compareTab.comparison || D.comparison;" in text
    assert "compareTab.agent_comparison || currentAgentComparison()" in text
    assert "const overviewTab = sqlTab('overview');" in text
    assert "overviewTab.models_by_count || sqlTab('models').models_by_count || D.models_by_count || {}" in text
    assert "overviewTab.events_by_type || sqlTab('activity').events_by_type || D.events_by_type || {}" in text
    assert "overviewTab.total_input_tokens ?? D.total_input_tokens ?? 0" in text
    assert "sqlTab('graphs').graph_tool_transitions || D.graph_tool_transitions || []" in text
    assert "sqlTab('graphs').graph_dep || D.graph_dep" in text
    assert "Cost Basis" in text
    assert "Pricing Source" not in text


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
    assert "D.session_list_total || sessions.length || D.unique_sessions" in text
    assert "fetch(reportUrlWithCurrentFilters()" in text
    assert "['q','agents','agent','model','status','range','session']" in text
    assert "scheduleDashboardReload();" in text
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
def test_dashboard_agent_tool_network_supports_sql_graph_value_shapes(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "const raw = node && (node.size ?? node.value ?? node.count ?? node.events ?? 0);" in text
    assert "const label = server.id ?? server.server ?? '';" in text
    assert "const count = server.events ?? server.count ?? server.value ?? 0;" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_session_detail_has_quality_rules_tab(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'data-tab="quality">Quality</button>' in text
    assert "function renderQualityPanel(session)" in text
    assert "D.quality_rules || []" in text
    assert "Score Breakdown" in text
    assert "quality_breakdown" in text
    assert "quality-breakdown-table" in text
    assert "item.inputs" in text
    assert "renderBreakdownRows" in text
    assert "rowspan=" in text
    assert "<th>Input</th>" in text
    assert "<th>Value</th>" in text
    assert "Final displayed score" in text
    assert "No score" in text
    assert "Quality Rules" in text
    assert "SQL summary heuristic" not in text
    assert "Session summary" not in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_wires_sql_data_tab_surfaces(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'data-tab="data">Context</button>' in text
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
    assert "const obsTab = sqlTab('observations');" in text
    assert "obsTab.token_economy || D.token_economy || {}" in text
    assert "SQLite Store" not in text
    assert "SQL Sessions" not in text
    assert "Top SQL" not in text
    assert "No SQL rows yet" not in text
    assert "SQL-backed report data" not in text
    assert "SQL Report Store" not in text
    assert "Comparison Snapshot" not in text
    assert "renderOverviewComparisonSummary" not in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_activity_widgets_live_on_overview_tab(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'data-tab="overview">Activity</button>' in text
    assert 'data-tab="activity"' not in text
    assert 'id="tab-activity"' not in text
    assert 'id="hm-grid"' in text
    assert 'id="hour-bars"' in text
    assert 'id="weekly-trends-table"' in text
    assert "if (tabName === 'activity') tabName = 'overview';" in text
    assert "if (tabName === 'context') tabName = 'data';" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_overview_separates_source_provenance_from_event_semantics(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'id="source-provenance"' in text
    assert "const sourceProvenance = overviewTab.source_provenance || D.source_provenance || [];" in text
    assert "Transport/source provenance. The chart above stays semantic by event type." in text


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
def test_dashboard_graph_tab_renders_semantic_force_graph(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "Behavioral Memory Graph" in text
    assert "buildSemanticGraph" in text
    assert 'id="semantic-graph-svg"' in text
    assert "d3.forceSimulation" in text
    assert "d3.forceLink" in text
    assert "d3.forceManyBody" in text
    assert "d3.zoom" in text
    assert "d3.drag" in text
    assert "visibleIdsForSession" in text
    assert "if (!node.session_id || node.session_id === sessionId)" not in text
    assert "if (!edge.session_id || edge.session_id === sessionId)" not in text


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
def test_dashboard_session_detail_uses_shared_timeline_above_tabs(path: Path):
    text = path.read_text(encoding="utf-8")

    assert ".session-timeline-panel{" in text
    assert "function renderSessionTimelinePanel(session, conversation)" in text
    assert "const sessionTimelineHtml = renderSessionTimelinePanel(session, conversation);" in text
    assert "detailEl.innerHTML = headerHtml\n      + sessionTimelineHtml\n      + detailTabsHtml" in text
    assert 'class="session-timeline-span${failureClass}"' in text
    assert ".session-timeline-span.is-failure{" in text
    assert ".session-timeline-gap{" in text
    assert "Idle gap compressed:" in text
    assert "pauses compressed" in text
    assert 'data-tip="${safeTip}"' in text
    assert 'data-tip-align="${tooltipAlign(left)}"' in text
    assert 'const tooltipAlign = left => left < 16 ? \'left\' : (left > 84 ? \'right\' : \'center\');' in text
    assert '.session-timeline-span[data-tip-align="left"]::after' in text
    assert '.session-timeline-span[data-tip-align="right"]::after' in text
    assert 'tabindex="0"' in text
    assert ".session-timeline-span::after" in text
    assert "content:attr(data-tip)" in text
    assert "title=\"${safeTip}\"" not in text
    assert "Trace waterfall" in text
    assert "<span>Trace timeline</span>" not in text


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
