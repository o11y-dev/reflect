/* ════════════════════ EXPLORE: USAGE ════════════════════ */

/* Stat row */
function renderUsageSummary(){
  const usageTab = sqlTab('usage');
  const statDefs = [
    { label:'Avg Quality',   value:Number(usageTab.avg_quality_score || 0).toFixed(1)+'%', sub:'current selection',              c:Number(usageTab.avg_quality_score || 0) > 70 ? 'var(--green)' : 'var(--yellow)' },
    { label:'Sessions',      value:fmt(usageTab.unique_sessions || 0),  sub:isStaticDemo() ? 'Full illustrative sample' : D.measurements?.scope?.kind === 'event_window' ? String(D.measurements.scope.date_from || '').slice(0,10)+' — '+String(D.measurements.scope.date_to || '').slice(0,10) : String(usageTab.first_event_ts || '').slice(0,10)+' - now',   c:'var(--purple)' },
    { label:'Prompts',       value:fmt(usageTab.prompt_submits || 0),   sub:'user prompt submits',                   c:'var(--teal)' },
    { label:'Tool Calls',    value:fmt(usageTab.tool_calls || 0),       sub:String(usageTab.tool_to_prompt_ratio || '0.0')+':1 per prompt',  c:'var(--blue)' },
  ];
  document.getElementById('stat-row').innerHTML = statDefs.map(s=>`
    <div class="stat" style="--c:${s.c}">
      <div class="stat-label">${s.label}</div>
      <div class="stat-value">${s.value}</div>
      <div class="stat-sub">${s.sub}</div>
    </div>`).join('');
}

function renderSqlTabPayloads(){
  if (D._unpublishedNavigation && exploreViewScopes.get('context')?.scope !== exploreViewUrl('context').url.toString()) return;
  const tabs = (D.sqlite && D.sqlite.tabs) || {};
  const specs = tabs.specs || {};
  const memory = tabs.memory || {};
  const privacy = tabs.privacy || {};
  const exports = tabs.exports || {};
  const statRow = document.getElementById('data-stat-row');
  const specsEl = document.getElementById('sql-specs-panel');
  const memoryEl = document.getElementById('sql-memory-panel');
  const privacyEl = document.getElementById('sql-privacy-panel');
  const exportsEl = document.getElementById('sql-exports-panel');
  if (!statRow || !specsEl || !memoryEl || !privacyEl || !exportsEl) return;

  const rowCounts = exports.row_counts || {};
  statRow.innerHTML = [
    {label:'Task Contracts', value:fmt(specs.total_specs || 0), sub:`${fmt(Object.values(specs.requirements_by_status || {}).reduce((a,b)=>a+b,0))} requirements`},
    {label:'Memory', value:fmt(memory.total_memories || 0), sub:`${fmt(Object.keys(memory.memories_by_type || {}).length)} types`},
    {label:'Privacy', value:fmt(privacy.total_findings || 0), sub:`${fmt(Object.keys(privacy.findings_by_severity || {}).length)} severities`},
    {label:'Records', value:fmt(Object.values(rowCounts).reduce((a,b)=>a + Number(b || 0), 0)), sub:(exports.export_ready ? 'Ready for export' : 'No records yet')},
  ].map(item => `<div class="stat"><div class="stat-label">${item.label}</div><div class="stat-value">${item.value}</div><div class="stat-sub">${item.sub}</div></div>`).join('');

  const empty = label => `<div class="empty-note">No ${label} data yet.</div>`;
  const countTable = (items, label) => {
    const entries = Object.entries(items || {});
    if (!entries.length) return empty(label);
    return `<table class="data-table"><thead><tr><th>${label}</th><th style="text-align:right">Count</th></tr></thead><tbody>${
      entries.map(([name,count]) => `<tr><td>${escHtml(name)}</td><td style="text-align:right">${fmt(count || 0)}</td></tr>`).join('')
    }</tbody></table>`;
  };

  const specRows = (specs.specs || []).slice(0, 8);
  specsEl.innerHTML = specRows.length
    ? `<table class="data-table"><thead><tr><th>Contract</th><th>Status</th><th style="text-align:right">Req</th><th style="text-align:right">Evidence</th></tr></thead><tbody>${
        specRows.map(spec => `<tr>
          <td><strong>${escHtml(spec.title || spec.id || 'Untitled')}</strong><div style="color:var(--text-3);font-size:12px">${escHtml(spec.source_path || spec.owner || '')}</div></td>
          <td>${escHtml(spec.status || 'unknown')}</td>
          <td style="text-align:right">${fmt(spec.requirements || 0)}</td>
          <td style="text-align:right">${fmt(spec.evidence || 0)}</td>
        </tr>`).join('')
      }</tbody></table>`
    : countTable(specs.requirements_by_status, 'Requirement Status');

  const memoryRows = (memory.recent_memories || []).slice(0, 8);
  memoryEl.innerHTML = memoryRows.length
    ? `<table class="data-table"><thead><tr><th>Memory</th><th>Scope</th><th>Type</th><th style="text-align:right">Sessions</th><th style="text-align:right">Confidence</th></tr></thead><tbody>${
        memoryRows.map(item => `<tr>
          <td><strong>${escHtml(item.preview || item.id || 'Memory')}</strong><div style="color:var(--text-3);font-size:12px">${escHtml(item.source || '')}</div></td>
          <td>${escHtml(item.scope || '')}</td>
          <td>${escHtml(item.type || '')}</td>
          <td style="text-align:right">${fmt(item.exposure_count || 0)}</td>
          <td style="text-align:right">${Math.round(Number(item.confidence || 0) * 100)}%</td>
        </tr>`).join('')
      }</tbody></table>`
    : countTable(memory.memories_by_type, 'Memory Type');

  const privacyRows = (privacy.recent_findings || []).slice(0, 8);
  privacyEl.innerHTML = privacyRows.length
    ? `<table class="data-table"><thead><tr><th>Finding</th><th>Severity</th><th>Action</th><th>Field</th></tr></thead><tbody>${
        privacyRows.map(item => `<tr>
          <td>${escHtml(item.type || 'finding')}</td>
          <td>${escHtml(item.severity || 'unknown')}</td>
          <td>${escHtml(item.action_taken || '')}</td>
          <td>${escHtml(item.field_name || '')}</td>
        </tr>`).join('')
      }</tbody></table>`
    : countTable(privacy.findings_by_severity, 'Severity');

  exportsEl.innerHTML = Object.keys(rowCounts).length
    ? `<table class="data-table"><thead><tr><th>Table</th><th style="text-align:right">Rows</th></tr></thead><tbody>${
        Object.entries(rowCounts).map(([table,count]) => `<tr><td>${escHtml(table)}</td><td style="text-align:right">${fmt(count || 0)}</td></tr>`).join('')
      }</tbody></table>`
    : empty('export');
}
renderSqlTabPayloads();

