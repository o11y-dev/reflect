// ════════════════════ DATA LOADING ════════════════════
let D;
let IMPROVEMENT_DATA = {observations:[], loops:[], skills:[], workflows:[], measurements:[], rules:[], counts_by_status:{}, finding_total_count:0, observation_record_count:0, skill_total_count:0, skill_archived_count:0};
function renderDashboardError(kind, message){
  if (kind === 'no-data') {
    document.body.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;gap:24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;color:rgba(255,255,255,.9)">'
      + '<h1 style="font-size:42px;font-weight:700;background:linear-gradient(135deg,#f28a1a,#d7d1c6);-webkit-background-clip:text;-webkit-text-fill-color:transparent">reflect.o11y.dev</h1>'
      + '<p style="color:rgba(255,255,255,.5);font-size:16px;max-width:460px;text-align:center;line-height:1.7">Behavioral memory for developer-agent telemetry.<br>Run <code style="background:rgba(255,255,255,.08);padding:2px 8px;border-radius:4px">reflect</code> to open your local browser report.</p>'
      + '<a href="https://github.com/o11y-dev/reflect" style="color:#f28a1a;font-size:14px;text-decoration:none">github.com/o11y-dev/reflect</a>'
      + '</div>';
    return;
  }
  if (kind === 'report-fetch-failed') {
    document.body.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;background:#050505;color:#f5f2ea;font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;text-align:center;padding:40px">
      <div style="font-size:48px;font-weight:800;letter-spacing:-.04em;margin-bottom:16px">reflect</div>
      <div style="color:rgba(255,255,255,.55);font-size:18px;max-width:480px;line-height:1.65;margin-bottom:32px">Local-first telemetry for AI coding agents.<br>See why your agents fail, stall, or burn budget.</div>
      <pre style="background:#080807;border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:20px 28px;font-size:16px;color:#ffb156;text-align:left;margin-bottom:32px">pipx install o11y-reflect
reflect setup
reflect</pre>
      <div style="display:flex;gap:16px;flex-wrap:wrap;justify-content:center">
        <a href="https://github.com/o11y-dev/reflect" style="padding:12px 20px;background:linear-gradient(135deg,#ffb156,#f28a1a);color:#100903;border-radius:8px;text-decoration:none;font-weight:700">View on GitHub</a>
        <a href="https://reflect.o11y.dev/" style="padding:12px 20px;border:1px solid rgba(255,255,255,.12);color:#f5f2ea;border-radius:8px;text-decoration:none;font-weight:700">reflect.o11y.dev</a>
      </div>
    </div>`;
    return;
  }
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;min-height:100vh;color:#ff453a;font-family:monospace">Error decoding data: ' + message + '</div>';
}

function hasReportScopeFilters(){
  const params = new URLSearchParams(window.location.search);
  return Boolean(params.get('q') || params.get('agents') || params.get('model') || params.get('status') || params.get('range') || params.get('session'));
}

function reportLoaderCopy(){
  return hasReportScopeFilters() ? 'Filtering sessions...' : 'Preparing report...';
}

function showReportLoader(message){
  const loader = document.getElementById('report-loader');
  const copy = document.getElementById('report-loader-copy');
  if (copy) copy.textContent = message || reportLoaderCopy();
  if (loader) loader.classList.remove('is-hidden');
}

function hideReportLoader(){
  const loader = document.getElementById('report-loader');
  if (loader) loader.classList.add('is-hidden');
}

let preparationStatusPollTimer = null;
const DASHBOARD_REFRESH_MIN_INTERVAL_MS = 30000;
const PREPARATION_PHASES = Object.freeze({
  opening_store: 1,
  backing_up_store: 1,
  migrating_schema: 1,
  ingesting_traces: 2,
  ingesting_logs: 2,
  ingesting_sessions: 3,
  normalizing: 3,
  updating_canonical_state: 4,
  pruning_sessions: 4,
  refreshing_graph: 5,
  refreshing_rollups: 5,
  refreshing_improvements: 5,
  vacuuming_store: 5,
  complete: 5,
});
const PREPARATION_PHASE_LABELS = ['Store', 'Signals', 'Reconcile', 'Index', 'Derive'];

function formatStorageBytes(value){
  const bytes = Math.max(0, Number(value || 0));
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && size >= 1024; index += 1) {
    size /= 1024;
    unit = units[index];
  }
  return `${size.toFixed(1)} ${unit}`;
}

function formatPreparationElapsed(startedAt){
  const started = Date.parse(startedAt || '');
  if (!Number.isFinite(started)) return '';
  const seconds = Math.max(1, Math.round((Date.now() - started) / 1000));
  if (seconds < 60) return `${seconds}s elapsed`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s elapsed`;
}

function formatNextRefresh(automaticRefresh){
  if (!automaticRefresh?.enabled) return 'Refresh on demand';
  const nextRun = new Date(automaticRefresh.next_run_at || '');
  if (Number.isNaN(nextRun.getTime())) return 'Automatic refresh on';
  return `Next refresh ${nextRun.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`;
}

function updatePreparationStatus({tone, kicker, message, detail, phase = 0, active = false}){
  const status = document.getElementById('preparation-status');
  const kickerNode = document.getElementById('preparation-status-kicker');
  const copy = document.getElementById('preparation-status-copy');
  const detailNode = document.getElementById('preparation-status-detail');
  if (!status || !kickerNode || !copy || !detailNode) return;
  const boundedPhase = Math.max(0, Math.min(5, Number(phase || 0)));
  status.className = `preparation-status ${tone}`;
  status.dataset.phase = String(boundedPhase);
  status.hidden = false;
  kickerNode.textContent = kicker;
  copy.textContent = message;
  copy.title = message;
  detailNode.textContent = detail;
  detailNode.title = detail;
  status.querySelectorAll('.preparation-status-bar').forEach((bar, index) => {
    bar.classList.toggle('is-filled', index < boundedPhase);
    bar.classList.toggle('is-active', active && index === boundedPhase - 1);
  });
}

function renderRawStorageStatus(rawStorage){
  if (!rawStorage) return false;
  const used = formatStorageBytes(rawStorage.raw_bytes);
  const limit = formatStorageBytes(rawStorage.raw_limit_bytes);
  if (rawStorage.accepting_telemetry === false) {
    updatePreparationStatus({
      tone: 'failed',
      kicker: 'Storage limit',
      message: 'Telemetry capture is paused. Refresh to reclaim processed segments.',
      detail: `${used} of ${limit} · action required`,
      phase: 5,
    });
    return true;
  }
  const ratio = Number(rawStorage.usage_ratio || 0);
  if (ratio >= 0.8) {
    updatePreparationStatus({
      tone: 'warning',
      kicker: 'Storage capacity',
      message: `Raw OTLP storage is ${Math.round(ratio * 100)}% full. The next refresh will reclaim processed segments.`,
      detail: `${used} of ${limit}`,
      phase: Math.ceil(ratio * 5),
    });
    return true;
  }
  if (rawStorage.error) {
    updatePreparationStatus({
      tone: 'warning',
      kicker: 'Capture status',
      message: 'Storage status is unavailable. Check reflect doctor for details.',
      detail: 'Capture health unknown',
    });
    return true;
  }
  return false;
}

function renderPreparationStatus(preparation, automaticRefresh){
  const state = preparation?.state || 'idle';
  const phase = PREPARATION_PHASES[preparation?.stage] || 0;
  const phaseLabel = PREPARATION_PHASE_LABELS[phase - 1] || 'Ready';
  if (state === 'running') {
    const elapsed = formatPreparationElapsed(preparation.started_at);
    updatePreparationStatus({
      tone: 'running',
      kicker: `Telemetry refresh · ${phaseLabel}`,
      message: preparation.message || 'Refreshing local telemetry…',
      detail: `Step ${phase || 1} of 5${elapsed ? ` · ${elapsed}` : ''}`,
      phase: phase || 1,
      active: true,
    });
    return true;
  }
  if (state === 'failed') {
    updatePreparationStatus({
      tone: 'failed',
      kicker: 'Refresh failed',
      message: preparation.error || 'Check the server log for details.',
      detail: phase ? `Stopped at step ${phase} of 5` : 'Action required',
      phase,
    });
    return false;
  }
  if (state === 'complete') {
    const completion = preparation.finished_at || `generation:${preparation.generation || 0}`;
    const details = preparation.result?.details || {};
    const deferredReplays = details.deferred_replays || [];
    let previousCompletion = '';
    try {
      previousCompletion = window.sessionStorage.getItem('reflect.preparation.finished_at') || '';
      window.sessionStorage.setItem('reflect.preparation.finished_at', completion);
    } catch {}
    const sessionCount = Number(details.sessions ?? details.changed_sessions ?? details.refreshed_sessions ?? 0);
    const summary = sessionCount > 0
      ? `${sessionCount.toLocaleString()} session${sessionCount === 1 ? '' : 's'} reconciled`
      : 'Snapshot verified';
    updatePreparationStatus({
      tone: deferredReplays.length ? 'warning' : 'complete',
      kicker: deferredReplays.length ? 'Replay deferred' : 'Telemetry ready',
      message: deferredReplays.length
        ? `Sessions refreshed. Run reflect refresh to reconcile ${deferredReplays.join(', ')}.`
        : previousCompletion === completion
          ? `${summary}. Local telemetry is current.`
          : 'Refresh complete. Loading the new snapshot…',
      detail: formatNextRefresh(automaticRefresh),
      phase: 5,
    });
    if (previousCompletion !== completion) {
      window.setTimeout(() => window.location.reload(), 450);
      return false;
    }
    return false;
  }
  updatePreparationStatus({
    tone: 'idle',
    kicker: 'Telemetry ready',
    message: 'Waiting for the next local refresh.',
    detail: formatNextRefresh(automaticRefresh),
  });
  return false;
}

async function pollPreparationStatus(){
  try {
    const response = await fetch('/api/status', {
      headers: {Accept: 'application/json'},
      cache: 'no-store',
    });
    if (!response.ok) throw new Error('status-unavailable');
    const payload = await response.json();
    const preparationRunning = renderPreparationStatus(
      payload.preparation,
      payload.automatic_refresh,
    );
    renderRawStorageStatus(payload.raw_storage);
    preparationStatusPollTimer = window.setTimeout(
      pollPreparationStatus,
      preparationRunning ? 750 : DASHBOARD_REFRESH_MIN_INTERVAL_MS,
    );
  } catch {
    updatePreparationStatus({
      tone: 'warning',
      kicker: 'Status unavailable',
      message: 'Dashboard data remains available while Reflect reconnects.',
      detail: 'Retrying in 30s',
    });
    preparationStatusPollTimer = window.setTimeout(
      pollPreparationStatus,
      DASHBOARD_REFRESH_MIN_INTERVAL_MS,
    );
  }
}

function startPreparationStatusPolling(){
  if (!D?.sql_backed || !reportSupportsServerFiltering()) return;
  if (preparationStatusPollTimer) window.clearTimeout(preparationStatusPollTimer);
  pollPreparationStatus();
}

async function requestDashboardRefresh(){
  if (!D?.sql_backed || !reportSupportsServerFiltering()) return;
  try {
    const statusResponse = await fetch('/api/status', {
      headers: {Accept: 'application/json'},
      cache: 'no-store',
    });
    if (!statusResponse.ok) return;
    const statusPayload = await statusResponse.json();
    if (!statusPayload.refresh_available) return;
    const preparation = statusPayload.preparation || {};
    if (statusPayload.automatic_refresh?.enabled) {
      if (preparation.state === 'running') startPreparationStatusPolling();
      return;
    }
    if (preparation.state === 'running') {
      startPreparationStatusPolling();
      return;
    }
    const finishedAt = Date.parse(preparation.finished_at || '');
    if (Number.isFinite(finishedAt) && Date.now() - finishedAt < DASHBOARD_REFRESH_MIN_INTERVAL_MS) return;
    const refreshResponse = await fetch('/api/refresh', {
      method: 'POST',
      headers: {Accept: 'application/json'},
      cache: 'no-store',
    });
    if (!refreshResponse.ok) return;
    const refreshPayload = await refreshResponse.json();
    if (refreshPayload.preparation?.state === 'running') startPreparationStatusPolling();
  } catch {}
}

async function loadDashboardData(){
  showReportLoader();
  const params = new URLSearchParams(window.location.search);
  const report = params.get('report');
  if (!report && !params.get('data')) {
    const publicHome = 'https://reflect.o11y.dev/';
    const currentPublicUrl = `${window.location.origin}${window.location.pathname}`;
    if (currentPublicUrl !== publicHome) {
      window.location.replace(publicHome);
      return null;
    }
    renderDashboardError('no-data', 'no-data');
    return null;
  }
  try {
    if (report) {
      const response = await fetch(reportUrlWithCurrentFilters(), {headers: {Accept: 'application/json'}});
      if (!response.ok) throw new Error('report-fetch-failed');
      return await response.json();
    }
    const encoded = params.get('data');
    if (!encoded) throw new Error('no-data');
    const b64 = encoded.replace(/-/g, '+').replace(/_/g, '/');
    const binary = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    const json = pako.inflate(binary, { to: 'string' });
    return JSON.parse(json);
  } catch (e) {
    renderDashboardError(e.message, e.message);
    throw e;
  }
}

async function loadImprovementData(){
  if (!D || !D.sql_backed || !reportSupportsServerFiltering()) return IMPROVEMENT_DATA;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 3000);
  const requestOptions = {headers: {Accept: 'application/json'}, signal: controller.signal};
  try {
    const [findingResponse, workflowResponse, measurementResponse, ruleResponse] = await Promise.all([
      fetch('/api/findings', requestOptions),
      fetch('/api/workflows', requestOptions),
      fetch('/api/impact', requestOptions),
      fetch('/api/rules', requestOptions),
    ]);
    if (!findingResponse.ok || !workflowResponse.ok || !measurementResponse.ok || !ruleResponse.ok) return IMPROVEMENT_DATA;
    const [loopResponse, skillResponse] = await Promise.all([
      fetch('/api/loops', requestOptions),
      fetch('/api/skills?limit=500', requestOptions),
    ]);
    const findingPayload = await findingResponse.json();
    const workflows = await workflowResponse.json();
    const measurements = await measurementResponse.json();
    const rules = await ruleResponse.json();
    const loops = loopResponse.ok ? await loopResponse.json() : {loops:[]};
    const skills = skillResponse.ok ? await skillResponse.json() : {skills:[]};
    return {
      ...findingPayload,
      observations: findingPayload.findings || [],
      loops: loops.loops || [],
      skills: skills.skills || [],
      skill_total_count: Number(skills.total_count ?? (skills.skills || []).length),
      skill_archived_count: Number(skills.archived_count || 0),
      skill_counts_by_lifecycle: skills.counts_by_lifecycle || {},
      workflows: workflows.workflows || [],
      measurements: measurements.impact_checks || [],
      rules: rules.rules || [],
      rule_extension: rules.extension || {},
    };
  } catch {
    return IMPROVEMENT_DATA;
  } finally {
    window.clearTimeout(timeout);
  }
}

