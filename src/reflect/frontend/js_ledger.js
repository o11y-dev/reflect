/* ── Palette — reflect showcase colors ── */
const P = {
  blue:    '#f28a1a',
  indigo:  '#ffb156',
  purple:  '#d7d1c6',
  teal:    '#f6c47b',
  green:   '#30d158',
  yellow:  '#ffd60a',
  orange:  '#ff9f0a',
  red:     '#ff453a',
  pink:    '#ff375f',
  mint:    '#66d4cf',
};
const COLORS = Object.values(P);

const EVENT_COLORS = {
  prompt:           '#f28a1a',
  response:         '#d7d1c6',
  tool_call:        '#f6c47b',
  tool_result_ok:   '#30d158',
  tool_result_fail: '#ff453a',
  subagent_start:   '#ffb156',
  subagent_stop:    '#ffb156',
  mcp_call:         '#ff9f0a',
  mcp_result:       '#ff9f0a',
  session_start:    'rgba(255,255,255,.3)',
  session_end:      'rgba(255,255,255,.3)',
};

/* ── Helpers ── */
const fmt  = n => typeof n === 'number' ? n.toLocaleString() : String(n);
const pct  = (n,d) => d ? (100*n/d).toFixed(1)+'%' : '0%';
const h12  = h => h === 0 ? '12a' : h < 12 ? h+'a' : h === 12 ? '12p' : (h-12)+'p';
const currentParams = () => new URLSearchParams(window.location.search);

function fmtDur(ms){
  if(!ms && ms !== 0) return '--';
  if(ms < 1000) return Math.round(ms)+'ms';
  if(ms < 60000) return (ms/1000).toFixed(1)+'s';
  return Math.floor(ms/60000)+'m '+(Math.floor(ms/1000)%60)+'s';
}

function fmtRelTime(deltaMs){
  if(deltaMs < 0) deltaMs = 0;
  const totalSec = Math.floor(deltaMs / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return '+' + min + ':' + String(sec).padStart(2,'0');
}

function fmtTokenShort(n){
  const value = Math.max(0, Number(n) || 0);
  if(!value) return '0';
  if(value >= 1000000) return (value/1000000).toFixed(1) + 'M';
  if(value >= 1000) return (value/1000).toFixed(1) + 'K';
  return String(Math.round(value));
}

function fmtCost(n){
  const value = Number(n) || 0;
  return value.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function sessionTokenTotal(session, estimatedTokens = null){
  if (estimatedTokens) {
    return Number(estimatedTokens.input || 0) + Number(estimatedTokens.output || 0);
  }
  const componentTotal = Number(session?.input_tokens || 0)
    + Number(session?.output_tokens || 0)
    + Number(session?.cache_creation_tokens || session?.cache_write_tokens || 0)
    + Number(session?.cache_read_tokens || 0);
  return componentTotal || Number(session?.total_tokens || 0);
}

function sessionCostPresentation(session, estimatedTokens = null){
  const cost = Number(session?.total_cost_usd || session?.total_cost || 0);
  const coverage = session?.pricing_coverage || session?.measurements?.coverage;
  if (coverage) {
    const scope = session?.cost_scope === 'event_window' ? 'Selected dates' : 'Session lifetime';
    if (coverage.cost_status === 'complete') return {cost, status:'estimated', label:'Estimated cost', reason:`${scope}: all observed usage priced, including explicit zero rates.`};
    if (coverage.cost_status === 'partial') return {cost, status:'partial', label:`Known subtotal $${fmtCost(cost)}`, reason:`${scope}: ${coverage.complete_priced_usage || 0}/${coverage.observed_usage || 0} usage records fully priced. Missing evidence is excluded from this subtotal.`};
    return {cost:0, status:'unavailable', label:'Cost unavailable', reason:'Captured usage has no supported price. Missing data does not imply free usage.'};
  }
  if (cost > 0) return {cost, status:'estimated', label:'Estimated cost', reason:''};
  const inferredStatus = sessionTokenTotal(session, estimatedTokens) <= 0
    ? 'tokens_unavailable'
    : session?.primary_model
      ? 'pricing_unavailable'
      : 'model_unavailable';
  const status = String(session?.cost_status || inferredStatus);
  const presentation = {
    zero_usage:['No billable usage', 'Token telemetry was captured and reported no usage.'],
    tokens_unavailable:['Tokens not captured', 'Token usage was not captured for this session.'],
    model_unavailable:['Model not captured', 'Tokens were captured, but no model was available to resolve pricing.'],
    pricing_unavailable:['Price unresolved', `Model "${session?.primary_model || 'unknown'}" did not resolve to a local price.`],
  }[status] || ['Cost unavailable', 'The captured telemetry is insufficient to estimate this session cost.'];
  return {
    cost,
    status,
    label:presentation[0],
    reason:String(session?.cost_unavailable_reason || presentation[1]),
  };
}

function sessionToolCallTotal(session){
  const canonical = session?.tool_calls ?? session?.tool_call_count;
  if (canonical !== undefined && canonical !== null && Number.isFinite(Number(canonical))) {
    return Math.max(0, Number(canonical));
  }
  const inventory = session?.tool_inventory?.tools;
  if (Array.isArray(inventory)) {
    return inventory.reduce((sum, tool) => sum + Number(tool?.count || 0), 0);
  }
  return Object.values(session?.tools || {}).reduce((sum, count) => sum + Number(count || 0), 0);
}

function colorForAgent(agent){
  const name = String(agent || '');
  if (!name) return 'var(--text-3)';
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = ((hash << 5) - hash + name.charCodeAt(i)) | 0;
  }
  return COLORS[Math.abs(hash) % COLORS.length] || 'var(--text-3)';
}

function formatAgentLabel(agent){
  const name = String(agent || '').trim();
  if (!name) return 'Unknown';
  return name.split('-').filter(Boolean).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(' ');
}

function agentIconSvg(agent){
  const name = String(agent || '').toLowerCase();
  if (name.includes('claude')) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v18M3 12h18M5.6 5.6l12.8 12.8M18.4 5.6L5.6 18.4"/></svg>';
  }
  if (name.includes('copilot')) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10.5V8.8a5 5 0 0 1 10 0v1.7"/><path d="M5.5 11.5h13a2 2 0 0 1 2 2v3a4 4 0 0 1-4 4h-9a4 4 0 0 1-4-4v-3a2 2 0 0 1 2-2Z"/><path d="M8.5 16h.01M15.5 16h.01"/></svg>';
  }
  if (name.includes('cursor')) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 4.8 7.2v9.6L12 21l7.2-4.2V7.2L12 3Z"/><path d="M12 3v18M4.8 7.2 12 11.4l7.2-4.2"/></svg>';
  }
  if (name.includes('codex')) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5a4.5 4.5 0 0 1 4.2 2.9 4.5 4.5 0 0 1 3.1 7.4 4.5 4.5 0 0 1-5.1 6.4 4.5 4.5 0 0 1-6.4-2.6 4.5 4.5 0 0 1-3.1-7.4A4.5 4.5 0 0 1 9.8 3.8"/><path d="M8 9.2 12 7l4 2.2v5.6L12 17l-4-2.2V9.2Z"/></svg>';
  }
  if (name.includes('gemini')) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3c1.2 4.4 3.6 6.8 8 8-4.4 1.2-6.8 3.6-8 8-1.2-4.4-3.6-6.8-8-8 4.4-1.2 6.8-3.6 8-8Z"/></svg>';
  }
  if (name.includes('opencode')) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 8-4 4 4 4M15 8l4 4-4 4M13 5l-2 14"/></svg>';
  }
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M9.8 9a2.5 2.5 0 0 1 4.5 1.5c0 1.8-2.3 2-2.3 3.5M12 17h.01"/></svg>';
}

