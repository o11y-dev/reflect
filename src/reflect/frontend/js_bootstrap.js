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
function renderPreparationStatus(preparation){
  const status = document.getElementById('preparation-status');
  const copy = document.getElementById('preparation-status-copy');
  if (!status || !copy) return false;
  const state = preparation?.state || 'idle';
  status.className = `preparation-status ${state}`;
  if (state === 'running') {
    status.hidden = false;
    copy.textContent = preparation.message || 'Refreshing local telemetry...';
    return true;
  }
  if (state === 'failed') {
    status.hidden = false;
    copy.textContent = preparation.error
      ? `Refresh failed: ${preparation.error}`
      : 'Refresh failed. Check the server log for details.';
    return false;
  }
  if (state === 'complete') {
    const completion = preparation.finished_at || `generation:${preparation.generation || 0}`;
    const deferredReplays = preparation.result?.deferred_replays || [];
    let previousCompletion = '';
    try {
      previousCompletion = window.sessionStorage.getItem('reflect.preparation.finished_at') || '';
      window.sessionStorage.setItem('reflect.preparation.finished_at', completion);
    } catch {}
    status.hidden = false;
    copy.textContent = deferredReplays.length
      ? `Sessions refreshed. Deferred replay for replaced ${deferredReplays.join(', ')}; run reflect refresh to reconcile it.`
      : previousCompletion === completion
        ? 'Local telemetry is current.'
        : 'Refresh complete. Loading the new snapshot...';
    if (previousCompletion !== completion) {
      window.setTimeout(() => window.location.reload(), 450);
      return false;
    }
    window.setTimeout(() => { status.hidden = true; }, 5000);
    return false;
  }
  status.hidden = true;
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
    if (renderPreparationStatus(payload.preparation)) {
      preparationStatusPollTimer = window.setTimeout(pollPreparationStatus, 750);
    }
  } catch {
    const status = document.getElementById('preparation-status');
    if (status) status.hidden = true;
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
