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
def test_dashboard_semantic_graph_exposes_workspace_relationship_mode(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'id="semantic-graph-mode"' in text
    assert '<option value="workspace">Same workspace</option>' in text
    assert "function workspaceSessionIds(sessionId)" in text
    assert "edge.kind !== 'ran_in_workspace'" in text
    assert "function useGraphData(nextGraph, selectedSession = '')" in text
    assert "url.searchParams.set('session', selectedSession)" in text
    assert "Loading every session in this canonical workspace" in text
    assert "edge.kind !== 'ran_session'" in text
    assert "connected through the same canonical workspace" in text
    assert "Workspace:.29" in text


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
    assert "const cohortTab = sqlTab('cohort_comparison');" in text
    assert "const comparison = cohortTab.comparison || D.comparison;" in text
    assert "cohortTab.agent_comparison || currentAgentComparison()" in text
    assert "const usageTab = sqlTab('usage');" in text
    assert "usageTab.models_by_count || sqlTab('models').models_by_count || D.models_by_count || {}" in text
    assert "usageTab.events_by_type || sqlTab('activity').events_by_type || D.events_by_type || {}" in text
    assert "usageTab.total_input_tokens ?? D.total_input_tokens ?? 0" in text
    assert "sqlTab('graph').graph_tool_transitions || D.graph_tool_transitions || []" in text
    assert "sqlTab('graph').graph_dep || D.graph_dep" in text
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
    assert "['q','agents','agent','model','status','range','session','tab','view']" in text
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
def test_dashboard_grids_and_subagent_table_stay_inside_report_content(path: Path):
    text = path.read_text(encoding="utf-8")

    assert ".cols-2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in text
    assert re.search(r"\.panel\s*\{[^}]*min-width\s*:\s*0\s*;", text)
    assert "#subagent-effectiveness{min-width:0;overflow-x:auto" in text
    assert "#subagent-effectiveness .data-table{min-width:340px;table-layout:fixed}" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_agent_tool_network_supports_sql_graph_value_shapes(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "const raw = node && (node.size ?? node.value ?? node.count ?? node.events ?? 0);" in text
    assert "const label = server.id ?? server.server ?? '';" in text
    assert "const count = server.events ?? server.count ?? server.value ?? 0;" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_session_detail_restructures_quality_and_telemetry_as_product_views(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'data-tab="summary">Summary</button>' in text
    assert 'data-tab="conversation">Conversation</button>' in text
    assert 'data-tab="execution">Execution</button>' in text
    assert 'data-tab="changes">Changes</button>' in text
    assert 'data-tab="evidence">Evidence</button>' in text
    assert 'data-tab="quality">Quality</button>' not in text
    assert 'data-tab="telemetry">Telemetry</button>' not in text
    assert "function renderQualityPanel(session)" in text
    assert "function renderSessionChangesPanel(session)" in text
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

    assert 'data-explore-view="context">Context &amp; system</button>' in text
    assert 'id="tab-explore-context"' in text
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
    assert text.index('id="tab-explore-context"') < text.index('id="rule-registry"')
    assert "Improvement Rules create durable findings" in text
    assert "SQLite Store" not in text
    assert "SQL Sessions" not in text
    assert "Top SQL" not in text
    assert "No SQL rows yet" not in text
    assert "SQL-backed report data" not in text
    assert "SQL Report Store" not in text
    assert "Comparison Snapshot" not in text
    assert "renderOverviewComparisonSummary" not in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_explains_rules_workflow_changes_and_session_provenance(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'id="rule-registry"' in text
    assert "fetch('/api/rules'" in text
    assert "BaseImprovementRule" in text
    assert "RuleRegistry" in text
    assert "DEFAULT_RULE_REGISTRY" in text
    assert "Session Rules score one session" in text
    assert "View Source Sessions" in text
    assert "Source Evidence" in text
    assert "Related Sessions" in text
    assert "Observed Uses" in text
    assert "After Activation" in text
    assert "Review Agent Draft & Diff" in text
    assert "Review Blueprint & Diff" in text
    assert "Rule blueprint" in text
    assert "Agent-authored draft" in text
    assert "Imported skill" in text
    assert "suggested_artifact" in text
    assert "Exact File Diff" in text
    assert "Edit Structured Workflow" in text
    assert 'id="workflow-project-root"' in text
    assert "Choose a Project Folder" in text
    assert "Selected Project Folder" in text
    assert "Verify Folder" in text
    assert 'id="workflow-type-filter"' in text
    assert 'id="workflow-status-filter"' in text
    assert "not a Git or filesystem lock" in text
    assert "Why Reflect Suggested This" in text
    assert 'aria-label="Workflow approval summary"' in text
    assert "Apply to One Project" in text
    assert "Linked to source evidence" in text
    assert "Different from source evidence" in text
    assert "Only the selected project will be changed" in text
    assert "content.source?.rule_id" in text
    assert "content.behavior_type" in text
    assert "workflow_type" in text
    assert 'class="review-kpi-strip"' in text
    assert 'class="review-grid"' in text
    assert 'class="ledger-action-spacer"' in text
    assert "Show ${fmt(items.length - initial)} More Sessions" in text
    assert "/sessions`" in text
    assert "function sessionInspectionUrl(session)" in text
    assert "data-related-session-link" in text
    assert "url.searchParams.set('tab', 'sessions')" in text
    assert "url.searchParams.set('session', session.session_id || '')" in text
    assert "supporting_observation_count" in text
    assert "Evidence Patterns" in text
    assert "inbox_total_count" in text
    assert "skill_total_count" in text
    assert "current skills" in text
    assert "linked session(s)" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_activity_widgets_live_on_explore_usage_view(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'data-tab="explore">Explore</button>' in text
    assert 'data-explore-view="usage">Usage</button>' in text
    assert 'data-tab="activity"' not in text
    assert 'id="tab-activity"' not in text
    assert 'id="hm-grid"' in text
    assert 'id="hour-bars"' in text
    assert 'id="weekly-trends-table"' in text
    assert "activity: {tab:'explore', view:'usage'}" in text
    assert "context: {tab:'explore', view:'context'}" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_uses_product_navigation_and_durable_improvement_surfaces(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'data-tab="inbox">Inbox</button>' in text
    assert 'data-tab="sessions">Sessions</button>' in text
    assert 'data-tab="workflows">Workflows</button>' in text
    assert 'data-tab="skills">Skills</button>' in text
    assert 'data-tab="impact">Impact</button>' in text
    assert 'data-tab="explore">Explore</button>' in text
    assert 'data-tab="observations"' not in text
    assert 'data-tab="compare"' not in text
    assert 'data-tab="overview"' not in text
    assert 'id="tab-impact"' in text
    assert 'id="tab-explore-usage"' in text
    assert 'id="tab-explore-tools"' in text
    assert 'id="tab-explore-graph"' in text
    assert 'id="tab-explore-context"' in text
    assert 'id="improvement-inbox"' in text
    assert 'id="workflow-ledger"' in text
    assert 'id="loop-ledger"' in text
    assert 'id="skill-registry"' in text
    assert 'id="tab-skills"' in text
    assert 'id="measurement-ledger"' in text
    assert "Supporting telemetry analysis" not in text
    assert 'id="obs-hero-grid"' not in text
    assert 'id="obs-signals"' not in text
    assert 'id="obs-next-moves"' not in text
    assert "fetch('/api/inbox'" in text
    assert "fetch('/api/workflows'" in text
    assert "fetch('/api/loops'" in text
    assert "fetch('/api/skills?limit=500'" in text
    assert "fetch('/api/impact'" in text
    assert "new URL(`/api/explore/${encodeURIComponent(viewName)}`" in text
    assert "/api/improvements" not in text
    assert "/api/measurements" not in text
    assert "/api/tabs/" not in text
    assert "observations: {tab:'inbox'}" in text
    assert "compare: {tab:'impact'}" in text
    assert "overview: {tab:'explore', view:'usage'}" in text
    assert "params.set('view', activeExploreView)" in text
    assert "params.delete('view')" in text
    assert "groupImpactMeasurements(measurements)" in text
    assert 'data-ledger-action="review-impact-sessions"' in text
    assert "View Compared Sessions" in text
    assert "Post-application session collection progress" in text
    assert "function impactTrendPresentation(item, previous, metric)" in text
    assert "metric?.direction || 'lower_is_better'" in text
    assert "direction:'higher_is_better'" in text
    assert "Needs Attention vs Baseline" in text
    assert "Improving${amount} since last check" in text
    assert "moving in the right direction, but not enough yet" in text
    assert "regressedButImproving ? 'Review Progress'" in text
    assert 'data-trend="${escHtml(trend?.kind || \'unknown\')}"' in text
    assert 'id="ledger-dialog"' in text
    assert 'data-ledger-action="evidence"' in text
    assert 'data-ledger-action="review-loop"' in text
    assert 'data-ledger-action="review-skill"' in text
    assert 'data-ledger-action="review-workflow"' in text
    assert "showLoopReview(trigger.dataset.loopId || '')" in text
    assert "showSkillReview(trigger.dataset.skillId || '')" in text
    assert "showWorkflowReview(candidateId)" in text
    assert "submitSessionFeedback(sessionId, outcome, button)" in text
    assert 'data-session-feedback="no-change-correct"' in text
    assert "trigger.textContent = 'Applying…'" in text
    assert "trigger.textContent = 'Approve & Apply to This Project'" in text
    assert "create:'New file'" in text
    assert "Exact File Diff" in text
    assert "Review &amp; Roll Back" in text
    assert "const defaultProductTab = (IMPROVEMENT_DATA.observations || []).length || (IMPROVEMENT_DATA.loops || []).length ? 'inbox' : 'sessions';" in text

    sessions_panel = text[text.index('id="tab-sessions"'):text.index('id="tab-explore-usage"')]
    usage_panel = text[text.index('id="tab-explore-usage"'):text.index('id="tab-impact"')]
    impact_panel = text[text.index('id="tab-impact"'):text.index('id="tab-inbox"')]
    assert 'id="cmp-a"' in sessions_panel
    assert 'id="cohort-comparison-panel"' in usage_panel
    assert 'id="measurement-ledger"' in impact_panel
    assert 'id="cmp-a"' not in impact_panel
    assert 'id="cohort-comparison-panel"' not in impact_panel


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_workflows_and_skills_use_responsive_tiles(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'class="workflow-list workflow-tile-grid" id="workflow-ledger"' in text
    assert 'class="workflow-list skill-tile-grid" id="skill-registry"' in text
    assert 'id="skill-search" type="search"' in text
    assert 'id="skill-filter-summary" aria-live="polite"' in text
    assert "function skillMatchesSearch(skill, query)" in text
    assert "params.set('skill_q', query.trim())" in text
    assert "fetch('/api/skills?limit=500'" in text
    assert ".workflow-tile-grid{grid-template-columns:repeat(auto-fit" in text
    assert ".skill-tile-grid{grid-template-columns:repeat(auto-fit" in text
    assert "function renderWorkflowSteps(steps, {limit = 0, compact = false} = {})" in text
    assert 'class="workflow-step-number" aria-hidden="true"' in text
    assert "renderWorkflowSteps(content.steps)" in text
    assert "renderWorkflowSteps(steps, {limit:4, compact:true})" in text
    assert '<dl class="tile-metric-grid">' in text
    assert 'class="skill-detail-grid"' in text
    assert 'class="tile-card-title"' in text
    assert "touch-action:manipulation" in text
    assert "function skillAvailabilityPresentation(skill, installations = null)" in text
    assert "Available in Codex" in text
    assert "Available in workspace" in text
    assert "Telemetry only" in text
    assert "Available to other agents" in text
    assert "Registry history and observed usage do not install a skill." in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_does_not_keep_replaced_conversation_or_telemetry_rails(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "function getEventBadge(" not in text
    assert "function getEventBodyLabel(" not in text
    assert "function buildTelemetryBeatRail(" not in text
    assert "function buildSessionObservationMarkers(" not in text
    assert "function replaceChart(" not in text
    assert ".telemetry-beat-row{" not in text
    assert ".telemetry-observation{" not in text
    assert ".carousel-outer{" not in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_usage_separates_source_provenance_from_event_semantics(path: Path):
    text = path.read_text(encoding="utf-8")

    assert 'id="source-provenance"' in text
    assert 'id="agentCostChart"' in text
    assert "const sourceProvenance = usageTab.source_provenance || D.source_provenance || [];" in text
    assert "function validCostTrendDay(value)" in text
    assert "Number(day.slice(0, 4)) < 2000" in text
    assert "function normalizeAgentCostRows(rows)" in text
    assert "function deriveAgentCostRowsFromSessions(sessions)" in text
    assert "const rawUsageAgentCostRows = usageTab.agent_cost_over_time || D.agent_cost_over_time || [];" in text
    assert "const normalizedUsageAgentCostRows = normalizeAgentCostRows(rawUsageAgentCostRows);" in text
    assert "deriveAgentCostRowsFromSessions(D.sessions || [])" in text
    assert "Cost totals are available, but priced sessions do not have valid dates for a trend chart." in text
    assert "makeLine('agentCostChart'" in text
    assert "Transport/source provenance. The chart above stays semantic by event type." in text
    assert "No model calls match the current selection." in text
    assert "No events match the current selection." in text
    assert "No token usage is available for the current selection." in text
    assert "No hourly activity matches the current selection." in text
    assert "At least one dated activity week is needed for a trend." in text
    assert "function renderChartEmpty(id, message)" in text
    assert "const usageModelSeries = usageModelEntries.slice(0, 8);" in text
    assert "if (usageModelOther > 0) usageModelSeries.push(['Other models', usageModelOther]);" in text
    assert "const types = allTypes.slice(0, 12);" in text
    assert "Showing the 12 most-launched types" in text
    assert "const wt = (D.weekly_trends || []).slice(-12);" in text
    assert 'title="${escHtml(String(m.value))}"' in text
    assert "params.delete('workflow_type');" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_prefers_sql_tab_payloads_for_existing_tabs(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "function sqlTab(name)" in text
    assert "const activityTab = sqlTab('activity');" in text
    assert "activityTab.activity_by_day || D.activity_by_day || {}" in text
    assert "sqlTab('activity').tool_percentiles" not in text
    assert "toolsTab.tools_by_count || D.tools_by_count || {}" in text
    assert "mcpTab.mcp_server_before || D.mcp_server_before || {}" in text
    assert "sqlTab('graph').graph_tool_transitions || D.graph_tool_transitions || []" in text
    assert "sqlTab('graph').graph_dep || D.graph_dep" in text


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
    assert "visibleIdsForSessions" in text
    assert "let adjacency = new Map();" in text
    assert "while (frontier.length)" in text
    assert "if (edge.session_id && !sessionIds.has(edge.session_id)) continue;" in text


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
def test_dashboard_session_timeline_controls_conversation_playhead(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "class SessionConversationPlayhead" in text
    assert 'class="session-timeline-playhead" role="slider"' in text
    assert 'aria-label="Conversation position"' in text
    assert 'data-timeline-event-index="${ev._conversationIndex}"' in text
    assert 'data-timeline-summary="${tooltipAttr(summary)}"' in text
    assert 'data-conversation-event-index="${index}"' in text
    assert 'data-conversation-event-index="${ev._conversationIndex}"' in text
    assert "this.track.setPointerCapture?.(event.pointerId)" in text
    assert "key === 'ArrowLeft' || key === 'ArrowUp'" in text
    assert "key === 'ArrowRight' || key === 'ArrowDown'" in text
    assert "this.syncFromConversation()" in text
    assert "Drag to scan conversation" in text
    assert ".chat-msg.is-playhead-active .chat-bubble{" in text
    assert ".session-timeline-playhead:focus-visible{" in text
    assert "this.reducedMotion" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_conversation_reader_supports_focus_search_and_actions(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "function conversationMatchIndexes(conversation, query)" in text
    assert "function highlightConversationText(value, query)" in text
    assert "function renderConversationReaderToolbar(session, conversation)" in text
    assert 'data-conversation-mode="focused"' in text
    assert 'data-conversation-mode="full"' in text
    assert 'aria-label="Search this conversation"' in text
    assert 'aria-keyshortcuts="Meta+f Control+f /"' in text
    assert 'data-conversation-search-nav="previous"' in text
    assert 'data-conversation-search-nav="next"' in text
    assert "session._conversationMode === 'full' || Boolean" in text
    assert "const lastResponseIndex = turn.events.findLastIndex" in text
    assert 'data-conversation-failure' in text
    assert 'data-copy-conversation="${index}"' in text
    assert "selectConversationEvent(session, matches[session._conversationMatchCursor])" in text
    assert "const findShortcut = (event.metaKey || event.ctrlKey)" in text
    assert ".conversation-reader-toolbar{" in text
    assert ".chat-msg.is-search-match .chat-bubble" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_conversation_preview_expands_real_content(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "session._expandedConversationEvents instanceof Set" in text
    assert "const previewLimit = isExpanded ? 20000 : 280;" in text
    assert "fullText.slice(0, expanded ? 20000 : 280)" in text
    assert ".ev-preview.full-content.expanded{max-height:none}" in text
    assert "hint.textContent = expanded ? 'Tap to collapse' : 'Tap to expand';" in text


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_supplementary_data_cannot_block_initial_render(path: Path):
    text = path.read_text(encoding="utf-8")

    assert "const controller = new AbortController();" in text
    assert "window.setTimeout(() => controller.abort(), 3000)" in text
    assert "signal: controller.signal" in text
    assert "window.clearTimeout(timeout);" in text


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