function updateUrlParams(mutator){
  const url = new URL(window.location.href);
  mutator(url.searchParams);
  const query = url.searchParams.toString();
  window.history.replaceState({}, '', `${url.pathname}${query ? `?${query}` : ''}${url.hash}`);
}

function reportSupportsServerFiltering(){
  const report = new URLSearchParams(window.location.search).get('report') || '/api/data';
  try {
    const url = new URL(report, window.location.href);
    return url.pathname === '/api/data';
  } catch {
    return report === '/api/data' || report.endsWith('/api/data');
  }
}

function reportUrlWithCurrentFilters(){
  const params = new URLSearchParams(window.location.search);
  const report = params.get('report') || '/api/data';
  const url = new URL(report, window.location.href);
  ['q','agents','agent','model','status','range','session','tab','view'].forEach(key => {
    const value = params.get(key);
    if (value) url.searchParams.set(key, value);
  });
  return url.toString();
}

async function initApp(){
  D = await loadDashboardData();
  if (!D) {
    hideReportLoader();
    return;
  }
  IMPROVEMENT_DATA = await loadImprovementData();
  const demoBadge = document.getElementById('demo-badge');
  const reportParam = new URLSearchParams(window.location.search).get('report') || '';
  const isLocalApiReport = ['localhost', '127.0.0.1', '::1'].includes(window.location.hostname)
    && reportParam.replace(/^\//, '') === 'api/data';
  if (demoBadge && isLocalApiReport) demoBadge.hidden = true;