function escHtml(s){
  if(!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

const ledgerDialog = document.getElementById('ledger-dialog');
const ledgerDialogTitle = document.getElementById('ledger-dialog-title');
const ledgerDialogSubtitle = document.getElementById('ledger-dialog-subtitle');
const ledgerDialogBody = document.getElementById('ledger-dialog-body');
const ledgerDialogActions = document.getElementById('ledger-dialog-actions');

function openLedgerDialog({title, subtitle = '', body = '', actions = ''}){
  ledgerDialogTitle.textContent = title;
  ledgerDialogSubtitle.textContent = subtitle;
  ledgerDialogBody.innerHTML = body;
  ledgerDialogActions.innerHTML = actions;
  ledgerDialogBody.scrollTop = 0;
  if (!ledgerDialog.open) ledgerDialog.showModal();
}

function closeLedgerDialog(){
  if (ledgerDialog) ledgerDialog.inspection = null;
  if (ledgerDialog?.open) ledgerDialog.close();
  updateUrlParams(params => {
    params.delete('workflow');
    params.delete('workflow_root');
    params.delete('loop');
    params.delete('skill');
  });
}

document.getElementById('ledger-dialog-close')?.addEventListener('click', closeLedgerDialog);
ledgerDialog?.addEventListener('close', () => {ledgerDialog.inspection = null;});
ledgerDialog?.addEventListener('click', event => {
  if (event.target === ledgerDialog) closeLedgerDialog();
});

async function ledgerRequest(url, {method = 'GET', body} = {}){
  const controller = new AbortController();
  const deadline = method === 'GET' ? window.setTimeout(() => controller.abort(), 5000) : null;
  try {
    const response = await fetch(url, {
      method, signal:controller.signal,
      headers:{Accept:'application/json',...(body === undefined ? {} : {'Content-Type':'application/json'})},
      body:body === undefined ? undefined : JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Evidence is taking longer to load. Retry while analysis continues.');
    throw error;
  } finally {
    if (deadline != null) window.clearTimeout(deadline);
  }
}

let improvementRequest = null;
async function refreshImprovementSurfaces(){
  if (improvementRequest) return improvementRequest;
  improvementRequest = (async () => {
    IMPROVEMENT_DATA = await loadImprovementData((partial, name) => {
      IMPROVEMENT_DATA = partial;
      buildImprovementSurfaces();
      refreshOpenLedger(name, partial.resources?.[name]);
    });
    buildImprovementSurfaces();
  })().finally(() => {improvementRequest = null;});
  return improvementRequest;
}

const improvementTabs = new Set(['inbox', 'workflows', 'skills', 'impact']);
window.setInterval(() => {
  if (!dashboardDocumentActive || document.visibilityState === 'hidden') return;
  if (currentParams().get('tab') === 'explore') {
    hydrateExploreView(activeExploreView).catch(() => {});
    return;
  }
  if (!improvementTabs.has(currentParams().get('tab'))) return;
  if (IMPROVEMENT_DATA.skill_analysis_state === 'pending' || Object.values(IMPROVEMENT_DATA.resources || {}).some(resource => resource.state !== 'ready')) {
    refreshImprovementSurfaces().catch(() => {});
  }
}, 15000);

const workflowDateFormatter = new Intl.DateTimeFormat(navigator.language || undefined, {
  dateStyle:'medium',
  timeStyle:'short',
});

function fmtWorkflowDate(value){
  if (!value) return 'Time unavailable';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : workflowDateFormatter.format(date);
}

function humanizeLedgerLabel(value){
  return String(value || '')
    .replaceAll('_',' ')
    .replaceAll('-',' ')
    .replace(/\b\w/g, character => character.toUpperCase());
}

function humanizeLedgerText(value){
  return String(value || '').replace(/\b[a-z0-9]+(?:_[a-z0-9]+)+\b/gi, token => token.replaceAll('_',' '));
}

function sortObservedLoops(items){
  const statusRank = {detected:0, acknowledged:1, promoted:2, dismissed:3, resolved:4};
  const kindRank = {agent_native:0, stalled:1, productive:2};
  return [...items].sort((left, right) => {
    const statusDelta = (statusRank[left.status] ?? 9) - (statusRank[right.status] ?? 9);
    if (statusDelta) return statusDelta;
    const kindDelta = (kindRank[left.kind] ?? 9) - (kindRank[right.kind] ?? 9);
    if (kindDelta) return kindDelta;
    return String(right.last_seen_at || right.updated_at || '').localeCompare(String(left.last_seen_at || left.updated_at || ''));
  });
}

function skillTargetLabel(targetKind){
  const labels = {
    codex:'Codex',
    claude:'Claude',
    cursor:'Cursor',
    repository:'repository agents',
    filesystem:'filesystem',
  };
  return labels[String(targetKind || '')] || humanizeLedgerLabel(targetKind || 'agent');
}

function skillAvailabilityPresentation(skill, installations = null){
  const targets = Array.isArray(installations)
    ? installations.filter(item => item.status === 'active').map(item => String(item.target_kind || 'filesystem'))
    : (Array.isArray(skill?.installation_targets) ? skill.installation_targets.map(String) : []);
  const uniqueTargets = [...new Set(targets)];
  const lifecycle = String(skill?.lifecycle_state || '');
  if (lifecycle === 'pending') return {kind:'pending', label:'Pending review', detail:'Not installed'};
  if (uniqueTargets.includes('codex')) return {kind:'codex', label:'Available in Codex', detail:'Codex installation detected'};
  if (uniqueTargets.includes('repository')) return {kind:'repository', label:'Available in workspace', detail:'Repository or shared-agent installation detected'};
  if (uniqueTargets.length) {
    const names = uniqueTargets.map(skillTargetLabel);
    return {kind:'other-agent', label:names.length === 1 ? `Available in ${names[0]}` : 'Available to other agents', detail:names.join(', ')};
  }
  if (!Number(skill?.version_count || 0) && Number(skill?.usage_count || 0)) return {kind:'telemetry', label:'Telemetry only', detail:'Observed historically; no package is installed'};
  if (lifecycle === 'stale' || lifecycle === 'retired') return {kind:'archived', label:'Archived', detail:'No active installation'};
  return {kind:'unavailable', label:'Not installed', detail:'Registry record only'};
}

function skillMatchesSearch(skill, query){
  const terms = String(query || '').trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const availability = skillAvailabilityPresentation(skill);
  const searchable = [
    skill?.id,
    skill?.slug,
    skill?.name,
    skill?.description,
    skill?.origin,
    skill?.lifecycle_state,
    skill?.current_version_status,
    skill?.source_agent,
    ...(skill?.installation_targets || []).map(skillTargetLabel),
    availability.label,
    availability.detail,
  ].filter(Boolean).join(' ').toLocaleLowerCase();
  return terms.every(term => searchable.includes(term));
}

function renderProcedureEvidence(content){
  const procedure = content?.procedure;
  if (!procedure || !Array.isArray(procedure.operations)) return '';
  const total = Number(procedure.execution_count || 0);
  const checked = Number(procedure.verification_counts?.passed || 0);
  const states = values => Object.entries(values || {}).map(([key, value]) => `${humanizeLedgerLabel(key)}: ${fmt(Number(value))}`).join(' · ') || 'Unknown';
  const examples = [...(procedure.supporting_examples || []), ...(procedure.counterexamples || [])];
  const operations = procedure.operations.slice(0, 12).map(item => `<li>${escHtml(humanizeLedgerLabel(item.operation))} <span translate="no">${escHtml(item.target)}</span></li>`).join('');
  const matches = (content.existing_workflow_matches || []).slice(0, 5).map(item => `<li>${item.id ? `<button type="button" class="ledger-button" data-ledger-action="review-workflow" data-candidate-id="${escHtml(item.id)}">${escHtml(item.title || item.id)}</button>` : escHtml(item.title || 'Related workflow')} · ${escHtml(humanizeLedgerLabel(item.status || 'unknown'))}</li>`).join('');
  return `<section class="review-panel" aria-label="Procedure evidence">
    <div class="review-panel-head"><h3 class="review-panel-title">Why these executions match</h3><span class="ledger-pill">${fmt(Number(procedure.session_count || 0))} sessions</span></div>
    <p class="review-panel-copy">${escHtml(procedure.match_explanation || 'Matching task scope and recorded procedure.')}</p>
    <p class="review-panel-copy">Within this recorded sample, ${fmt(checked)} of ${fmt(total)} executions had passing checks. This does not establish that the workflow improved outcomes.</p>
    <div class="ledger-meta"><span>Checks · ${escHtml(states(procedure.verification_counts))}</span><span>Task outcomes · ${escHtml(states(procedure.outcome_counts))}</span></div>
    <ol class="workflow-readable">${operations}</ol>
    ${procedure.operations.length > 12 ? `<p class="review-panel-copy">Showing 12 of ${fmt(procedure.operations.length)} matched operations.</p>` : ''}
    ${procedure.signals_truncated ? '<p class="review-panel-copy">Execution detail is incomplete; this sample cannot establish a complete procedure.</p>' : ''}
    <details class="advanced-review"><summary>Supporting and uncertain examples · ${fmt(examples.length)}</summary><div class="session-ledger">
      ${examples.slice(0, 20).map(item => `<article class="session-ledger-item"><div class="session-ledger-main"><div class="session-ledger-title">Checks ${escHtml(humanizeLedgerLabel(item.verification || 'unknown'))} · outcome ${escHtml(humanizeLedgerLabel(item.outcome || 'unknown'))}</div><div class="session-ledger-id" translate="no">${escHtml(item.execution_unit_id || '')}</div></div><a class="ledger-button" href="${escHtml(sessionInspectionUrl({session_id:item.session_id}))}">Open Session</a></article>`).join('')}
    </div></details>
    ${matches ? `<div class="review-eyebrow">Related existing workflows</div><ul class="workflow-readable">${matches}</ul><p class="review-panel-copy">Review these matches before creating another workflow. Existing installations are unchanged.</p>` : ''}
  </section>`;
}

function renderWorkflowSteps(steps, {limit = 0, compact = false} = {}){
  const items = (Array.isArray(steps) ? steps : []).map(step => String(step || '').trim()).filter(Boolean);
  if (!items.length) return '<div class="workflow-step-empty">No workflow steps are defined.</div>';
  const visible = limit > 0 ? items.slice(0, limit) : items;
  const remaining = Math.max(0, items.length - visible.length);
  return `<ol class="workflow-step-tiles${compact ? ' compact' : ''}" aria-label="Workflow Steps">
    ${visible.map((step, index) => `<li class="workflow-step-tile"><span class="workflow-step-number" aria-hidden="true">${index + 1}</span><span class="workflow-step-copy">${escHtml(humanizeLedgerText(step))}</span></li>`).join('')}
  </ol>${remaining ? `<div class="workflow-step-more">+${fmt(remaining)} more ${remaining === 1 ? 'step' : 'steps'} in review</div>` : ''}`;
}

function workflowSourcePresentation(content, provenance = {}){
  const source = content?.source || {};
  const rawKind = String(source.kind || provenance.source || '');
  const kind = rawKind === 'skill_extraction' ? 'agent_authored' : rawKind;
  if (kind === 'agent_authored' || source.rule_id === 'discovered_reusable_workflow') {
    const agent = source.agent ? ` · ${humanizeLedgerLabel(source.agent)}` : '';
    return {
      kind:'agent_authored',
      label:`Agent-authored draft${agent}`,
      detail:'A coding agent drafted this workflow from a bounded Reflect evidence bundle. Reflect recorded the evidence and confidence; the agent did not approve or validate its own draft.',
      reviewLabel:'Review Agent Draft & Diff',
      sectionLabel:'Agent-authored Workflow',
    };
  }
  if (kind === 'manual_skill_file') {
    return {
      kind,
      label:'Imported skill',
      detail:'An existing SKILL.md supplied this workflow. Reflect imported its instructions without claiming authorship.',
      reviewLabel:'Review Imported Skill & Diff',
      sectionLabel:'Imported Workflow',
    };
  }
  return {
    kind:'rule_blueprint',
    label:'Rule blueprint',
    detail:'A deterministic Improvement Rule matched evidence to a known remediation workflow. No coding agent authored this text. The current apply renderer can package the reviewed workflow as a repo-local skill.',
    reviewLabel:'Review Blueprint & Diff',
    sectionLabel:'Suggested Workflow',
  };
}

function compactLedgerId(value){
  const id = String(value || '');
  return id.length > 22 ? `${id.slice(0,8)}…${id.slice(-6)}` : id;
}

function ledgerWorkspaceName(value){
  const parts = String(value || '').split('/').filter(Boolean);
  return parts.at(-1) || '';
}

function sessionInspectionUrl(session){
  const url = new URL(window.location.href);
  ['workflow','workflow_root','workflow_type','workflow_status','loop','skill','skill_q','q','agents','agent','model','status','range'].forEach(key => url.searchParams.delete(key));
  url.searchParams.set('tab', 'sessions');
  url.searchParams.set('session', session.session_id || '');
  if (session.evidence_focus_id) url.searchParams.set('evidence', session.evidence_focus_id);
  else url.searchParams.delete('evidence');
  return `${url.pathname}?${url.searchParams.toString()}${url.hash}`;
}

function renderWorkflowSessionLedger(sessions, {empty, total, initial = 5, occurrences = false} = {}){
  const items = sessions || [];
  if (!items.length) return `<div class="session-ledger-empty">${escHtml(empty || 'No linked sessions yet.')}</div>`;
  const taskLevel = items.some(item => item.execution_unit_id);
  const renderRow = session => {
    const agent = humanizeLedgerLabel(session.agent || 'Agent');
    const workspace = ledgerWorkspaceName(session.workspace);
    const displayTitle = session.title || `${agent}${workspace ? ` · ${workspace}` : ' Session'}`;
    const summary = (session.evidence_summaries || [])[0] || '';
    const signalCount = Number(session.evidence_count || 0);
    const evidenceLabel = session.evidence_label || `${fmt(signalCount)} ${signalCount === 1 ? 'signal' : 'signals'}`;
    const evidenceId = session.execution_unit_id || session.session_id;
    return `<article class="session-ledger-item">
      <div class="session-ledger-main">
        <div class="session-ledger-head">
          <div class="session-ledger-title">${escHtml(displayTitle)}</div>
          <span class="ledger-pill">${escHtml(humanizeLedgerLabel(session.status || 'unknown'))}</span>
        </div>
        <div class="session-ledger-meta"><span>${escHtml(fmtWorkflowDate(session.started_at))}</span>${session.workspace ? `<span title="${escHtml(session.workspace)}" translate="no">${escHtml(workspace)}</span>` : ''}${session.task_archetype_id ? `<span>${escHtml(humanizeLedgerLabel(session.task_archetype_id))} task</span>` : ''}</div>
        ${summary ? `<div class="session-ledger-summary" title="${escHtml(summary)}">${escHtml(summary)}</div>` : ''}
        <div class="session-ledger-id" title="${escHtml(evidenceId)}" translate="no">${session.execution_unit_id ? 'Task ' : ''}${escHtml(compactLedgerId(evidenceId))}</div>
      </div>
      <div class="session-ledger-side">
        ${session.exposure_state ? `<span class="ledger-pill signal">${escHtml(humanizeLedgerLabel(session.exposure_state))}</span>` : `<span class="ledger-pill">${escHtml(evidenceLabel)}</span>`}
        ${session.session_id ? `<a class="ledger-button" data-related-session-link href="${escHtml(sessionInspectionUrl(session))}">Open Session</a>` : '<span class="ledger-status">Session reference unavailable</span>'}
      </div>
    </article>`;
  };
  const visibleRows = items.slice(0, initial).map(renderRow).join('');
  const remainingRows = items.slice(initial).map(renderRow).join('');
  const count = Number(total || items.length);
  const more = remainingRows
    ? `<details class="session-more"><summary>Show ${fmt(items.length - initial)} More ${occurrences ? 'References' : taskLevel ? 'Tasks' : 'Sessions'}</summary><div class="session-ledger">${remainingRows}</div></details>`
    : '';
  const countNote = count > items.length
    ? `<div class="ledger-status">Showing ${fmt(items.length)} bounded examples from ${fmt(count)} ${occurrences ? 'captured references' : taskLevel ? 'linked execution units' : 'linked sessions'}.</div>`
    : '';
  return `<div class="session-ledger">${visibleRows}</div>${more}${countNote}`;
}

function staleEvidenceNotice(...payloads){
  const recorded = payloads.filter(payload => payload?.publication?.state === 'recorded');
  const recordedNotice = recorded.length
    ? `<div class="review-warning" role="status"><strong>Recorded evidence.</strong> Stored references are available for inspection. Current comparability, outcomes, and impact validation are pending.${recorded.some(payload => payload.publication.recorded_at) ? ` Stored measurement: ${escHtml(recorded.filter(payload => payload.publication.recorded_at).map(payload => fmtWorkflowDate(payload.publication.recorded_at)).join('; '))}.` : ''}</div>`
    : '';
  const stale = payloads.filter(payload => payload?.publication?.state === 'stale');
  if (!stale.length) return recordedNotice;
  const timestamps = [...new Set(stale.map(payload => {
    const published = payload.publication.snapshot_completed_at || payload.snapshot_completed_at;
    if (published && Number.isFinite(Date.parse(published))) return `published ${new Date(published).toLocaleString()}`;
    const loaded = payload.publication.loaded_at;
    return loaded && Number.isFinite(Date.parse(loaded))
      ? `last loaded ${new Date(loaded).toLocaleString()}; publication timestamp unavailable`
      : 'publication timestamp unavailable';
  }))];
  const pending = stale.some(payload => payload.publication.reason === 'historical_pending')
    ? 'Historical analysis is pending; an explicit historical refresh is required.'
    : 'Current analysis is pending. Review again after refresh.';
  return `${recordedNotice}<div class="review-warning" role="status"><strong>Historical evidence.</strong> ${escHtml(timestamps.join('; '))}. ${escHtml(pending)}</div>`;
}

function beginLedgerRead(kind, id, title){
  const previous = ledgerDialog.inspection;
  const request = {kind,id,retain:Boolean(ledgerDialog.open && previous?.kind === kind && previous?.id === id)};
  if (request.retain) request.projectRoot = previous.projectRoot;
  ledgerDialog.inspection = request;
  if (!request.retain) openLedgerDialog({title});
  return request;
}

function presentLedgerRead(request, content){
  if (ledgerDialog.inspection !== request) return;
  const body = ledgerDialog.querySelector?.('.ledger-dialog-body');
  const scroll = request.retain ? body?.scrollTop || 0 : 0;
  openLedgerDialog(content);
  if (body && request.retain) body.scrollTop = scroll;
}

function failLedgerRead(request, content){
  if (ledgerDialog.inspection !== request) return;
  const body = ledgerDialog.querySelector?.('.ledger-dialog-body');
  if (request.retain && body) {
    let notice = body.querySelector('[data-evidence-retry-status]');
    if (!notice) {
      notice = document.createElement('div');
      notice.dataset.evidenceRetryStatus = '';
      notice.className = 'review-warning';
      notice.setAttribute('role','status');
      body.prepend(notice);
    }
    notice.textContent = 'Updated evidence could not be loaded. Showing the previous result; retry when ready.';
    return;
  }
  presentLedgerRead(request, content);
}

function refreshOpenLedger(name, resource){
  const request = ledgerDialog.inspection;
  if (!ledgerDialog.open || !request?.pending || resource?.state !== 'ready') return;
  if (name === 'workflows' && request.kind === 'workflow') showWorkflowReview(request.id, request.projectRoot || '');
  if (name === 'impact' && request.kind === 'impact') showImpactSessions(request.id);
  if (name === 'skills' && request.kind === 'skill') showSkillReview(request.id);
}

async function showObservationEvidence(observationId){
  const request = beginLedgerRead('finding', observationId, 'Loading evidence…');
  try {
    const item = await ledgerRequest(`/api/findings/${encodeURIComponent(observationId)}`);
    const ledger = await ledgerRequest(`/api/findings/${encodeURIComponent(observationId)}/evidence`);
    if (ledgerDialog.inspection !== request) return;
    const rawEvidence = (item.evidence || []).map(entry => `<div class="evidence-item">
      <div class="ledger-meta" style="margin-top:0"><span class="ledger-pill" translate="no">${escHtml(entry.entity_type)}:${escHtml(entry.entity_id)}</span><span class="ledger-pill">${Math.round(Number(entry.confidence || 0)*100)}% confidence</span></div>
      <div class="inbox-copy" style="margin-top:8px">${escHtml(entry.summary_redacted || '')}</div>
    </div>`).join('');
    const supportingTasks = renderWorkflowSessionLedger(ledger.support_execution_units, {
      empty:'No comparable execution units support this finding.',
      total:ledger.support_execution_unit_count,
      initial:6,
    });
    const provenanceSessions = renderWorkflowSessionLedger(ledger.provenance_sessions, {
      empty:'No provenance sessions are linked to this finding.',
      total:ledger.provenance_session_count,
      initial:6,
    });
    const confidence = Math.round(Number(item.confidence || 0) * 100);
    const metricValue = Number(item.metric_value);
    const metricDisplay = Number.isFinite(metricValue) ? fmt(metricValue) : String(item.metric_value ?? '—');
    const ruleLabel = humanizeLedgerLabel(item.category || item.rule_id || 'Improvement');
    presentLedgerRead(request, {
      title:item.title || 'Observation evidence',
      subtitle:`${ruleLabel} Rule · Version ${item.rule_version} · ${confidence}% confidence`,
      body:`${staleEvidenceNotice(item,ledger)}<div class="review-lead">
          <div class="review-eyebrow">Why This Matters</div>
          <p>${escHtml(item.summary || '')}</p>
        </div>
        <div class="review-kpi-strip">
          <div class="review-kpi"><div class="review-kpi-label">Affected Sessions</div><div class="review-kpi-value signal">${fmt(Number(item.affected_session_count || 0))}</div></div>
          <div class="review-kpi"><div class="review-kpi-label">Supporting Tasks</div><div class="review-kpi-value">${fmt(Number(ledger?.support_execution_unit_count || 0))}</div></div>
          <div class="review-kpi"><div class="review-kpi-label">${escHtml(humanizeLedgerLabel(item.metric_name || 'Metric'))}</div><div class="review-kpi-value">${escHtml(metricDisplay)} ${escHtml(item.metric_unit || '')}</div></div>
          <div class="review-kpi"><div class="review-kpi-label">Impact Score</div><div class="review-kpi-value">${fmt(Number(item.impact_score || 0))} / 100</div></div>
        </div>
        <div class="review-grid">
          <section class="review-panel">
            <div class="review-panel-head"><h3 class="review-panel-title">Supporting Tasks</h3><span class="ledger-pill signal">${fmt(Number(ledger?.support_execution_unit_count || 0))} comparable</span></div>
            <p class="review-panel-copy">Task-bounded evidence used by the finding. Open the containing session to inspect the highlighted signals in context.</p>
            ${supportingTasks}
            <details class="advanced-review"><summary>Provenance Sessions · ${fmt(Number(ledger?.provenance_session_count || 0))}</summary>${provenanceSessions}</details>
          </section>
          <aside class="review-stack">
            <section class="review-panel">
              <div class="review-panel-head"><h3 class="review-panel-title">Finding Details</h3><span class="ledger-pill">${escHtml(humanizeLedgerLabel(item.status || 'new'))}</span></div>
              <dl class="review-facts">
                <div class="review-fact"><dt>Rule</dt><dd translate="no">${escHtml(item.rule_id)} v${fmt(Number(item.rule_version || 1))}</dd></div>
                <div class="review-fact"><dt>Scope</dt><dd>${escHtml(humanizeLedgerLabel(item.scope_type || 'user'))} · ${escHtml(item.scope_id || 'local')}</dd></div>
                <div class="review-fact"><dt>Metric</dt><dd>${escHtml(humanizeLedgerLabel(item.metric_name || 'Unavailable'))}</dd></div>
                <div class="review-fact"><dt>Confidence</dt><dd>${confidence}%</dd></div>
              </dl>
            </section>
            <details class="advanced-review"><summary>Raw Evidence Links · ${fmt((item.evidence || []).length)}</summary><div class="evidence-list">${rawEvidence || '<div class="session-ledger-empty">No bounded evidence is available.</div>'}</div></details>
          </aside>
        </div>`,
      actions:'<div class="ledger-action-spacer"></div><button type="button" class="ledger-button primary" data-ledger-action="close">Done</button>',
    });
  } catch (error) {
    failLedgerRead(request, {title:'Evidence unavailable', body:`<div class="inbox-copy">${escHtml(error.message)}</div>`});
  }
}

async function showLoopReview(loopId){
  const request = beginLedgerRead('loop', loopId, 'Loading loop evidence…');
  try {
    const detail = await ledgerRequest(`/api/loops/${encodeURIComponent(loopId)}`);
    if (ledgerDialog.inspection !== request) return;
    const loop = detail.loop || {};
    const occurrences = detail.occurrences || [];
    const nativeLoop = loop.kind === 'agent_native';
    updateUrlParams(params => {
      params.delete('workflow');
      params.delete('workflow_root');
      params.delete('skill');
      params.set('loop', loopId);
    });
    const sourceSessions = renderWorkflowSessionLedger(occurrences.map(item => ({
      session_id:item.session_id,
      title:`${humanizeLedgerLabel(item.tool_name || loop.tool_name || 'Tool')} · ${fmt(Number(item.repeat_count || 0))} ${nativeLoop ? 'signals' : 'repeats'}`,
      status:item.outcome || (nativeLoop ? 'native continuation observed' : (item.state_changed ? 'state changed' : 'no state change')),
      evidence_count:Number(item.repeat_count || 0),
      evidence_summaries:[
        nativeLoop
          ? `${humanizeLedgerLabel(item.evidence?.signal || 'agent native command')} · ${humanizeLedgerLabel(item.evidence?.command_family || 'continuation')}`
          : `${fmt(Number(item.error_count || 0))} errors · ${item.state_changed ? 'relevant state changed' : 'no relevant state change'}${item.evidence?.retry_run_count ? ` · ${fmt(Number(item.evidence.retry_run_count))} retry runs` : ''}`,
      ],
    })), {
      empty:'No source sessions are linked to this loop.',
      total:loop.affected_session_count,
      initial:8,
    });
    const evidenceFacts = Object.entries(loop.evidence || {}).map(([key, value]) => `
      <div class="review-fact"><dt>${escHtml(humanizeLedgerLabel(key))}</dt><dd>${escHtml(Array.isArray(value) ? value.join(', ') : String(value))}</dd></div>
    `).join('');
    const stale = detail.publication?.state === 'stale';
    const canBuild = !stale && (loop.status === 'detected' || loop.status === 'acknowledged');
    presentLedgerRead(request, {
      title:loop.title || 'Observed loop',
      subtitle:`${humanizeLedgerLabel(loop.kind || 'loop')} · ${humanizeLedgerLabel(loop.status || 'detected')} · ${Math.round(Number(loop.confidence || 0) * 100)}% confidence`,
      body:`${staleEvidenceNotice(detail)}<div class="review-lead">
          <div class="review-eyebrow">Observed Behavior</div>
          <p>${escHtml(loop.summary || '')}</p>
        </div>
        <div class="review-kpi-strip">
          <div class="review-kpi"><div class="review-kpi-label">Source Sessions</div><div class="review-kpi-value signal">${fmt(Number(loop.affected_session_count || 0))}</div></div>
          <div class="review-kpi"><div class="review-kpi-label">${nativeLoop ? 'Native Signals' : 'Repeated Actions'}</div><div class="review-kpi-value">${fmt(Number(loop.occurrence_count || 0))}</div></div>
          <div class="review-kpi"><div class="review-kpi-label">${nativeLoop ? 'Command' : 'State Changes'}</div><div class="review-kpi-value">${nativeLoop ? escHtml(loop.tool_name || 'Native') : fmt(Number(loop.state_change_count || 0))}</div></div>
          <div class="review-kpi"><div class="review-kpi-label">Confidence</div><div class="review-kpi-value">${Math.round(Number(loop.confidence || 0) * 100)}%</div></div>
        </div>
        <div class="review-grid">
          <section class="review-panel">
            <div class="review-panel-head"><h3 class="review-panel-title">Source Sessions</h3><span class="ledger-pill signal">${fmt(occurrences.length)} shown</span></div>
            <p class="review-panel-copy">Inspect the repeated behavior in context before deciding whether it deserves a reusable workflow.</p>
            ${sourceSessions}
          </section>
          <aside class="review-stack">
            <section class="review-panel">
              <div class="review-panel-head"><h3 class="review-panel-title">Loop Details</h3><span class="ledger-pill">${escHtml(humanizeLedgerLabel(loop.scope_type || 'user'))}</span></div>
              <dl class="review-facts">
                <div class="review-fact"><dt>Tool</dt><dd>${escHtml(loop.tool_name || 'Multiple tools')}</dd></div>
                <div class="review-fact"><dt>Scope</dt><dd>${escHtml(loop.scope_id || 'local')}</dd></div>
                ${evidenceFacts}
              </dl>
            </section>
            <section class="review-panel">
              <div class="review-panel-head"><h3 class="review-panel-title">Next Step</h3></div>
              <p class="review-panel-copy">A loop is evidence, not yet a workflow. Build stages one bounded workflow for review. After explicit installation, Reflect monitors comparable future task executions in Impact.</p>
              ${canBuild ? `<div class="ledger-command">reflect loops build ${escHtml(loopId)} --agent codex</div><div class="ledger-command">reflect workflows apply WORKFLOW_ID</div>` : '<p class="review-panel-copy">Current loop evidence is required before building a workflow.</p>'}
            </section>
          </aside>
        </div>`,
      actions:`<button type="button" class="ledger-button" data-ledger-action="close">Close</button><div class="ledger-action-spacer"></div>${canBuild ? `<span class="ledger-status">Next: build a workflow, review it, then apply it to start monitoring.</span>` : ''}`,
    });
  } catch (error) {
    failLedgerRead(request, {title:'Loop evidence unavailable', body:`<div class="inbox-copy">${escHtml(error.message)}</div>`});
  }
}

async function showSkillReview(skillId){
  const request = beginLedgerRead('skill', skillId, 'Loading skill history…');
  try {
    const detail = await ledgerRequest(`/api/skills/${encodeURIComponent(skillId)}`);
    if (ledgerDialog.inspection !== request) return;
    const analysisPending = detail.analysis_state === 'pending';
    const analysisStale = detail.analysis_state === 'stale' || detail.publication?.state === 'stale';
    const recordedEvidence = detail.evidence_state === 'recorded';
    request.pending = analysisPending || analysisStale;
    const analysisUnavailable = analysisPending && !recordedEvidence;
    const skill = detail.skill || {};
    const versions = detail.versions || [];
    const installations = detail.installations || [];
    const measurements = analysisUnavailable ? [] : detail.measurements || [];
    const evidence = analysisUnavailable ? [] : detail.evidence || [];
    const usageOccurrences = analysisUnavailable ? [] : detail.usage_occurrences || [];
    const analysisCount = value => analysisUnavailable || value == null ? '—' : fmt(Number(value));
    const pendingAnalysis = '<div class="session-ledger-empty">Analysis is updating. These results are not available yet.</div>';
    const availability = skillAvailabilityPresentation(skill, installations);
    const workflowCandidateId = analysisPending || analysisStale ? '' : versions.find(item => item.workflow_candidate_id)?.workflow_candidate_id || '';
    updateUrlParams(params => {
      params.delete('workflow');
      params.delete('workflow_root');
      params.delete('loop');
      params.set('skill', skillId);
    });
    const versionRows = versions.map(item => `<article class="skill-detail-tile">
      <div class="inbox-card-head"><div class="inbox-title">Version ${fmt(Number(item.version || 0))}</div><span class="ledger-pill signal">${escHtml(humanizeLedgerLabel(item.status || 'pending'))}</span></div>
      <div class="ledger-meta"><span class="ledger-pill">${escHtml(humanizeLedgerLabel(item.source_kind || skill.origin || 'imported'))}</span>${item.source_agent ? `<span class="ledger-pill">${escHtml(humanizeLedgerLabel(item.source_agent))}</span>` : ''}${item.source_loop_id ? `<span class="ledger-pill">from loop</span>` : ''}<span class="ledger-pill">${escHtml(fmtWorkflowDate(item.created_at))}</span></div>
      ${item.content_markdown ? `<details class="advanced-review" style="margin-top:10px"><summary>View Package Content</summary><pre class="ledger-diff">${escHtml(item.content_markdown)}</pre></details>` : ''}
    </article>`).join('');
    const installationRows = installations.map(item => `<article class="skill-detail-tile">
      <div class="inbox-card-head"><div class="inbox-title" translate="no">${escHtml(item.path || item.target_ref || 'Installation')}</div><span class="ledger-pill ${item.status === 'active' ? 'signal' : ''}">${escHtml(humanizeLedgerLabel(item.status || 'unknown'))}</span></div>
      <div class="inbox-copy">${escHtml(skillTargetLabel(item.target_kind || 'repository'))} · last seen ${escHtml(fmtWorkflowDate(item.last_seen_at))}</div>
    </article>`).join('');
    const measurementRows = measurements.map(item => {
      const metric = impactMetricPresentation(item.metric_name);
      if (recordedEvidence) {
        const result = item.recorded_result;
        const value = number => number == null ? 'Unknown' : formatImpactValue(metric, number);
        return `<article class="skill-detail-tile"><div class="inbox-card-head"><div class="inbox-title">${escHtml(metric.goal)}</div><span class="ledger-pill">Validation pending</span></div><div class="inbox-copy">${result ? `Recorded result: ${escHtml(value(result.before_value))} before → ${escHtml(value(result.after_value))} after · ${escHtml(humanizeLedgerLabel(result.verdict || 'unknown'))}` : 'Stored result details are unavailable.'}</div><div class="ledger-status">Measured ${escHtml(item.measured_at ? fmtWorkflowDate(item.measured_at) : 'at an unknown time')}; current validation is pending.</div></article>`;
      }
      const verdict = impactVerdictPresentation(item.verdict);
      const ready = item.verdict !== 'insufficient_data';
      const afterCount = Number(item.details?.after_count || 0);
      const minimumAfter = Number(item.details?.cohort?.minimum_after_execution_units || 5);
      return `<article class="skill-detail-tile">
        <div class="inbox-card-head"><div class="inbox-title">${escHtml(metric.goal)}</div><span class="ledger-pill ${item.verdict === 'improved' ? 'signal' : ''}">${escHtml(verdict.label)}</span></div>
        <div class="inbox-copy">${ready ? `${escHtml(formatImpactValue(metric, item.before_value))} before → ${escHtml(formatImpactValue(metric, item.after_value))} after · ${Math.round(Number(item.confidence || 0) * 100)}% confidence` : `${fmt(afterCount)} of ${fmt(minimumAfter)} post-application tasks collected`}</div>
      </article>`;
    }).join('');
    const sessionEvidence = evidence.filter(item => item.entity_type === 'session');
    const evidenceRows = sessionEvidence.slice(0, 20).map(item => `<article class="session-ledger-item">
      <div class="session-ledger-main"><div class="session-ledger-title">Source Session</div><div class="session-ledger-id" title="${escHtml(item.entity_id)}" translate="no">${escHtml(compactLedgerId(item.entity_id))}</div><div class="session-ledger-meta"><span>${escHtml(humanizeLedgerLabel(item.relationship || 'source evidence'))}</span><span>${Math.round(Number(item.confidence || 0) * 100)}% confidence</span></div></div>
      <div class="session-ledger-side"><a class="ledger-button" href="${escHtml(sessionInspectionUrl({session_id:item.entity_id}))}">Open Session</a></div>
    </article>`).join('');
    const usageOccurrenceRows = analysisUnavailable ? pendingAnalysis : renderWorkflowSessionLedger(usageOccurrences.map(item => ({
      session_id:item.session_id,
      title:item.title,
      agent:item.agent,
      started_at:item.started_at,
      status:item.status,
      workspace:item.workspace,
      exposure_state:item.state,
      evidence_count:1,
      evidence_summaries:[`Reference captured ${fmtWorkflowDate(item.observed_at)}${!recordedEvidence && item.outcome ? ` · outcome ${humanizeLedgerLabel(item.outcome)}` : ''}`],
    })), {
      empty:recordedEvidence ? 'No stored use references are linked.' : analysisStale ? 'No captured references in the last published analysis.' : 'No captured references are linked yet.',
      total:Number(skill.usage_count || usageOccurrences.length),
      initial:5,
      occurrences:true,
    });
    presentLedgerRead(request, {
      title:humanizeLedgerLabel(skill.name || skill.slug || 'Skill'),
      subtitle:`${humanizeLedgerLabel(skill.lifecycle_state || 'pending')} · ${humanizeLedgerLabel(skill.origin || 'imported')} · ${fmt(Number(skill.version_count || versions.length))} versions`,
      body:`${staleEvidenceNotice(detail)}${analysisUnavailable ? '<div class="inbox-copy" role="status">Showing recorded packages and installations. Usage, evidence, and impact are updating.</div>' : ''}<div class="review-lead"><div class="review-eyebrow">Durable Package</div><p>${escHtml(skill.description || 'No description is available.')}</p><div class="tile-card-meta"><span class="ledger-pill skill-availability ${escHtml(availability.kind)}">${escHtml(availability.label)}</span><span class="ledger-pill">${escHtml(availability.detail)}</span></div></div>
        <div class="review-kpi-strip">
          <div class="review-kpi"><div class="review-kpi-label">Versions</div><div class="review-kpi-value signal">${fmt(Number(skill.version_count || versions.length))}</div></div>
          <div class="review-kpi"><div class="review-kpi-label">Evidence Links</div><div class="review-kpi-value">${analysisCount(skill.evidence_count)}</div></div>
          <div class="review-kpi"><div class="review-kpi-label">Availability</div><div class="review-kpi-value">${escHtml(availability.label)}</div></div>
          <div class="review-kpi"><div class="review-kpi-label">Captured References · All Time</div><div class="review-kpi-value">${analysisCount(skill.usage_count)}</div></div>
        </div>
        <section class="review-panel skill-usage-section"><div class="review-panel-head"><h3 class="review-panel-title">Where used</h3></div><div class="skill-usage-graph" data-skill-usage-graph><div class="session-ledger-empty" role="status">Loading captured usage…</div></div></section>
        ${versions[0]?.workflow?.procedure ? renderProcedureEvidence(versions[0].workflow) : ''}
        <div class="review-grid">
          <main class="review-stack">
            <section class="review-panel"><div class="review-panel-head"><h3 class="review-panel-title">Version History</h3><span class="ledger-pill signal">${fmt(versions.length)} semantic versions</span></div><div class="skill-detail-grid">${versionRows || '<div class="session-ledger-empty">No versions are registered.</div>'}</div></section>
            <section class="review-panel"><div class="review-panel-head"><h3 class="review-panel-title">Installations</h3><span class="ledger-pill">${fmt(installations.length)} tracked</span></div><p class="review-panel-copy">Only active filesystem packages are available to an agent. Registry history and observed usage do not install a skill.</p><div class="skill-detail-grid">${installationRows || `<div class="session-ledger-empty">${escHtml(availability.detail)}.</div>`}</div></section>
          </main>
          <aside class="review-stack">
            <section class="review-panel">
              <div class="review-panel-head"><h3 class="review-panel-title">Captured References</h3><span class="ledger-pill signal">${analysisCount(skill.usage_count)} references</span></div>
              <p class="review-panel-copy">All-time registry history. Each row is one captured reference; several can link to the same session. The graph above follows your selected date, agent, and repository filters.</p>
              <h4>References</h4>
              ${usageOccurrenceRows}
              <h4>Source Evidence</h4>
              <div class="session-ledger">${analysisUnavailable ? pendingAnalysis : evidenceRows || `<div class="session-ledger-empty">${recordedEvidence ? 'No stored source session references are linked.' : analysisStale ? 'No source sessions in the last published analysis.' : 'This imported skill was not generated from Reflect source sessions.'}</div>`}</div>
              ${sessionEvidence.length > 20 ? `<div class="ledger-status">Showing 20 of ${fmt(sessionEvidence.length)} source sessions.</div>` : ''}
            </section>
            <section class="review-panel"><div class="review-panel-head"><h3 class="review-panel-title">Impact Results</h3><span class="ledger-pill">${analysisUnavailable ? '—' : fmt(measurements.length)} checks</span></div><div class="skill-detail-grid">${analysisUnavailable ? pendingAnalysis : measurementRows || `<div class="session-ledger-empty">${recordedEvidence ? 'No stored impact checks are linked. Current validation is pending.' : analysisStale ? 'No before/after impact check in the last published analysis.' : 'No before/after impact check is available yet.'}</div>`}</div></section>
          </aside>
        </div>`,
      actions:`<button type="button" class="ledger-button" data-ledger-action="close">Close</button><div class="ledger-action-spacer"></div>${analysisPending || analysisStale ? `<button type="button" class="ledger-button" data-ledger-action="review-skill" data-skill-id="${escHtml(skillId)}">Retry Analysis</button>` : ''}${workflowCandidateId ? `<button type="button" class="ledger-button primary" data-ledger-action="review-workflow" data-candidate-id="${escHtml(workflowCandidateId)}">Review Source Workflow</button>` : ''}`,
    });
    if (ledgerDialog.inspection !== request) return;
    const graphContainer = ledgerDialog.querySelector?.('[data-skill-usage-graph]');
    if (graphContainer && typeof loadSkillUsageGraph === 'function') loadSkillUsageGraph(skillId, graphContainer);
  } catch (error) {
    failLedgerRead(request, {title:'Skill history unavailable', body:`<div class="inbox-copy">${escHtml(error.message)}</div>`,
      actions:`<button type="button" class="ledger-button" data-ledger-action="close">Close</button><button type="button" class="ledger-button primary" data-ledger-action="review-skill" data-skill-id="${escHtml(skillId)}">Retry</button>`});
  }
}

async function showWorkflowReview(candidateId, requestedRoot = ''){
  const request = beginLedgerRead('workflow', candidateId, 'Preparing workflow review…');
  requestedRoot = request.projectRoot = requestedRoot || request.projectRoot || '';
  try {
    const sourceCandidate = (IMPROVEMENT_DATA.workflows || []).find(item => item.id === candidateId);
    const candidateSnapshot = sourceCandidate ? JSON.stringify(sourceCandidate) : null;
    const resource = IMPROVEMENT_DATA.resources?.workflows || {};
    const listedCandidateCurrent = Boolean(sourceCandidate) && resource.state === 'ready';
    const previewUrl = new URL(`/api/workflows/${encodeURIComponent(candidateId)}/preview`, window.location.origin);
    if (requestedRoot) previewUrl.searchParams.set('project_root', requestedRoot);
    let [previewRead, evidenceRead] = await Promise.allSettled([
      listedCandidateCurrent ? ledgerRequest(previewUrl.pathname + previewUrl.search) : Promise.resolve(null),
      ledgerRequest(`/api/workflows/${encodeURIComponent(candidateId)}/evidence`),
    ]);
    let ledger = evidenceRead.status === 'fulfilled' ? evidenceRead.value || {} : {};
    // Evidence includes its candidate in the same database read. Deep links and
    // Impact inspection do not depend on a paginated Workflow list being loaded.
    const candidate = ledger.candidate || (sourceCandidate && JSON.parse(candidateSnapshot));
    if (!candidate) throw new Error(evidenceRead.reason?.message || 'Workflow not found.');
    const current = payload => !['pending','stale'].includes(payload.analysis_state) && !['stale','recorded'].includes(payload.publication?.state);
    const candidateCurrent = ledger.candidate ? current(ledger) : listedCandidateCurrent;
    if (!listedCandidateCurrent && ledger.candidate && candidateCurrent) {
      [previewRead] = await Promise.allSettled([ledgerRequest(previewUrl.pathname + previewUrl.search)]);
    }
    if (ledgerDialog.inspection !== request) return;
    const preview = previewRead.status === 'fulfilled' ? previewRead.value || {} : {};
    const candidateStable = !sourceCandidate || JSON.stringify((IMPROVEMENT_DATA.workflows || []).find(item => item.id === candidateId)) === candidateSnapshot;
    const evidenceMatches = (!ledger.contract_signature || !candidate.contract_signature || ledger.contract_signature === candidate.contract_signature)
      && (!preview.contract_signature || preview.contract_signature === candidate.contract_signature)
      && (!preview.candidate_updated_at || preview.candidate_updated_at === candidate.updated_at);
    const evidenceAvailable = evidenceRead.status === 'fulfilled' && candidateStable && evidenceMatches;
    if (!evidenceAvailable) ledger = {};
    const canReview = candidateCurrent && candidateStable
      && (ledger.candidate || IMPROVEMENT_DATA.resources?.workflows?.state === 'ready')
      && previewRead.status === 'fulfilled' && previewRead.value != null && evidenceAvailable
      && current(preview) && current(ledger);
    request.pending = !canReview;
    const evidenceCount = (value, fallback) => value == null && fallback == null ? '—' : fmt(Number(value ?? fallback));
    const unavailableEvidence = '<div class="session-ledger-empty">Evidence references are updating. Their absence has not been established.</div>';
    const readOnlyNotice = canReview ? '' : `<div class="review-warning" role="status"><strong>Read-only inspection.</strong> ${!candidateStable ? 'The workflow changed while loading. ' : ''}${!evidenceMatches ? 'The evidence belongs to a different workflow revision. ' : ''}Current workflow validation and a fresh project preview are required before editing or applying.${previewRead.status === 'rejected' ? ` Project preview unavailable: ${escHtml(previewRead.reason.message)}.` : ''}${evidenceRead.status === 'rejected' ? ` Evidence unavailable: ${escHtml(evidenceRead.reason.message)}.` : ''}</div>`;
    const candidatePublication = ledger.candidate ? ledger : !candidateCurrent ? {publication:{...resource.publication,
      state:resource.publication?.state === 'recorded' || resource.state === 'recorded' ? 'recorded' : 'stale',
      loaded_at:resource.publication?.loaded_at ?? resource.loaded_at}} : {};
    updateUrlParams(params => {
      params.delete('loop');
      params.delete('skill');
      params.set('workflow', candidateId);
      if (preview.project_root) params.set('workflow_root', preview.project_root);
    });
    const active = candidate.lifecycle?.deployment === 'active';
    const reviewable = !['rejected','stale'].includes(candidate.status);
    const applyAllowed = Boolean(preview.checks?.apply_allowed);
    const evidenceReady = Number(ledger.support_execution_unit_count || 0) > 0;
    const applyLabel = applyAllowed
      ? (evidenceReady ? 'Approve & Apply to This Project' : 'Apply Unvalidated Draft')
      : 'Choose a Project Folder';
    const reviewActions = !canReview
      ? `<button type="button" class="ledger-button" data-ledger-action="review-workflow" data-candidate-id="${escHtml(candidateId)}">Retry Review</button>`
      : active
      ? `<button type="button" class="ledger-button danger" data-ledger-action="rollback" data-candidate-id="${escHtml(candidateId)}">Roll Back</button>`
      : !reviewable
        ? ''
        : `<button type="button" class="ledger-button" data-ledger-action="save-edit" data-candidate-id="${escHtml(candidateId)}">Save Draft</button><button type="button" class="ledger-button danger" data-ledger-action="reject" data-candidate-id="${escHtml(candidateId)}">Reject</button><button type="button" class="ledger-button${evidenceReady ? ' primary' : ''}" data-ledger-action="apply" data-candidate-id="${escHtml(candidateId)}" data-evidence-ready="${evidenceReady ? 'true' : 'false'}" data-idle-label="${escHtml(applyLabel)}" ${applyAllowed ? '' : 'disabled'}>${escHtml(applyLabel)}</button>`;
    const content = candidate.content || {};
    const suggestions = (preview.suggested_project_roots || []).map(item => `<option value="${escHtml(item.path)}">${fmt(Number(item.provenance_sessions || 0))} provenance sessions${item.is_repository ? ' · Git repository' : item.is_directory ? ' · project folder' : ' · unavailable'}</option>`).join('');
    const issues = preview.checks?.issues || [];
    const checkClass = applyAllowed ? 'review-check' : 'review-check blocked';
    const checkCopy = applyAllowed
      ? `Target ready. Reflect will ${preview.change_kind === 'create' ? 'create' : preview.change_kind === 'update' ? 'update' : 'keep'} ${preview.target_relative_path || 'the project-local skill file'} in this project only.`
      : issues.join(' ') || 'Choose an existing writable project folder before applying.';
    const advisories = (preview.checks?.advisories || []).filter(item => !String(item).startsWith('This application folder differs from'));
    const targetOwner = preview.checks?.target_owner;
    const suggestedUniqueSlug = String(preview.suggested_unique_slug || preview.checks?.suggested_unique_slug || '');
    const evidenceVariantNotice = Number(candidate.variant_count || 1) > 1
      ? `<div class="review-warning">${fmt(Number(candidate.variant_count || 1))} evidence-specific findings are aggregated into this one workflow proposal. Source sessions below combine those findings.</div>`
      : '';
    const ownerNotice = targetOwner
      ? `<div class="review-warning">Active target owner: ${escHtml(targetOwner.title || targetOwner.candidate_id)}. This is ownership recorded by Reflect for safe rollback, not a Git or filesystem lock.</div>`
      : '';
    const advisoryNotice = advisories.map(item => `<div class="review-warning">${escHtml(item)}</div>`).join('');
    const evidenceReadinessNotice = evidenceReady
      ? ''
      : '<div class="review-warning"><strong>Unvalidated draft.</strong> No comparable task evidence supports this workflow yet. Applying installs the draft, but Impact must establish whether it helps.</div>';
    const supportTasks = !evidenceAvailable ? unavailableEvidence : renderWorkflowSessionLedger(ledger.support_execution_units, {
      empty:canReview ? 'No comparable execution units support this workflow.' : 'No supporting task references in this recorded evidence.',
      total:ledger.support_execution_unit_count,
      initial:3,
    });
    const provenanceSessions = !evidenceAvailable ? unavailableEvidence : renderWorkflowSessionLedger(ledger.provenance_sessions, {
      empty:'No provenance sessions are linked to this workflow.',
      total:ledger.provenance_session_count,
      initial:3,
    });
    const exposedTasks = !evidenceAvailable ? unavailableEvidence : renderWorkflowSessionLedger(ledger.exposed_execution_units, {
      empty:canReview ? 'No tasks have been exposed to this workflow yet. Exposure begins after approval and application.' : 'No exposure references in this recorded evidence.',
      total:ledger.exposed_execution_unit_count,
      initial:3,
    });
    const projectRoot = String(preview.project_root || '').replace(/\/$/, '');
    const targetPath = String(preview.target_path || '');
    const targetFile = projectRoot && targetPath.startsWith(`${projectRoot}/`)
      ? targetPath.slice(projectRoot.length + 1)
      : targetPath;
    const changeLabel = {
      create:'New file',
      update:'File update',
      no_change:'Already current',
      blocked:'Blocked',
    }[preview.change_kind] || humanizeLedgerLabel(preview.change_kind || 'change');
    const evidenceProjectPaths = [...new Set((preview.evidence_project_paths || []).map(path => String(path || '').replace(/\/$/, '')).filter(Boolean))];
    const selectedEvidence = (preview.suggested_project_roots || []).find(item => String(item.path || '').replace(/\/$/, '') === projectRoot);
    const relationState = selectedEvidence ? 'related' : evidenceProjectPaths.length ? 'unrelated' : 'unknown';
    const relationLabel = relationState === 'related'
      ? 'Linked to source evidence'
      : relationState === 'unrelated'
        ? 'Different from source evidence'
        : 'Project relation unavailable';
    const evidenceProjectNames = [...new Set(evidenceProjectPaths.map(ledgerWorkspaceName).filter(Boolean))];
    const relationCopy = relationState === 'related'
      ? `${fmt(Number(selectedEvidence.provenance_sessions || 0))} of ${fmt(Number(ledger.provenance_session_count || 0))} provenance sessions came from this project.`
      : relationState === 'unrelated'
        ? `Source evidence points to ${evidenceProjectNames.join(', ') || 'another project folder'}; applying here is a deliberate scope change.`
        : 'The source sessions do not contain a reliable project folder.';
    const projectName = ledgerWorkspaceName(projectRoot) || projectRoot || 'Project not selected';
    const reviewTitle = humanizeLedgerLabel(content.slug || candidate.title || candidate.id);
    const behaviorType = humanizeLedgerLabel(content.behavior_type || 'proven_pattern');
    const sourceRule = content.source?.rule_id || candidate.provenance?.source || 'manual';
    const origin = workflowSourcePresentation(content, candidate.provenance || {});
    const sourceMarkdown = String(content.source_markdown || '').trim();
    const proposedWorkflow = sourceMarkdown
      ? `<pre class="ledger-diff">${escHtml(sourceMarkdown)}</pre>`
      : renderWorkflowSteps(content.steps);
    const proposedSize = sourceMarkdown ? 'SKILL.md draft' : `${fmt((content.steps || []).length)} steps`;
    ledgerDialog.dataset.projectRoot = preview.project_root || '';
    ledgerDialog.dataset.targetPath = preview.target_path || '';
    ledgerDialog.dataset.workflowSlug = String(content.slug || '');
    presentLedgerRead(request, {
      title:reviewTitle,
      subtitle:`${origin.label} · ${behaviorType} · ${humanizeLedgerLabel(candidate.lifecycle?.display || candidate.status)} · Rule ${humanizeLedgerLabel(sourceRule)}`,
      body:`${staleEvidenceNotice(candidatePublication,ledger)}${readOnlyNotice}<div class="review-lead">
          <div class="review-eyebrow">Why Reflect Suggested This</div>
          <p>${escHtml(humanizeLedgerText(candidate.hypothesis || 'Review the linked evidence and proposed workflow before applying it.'))}</p>
          <p class="review-panel-copy">${escHtml(origin.detail)}</p>
        </div>
        ${content.procedure ? renderProcedureEvidence(content) : ''}
        ${canReview ? `<div class="workflow-approval-summary" aria-label="Workflow approval summary">
          <div class="workflow-approval-item"><div class="workflow-approval-label">Evidence</div><div class="workflow-approval-value signal">${fmt(Number(ledger.provenance_session_count || 0))} linked sessions</div><div class="workflow-approval-copy">${fmt(Number(ledger.support_execution_unit_count || 0))} comparable tasks · ${evidenceReady ? 'task-bounded evidence' : 'unvalidated draft'}</div></div>
          <div class="workflow-approval-item"><div class="workflow-approval-label">Selected Project</div><div class="workflow-approval-value">${escHtml(projectName)}</div><div class="workflow-approval-copy">${escHtml(relationLabel)}</div></div>
          <div class="workflow-approval-item"><div class="workflow-approval-label">File Change</div><div class="workflow-approval-value">${escHtml(changeLabel)}</div><div class="workflow-approval-copy" translate="no">${escHtml(targetFile || 'Target file unavailable')}</div></div>
        </div>
        <div class="workflow-scope-note ${escHtml(relationState)}"><div><strong>${escHtml(relationLabel)}.</strong> ${escHtml(relationCopy)} Only the selected project will be changed; this is not a global or multi-project install.</div></div>` : ''}
        <div class="review-grid">
          <main class="review-stack">
            ${canReview ? `<section class="review-panel">
              <div class="review-panel-head"><h3 class="review-panel-title">Apply to One Project</h3><span class="ledger-pill">${escHtml(changeLabel)}</span></div>
              <p class="review-panel-copy">Reflect writes one project-local skill file. No other project or global agent folder will be changed.</p>
              <div class="workflow-target">
                <label for="workflow-name">Workflow Name</label>
                <div class="workflow-target-row"><input class="workflow-target-input" id="workflow-name" name="workflow-name" value="${escHtml(content.slug || '')}" autocomplete="off" spellcheck="false" maxlength="63" pattern="[a-z0-9][a-z0-9-]{0,62}">${suggestedUniqueSlug ? `<button type="button" class="ledger-button" data-ledger-action="use-unique-name" data-suggested-slug="${escHtml(suggestedUniqueSlug)}">Use ${escHtml(suggestedUniqueSlug)}</button>` : ''}</div>
                <p class="review-panel-copy">Use lowercase letters, numbers, and hyphens. Save the draft to review the updated target path before applying.</p>
                <label for="workflow-project-root">Selected Project Folder</label>
                <div class="workflow-target-row"><input class="workflow-target-input" id="workflow-project-root" name="workflow-project-root" value="${escHtml(preview.project_root || '')}" list="workflow-project-root-options" autocomplete="off" spellcheck="false"><button type="button" class="ledger-button" data-ledger-action="review-target" data-candidate-id="${escHtml(candidateId)}">Verify Folder</button></div>
                <datalist id="workflow-project-root-options">${suggestions}</datalist>
                <div class="workflow-target-file" title="${escHtml(preview.target_path || '')}" translate="no">${escHtml(targetFile || 'Target file unavailable')}</div>
              </div>
              <div class="${checkClass}" aria-live="polite">${escHtml(checkCopy)}</div>${evidenceReadinessNotice}${advisoryNotice}${evidenceVariantNotice}${ownerNotice}
            </section>` : ''}
            <section class="review-panel review-section">
              <div class="review-panel-head"><h3 class="review-panel-title">${escHtml(origin.sectionLabel)}</h3><span class="ledger-pill signal">${escHtml(proposedSize)}</span></div>
              ${proposedWorkflow}
            </section>
            <div class="workflow-support-grid">
              ${(content.preconditions || []).length ? `<section class="review-panel review-section"><h3>Prerequisites</h3><ul class="workflow-readable">${content.preconditions.map(item => `<li>${escHtml(item)}</li>`).join('')}</ul></section>` : ''}
              ${(content.checkpoints || []).length ? `<section class="review-panel review-section"><h3>Checkpoints</h3><ul class="workflow-readable">${content.checkpoints.map(item => `<li>${escHtml(item)}</li>`).join('')}</ul></section>` : ''}
              <section class="review-panel review-section"><h3>Stop Conditions</h3><ul class="workflow-readable">${(content.abstain_when || []).map(item => `<li>${escHtml(item)}</li>`).join('') || '<li>No stop conditions are defined.</li>'}</ul></section>
              <section class="review-panel review-section"><h3>Verification</h3><ul class="workflow-readable">${(content.verification || []).map(item => `<li>${escHtml(item)}</li>`).join('') || '<li>No verification steps are defined.</li>'}</ul></section>
            </div>
            ${canReview ? `<details class="advanced-review"><summary>Exact File Diff</summary><pre class="ledger-diff">${escHtml(preview.diff || 'No filesystem change; rendered content already matches.')}</pre></details>
            <details class="advanced-review"><summary>Edit Structured Workflow</summary><label class="review-summary-label" for="workflow-content-editor">Workflow JSON</label><textarea class="ledger-editor" id="workflow-content-editor" name="workflow-content" autocomplete="off" spellcheck="false" ${active ? 'readonly' : ''}>${escHtml(JSON.stringify(content, null, 2))}</textarea></details>` : ''}
          </main>
          <aside class="review-stack">
            <section class="review-panel">
              <div class="review-panel-head"><h3 class="review-panel-title">${canReview ? 'Supporting Tasks' : 'Recorded Task References'}</h3><span class="ledger-pill signal">${evidenceCount(ledger.support_execution_unit_count,candidate.support_execution_unit_count)} ${canReview ? 'comparable' : 'linked'}</span></div>
              <p class="review-panel-copy">${canReview ? 'Task-bounded evidence behind this contract. Provenance sessions are retained separately and never counted as comparable executions.' : 'Stored references are available for inspection. Current comparability and verification have not been established.'}</p>
              ${supportTasks}
              <details class="advanced-review"><summary>Provenance Sessions · ${evidenceCount(ledger.provenance_session_count)}</summary>${provenanceSessions}</details>
            </section>
            <section class="review-panel">
              <div class="review-panel-head"><h3 class="review-panel-title">After Activation</h3><span class="ledger-pill">${evidenceCount(ledger.exposed_execution_unit_count)} tasks</span></div>
              <p class="review-panel-copy">${canReview ? 'Comparable execution units where Reflect observed this workflow after it was applied.' : 'Stored workflow exposure references; current applicability and outcomes remain unverified.'}</p>
              ${exposedTasks}
            </section>
          </aside>
        </div>`,
      actions:`<button type="button" class="ledger-button" data-ledger-action="close">Close</button><div class="ledger-action-spacer"></div>${reviewActions}`,
    });
  } catch (error) {
    failLedgerRead(request, {title:'Workflow review unavailable', body:`<div class="inbox-copy">${escHtml(error.message)}</div>`,
      actions:`<button type="button" class="ledger-button" data-ledger-action="review-workflow" data-candidate-id="${escHtml(candidateId)}">Retry Review</button>`});
  }
}

async function submitSessionFeedback(sessionId, outcome, button){
  const reason = ['bad','corrected'].includes(outcome)
    ? window.prompt('Optional local reason (avoid secrets or sensitive source content):', '')
    : null;
  if (reason === null && ['bad','corrected'].includes(outcome)) return;
  button.disabled = true;
  try {
    await ledgerRequest(`/api/feedback/${encodeURIComponent(sessionId)}`, {
      method:'POST',
      body:{outcome, ...(reason ? {reason} : {})},
    });
    const status = button.closest('.feedback-row')?.querySelector('[data-feedback-status]');
    if (status) status.textContent = `Recorded: ${outcome.replaceAll('-',' ')}`;
  } catch (error) {
    window.alert(error.message);
  } finally {
    button.disabled = false;
  }
}

async function showImpactSessions(measurementId){
  const request = beginLedgerRead('impact', measurementId, 'Loading comparison evidence…');
  try {
    const ledger = await ledgerRequest(`/api/impact/${encodeURIComponent(measurementId)}/evidence`);
    if (ledgerDialog.inspection !== request) return;
    request.pending = ['pending','stale'].includes(ledger.analysis_state) || ['stale','recorded'].includes(ledger.publication?.state);
    const metric = impactMetricPresentation(ledger.metric_name);
    const recordedEvidence = ledger.evidence_state === 'recorded';
    const unitPlural = 'tasks';
    const cohortCopy = `These are the stored execution-unit cohorts behind this impact snapshot.${ledger.measured_at ? ` Measured ${fmtWorkflowDate(ledger.measured_at)}.` : recordedEvidence ? ' Measurement time is unavailable.' : ''}`;
    const count = value => value == null ? '—' : fmt(Number(value));
    const measurementReasons = ledger.measurement_reasons || [];
    const qualityNotice = measurementReasons.length
      ? `<div class="review-warning"><strong>${escHtml(humanizeLedgerLabel(ledger.measurement_state || 'not measurable'))}.</strong> ${escHtml(measurementReasons.join(' '))}</div>`
      : '';
    const prepare = (items, relationship) => (items || []).map(item => ({
      ...item,
      relationship,
      evidence_label:item.snapshot_missing ? 'Snapshot details unavailable'
        : `${recordedEvidence ? 'Recorded: ' : ''}${formatImpactSessionValue(ledger.metric_name, item.metric_value, item)}`,
    }));
    const beforeSessions = renderWorkflowSessionLedger(prepare(ledger.before_execution_units, 'baseline'), {
      empty:recordedEvidence ? 'No baseline references are stored in this snapshot.' : 'No eligible baseline tasks are selected in this snapshot. Candidate counts and exclusions are shown above.',
      total:ledger.before_count,
      initial:5,
    });
    const afterSessions = renderWorkflowSessionLedger(prepare(ledger.after_execution_units, 'after activation'), {
      empty:recordedEvidence ? 'No post-application references are stored in this snapshot.' : 'No eligible post-application tasks are selected in this snapshot. Candidate counts and exclusions are shown above.',
      total:ledger.after_count,
      initial:5,
    });
    presentLedgerRead(request, {
      title:`Tasks Compared for ${metric.goal}`,
      subtitle:cohortCopy,
      body:`${staleEvidenceNotice(ledger)}<div class="review-lead"><div class="review-eyebrow">Impact Evidence</div><p>${escHtml(cohortCopy)} Open a linked session to inspect the captured conversation and tools.</p></div>${qualityNotice}${ledger.snapshot_exact === false ? '<div class="review-warning">Some stored cohort references have no frozen snapshot details. Their metrics and outcomes remain unknown; current session data is not substituted.</div>' : ''}
        <div class="review-grid">
          <section class="review-panel">
            <div class="review-panel-head"><h3 class="review-panel-title">Before Activation</h3><span class="ledger-pill">${count(ledger.before_count)} ${unitPlural}</span></div>
            <p class="review-panel-copy">Baseline ${unitPlural} used to establish the earlier behavior.</p>
            ${impactCohortDetails({cohort:ledger,before_count:ledger.before_count}, 'before')}
            ${beforeSessions}
          </section>
          <section class="review-panel">
            <div class="review-panel-head"><h3 class="review-panel-title">After Activation</h3><span class="ledger-pill signal">${count(ledger.after_count)} ${unitPlural}</span></div>
            <p class="review-panel-copy">${recordedEvidence ? 'Stored post-application references. Current comparability and impact validation are pending.' : `Comparable ${unitPlural} available after the workflow was applied.`}</p>
            ${impactCohortDetails({cohort:ledger,after_count:ledger.after_count}, 'after')}
            ${afterSessions}
          </section>
        </div>`,
      actions:`<button type="button" class="ledger-button" data-ledger-action="close">Close</button><div class="ledger-action-spacer"></div><button type="button" class="ledger-button" data-ledger-action="review-impact-sessions" data-measurement-id="${escHtml(measurementId)}">Retry Evidence</button><button type="button" class="ledger-button primary" data-ledger-action="review-workflow" data-candidate-id="${escHtml(String(ledger.candidate_id || ''))}">Inspect Applied Workflow</button>`,
    });
  } catch (error) {
    failLedgerRead(request, {title:'Comparison sessions unavailable', body:`<div class="inbox-copy">${escHtml(error.message)}</div>`,
      actions:`<button type="button" class="ledger-button" data-ledger-action="review-impact-sessions" data-measurement-id="${escHtml(measurementId)}">Retry Evidence</button>`});
  }
}

document.addEventListener('click', async event => {
  const trigger = event.target.closest('[data-ledger-action]');
  if (!trigger) return;
  const action = trigger.dataset.ledgerAction;
  const candidateId = trigger.dataset.candidateId || '';
  if (action === 'close') return closeLedgerDialog();
  if (action === 'evidence') return showObservationEvidence(trigger.dataset.observationId || '');
  if (action === 'review-loop') return showLoopReview(trigger.dataset.loopId || '');
  if (action === 'review-skill') return showSkillReview(trigger.dataset.skillId || '');
  if (action === 'open-workflow') {
    activateTab('workflows');
    return showWorkflowReview(candidateId);
  }
  if (action === 'retry-improvements') return refreshImprovementSurfaces();
  if (action === 'review-workflow') return showWorkflowReview(candidateId);
  if (action === 'review-impact-sessions') return showImpactSessions(trigger.dataset.measurementId || '');
  if (action === 'use-unique-name') {
    const input = document.getElementById('workflow-name');
    if (input) {
      input.value = trigger.dataset.suggestedSlug || '';
      input.focus();
    }
    return;
  }
  if (action === 'review-target') {
    const root = document.getElementById('workflow-project-root')?.value || '';
    return showWorkflowReview(candidateId, root);
  }
  if (action === 'open-session') {
    const url = new URL(window.location.href);
    url.searchParams.set('tab', 'sessions');
    url.searchParams.set('session', trigger.dataset.sessionId || '');
    if (trigger.dataset.evidenceId) url.searchParams.set('evidence', trigger.dataset.evidenceId);
    window.location.assign(url.toString());
    return;
  }
  try {
    if (action === 'save-edit') {
      const content = JSON.parse(document.getElementById('workflow-content-editor')?.value || '{}');
      content.slug = String(document.getElementById('workflow-name')?.value || '').trim();
      await ledgerRequest(`/api/workflows/${encodeURIComponent(candidateId)}`, {method:'PUT', body:{content}});
    } else if (action === 'apply') {
      const projectRoot = document.getElementById('workflow-project-root')?.value || ledgerDialog.dataset.projectRoot || '';
      const workflowName = String(document.getElementById('workflow-name')?.value || '').trim();
      if (workflowName !== String(ledgerDialog.dataset.workflowSlug || '')) {
        throw new Error('Save the renamed draft and review its updated target path before applying.');
      }
      if (trigger.dataset.evidenceReady === 'false' && !window.confirm('No comparable task evidence supports this workflow yet. Apply this unvalidated draft to the selected project?')) return;
      trigger.disabled = true;
      trigger.textContent = 'Applying…';
      await ledgerRequest(`/api/workflows/${encodeURIComponent(candidateId)}/apply`, {method:'POST', body:{project_root:projectRoot}});
    } else if (action === 'reject') {
      if (!window.confirm('Reject this workflow proposal?')) return;
      await ledgerRequest(`/api/workflows/${encodeURIComponent(candidateId)}/reject`, {method:'POST', body:{reason:'browser_review'}});
    } else if (action === 'rollback') {
      if (!window.confirm('Roll back the active workflow to its previous file state?')) return;
      await ledgerRequest(`/api/workflows/${encodeURIComponent(candidateId)}/rollback`, {method:'POST'});
    } else {
      return;
    }
    closeLedgerDialog();
    await refreshImprovementSurfaces();
  } catch (error) {
    if (action === 'apply') {
      trigger.disabled = false;
      trigger.textContent = trigger.dataset.idleLabel || 'Apply Workflow';
      const status = document.querySelector('#ledger-dialog .review-check');
      if (status) {
        status.classList.add('blocked');
        status.textContent = error.message;
      } else {
        window.alert(error.message);
      }
    } else {
      window.alert(error.message);
    }
  }
});

function sqlTab(name){
  return ((D.sqlite && D.sqlite.tabs) || {})[name] || {};
}

let filteredDashboardSessions = (D.sessions || []).slice();
let dashboardFilterActive = false;
let dashboardSelectedSessionId = currentParams().get('session') || '';
const lazySqlTabRequests = new Map();
const exploreViewStates = new Map([['usage', 'ready']]);
const exploreViewScopes = new Map();
let explorePublication = 0;
document.addEventListener('reflect-snapshot-published', event => {
  // The first publication fills missing navigation through the existing URL-
  // preserving reload. Later publications retain independent section data.
  if (D._unpublishedNavigation) return;
  explorePublication += 1;
  if (currentParams().get('tab') === 'explore') {
    event.preventDefault();
    Promise.resolve().then(() => hydrateExploreView(activeExploreView)).catch(() => {});
  }
  if (improvementTabs.has(currentParams().get('tab'))) {
    // Usage can publish while every improvement endpoint is still preparing.
    // Preserve the loaded Inbox instead of reloading into an empty page.
    event.preventDefault();
    if (event.detail?.profile !== 'snapshot') {
      for (const resource of Object.values(IMPROVEMENT_DATA.resources || {})) resource.state = 'preparing';
      buildImprovementSurfaces();
    }
  }
  if (event.detail?.profile === 'snapshot') {
    Promise.resolve().then(() => {
      if (event.defaultPrevented) return refreshImprovementSurfaces();
    }).catch(() => {});
  }
});
const exploreViewLoadingCopy = {
  usage:'Loading observed usage and Git activity...',
  tools:'Loading command and tool patterns...',
  graph:'Loading graphs...',
  context:'Loading context and system data...',
};

function setExploreViewState(viewName, state, errorMessage = ''){
  exploreViewStates.set(viewName, state);
  const panel = document.getElementById(`tab-explore-${viewName}`);
  const status = document.getElementById(`${viewName}-view-status`);
  if (!panel || !status) return;
  const loading = state === 'loading';
  const previous = exploreViewScopes.get(viewName);
  const retained = previous?.scope === exploreViewUrl(viewName).url.toString();
  panel.setAttribute('aria-busy', loading ? 'true' : 'false');
  status.classList.toggle('error', state === 'error');
  const message = loading ? exploreViewLoadingCopy[viewName] : errorMessage;
  status.textContent = retained && state !== 'ready'
    ? `${previous.publication_state === 'recorded'
      ? `Showing stored telemetry${previous.loaded_at ? ` read ${new Date(previous.loaded_at).toLocaleString()}` : '; read time unavailable'}.`
      : previous.loaded_at ? `Showing data from ${new Date(previous.loaded_at).toLocaleString()}.` : 'Showing last published data; timestamp unavailable.'} ${message || 'Updating in the background.'}`
    : message;
  status.hidden = state === 'ready';
  if (['error','preparing','stale','recorded'].includes(state)) {
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'ledger-button';
    retry.textContent = 'Retry';
    retry.addEventListener('click', () => hydrateExploreView(viewName).catch(() => {}));
    status.appendChild(retry);
  }
  [...panel.children].forEach(child => {
    if (child === status || child.classList.contains('explore-subnav')) return;
    child.hidden = state !== 'ready' && !retained;
  });
}

function exploreViewUrl(viewName){
  const url = new URL(`/api/explore/${encodeURIComponent(viewName)}`, window.location.href);
  const sessionId = viewName === 'context' ? '' : currentParams().get('session') || dashboardSelectedSessionId || '';
  if (sessionId) url.searchParams.set('session', sessionId);
  if (viewName !== 'context') {
    ['q','agents','agent','model','status','range'].forEach(key => {
      const value = currentParams().get(key);
      if (value) url.searchParams.set(key, value);
    });
  }
  return {url, sessionId};
}

// The initial Usage payload may belong to the last completed navigation cache,
// rather than the session/filter requested by a deep link.
if (!D._navigationFromCache && Object.hasOwn(sqlTab('usage'), 'unique_sessions')) {
  const initialUsageUrl = exploreViewUrl('usage').url;
  if (D.focused_session_id) initialUsageUrl.searchParams.set('session', D.focused_session_id);
  else initialUsageUrl.searchParams.delete('session');
  exploreViewScopes.set('usage', {
    scope:initialUsageUrl.toString(), publication:explorePublication,
    publication_state:D.publication?.state || 'ready',
    loaded_at:D.publication?.state === 'recorded' ? D.publication.loaded_at || null
      : D.publication?.snapshot_completed_at || D.snapshot_completed_at || D.publication?.loaded_at
      || (D.publication?.state === 'stale' ? null : new Date().toISOString()),
  });
  if (D.publication?.state === 'stale') setExploreViewState('usage', 'stale', 'Refresh is pending; these are the last published results.');
  if (D.publication?.state === 'recorded') setExploreViewState('usage', 'recorded', 'Analysis is pending; stored telemetry remains available.');
}
if (D._unpublishedNavigation) {
  for (const view of Object.keys(exploreViewLoadingCopy)) {
    setExploreViewState(view, 'preparing', 'No published report loaded. Captured session activity is available in Sessions.');
  }
}

async function loadExploreView(viewName){
  if (!dashboardDocumentActive || !D.sql_backed || !reportSupportsServerFiltering()) return null;
  if (!D.sqlite) D.sqlite = {};
  if (!D.sqlite.tabs) D.sqlite.tabs = {};
  const {url} = exploreViewUrl(viewName);
  const cacheKey = url.toString();
  if (lazySqlTabRequests.has(cacheKey)) return lazySqlTabRequests.get(cacheKey);
  const publication = explorePublication;
  const requestUrl = new URL(url);
  if (viewName === 'usage') {
    requestUrl.pathname = '/api/data';
    requestUrl.searchParams.set('tab', 'explore');
    requestUrl.searchParams.set('view', 'usage');
  }
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 30000);
  const request = fetch(requestUrl.toString(), {headers: {Accept: 'application/json'}, signal: controller.signal, cache:'no-store'})
    .then(async response => {
      if (response.status === 409) {
        const payload = await response.json();
        throw new Error(payload.error || 'Historical analysis is pending. Run an explicit historical refresh.');
      }
      if (response.status === 503) {
        const pending = await response.json();
        if (pending.state === 'preparing' || (pending.state === 'busy' && pending.reason === 'reading')) {
          const error = new Error(pending.error || 'This view is refreshing. Retry after telemetry refresh completes.');
          error.preparing = true;
          throw error;
        }
      }
      if (!response.ok) throw new Error('This view could not be loaded. Please retry.');
      return response.json();
    })
    .then(payload => {
      if (publication !== explorePublication || exploreViewUrl(viewName).url.toString() !== cacheKey) return null;
      if (viewName === 'context') {
        ['specs','memory','privacy','exports'].forEach(name => {
          if (payload[name]) D.sqlite.tabs[name] = payload[name];
        });
      } else if (viewName === 'usage') {
        if (!Object.hasOwn(payload.sqlite?.tabs?.usage || {}, 'unique_sessions')) {
          const error = new Error('Usage details are preparing. Retry after telemetry refresh completes.');
          error.preparing = true;
          throw error;
        }
        // Each lazy view owns its SQL payload and exact scope. A Usage response
        // must not replace it with a different cohort's incidental tab data.
        const retainedTabs = {};
        for (const [view, names] of Object.entries({tools:['tools','mcp'], graph:['graph'], context:['specs','memory','privacy','exports']})) {
          if (exploreViewScopes.has(view)) {
            for (const name of names) retainedTabs[name] = D.sqlite.tabs[name];
          }
        }
        Object.assign(D, payload, {sessions:D.sessions, _unpublishedNavigation:D._unpublishedNavigation, publication:payload.publication || null});
        Object.assign(D.sqlite.tabs, retainedTabs);
      } else if (viewName === 'tools') {
        ['tools','mcp'].forEach(name => D.sqlite.tabs[name] = payload);
      } else {
        D.sqlite.tabs[viewName] = payload;
      }
      return payload;
    })
    .finally(() => {
      window.clearTimeout(timeout);
      lazySqlTabRequests.delete(cacheKey);
    });
  lazySqlTabRequests.set(cacheKey, request);
  return request;
}

async function hydrateExploreView(viewName){
  const cacheKey = exploreViewUrl(viewName).url.toString();
  const previous = exploreViewScopes.get(viewName);
  const publication = explorePublication;
  if (exploreViewStates.get(viewName) === 'ready' && previous?.scope === cacheKey && previous.publication === publication) return;
  if (exploreViewStates.get(viewName) === 'loading' && lazySqlTabRequests.has(cacheKey)) return;
  if (!D.sql_backed || !reportSupportsServerFiltering()) return;
  setExploreViewState(viewName, 'loading');
  try {
    const payload = await loadExploreView(viewName);
    if (exploreViewUrl(viewName).url.toString() !== cacheKey) return;
    if (publication !== explorePublication) return hydrateExploreView(viewName);
    if (!payload) return;
    exploreViewScopes.set(viewName, {
      scope:cacheKey, publication,
      publication_state:payload.publication?.state || 'ready',
      loaded_at:payload.publication?.state === 'recorded' ? payload.publication.loaded_at || null
        : payload.publication?.snapshot_completed_at || payload.snapshot_completed_at || payload.publication?.loaded_at
        || (payload.publication?.state === 'stale' ? null : new Date().toISOString()),
    });
    setExploreViewState(viewName, ['stale','recorded'].includes(payload.publication?.state) ? payload.publication.state : 'ready',
      payload.publication?.state === 'recorded'
        ? payload.publication.reason === 'historical_pending'
          ? 'Historical analysis is pending. Stored telemetry is available; an explicit historical refresh is needed to update analysis.'
          : 'Analysis is pending; stored telemetry remains available and will update after refresh.'
        : payload.publication?.reason === 'reading'
        ? 'Reading the current selection in the background; showing the last available results.'
        : payload.publication?.reason === 'historical_pending'
        ? 'Historical analysis is pending. These are the last published results.'
        : 'Refresh is pending; these are the last published results.');
    if (viewName === 'usage') renderUsageView();
    if (viewName === 'context') renderSqlTabPayloads();
    if (viewName === 'tools') renderToolsView();
    if (viewName === 'graph') {
      _graphsInited = true;
      requestAnimationFrame(initGraphs);
    }
  } catch (error) {
    if (exploreViewUrl(viewName).url.toString() !== cacheKey) return;
    if (publication !== explorePublication) return hydrateExploreView(viewName);
    setExploreViewState(viewName, error.preparing ? 'preparing' : 'error', error.name === 'AbortError'
      ? 'This view took too long to load. Please retry.' : error.message);
  }
}