function formatSignedDelta(delta, suffix = ''){
  const value = Number(delta || 0);
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toFixed(1)}${suffix}`;
}

function buildCohortComparison(){
  const costUnit = String(sqlTab('usage').pricing_unit || 'usd').toUpperCase();
  const summaryEl = document.getElementById('cohort-comparison-summary');
  const detailsEl = document.getElementById('cohort-comparison-details');
  const baselineEl = document.getElementById('cohort-baseline-agents');
  if (!summaryEl || !detailsEl || !baselineEl) return;

  const cohortTab = sqlTab('cohort_comparison');
  const comparison = cohortTab.comparison;
  const comparisonScopeCaption = `<caption style="caption-side:top;text-align:left;color:var(--text-2);font-size:12px;padding-bottom:10px"><strong>Full lifetime of matching sessions</strong>${D.measurements?.scope?.kind === 'event_window' ? ' · Includes activity outside the selected period; period totals count only events within it.' : ''}</caption>`;
  if (comparison && comparison.primary && comparison.baseline) {
    const primary = comparison.primary;
    const baseline = comparison.baseline;
    const deltas = comparison.deltas || {};
    const metricRows = [
      ['Sessions', fmt(primary.sessions), fmt(baseline.sessions), deltas.sessions],
      ['Prompts', fmt(primary.prompts), fmt(baseline.prompts), deltas.prompts],
      ['Tool Calls', fmt(primary.tool_calls), fmt(baseline.tool_calls), deltas.tool_calls],
      ['Avg Quality', `${primary.avg_quality.toFixed(1)}%`, `${baseline.avg_quality.toFixed(1)}%`, deltas.avg_quality],
      ['Failure Rate', `${primary.failure_rate_pct.toFixed(1)}%`, `${baseline.failure_rate_pct.toFixed(1)}%`, deltas.failure_rate_pct],
      ['Tokens', fmt(primary.tokens), fmt(baseline.tokens), deltas.tokens],
      ['Shell Runs', fmt(primary.shell_runs), fmt(baseline.shell_runs), deltas.shell_runs],
      ['MCP Calls', fmt(primary.mcp_calls), fmt(baseline.mcp_calls), deltas.mcp_calls],
    ];

    summaryEl.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px">
        <div class="compare-card compare-card-strong">
          <div class="compare-kicker">Primary cohort</div>
          <div class="compare-value">${escHtml(primary.label)}</div>
          <div class="compare-copy">${fmt(primary.sessions)} sessions across ${fmt(primary.agents.length)} agent type${primary.agents.length === 1 ? '' : 's'}</div>
        </div>
        <div class="compare-card">
          <div class="compare-kicker">Baseline</div>
          <div class="compare-value">${escHtml(baseline.label)}</div>
          <div class="compare-copy">${fmt(baseline.sessions)} sessions across ${fmt(baseline.agents.length)} agent type${baseline.agents.length === 1 ? '' : 's'}</div>
        </div>
        <div class="compare-card">
          <div class="compare-kicker">Headline</div>
          <div class="compare-value" style="color:${(deltas.avg_quality?.absolute || 0) >= 0 ? 'var(--green)' : 'var(--red)'}">${formatSignedDelta(deltas.avg_quality?.absolute || 0, ' pts')}</div>
          <div class="compare-copy">quality delta, ${formatSignedDelta(deltas.failure_rate_pct?.absolute || 0, ' pts')} failure-rate delta</div>
        </div>
      </div>
    `;
    detailsEl.innerHTML = `<table class="tbl">${comparisonScopeCaption}<thead><tr><th>Metric</th><th style="text-align:right">${escHtml(primary.label)}</th><th style="text-align:right">${escHtml(baseline.label)}</th><th style="text-align:right">Delta</th></tr></thead><tbody>${
      metricRows.map(([label, a, b, delta]) => `
        <tr>
          <td style="font-weight:600">${label}</td>
          <td style="text-align:right">${a}</td>
          <td style="text-align:right">${b}</td>
          <td style="text-align:right;color:${(delta?.absolute || 0) >= 0 ? 'var(--green)' : 'var(--red)'}">${delta?.pct == null ? formatSignedDelta(delta?.absolute || 0) : `${formatSignedDelta(delta?.absolute || 0)} (${delta.pct > 0 ? '+' : ''}${delta.pct}%)`}</td>
        </tr>
      `).join('')
    }</tbody></table>`;
    baselineEl.innerHTML = (comparison.baseline_agents || []).length
      ? `<table class="data-table">${comparisonScopeCaption}<thead><tr><th>Agent</th><th style="text-align:right">Sessions</th><th style="text-align:right">Tools</th><th style="text-align:right">Cost</th><th style="text-align:right">Quality</th></tr></thead><tbody>${
          comparison.baseline_agents.map(agent => `
            <tr>
              <td style="font-weight:600">${escHtml(agent.name || 'unknown')}</td>
              <td style="text-align:right">${fmt(agent.sessions || 0)}</td>
              <td style="text-align:right">${fmt(agent.tools || 0)}</td>
              <td style="text-align:right">${fmtCost(agent.total_cost_usd || agent.total_cost || 0)} ${escHtml(costUnit)}</td>
              <td style="text-align:right">${Number(agent.avg_quality || 0).toFixed(1)}%</td>
            </tr>
          `).join('')
        }</tbody></table>`
      : `<div style="color:var(--text-3)">No baseline agent mix available.</div>`;
    return;
  }

  const ranking = (cohortTab.agent_comparison || currentAgentComparison())
    .slice()
    .sort((a, b) => Number(b.sessions || 0) - Number(a.sessions || 0));
  summaryEl.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap">
      <div>
        <div style="font-size:24px;font-weight:800;color:var(--signal-2)">Cross-agent comparison</div>
        <div style="color:var(--text-2);margin-top:4px">Filter to one agent to compare that cohort against all other agents still in scope.</div>
      </div>
      <div style="font-size:13px;color:var(--text-3)">Current filters select the sessions; comparison totals cover their full lifetimes.</div>
    </div>
  `;
  detailsEl.innerHTML = ranking.length
    ? `<table class="tbl">${comparisonScopeCaption}<thead><tr><th>Agent</th><th style="text-align:right">Sessions</th><th style="text-align:right">Prompts</th><th style="text-align:right">Tools</th><th style="text-align:right">Cost</th><th style="text-align:right">Failures</th><th style="text-align:right">Quality</th></tr></thead><tbody>${
        ranking.map(agent => `
          <tr>
            <td style="font-weight:600">${escHtml(agent.name || 'unknown')}</td>
            <td style="text-align:right">${fmt(agent.sessions || 0)}</td>
            <td style="text-align:right">${fmt(agent.prompts || 0)}</td>
            <td style="text-align:right">${fmt(agent.tools || 0)}</td>
            <td style="text-align:right">${fmtCost(agent.total_cost_usd || agent.total_cost || 0)} ${escHtml(costUnit)}</td>
            <td style="text-align:right">${fmt(agent.failures || 0)}</td>
            <td style="text-align:right">${Number(agent.avg_quality || 0).toFixed(1)}%</td>
          </tr>
        `).join('')
      }</tbody></table>`
    : `<div style="color:var(--text-3)">No comparison data available for the current report.</div>`;
  baselineEl.innerHTML = `<div style="color:var(--text-3)">Apply an agent filter to unlock primary-vs-baseline comparison and keep session A/B below for drill-down.</div>`;
}
document.addEventListener('dashboard-filters-changed', event => {
  dashboardSelectedSessionId = event.detail?.selectedSessionId || dashboardSelectedSessionId || '';
  dashboardFilterActive = Boolean(event.detail?.hasFilters || dashboardSelectedSessionId);
  buildCohortComparison();
});

/* Metric tiles */
function renderUsageMetrics(){
  const usageTab = sqlTab('usage');
  const usageModels = usageTab.models_by_count || sqlTab('models').models_by_count || {};
  const usageEvents = usageTab.events_by_type || sqlTab('activity').events_by_type || {};
  const usageModelCosts = usageTab.model_costs || sqlTab('costs').model_costs || {};
  const costUnit = String(usageTab.pricing_unit || 'usd').toUpperCase();
  const ph   = Number(usageTab.peak_hour ?? -1) >= 0 ? h12(usageTab.peak_hour) : '--';
  const signatureCommand = usageTab.signature_command || '';
  const sig  = signatureCommand ? signatureCommand.slice(0,28)+(signatureCommand.length>28?'…':'') : '—';
  const metricDefs = [
    { label:'MCP Calls',          value:fmt(usageTab.mcp_calls || 0),           sub:Object.keys(usageTab.mcp_servers_by_count || {}).length+' servers' },
    { label:'Subagents Launched', value:fmt(usageTab.subagent_launches || 0),   sub:Object.keys(usageTab.subagent_types_by_count || {}).length+' agent types' },
    { label:'File Edits',         value:fmt(usageTab.file_edits || 0),          sub:'modified files' },
    { label:'Shell Runs',         value:fmt(usageTab.shell_executions || 0),    sub:fmt(usageTab.unique_commands || 0)+' unique patterns' },
    { label:'Tool Failures',      value:fmt(usageTab.tool_failures || 0),       sub:Number(usageTab.failure_rate_pct || 0)+'% failure rate' },
    { label:'Peak Hour',          value:ph,                         sub:fmt(usageTab.peak_hour_count || 0)+' events at peak' },
    { label:'AI Models',          value:fmt(usageTab.unique_models || 0),       sub:'distinct models used' },
    { label:'Signature Command',  value:sig,                        sub:fmt(usageTab.signature_command_count || 0)+'x run' },
  ];
  document.getElementById('metric-row').innerHTML = metricDefs.map(m=>`
    <div class="metric">
      <div class="metric-label">${m.label}</div>
      <div class="metric-value" title="${escHtml(String(m.value))}">${escHtml(String(m.value))}</div>
      <div class="metric-sub">${escHtml(String(m.sub))}</div>
    </div>`).join('');

  /* Charts */
  Chart.defaults.color = 'rgba(255,255,255,.5)';
  Chart.defaults.borderColor = 'rgba(255,255,255,.06)';

  const usageModelEntries = Object.entries(usageModels)
    .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0) || String(a[0]).localeCompare(String(b[0])));
  const usageModelSeries = usageModelEntries.slice(0, 8);
  const usageModelOther = usageModelEntries.slice(8).reduce((sum, entry) => sum + Number(entry[1] || 0), 0);
  if (usageModelOther > 0) usageModelSeries.push(['Other models', usageModelOther]);
  if (usageModelSeries.length) {
    makeDoughnut('modelChart',
      usageModelSeries.map(([model])=>model.replace('claude-4.6-','cl-').replace('claude-','cl-').replace('gpt-5.4-','gpt-')),
      usageModelSeries.map(([, count])=>count));
  } else {
    renderUsageChartEmpty('modelChart', 'No model calls match the current selection.');
  }

  if (Object.keys(usageEvents).length) {
    makeHBar('eventChart',
      Object.keys(usageEvents).slice(0,8),
      Object.values(usageEvents).slice(0,8),
      P.blue);
  } else {
    renderUsageChartEmpty('eventChart', 'No events match the current selection.');
  }
  const sourceProvenance = usageTab.source_provenance || [];
  const sourceProvenanceEl = document.getElementById('source-provenance');
  if (sourceProvenanceEl) {
    sourceProvenanceEl.innerHTML = sourceProvenance.length
      ? `<div style="font-size:12px;color:var(--text-3);margin-bottom:8px">Transport/source provenance. The chart above stays semantic by event type.</div>
         <table class="tbl"><thead><tr><th>Source</th><th>Transport</th><th style="text-align:right">Events</th></tr></thead><tbody>${
            sourceProvenance.slice(0, 6).map(entry => `
              <tr>
                <td style="font-weight:600">${escHtml(entry.label || entry.origin_kind || 'unknown')}</td>
                <td>${escHtml(String(entry.transport || 'unknown').replace(/_/g, ' '))}</td>
                <td style="text-align:right">${fmt(entry.event_count || 0)}</td>
              </tr>
            `).join('')
         }</tbody></table>`
      : `<div style="font-size:12px;color:var(--text-3)">No source provenance available for the current selection.</div>`;
  }

  /* Token chart + stats */
  function validCostTrendDay(value){
    const day = String(value || '').slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return '';
    if (Number(day.slice(0, 4)) < 2000) return '';
    return day;
  }
  function normalizeAgentCostRows(rows){
    return (rows || []).map(row => ({
      day: validCostTrendDay(row.day),
      agent: String(row.agent || 'unknown'),
      total_cost: Number(row.total_cost || 0),
    })).filter(row => row.day && row.total_cost > 0);
  }
  function hasPricedSessionCost(sessions){
    return (sessions || []).some(session => Number(session.total_cost_usd ?? session.total_cost ?? session.estimated_cost_usd ?? 0) > 0);
  }
  function deriveAgentCostRowsFromSessions(sessions){
    const rows = new Map();
    (sessions || []).forEach(session => {
      const totalCost = Number(session.total_cost_usd ?? session.total_cost ?? session.estimated_cost_usd ?? 0);
      if (!totalCost) return;
      const day = validCostTrendDay(session.day || session.started_at || session.created_at || session.timestamp);
      if (!day) return;
      const agent = String(session.agent || 'unknown');
      const key = `${day}\u0000${agent}`;
      const current = rows.get(key) || {day, agent, total_cost: 0};
      current.total_cost += totalCost;
      rows.set(key, current);
    });
    return Array.from(rows.values()).sort((a, b) => String(a.day).localeCompare(String(b.day)) || Number(b.total_cost || 0) - Number(a.total_cost || 0) || String(a.agent).localeCompare(String(b.agent)));
  }
  const rawUsageAgentCostRows = usageTab.agent_cost_over_time || [];
  const normalizedUsageAgentCostRows = normalizeAgentCostRows(rawUsageAgentCostRows);
  const usageAgentCostRows = normalizedUsageAgentCostRows.length ? normalizedUsageAgentCostRows : D.sql_backed ? [] : deriveAgentCostRowsFromSessions(D.sessions || []);
  const agentCostChartEl = document.getElementById('agentCostChart');
  if (agentCostChartEl) {
    if (usageAgentCostRows.length) {
      const dayLabels = Array.from(new Set(usageAgentCostRows.map(row => String(row.day || '')))).filter(Boolean).sort();
      const totalsByAgent = new Map();
      const byAgentDay = new Map();
      usageAgentCostRows.forEach(row => {
        const agent = String(row.agent || 'unknown');
        const day = String(row.day || '');
        const totalCost = Number(row.total_cost || 0);
        totalsByAgent.set(agent, (totalsByAgent.get(agent) || 0) + totalCost);
        if (!byAgentDay.has(agent)) byAgentDay.set(agent, new Map());
        byAgentDay.get(agent).set(day, totalCost);
      });
      const topAgents = Array.from(totalsByAgent.entries())
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
        .slice(0, 6)
        .map(([agent]) => agent);
      makeLine('agentCostChart', dayLabels, topAgents.map((agent, idx) => ({
        label: agent,
        data: dayLabels.map(day => byAgentDay.get(agent)?.get(day) || 0),
        borderColor: COLORS[idx % COLORS.length],
        backgroundColor: COLORS[idx % COLORS.length],
      })), true);
    } else {
      const hasCost = D.sql_backed ? Number(usageTab.total_cost_usd || 0) > 0 : hasPricedSessionCost(D.sessions || []);
      const message = hasCost
        ? 'Cost totals are available, but priced sessions do not have valid dates for a trend chart.'
        : 'No priced sessions available yet for agent cost trends.';
      renderUsageChartEmpty('agentCostChart', message);
    }
  }
  const usageInputTokens = usageTab.total_input_tokens || 0;
  const usageOutputTokens = usageTab.total_output_tokens || 0;
  const usageCacheCreationTokens = usageTab.total_cache_creation_tokens || 0;
  const usageCacheReadTokens = usageTab.total_cache_read_tokens || 0;
  if (usageInputTokens > 0 || usageOutputTokens > 0 || usageCacheCreationTokens > 0 || usageCacheReadTokens > 0) {
    makeDoughnut('tokenChart',
      ['Input','Output','Cache Creation','Cache Read'],
      [usageInputTokens, usageOutputTokens, usageCacheCreationTokens, usageCacheReadTokens]);
  } else {
    renderUsageChartEmpty('tokenChart', 'No token usage is available for the current selection.');
  }
  document.getElementById('stat-input-tok').textContent = fmt(usageInputTokens);
  document.getElementById('stat-output-tok').textContent = fmt(usageOutputTokens);
  document.getElementById('stat-cache-tok').textContent = fmt(usageCacheReadTokens);
  document.getElementById('stat-tok-ratio').textContent = usageInputTokens ? (usageOutputTokens / usageInputTokens).toFixed(2) : '--';

  const pricingCoverage = D.measurements?.coverage || {};
  const bucketCost = amount => pricingCoverage.priced_usage ? fmtCost(amount || 0) : 'Unavailable';
  const costStats = [
    ['Total Cost', pricingCoverage.cost_status === 'unavailable' ? 'Unavailable' : fmtCost(usageTab.total_cost_usd || 0), costUnit],
    ['Input Cost', bucketCost(usageTab.input_cost_usd), costUnit],
    ['Output Cost', bucketCost(usageTab.output_cost_usd), costUnit],
    ['Cache Cost', bucketCost(Number(usageTab.cache_creation_cost_usd || 0) + Number(usageTab.cache_read_cost_usd || 0)), costUnit],
    ['Cost Basis', escHtml(pricingCoverage.cost_status || 'unavailable'), `${fmt(pricingCoverage.priced_usage || 0)}/${fmt(pricingCoverage.observed_usage || 0)} usage records priced`],
  ];
  const costStatsEl = document.getElementById('cost-stats');
  if (costStatsEl) {
    costStatsEl.innerHTML = costStats.map(([label, value, sub]) => `
      <div style="background:var(--surface2);border-radius:8px;padding:14px 16px">
        <div style="font-size:12px;color:var(--text-3);text-transform:uppercase;letter-spacing:.05em">${label}</div>
        <div style="font-size:22px;font-weight:700;margin-top:4px">${value}</div>
        <div style="font-size:12px;color:var(--text-3);margin-top:4px">${sub}</div>
      </div>
    `).join('');
  }
  const modelCostRows = Object.entries(usageModelCosts)
    .sort((a,b) => Number(b[1] || 0) - Number(a[1] || 0))
    .slice(0, 8);
  const modelCostEl = document.getElementById('model-cost-share');
  if (modelCostEl) {
    modelCostEl.innerHTML = modelCostRows.length
      ? `<table class="tbl"><thead><tr><th>Model</th><th style="text-align:right">Estimated cost</th></tr></thead><tbody>${
          modelCostRows.map(([model, cost]) => `
            <tr><td style="font-weight:600">${escHtml(model)}</td><td style="text-align:right">${fmtCost(cost)} ${escHtml(costUnit)}</td></tr>
          `).join('')
        }</tbody></table>`
      : `<div style="color:var(--text-3);padding:12px 0">${isStaticDemo() ? 'Pricing is unavailable in this sample; unknown cost is not zero.' : 'No model cost data available. Run reflect doctor to inspect pricing status.'}</div>`;
  }

}

const IMPACT_METRIC_PRESENTATION = Object.freeze({
  tool_failure_rate:{goal:'Recover From Tool Failures Without Repeating Them',measure:'Failed tool call rate',format:'percent',direction:'lower_is_better'},
  identical_retry_calls:{goal:'Avoid Repeating Unchanged Tool Calls',measure:'Identical retry calls',format:'number',direction:'lower_is_better'},
  unverified_change_sessions:{goal:'Finish Changes With Verification',measure:'Unverified change tasks',format:'percent',direction:'lower_is_better'},
  context_outlier_sessions:{goal:'Reduce Oversized Context',measure:'Average context size',format:'tokens',direction:'lower_is_better'},
  read_only_exploration_calls:{goal:'Shorten Unproductive Exploration',measure:'Read-only exploration calls',format:'number',direction:'lower_is_better'},
  operator_correction_rate:{goal:'Reduce Operator Corrections',measure:'Operator correction rate',format:'percent',direction:'lower_is_better'},
  constraint_violation_rate:{goal:'Reduce Constraint Violations',measure:'Constraint violations',format:'number',direction:'lower_is_better'},
  recovered_failure_sessions:{goal:'Improve Failure Recovery',measure:'Recovered failure tasks',format:'percent',direction:'higher_is_better'},
  successful_workflow_sessions:{goal:'Increase Successful Workflow Runs',measure:'Successful workflow tasks',format:'percent',direction:'higher_is_better'},
  correct_no_change_sessions:{goal:'Recognize Correct No-Change Outcomes',measure:'Correct no-change tasks',format:'percent',direction:'higher_is_better'},
  workflow_adherence:{goal:'Preserve the Proven Procedure',measure:'Procedure adherence',format:'percent',direction:'higher_is_better'},
});

function impactMetricPresentation(metricName){
  const name = String(metricName || 'workflow_outcome');
  return IMPACT_METRIC_PRESENTATION[name] || {
    goal:humanizeLedgerLabel(name),
    measure:humanizeLedgerLabel(name),
    format:'number',
    direction:'lower_is_better',
  };
}

function groupImpactMeasurements(measurements){
  const groups = new Map();
  (measurements || []).forEach(item => {
    const key = String(item.candidate_id || item.id || 'unknown');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  return [...groups.values()].map(items => items.sort((a,b) => String(b.measured_at || '').localeCompare(String(a.measured_at || ''))));
}

function formatImpactValue(metric, value){
  if (value == null || !Number.isFinite(Number(value))) return 'Not available';
  const number = Number(value);
  if (metric.format === 'percent') return `${fmt(number * 100)}%`;
  if (metric.format === 'tokens') return `${fmt(Math.round(number))} tokens/task`;
  return `${fmt(Number(number.toFixed(2)))} per task`;
}

function formatImpactSessionValue(metricName, value, item = {}){
  if (item.adherence_state === 'followed') return 'Procedure followed';
  if (item.adherence_state === 'not_followed') return 'Procedure not followed';
  if (value == null || !Number.isFinite(Number(value))) return 'Metric unavailable';
  const number = Number(value);
  if (metricName === 'tool_failure_rate') return `${fmt(Number((number * 100).toFixed(2)))}% failed`;
  if (metricName === 'identical_retry_calls') return `${fmt(number)} retry ${number === 1 ? 'call' : 'calls'}`;
  if (metricName === 'unverified_change_sessions') return number > 0 ? 'Unverified change' : 'No unverified change';
  if (metricName === 'context_outlier_sessions') return `${fmt(Math.round(number))} tokens`;
  if (metricName === 'read_only_exploration_calls') return `${fmt(number)} read-only ${number === 1 ? 'call' : 'calls'}`;
  if (metricName === 'operator_correction_rate') return number > 0 ? 'Correction recorded' : 'No correction';
  if (metricName === 'constraint_violation_rate') return `${fmt(number)} ${number === 1 ? 'violation' : 'violations'}`;
  if (metricName === 'recovered_failure_sessions') return number > 0 ? 'Failure recovered' : 'No recovered failure';
  if (metricName === 'successful_workflow_sessions') return number > 0 ? 'Successful run' : 'Run not successful';
  if (metricName === 'correct_no_change_sessions') return number > 0 ? 'Correct no-change' : 'No no-change outcome';
  return formatImpactValue(impactMetricPresentation(metricName), number);
}

function impactMeasurementState(item){
  const explicit = String(item?.cohort?.measurement_state || item?.measurement_state || '');
  if (explicit) return explicit;
  return item?.verdict === 'insufficient_data' ? 'collecting' : 'measured';
}

function impactBlockedGuidance(item = {}){
  const state = impactMeasurementState(item);
  const baseline = item.cohort?.baseline_state;
  const inspect = {action:'review-impact-sessions',actionLabel:'Inspect Task Evidence'};
  if (baseline === 'historical_unavailable') return {
    ...inspect, label:'Historical Baseline Unavailable',actionLabel:'Inspect Baseline Evidence',
    summary:'Historical baseline unavailable: the selected pre-activation evidence currently does not support the required sample for this bounded comparison. Post-activation tasks cannot fill this baseline; late or backfilled pre-activation evidence may change eligibility.',
    nextStep:'Inspect the baseline evidence to identify which pre-activation records are missing or ineligible.',
  };
  if (baseline === 'analysis_pending' || state === 'validation_pending' || state === 'analysis_pending') return {
    ...inspect, label:'Analysis Pending',actionLabel:'Inspect Recorded Tasks',
    summary:baseline === 'analysis_pending'
      ? 'Baseline analysis is pending. Candidate tasks are recorded; their eligibility has not been established yet.'
      : 'Current analysis has not established whether these recorded tasks support a comparison.',
    nextStep:'Inspect the recorded tasks while analysis establishes their eligibility.',
  };
  if (state === 'measured' && item.verdict !== 'insufficient_data'
      && item.before_value != null && item.after_value != null
      && Number.isFinite(Number(item.before_value)) && Number.isFinite(Number(item.after_value))) return null;
  if (state === 'not_measurable' || state === 'measured') return {
    ...inspect, label:'Comparison Blocked',
    summary:'The recorded tasks do not provide a reliable before-and-after comparison. No outcome change can be established.',
    nextStep:'Inspect the task evidence and exclusions to understand what prevents this comparison.',
  };
  if (baseline !== 'frozen') return {
    ...inspect, label:'Baseline Eligibility Unknown',actionLabel:'Inspect Baseline Evidence',
    summary:'Baseline eligibility and required metric coverage have not been established. Later tasks alone cannot establish a pre-activation baseline.',
    nextStep:'Inspect the pre-activation evidence before deciding whether later collection would help.',
  };
  return {
    label:'Collecting Evidence',collecting:true,action:'review-workflow',actionLabel:'Review Applied Workflow',
    summary:'The baseline is available. Reflect is still collecting eligible, comparable post-activation tasks with the required measurements.',
    nextStep:'Review the applied workflow before continuing comparable tasks with recorded outcomes and verification.',
  };
}

function impactVerdictPresentation(verdict, measurementState = ''){
  if (measurementState === 'not_measurable') return {label:'Not Measurable',className:'unchanged'};
  if (measurementState === 'collecting') return {label:'Collecting Evidence',className:'collecting'};
  if (verdict === 'improved') return {label:'Improved',className:'improved'};
  if (verdict === 'regressed') return {label:'Needs Attention vs Baseline',className:'regressed'};
  if (verdict === 'unchanged') return {label:'No Clear Change',className:'unchanged'};
  return {label:'Collecting Evidence',className:'collecting'};
}

function impactTrendPresentation(item, previous, metric){
  if (!previous || impactMeasurementState(item) !== 'measured' || impactMeasurementState(previous) !== 'measured') return null;
  if (item.after_value == null || previous.after_value == null) return null;
  const current = Number(item.after_value);
  const prior = Number(previous.after_value);
  if (!Number.isFinite(current) || !Number.isFinite(prior)) return null;
  const direction = String(item.cohort?.metric_direction || metric?.direction || 'lower_is_better');
  const delta = current - prior;
  const percent = prior === 0 ? null : Math.abs(delta / prior) * 100;
  const amount = percent == null ? '' : ` ${fmt(Number(percent.toFixed(1)))}%`;
  if (delta === 0) return {kind:'steady',label:'No movement since last check',shortLabel:'No change vs last check',percent};
  const improving = direction === 'higher_is_better' ? delta > 0 : delta < 0;
  return improving
    ? {kind:'improving',label:`Improving${amount} since last check`,shortLabel:`Improved${amount} vs last check`,percent}
    : {kind:'worsening',label:`Worsening${amount} since last check`,shortLabel:`Worsened${amount} vs last check`,percent};
}

function impactProgressGraph(history, metric){
  const snapshots = [...(history || [])].reverse();
  const latestSnapshot = snapshots[snapshots.length - 1];
  if (impactBlockedGuidance(latestSnapshot)) return '';
  const baselineSnapshot = latestSnapshot?.before_value != null && Number.isFinite(Number(latestSnapshot.before_value))
    ? latestSnapshot
    : [...snapshots].reverse().find(item => item.before_value != null && Number.isFinite(Number(item.before_value)));
  const checks = snapshots
    .filter(item => item.after_value != null && Number.isFinite(Number(item.after_value)))
    .map((item, index) => ({
      kind:'check',
      label:`Check ${index + 1}`,
      measuredAt:item.measured_at,
      value:Number(item.after_value),
    }));
  const series = baselineSnapshot
    ? [{kind:'baseline',label:'Baseline',measuredAt:null,value:Number(baselineSnapshot.before_value)}, ...checks]
    : checks;
  const direction = String(latestSnapshot?.cohort?.metric_direction || metric?.direction || 'lower_is_better');
  const directionLabel = direction === 'higher_is_better' ? 'Higher is better' : 'Lower is better';
  if (series.length < 2) {
    return `<section class="impact-chart" aria-label="Impact progress graph"><div class="impact-chart-head"><span class="impact-chart-title">Progress</span><span class="impact-chart-direction">${escHtml(directionLabel)}</span></div><div class="impact-chart-empty">Waiting for the first comparable post-activation result.</div></section>`;
  }
  const width = 720;
  const height = 132;
  const left = 12;
  const right = width - 12;
  const top = 12;
  const bottom = height - 12;
  const values = series.map(point => point.value);
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  const padding = maximum === minimum ? Math.max(Math.abs(maximum) * 0.1, 0.5) : (maximum - minimum) * 0.14;
  minimum -= padding;
  maximum += padding;
  const x = index => left + index * (right - left) / (series.length - 1);
  const y = value => bottom - (value - minimum) * (bottom - top) / (maximum - minimum);
  const coordinates = series.map((point, index) => `${x(index).toFixed(1)},${y(point.value).toFixed(1)}`);
  const latest = series[series.length - 1];
  const baseline = series[0];
  const delta = latest.value - baseline.value;
  const improving = direction === 'higher_is_better' ? delta > 0 : delta < 0;
  const trend = delta === 0 ? 'steady' : improving ? 'improving' : 'worsening';
  const summary = `${metric.measure}: ${formatImpactValue(metric, baseline.value)} at baseline, ${formatImpactValue(metric, latest.value)} at the latest check. ${directionLabel}.`;
  const latestLabel = latest.measuredAt ? `Latest · ${fmtWorkflowDate(latest.measuredAt)}` : latest.label;
  const circles = series.map((point, index) => `<circle class="impact-chart-point ${point.kind}" cx="${x(index).toFixed(1)}" cy="${y(point.value).toFixed(1)}" r="4"><title>${escHtml(`${point.label}: ${formatImpactValue(metric, point.value)}`)}</title></circle>`).join('');
  return `<section class="impact-chart" data-trend="${trend}" aria-label="${escHtml(summary)}">
    <div class="impact-chart-head"><span class="impact-chart-title">Progress across ${fmt(checks.length)} impact ${checks.length === 1 ? 'check' : 'checks'}</span><span class="impact-chart-direction">${escHtml(directionLabel)}</span></div>
    <svg class="impact-chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escHtml(summary)}" preserveAspectRatio="none">
      <line class="impact-chart-grid" x1="${left}" y1="${top}" x2="${right}" y2="${top}"></line>
      <line class="impact-chart-grid" x1="${left}" y1="${bottom}" x2="${right}" y2="${bottom}"></line>
      <line class="impact-chart-baseline" x1="${left}" y1="${y(baseline.value).toFixed(1)}" x2="${right}" y2="${y(baseline.value).toFixed(1)}"></line>
      <polygon class="impact-chart-area" points="${left},${bottom} ${coordinates.join(' ')} ${right},${bottom}"></polygon>
      <polyline class="impact-chart-line" points="${coordinates.join(' ')}"></polyline>
      ${circles}
    </svg>
    <div class="impact-chart-axis"><span>Baseline · ${escHtml(formatImpactValue(metric, baseline.value))}</span><span>${escHtml(latestLabel)} · ${escHtml(formatImpactValue(metric, latest.value))}</span></div>
  </section>`;
}

function impactSummary(item, metric, trend = null){
  const guidance = impactBlockedGuidance(item);
  if (guidance) return guidance.summary;
  const verdict = String(item.verdict || 'insufficient_data');
  const before = Number(item.before_value);
  const after = Number(item.after_value);
  const afterCount = item.after_count == null ? null : Number(item.after_count);
  const percent = Number.isFinite(before) && Number.isFinite(after) && before !== 0
    ? Math.abs((after - before) / before) * 100
    : null;
  const movement = after > before ? 'increased' : after < before ? 'decreased' : 'did not change';
  const amount = percent == null ? '' : ` by ${fmt(Number(percent.toFixed(1)))}%`;
  if (verdict === 'unchanged') return `${metric.measure} stayed within the 10% decision threshold across ${afterCount == null ? 'an unknown number of' : fmt(afterCount)} comparable post-activation tasks.`;
  if (verdict === 'regressed' && trend?.kind === 'improving') {
    const baselinePosition = after > before ? 'above' : 'below';
    const baselineGap = percent == null ? '' : `${fmt(Number(percent.toFixed(1)))}% `;
    const trendAmount = trend.percent == null ? '' : ` by ${fmt(Number(trend.percent.toFixed(1)))}%`;
    return `${metric.measure} remains ${baselineGap}${baselinePosition} the pre-activation baseline. The observed value improved${trendAmount} since the previous check.`;
  }
  return `${metric.measure} ${movement}${amount} across ${afterCount == null ? 'an unknown number of' : fmt(afterCount)} comparable post-activation tasks.`;
}

function impactCohortDetails(item, side){
  const cohort = item.cohort || {};
  const eligibility = cohort.eligibility?.[side] || {};
  const count = value => value == null || !Number.isFinite(Number(value)) ? 'Unknown' : fmt(Number(value));
  const metricCount = Object.hasOwn(eligibility, 'metric_count') ? eligibility.metric_count : item[`${side}_count`];
  const minimum = cohort[`minimum_${side}_execution_units`];
  const states = {provisional:'Provisional baseline',frozen:'Frozen baseline',analysis_pending:'Baseline analysis pending',historical_unavailable:'Historical baseline unavailable'};
  const baseline = side === 'before' ? `<div class="impact-period-note">${escHtml(states[cohort.baseline_state] || 'Baseline eligibility unknown')}</div>` : '';
  const exclusions = Object.entries(eligibility.exclusion_reasons || {})
    .filter(([,value]) => value != null && Number.isFinite(Number(value)) && Number(value) > 0)
    .map(([reason,value]) => `${humanizeLedgerLabel(reason)}: ${count(value)}`);
  const exposure = cohort.exposure?.[side];
  const exposureCopy = exposure
    ? `Recorded exposure in ${count(exposure.selected_count)} selected tasks: ${count(exposure.followed)} followed · ${count(exposure.invoked)} invoked · ${count(exposure.ignored)} ignored · ${count(exposure.unknown)} unknown`
    : 'Guidance exposure: unknown';
  return `${baseline}<div class="impact-period-note">${escHtml(count(eligibility.candidate_count))} candidates · ${escHtml(count(eligibility.eligible_count))} eligible · ${escHtml(count(minimum))} required</div>
    <div class="impact-period-note">${escHtml(count(eligibility.selected_count))} selected · ${escHtml(count(metricCount))} with metric values · ${escHtml(count(eligibility.missing_metric_count))} missing metric values</div>
    <div class="impact-period-note">${escHtml(count(eligibility.excluded_count))} excluded${exclusions.length ? `: ${escHtml(exclusions.join('; '))}` : ''}</div>
    <div class="impact-period-note">${escHtml(exposureCopy)}</div>`;
}

function impactScope(item){
  const cohort = item.cohort || {};
  const task = cohort.task_archetype_id ? `${humanizeLedgerLabel(cohort.task_archetype_id)} tasks` : 'All task types';
  const repository = cohort.repo_id ? 'Same attributed repository' : 'Tasks without repository attribution';
  return `${task} · ${repository}`;
}

/* Product operating surfaces */
function buildImprovementSurfaces(){
  const resource = name => IMPROVEMENT_DATA.resources?.[name] || {state:'ready'};
  const hasLoaded = name => ['ready','stale'].includes(resource(name).state) || Boolean(resource(name).loaded || resource(name).loaded_at);
  const skillAnalysisPending = ['pending','stale'].includes(IMPROVEMENT_DATA.skill_analysis_state) || resource('skills').state !== 'ready';
  const pendingCopy = name => resource(name.toLowerCase()).state === 'preparing'
    ? `${name} ${name === 'Impact' ? 'is' : 'are'} updating. Results will appear automatically when ready.`
    : `${name} could not be loaded. Reflect will retry automatically.`;
  for (const [name, sources] of Object.entries({inbox:['findings','loops'], workflows:['workflows'], skills:['skills'], impact:['impact']})) {
    const panel = document.getElementById(`tab-${name}`);
    if (!panel) continue;
    const unavailable = sources.some(source => resource(source).state !== 'ready');
    const analysisPending = name === 'skills' && skillAnalysisPending;
    const needsNotice = unavailable || analysisPending;
    // Retained results are historical observations, never a new assessment.
    // Only an initial unavailable read hides the outcome/impact surface.
    panel.classList.toggle('evidence-unavailable', unavailable && !sources.every(hasLoaded) && name !== 'inbox' && name !== 'skills');
    let notice = panel.querySelector('.evidence-unavailable-message');
    if (needsNotice && !notice) {
      notice = document.createElement('div');
      notice.className = 'evidence-unavailable-message inbox-copy';
      notice.setAttribute('role', 'status');
      panel.prepend(notice);
    }
    if (needsNotice) {
      const pending = sources.filter(source => resource(source).state !== 'ready');
      const messages = pending.map(source => {
        const label = source[0].toUpperCase() + source.slice(1);
        const loadedAt = resource(source).loaded_at;
        if (resource(source).state === 'recorded') return `${label}: showing stored references read ${loadedAt ? new Date(loadedAt).toLocaleString() : 'at an unknown time'}. Current validation is pending.`;
        return loadedAt
          ? `${label} ${name === 'impact' ? 'is' : 'are'} updating. Showing data last loaded ${new Date(loadedAt).toLocaleString()}; it may have changed.`
          : hasLoaded(source) ? `${label}: showing last published data; timestamp unavailable. Refresh is pending.`
          : pendingCopy(label);
      });
      if (name === 'skills' && hasLoaded('skills') && skillAnalysisPending) {
        const retainedAnalysis = (IMPROVEMENT_DATA.skills || []).some(item => item.analysis_state !== 'pending' && [item.usage_count,item.evidence_count,item.measurement_count].some(value => value != null));
        messages.push(IMPROVEMENT_DATA.skill_evidence_state === 'recorded'
          ? 'Showing recorded packages, installations, usage, and evidence. Stored impact results have not been revalidated.'
          : retainedAnalysis
          ? 'Showing recorded packages and installations with historical usage, evidence, and impact where available. Updated analysis is pending; missing results remain unknown.'
          : 'Showing recorded packages and installations. Usage, evidence, and impact are updating.');
        if (IMPROVEMENT_DATA.skill_evidence_state !== 'recorded' && (IMPROVEMENT_DATA.skills || []).some(item => item.evidence_state === 'recorded')) messages.push('Newly recorded references are marked separately and await validation.');
      }
      if ((name === 'workflows' || name === 'impact') && unavailable && sources.every(hasLoaded)) messages.push('Historical results remain available for inspection. A new workflow review requires current evidence.');
      notice.innerHTML = `${escHtml(messages.join(' '))} <button type="button" class="ledger-button" data-ledger-action="retry-improvements">Retry</button>`;
    }
    if (notice) notice.hidden = !needsNotice;
  }
  const observations = IMPROVEMENT_DATA.observations || [];
  const loops = sortObservedLoops(IMPROVEMENT_DATA.loops || []);
  const skills = IMPROVEMENT_DATA.skills || [];
  const workflows = IMPROVEMENT_DATA.workflows || [];
  const measurements = IMPROVEMENT_DATA.measurements || [];
  const measurementGroups = groupImpactMeasurements(measurements);
  const rules = IMPROVEMENT_DATA.rules || [];
  const inboxSignalCount = document.getElementById('inbox-signal-count');
  const loopCount = document.getElementById('loop-count');
  const workflowTotalCount = document.getElementById('workflow-total-count');
  const workflowCount = document.getElementById('workflow-count');
  const skillCount = document.getElementById('skill-count');
  const measurementCount = document.getElementById('measurement-count');
  const findingTotal = Number(IMPROVEMENT_DATA.finding_total_count ?? observations.length);
  const actionableLoopCount = loops.filter(item => item.status === 'detected' || item.status === 'acknowledged').length;
  const skillTotal = Number(IMPROVEMENT_DATA.skill_total_count ?? skills.length);
  if (inboxSignalCount) inboxSignalCount.firstChild.textContent = hasLoaded('findings') && hasLoaded('loops') ? String(findingTotal + actionableLoopCount) : '—';
  const findingCount = document.getElementById('finding-count');
  const inboxFindingsCount = document.getElementById('inbox-findings-count');
  const inboxLoopsCount = document.getElementById('inbox-loops-count');
  const inboxLoopsDetail = document.getElementById('inbox-loops-detail');
  if (findingCount) findingCount.firstChild.textContent = hasLoaded('findings') ? String(findingTotal) : '—';
  if (inboxFindingsCount) inboxFindingsCount.textContent = hasLoaded('findings') ? String(findingTotal) : '—';
  if (inboxLoopsCount) inboxLoopsCount.textContent = hasLoaded('loops') ? String(loops.length) : '—';
  if (inboxLoopsDetail) inboxLoopsDetail.textContent = actionableLoopCount
    ? `${fmt(actionableLoopCount)} ready to review and build` : 'Goals, schedules, and repeated behavior';
  if (loopCount) loopCount.firstChild.textContent = hasLoaded('loops') ? String(loops.length) : '—';
  if (workflowTotalCount) workflowTotalCount.firstChild.textContent = hasLoaded('workflows') ? String(workflows.length) : '—';
  const reviewableWorkflowStates = new Set(['pending','approved']);
  const workflowDeployment = item => String(item.lifecycle?.deployment || 'not_deployed');
  const workflowDisplay = item => String(item.lifecycle?.display || item.status || 'pending');
  const workflowMatchesState = (item, state) => {
    if (state === 'all') return true;
    if (state === 'reviewable') return reviewableWorkflowStates.has(String(item.status || 'pending'));
    if (state === 'active' || state === 'rolled_back') return workflowDeployment(item) === state;
    return String(item.status || 'pending') === state;
  };
  const reviewableWorkflows = workflows.filter(item => workflowMatchesState(item, 'reviewable'));
  if (workflowCount) workflowCount.firstChild.textContent = hasLoaded('workflows') ? String(reviewableWorkflows.length) : '—';
  if (skillCount) skillCount.firstChild.textContent = hasLoaded('skills') ? String(skillTotal) : '—';
  if (measurementCount) measurementCount.firstChild.textContent = hasLoaded('impact') ? String(measurementGroups.length) : '—';

  const inboxViewButtons = [...document.querySelectorAll('[data-inbox-view]')];
  const inboxViewPanels = {
    findings:document.getElementById('inbox-findings-panel'),
    loops:document.getElementById('inbox-loops-panel'),
  };
  const setInboxView = (requestedView, {persist=true}={}) => {
    const view = requestedView === 'loops' ? 'loops' : 'findings';
    inboxViewButtons.forEach(button => {
      const active = button.dataset.inboxView === view;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
    });
    Object.entries(inboxViewPanels).forEach(([name,panel]) => {
      if (panel) panel.hidden = name !== view;
    });
    if (persist) updateUrlParams(params => {
      view === 'findings' ? params.delete('inbox_view') : params.set('inbox_view', view);
    });
  };
  inboxViewButtons.forEach(button => {
    button.onclick = () => setInboxView(button.dataset.inboxView);
    button.onkeydown = event => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const nextView = button.dataset.inboxView === 'findings' ? 'loops' : 'findings';
      setInboxView(nextView);
      inboxViewButtons.find(item => item.dataset.inboxView === nextView)?.focus();
    };
  });
  const requestedInboxView = currentParams().get('inbox_view');
  const initialInboxView = requestedInboxView === 'loops' || currentParams().get('loop')
    ? 'loops'
    : (hasLoaded('findings') && findingTotal === 0 && loops.length ? 'loops' : 'findings');
  setInboxView(initialInboxView, {persist:false});

  const ruleCount = document.getElementById('rule-count');
  const ruleGrid = document.getElementById('rule-grid');
  if (ruleCount) ruleCount.textContent = resource('rules').state === 'ready'
    ? `${fmt(rules.length)} deterministic rules · ${fmt(rules.reduce((sum, rule) => sum + Number(rule.open_observation_count || 0), 0))} open findings` : 'Rule definitions unavailable';
  if (ruleGrid) {
    ruleGrid.innerHTML = resource('rules').state !== 'ready'
      ? `<div class="session-ledger-empty">${escHtml(pendingCopy('Rules'))}</div>`
      : rules.length ? rules.map(rule => {
      const config = Object.entries(rule.detector_config || {}).map(([key,value]) => `${key.replaceAll('_',' ')}: ${Array.isArray(value) ? value.join(', ') : value}`).join(' · ');
      return `<article class="rule-card">
        <div class="inbox-card-head"><div class="inbox-title">${escHtml(rule.title || rule.id)}</div><span class="ledger-pill signal">${fmt(Number(rule.open_observation_count || 0))} open</span></div>
        <div class="inbox-copy">${escHtml(rule.description || '')}</div>
        <div class="ledger-meta"><span class="ledger-pill">${escHtml(rule.category || 'uncategorized')}</span><span class="ledger-pill">version ${fmt(Number(rule.version || 1))}</span><span class="ledger-pill">${escHtml(rule.lifecycle_state || 'active')}</span><span class="ledger-pill">${fmt(Number(rule.candidate_count || 0))} candidates</span></div>
        ${config ? `<div class="rule-config">${escHtml(config)}</div>` : ''}
      </article>`;
    }).join('') + `<div class="rule-card"><div class="inbox-title">Adding Rules</div><div class="inbox-copy">Rules are deterministic, versioned objects—not free-form prompts. Extend <code translate="no">BaseImprovementRule</code>, build findings with its typed helpers, and add the instance through <code translate="no">RuleRegistry</code>. Built-ins are collected in <code translate="no">DEFAULT_RULE_REGISTRY</code>.</div></div>` : '<div class="session-ledger-empty">No rule definitions are available yet. Wait for preparation to finish, then refresh this page.</div>';
  }

  const findingLedger = document.getElementById('finding-ledger');
  if (findingLedger) {
    findingLedger.innerHTML = !hasLoaded('findings')
      ? `<div class="session-ledger-empty">${escHtml(pendingCopy('Findings'))}</div>`
      : observations.length ? observations.map(item => {
      const proposedWorkflow = workflows.find(workflow => workflow.id === item.candidate_id);
      const workflowState = proposedWorkflow
        ? humanizeLedgerLabel(proposedWorkflow.lifecycle?.display || proposedWorkflow.status || 'pending')
        : '';
      const priority = item.impact_score != null && Number.isFinite(Number(item.impact_score))
        ? `Priority ${Math.round(Number(item.impact_score))}/100` : 'Priority unavailable';
      const detectionConfidence = item.confidence != null && Number.isFinite(Number(item.confidence))
        ? `${Math.round(Number(item.confidence) * 100)}% detection confidence` : 'Detection confidence unavailable';
      return `<article class="inbox-card">
        <div class="inbox-card-head">
          <div><div class="inbox-title">${escHtml(String(item.title || 'Observation'))}</div><div class="inbox-copy">${escHtml(String(item.summary || ''))}</div></div>
          <span class="ledger-pill signal">${priority}</span>
        </div>
        <div class="ledger-meta">
          <span class="ledger-pill">${escHtml(String(item.status || 'new').replaceAll('_',' '))}</span>
          <span class="ledger-pill">${escHtml(String(item.severity || 'low'))} severity</span>
          <span class="ledger-pill">${detectionConfidence}</span>
          <span class="ledger-pill">${fmt(Number(item.affected_session_count || 0))} linked session(s)</span>
          <span class="ledger-pill">${fmt(Number(item.observation_count || 1))} evidence pattern(s)</span>
          <span class="ledger-pill">${fmt(Number(item.source_scope_count || 1))} scope(s)</span>
          ${proposedWorkflow ? `<span class="ledger-pill">Linked workflow · ${escHtml(workflowState)}</span>` : ''}
        </div>
        <div class="inbox-copy">Priority ranks this finding for review. Detection confidence describes the evidence pattern, not a measured outcome or likelihood of improvement.</div>
        <div class="ledger-actions"><button type="button" class="ledger-button" data-ledger-action="evidence" data-observation-id="${escHtml(String(item.id))}">View Task Evidence</button>${proposedWorkflow ? `<button type="button" class="ledger-button primary" data-ledger-action="open-workflow" data-candidate-id="${escHtml(String(proposedWorkflow.id))}">Open Linked Workflow</button>` : ''}</div>
      </article>`;
    }).join('') : resource('findings').state !== 'ready'
      ? '<div class="session-ledger-empty">No findings in the last loaded data. Updated findings are still pending.</div>'
      : `<div class="panel"><div class="panel-title">Inbox is clear</div><div class="inbox-copy">Reflect has no open finding with retained task evidence in this scope.</div></div>`;
  }

  const loopLedger = document.getElementById('loop-ledger');
  if (loopLedger) {
    loopLedger.innerHTML = !hasLoaded('loops')
      ? `<div class="session-ledger-empty">${escHtml(pendingCopy('Loops'))}</div>`
      : loops.length ? loops.map(item => `<article class="inbox-card">
      <div class="inbox-card-head"><div><div class="inbox-title">${escHtml(String(item.title || 'Observed loop'))}</div><div class="inbox-copy">${escHtml(String(item.summary || ''))}</div></div><span class="ledger-pill signal">${escHtml(humanizeLedgerLabel(item.kind || 'stalled'))}</span></div>
      <div class="ledger-meta">${item.status === 'detected' ? '<span class="ledger-pill signal">Ready to review</span>' : ''}<span class="ledger-pill">${fmt(Number(item.affected_session_count || 0))} sessions</span><span class="ledger-pill">${fmt(Number(item.occurrence_count || 0))} occurrences</span>${item.kind === 'agent_native' ? `<span class="ledger-pill">${escHtml(item.tool_name || 'native command')}</span>` : `<span class="ledger-pill">${fmt(Number(item.state_change_count || 0))} state changes</span>`}<span class="ledger-pill">${Math.round(Number(item.confidence || 0)*100)}% confidence</span><span class="ledger-pill">${escHtml(humanizeLedgerLabel(item.status || 'detected'))}</span></div>
      <div class="ledger-command">reflect loops show ${escHtml(String(item.id))}</div>
      <div class="ledger-actions"><button type="button" class="ledger-button primary" data-ledger-action="review-loop" data-loop-id="${escHtml(String(item.id))}">${item.status === 'detected' ? 'Review & Build Instructions' : 'Review Loop Evidence'}</button></div>
    </article>`).join('') : resource('loops').state !== 'ready'
      ? '<div class="session-ledger-empty">No loops in the last loaded data. Updated loops are still pending.</div>'
      : `<div class="session-ledger-empty">${isStaticDemo() ? 'No additional loops are included in this illustrative sample. Choose another example above.' : 'No agent-native or behavioral loop is currently detected. Run <code>reflect loops</code> after more sessions are captured.'}</div>`;
  }

  const skillRegistry = document.getElementById('skill-registry');
  if (skillRegistry) {
    const searchInput = document.getElementById('skill-search');
    const filterSummary = document.getElementById('skill-filter-summary');
    if (searchInput) searchInput.value = currentParams().get('skill_q') || '';
    const renderSkillRegistry = () => {
      const query = searchInput?.value || '';
      const visible = skills.filter(item => skillMatchesSearch(item, query));
      updateUrlParams(params => {
        query.trim() ? params.set('skill_q', query.trim()) : params.delete('skill_q');
      });
      if (filterSummary) filterSummary.textContent = hasLoaded('skills') ? `Showing ${fmt(visible.length)} of ${fmt(skills.length)} skills` : 'Skill registry is unavailable';
      skillRegistry.innerHTML = !hasLoaded('skills')
        ? `<div class="session-ledger-empty">${escHtml(pendingCopy('Skills'))}</div>`
        : visible.length ? visible.map(item => {
        const title = humanizeLedgerLabel(item.slug || item.name || 'Skill');
        const state = humanizeLedgerLabel(item.lifecycle_state || 'pending');
        const origin = humanizeLedgerLabel(item.origin || 'imported');
        const availability = skillAvailabilityPresentation(item);
        const command = `reflect skills show ${String(item.id)}`;
        const recordedEvidence = (item.evidence_state ?? IMPROVEMENT_DATA.skill_evidence_state) === 'recorded';
        const analysisCount = value => (item.analysis_state === 'pending' && !recordedEvidence) || value == null ? '—' : fmt(Number(value));
        return `<article class="workflow-card skill-tile" data-state="${escHtml(String(item.lifecycle_state || 'pending'))}">
          <div class="tile-card-main">
            <div class="tile-card-kicker"><span>Durable Skill</span><span>${escHtml(origin)}</span><span>${escHtml(state)}</span></div>
            <div class="tile-card-head"><div class="tile-card-heading"><h3 class="tile-card-title">${escHtml(title)}</h3><p class="tile-card-description">${escHtml(String(item.description || 'No description is available yet.'))}</p></div><span class="ledger-pill skill-availability ${escHtml(availability.kind)} tile-card-status">${escHtml(availability.label)}</span></div>
            <dl class="tile-metric-grid">
              <div class="tile-metric signal"><dt>Current Version</dt><dd>${fmt(Number(item.current_version || 0))}</dd></div>
              <div class="tile-metric"><dt>Installations</dt><dd>${fmt(Number(item.installation_count || 0))}</dd></div>
              <div class="tile-metric"><dt>References · All Time</dt><dd>${analysisCount(item.usage_count)}</dd></div>
              <div class="tile-metric"><dt>Impact Checks</dt><dd>${analysisCount(item.measurement_count)}</dd></div>
            </dl>
            <div class="tile-card-meta"><span class="ledger-pill">${escHtml(availability.detail)}</span><span class="ledger-pill">${analysisCount(item.evidence_count)} evidence links</span><span class="ledger-pill">${fmt(Number(item.version_count || item.current_version || 0))} versions</span>${recordedEvidence ? '<span class="ledger-pill">Recorded references · validation pending</span>' : ''}</div>
          </div>
          <footer class="tile-card-footer"><code class="tile-card-command" title="${escHtml(command)}" translate="no">${escHtml(command)}</code><button type="button" class="ledger-button primary" data-ledger-action="review-skill" data-skill-id="${escHtml(String(item.id))}">Inspect Skill</button></footer>
        </article>`;
      }).join('') : query.trim()
        ? `<div class="session-ledger-empty">No skills match <strong>${escHtml(query.trim())}</strong>. Clear the search to return to the full registry.</div>`
        : resource('skills').state !== 'ready'
          ? '<div class="session-ledger-empty">No skills in the last loaded registry. Updated records are still pending.</div>'
          : `<div class="session-ledger-empty">${isStaticDemo() ? 'No skills match this selection. Clear the filters to explore the sample skills.' : 'No skill records are registered. Run <code>reflect skills sync</code> to reconcile installed skills or <code>reflect skills discover</code> to stage evidence-backed drafts.'}</div>`;
    };
    if (searchInput) searchInput.oninput = renderSkillRegistry;
    renderSkillRegistry();
  }

  const workflowLedger = document.getElementById('workflow-ledger');
  if (workflowLedger) {
    const typeFilter = document.getElementById('workflow-type-filter');
    const statusFilter = document.getElementById('workflow-status-filter');
    const filterSummary = document.getElementById('workflow-filter-summary');
    const workflowParams = currentParams();
    if (typeFilter) typeFilter.value = workflowParams.get('workflow_type') || 'all';
    if (statusFilter) statusFilter.value = workflowParams.get('workflow_status') || 'reviewable';
    const renderWorkflowLedger = () => {
      const selectedType = typeFilter?.value || 'all';
      const selectedStatus = statusFilter?.value || 'all';
      const visible = workflows.filter(item => {
        const behavior = String((item.content || {}).behavior_type || 'proven_pattern');
        return (selectedType === 'all' || behavior === selectedType)
          && workflowMatchesState(item, selectedStatus);
      });
      updateUrlParams(params => {
        selectedType === 'all' ? params.delete('workflow_type') : params.set('workflow_type', selectedType);
        selectedStatus === 'reviewable' ? params.delete('workflow_status') : params.set('workflow_status', selectedStatus);
      });
      if (filterSummary) filterSummary.textContent = `Showing ${fmt(visible.length)} of ${fmt(workflows.length)} workflows · ${fmt(reviewableWorkflows.length)} reviewable`;
      workflowLedger.innerHTML = visible.length ? visible.map(item => {
        const content = item.content || {};
        const steps = Array.isArray(content.steps) ? content.steps : [];
        const slug = String(content.slug || item.id);
        const sourceRule = content.source?.rule_id || item.provenance?.source || 'manual';
        const behaviorType = String(content.behavior_type || 'proven_pattern');
        const origin = workflowSourcePresentation(content, item.provenance || {});
        const artifact = humanizeLedgerLabel(content.suggested_artifact || 'skill');
        const command = `reflect workflows show ${String(item.id)}`;
        const status = humanizeLedgerLabel(workflowDisplay(item));
        return `<article class="workflow-card workflow-tile" data-state="${escHtml(workflowDisplay(item))}">
          <div class="tile-card-main">
            <div class="tile-card-kicker"><span>${escHtml(origin.label)}</span><span>${escHtml(humanizeLedgerLabel(behaviorType))}</span></div>
            <div class="tile-card-head"><div class="tile-card-heading"><h3 class="tile-card-title">${escHtml(humanizeLedgerLabel(slug))}</h3><p class="tile-card-description">${escHtml(String(content.description || item.hypothesis || 'No description is available yet.'))}</p></div><span class="ledger-pill signal tile-card-status">${escHtml(status)}</span></div>
            <dl class="tile-metric-grid">
              <div class="tile-metric"><dt>${resource('workflows').state === 'ready' ? 'Supporting Tasks' : 'Recorded Task Links'}</dt><dd>${item.support_execution_unit_count == null ? '—' : fmt(Number(item.support_execution_unit_count))}</dd></div>
              <div class="tile-metric"><dt>Evidence Patterns</dt><dd>${fmt(Number(item.supporting_observation_count || 1))}</dd></div>
              <div class="tile-metric signal"><dt>Evidence Confidence</dt><dd>${resource('workflows').state === 'ready' ? `${Math.round(Number(item.confidence || 0) * 100)}% confidence` : 'Updating'}</dd></div>
            </dl>
            <section class="workflow-step-preview" aria-label="Workflow Step Preview"><div class="workflow-step-preview-head"><span>Workflow Steps</span><span>${fmt(steps.length)} total</span></div>${renderWorkflowSteps(steps, {limit:4, compact:true})}</section>
            <div class="tile-card-meta"><span class="ledger-pill">Suggested ${escHtml(artifact)}</span><span class="ledger-pill">Rule ${escHtml(humanizeLedgerLabel(sourceRule))}</span><span class="ledger-pill">${fmt((item.source_scopes || []).length)} scopes</span></div>
          </div>
          <footer class="tile-card-footer"><code class="tile-card-command" title="${escHtml(command)}" translate="no">${escHtml(command)}</code><button type="button" class="ledger-button${item.status === 'pending' ? ' primary' : ''}" data-ledger-action="review-workflow" data-candidate-id="${escHtml(String(item.id))}">${resource('workflows').state !== 'ready' ? 'Inspect Workflow' : workflowDeployment(item) === 'active' ? 'Review Application' : escHtml(origin.reviewLabel)}</button></footer>
        </article>`;
      }).join('') : resource('workflows').state !== 'ready'
        ? '<div class="panel"><div class="panel-title">Workflow analysis is updating</div><div class="workflow-copy">No stored workflows are available for these filters. New results will appear here when ready.</div></div>'
        : `<div class="panel"><div class="panel-title">No workflows match these filters</div><div class="workflow-copy">${isStaticDemo() ? 'Clear the behavior or state filter to explore the sample workflows.' : 'Clear the behavior or state filter, run <code>reflect improve</code> for deterministic proposals, build one reviewed loop with <code>reflect loops build LOOP_ID</code>, or import an existing procedure with <code>reflect workflows add SKILL.md</code>.'}</div></div>`;
    };
    if (typeFilter) typeFilter.onchange = renderWorkflowLedger;
    if (statusFilter) statusFilter.onchange = renderWorkflowLedger;
    renderWorkflowLedger();
  }

  const measurementLedger = document.getElementById('measurement-ledger');
  if (measurementLedger) {
    measurementLedger.innerHTML = measurementGroups.length ? measurementGroups.map(history => {
      const item = history[0];
      const metric = impactMetricPresentation(item.metric_name);
      const measurementState = impactMeasurementState(item);
      const verdict = impactVerdictPresentation(item.verdict, measurementState);
      const workflow = workflows.find(candidate => candidate.id === item.candidate_id);
      const workflowName = humanizeLedgerLabel(workflow?.content?.slug || 'Applied workflow');
      const minimumAfter = item.cohort?.minimum_after_execution_units;
      const afterCount = item.cohort?.eligibility?.after?.metric_count;
      const count = value => value == null || !Number.isFinite(Number(value)) ? 'Unknown' : fmt(Number(value));
      const guidance = impactBlockedGuidance(item);
      const ready = !guidance;
      const retained = resource('impact').state !== 'ready';
      const previous = history[1] || null;
      const trend = ready ? impactTrendPresentation(item, previous, metric) : null;
      const regressedButImproving = item.verdict === 'regressed' && trend?.kind === 'improving';
      const reviewLabel = item.verdict === 'regressed'
        ? (regressedButImproving ? 'Review Progress' : 'Review &amp; Roll Back')
        : 'Review Applied Workflow';
      const progressGraph = impactProgressGraph(history, metric);
      const reasons = item.cohort?.measurement_reasons || [];
      const historyRows = history.slice(1).map(previousItem => `<li class="impact-history-item"><span>${escHtml(fmtWorkflowDate(previousItem.measured_at))}</span><span>${escHtml(formatImpactValue(metric, previousItem.after_value))} · ${escHtml(impactVerdictPresentation(previousItem.verdict, impactMeasurementState(previousItem)).label)}</span></li>`).join('');
      return `<article class="measurement-card impact-card" data-verdict="${escHtml(String(item.verdict || 'insufficient_data'))}" data-trend="${escHtml(trend?.kind || 'unknown')}">
        <div class="impact-head">
          <div class="impact-head-main"><div class="impact-eyebrow">Applied Workflow · ${escHtml(workflowName)}</div><h3 class="impact-title">${escHtml(metric.goal)}</h3></div>
          <div class="impact-status-stack"><span class="impact-status ${escHtml(guidance ? guidance.collecting ? 'collecting' : 'unchanged' : verdict.className)}">${retained ? 'Stored check · ' : ''}${escHtml(guidance?.label || verdict.label)}</span>${trend ? `<span class="impact-trend-status ${escHtml(trend.kind)}">${escHtml(trend.label)}</span>` : ''}</div>
        </div>
        <p class="impact-summary">${escHtml(impactSummary(item, metric, trend))}</p>
        ${guidance ? `<p class="impact-summary"><strong>Next step:</strong> ${escHtml(guidance.nextStep)}</p>` : `<div class="impact-readiness" aria-label="Comparison readiness">
          <span><strong>${count(item.before_count)}</strong> baseline tasks</span>
          <span><strong>${count(item.after_count)}</strong> after activation</span>
          <span>${retained ? 'Current validation updating' : 'Comparison available'}</span>
        </div>`}
        ${guidance?.collecting && afterCount != null && Number.isFinite(Number(afterCount)) && minimumAfter > 0 ? `<div class="impact-progress"><div class="impact-progress-copy"><strong>Post-activation comparison sample: ${count(afterCount)} selected tasks with metrics of ${count(minimumAfter)} required</strong><span>${fmt(Math.max(0, minimumAfter - afterCount))} remaining</span></div><progress value="${Math.min(afterCount, minimumAfter)}" max="${minimumAfter}" aria-label="Selected post-activation tasks with metric coverage"></progress></div>` : ''}
        ${ready ? `<div class="impact-comparison">
          <div class="impact-period"><div class="impact-period-label">Before Activation</div><div class="impact-period-value">${escHtml(formatImpactValue(metric, item.before_value))}</div></div>
          <div class="impact-arrow" aria-hidden="true">→</div>
          <div class="impact-period"><div class="impact-period-label">After Activation</div><div class="impact-period-value">${escHtml(formatImpactValue(metric, item.after_value))}</div></div>
        </div>` : ''}
        ${progressGraph}
        <div class="impact-scope">${escHtml(impactScope(item))}${ready && item.confidence != null && Number.isFinite(Number(item.confidence)) ? ` · ${Math.round(Number(item.confidence) * 100)}% confidence` : ''}</div>
        <div class="impact-scope">${retained ? `Stored check ${escHtml(item.measured_at ? fmtWorkflowDate(item.measured_at) : 'date unknown')}; current validation is pending. ` : ''}Observed cohort differences do not establish that the workflow caused the change.</div>
        <div class="ledger-actions">${guidance ? `<button type="button" class="ledger-button primary" data-ledger-action="${guidance.action}" data-measurement-id="${escHtml(String(item.id))}" data-candidate-id="${escHtml(String(item.candidate_id))}">${guidance.actionLabel}</button>` : `<button type="button" class="ledger-button primary" data-ledger-action="review-impact-sessions" data-measurement-id="${escHtml(String(item.id))}">View Compared Tasks</button><button type="button" class="ledger-button${item.verdict === 'regressed' && !regressedButImproving ? ' danger' : ''}" data-ledger-action="review-workflow" data-candidate-id="${escHtml(String(item.candidate_id))}">${reviewLabel}</button>`}</div>
        <details class="impact-history"><summary>Comparison evidence and exclusions</summary>
          <div class="impact-comparison">
            <div class="impact-period"><div class="impact-period-label">Before Activation</div>${impactCohortDetails(item, 'before')}</div>
            <div class="impact-arrow" aria-hidden="true">→</div>
            <div class="impact-period"><div class="impact-period-label">After Activation</div>${impactCohortDetails(item, 'after')}</div>
          </div>
          ${reasons.length ? `<p class="impact-scope">${escHtml(reasons.join(' '))}</p>` : ''}
        </details>
        ${historyRows ? `<details class="impact-history"><summary>${fmt(history.length - 1)} Previous Check${history.length === 2 ? '' : 's'}</summary><ol class="impact-history-list">${historyRows}</ol></details>` : ''}
      </article>`;
    }).join('') : resource('impact').state !== 'ready'
      ? `<div class="panel"><div class="panel-title">Impact analysis is pending</div><div class="measurement-copy">${hasLoaded('impact') ? 'No checks were available in the last loaded result. Current results are unknown until refresh completes.' : 'Checks and baseline eligibility are unknown until analysis is available.'}</div></div>`
      : `<div class="panel"><div class="panel-title">No Impact Checks Yet</div><div class="measurement-copy">Install a reviewed workflow to start a bounded validation window. Reflect reports a result only when both task cohorts and their required signals are complete.</div></div>`;
  }

  const linkedWorkflow = currentParams().get('workflow') || '';
  if (linkedWorkflow && !window._linkedWorkflowInitialized) {
    window._linkedWorkflowInitialized = true;
    queueMicrotask(() => showWorkflowReview(linkedWorkflow, currentParams().get('workflow_root') || ''));
  }
  const linkedLoop = currentParams().get('loop') || '';
  if (!linkedWorkflow && linkedLoop && !window._linkedLoopInitialized && loops.some(item => item.id === linkedLoop)) {
    window._linkedLoopInitialized = true;
    queueMicrotask(() => showLoopReview(linkedLoop));
  }
  const linkedSkill = currentParams().get('skill') || '';
  if (!linkedWorkflow && !linkedLoop && linkedSkill && !window._linkedSkillInitialized && skills.some(item => item.id === linkedSkill)) {
    window._linkedSkillInitialized = true;
    queueMicrotask(() => showSkillReview(linkedSkill));
  }
}
improvementSurfacesReady = true;
buildImprovementSurfaces();

/* Explore achievements */
function buildAchievements(){
  const obsTab = sqlTab('observations');
  const achievements = obsTab.achievements || [];
  const badgeGrid = document.getElementById('obs-badges');
  if (!badgeGrid) return;
  badgeGrid.innerHTML = achievements.map(b => `
    <div class="badge">
      <span class="badge-icon">${b.icon}</span>
      <div>
        <div class="badge-name">${escHtml(b.name)}</div>
        <div class="badge-sub">${escHtml(b.sub)}</div>
      </div>
    </div>
  `).join('') || '<div class="empty-note">No outcomes are available for the current selection.</div>';

}
