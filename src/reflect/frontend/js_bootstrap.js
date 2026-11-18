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
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;min-height:100vh;color:#ff453a;font-family:monospace"></div>';
  document.body.firstElementChild.textContent = 'Unable to load report: ' + message;
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
  if (loader) {
    loader.hidden = false;
    loader.classList.remove('is-hidden');
  }
}

function hideReportLoader(){
  const loader = document.getElementById('report-loader');
  if (loader) {
    loader.hidden = true;
    loader.classList.add('is-hidden');
  }
}

let preparationStatusPollTimer = null;
let preparationStatusPolling = false;
let dashboardDocumentActive = true;
function suspendDashboardReads(){
  dashboardDocumentActive = false;
  if (preparationStatusPollTimer) window.clearTimeout(preparationStatusPollTimer);
  preparationStatusPollTimer = null;
}
window.addEventListener('pagehide', suspendDashboardReads);
window.addEventListener('pageshow', event => {
  dashboardDocumentActive = true;
  if (event.persisted) startPreparationStatusPolling();
});
const DASHBOARD_REFRESH_MIN_INTERVAL_MS = 10000;
let lastPublishedRevision = null;
const PREPARATION_PHASES = Object.freeze({
  opening_store: 1,
  backing_up_store: 1,
  migrating_schema: 1,
  ingesting_traces: 2,
  ingesting_logs: 2,
  ingesting_sessions: 3,
  normalizing: 3,
  publishing_usage: 4,
  publishing_snapshot: 5,
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
      detail: `${D.analysis_state === 'preparing' ? 'Usage available · analysis pending' : `Step ${phase || 1} of 5`}${elapsed ? ` · ${elapsed}` : ''}`,
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
          : 'Refresh complete. Updating displayed results…',
      detail: formatNextRefresh(automaticRefresh),
      phase: 5,
    });
    return false;
  }
  if (D?._unpublishedNavigation) {
    updatePreparationStatus({
      tone: 'warning',
      kicker: 'Report not loaded',
      message: 'Published report is not loaded. Captured session activity is available.',
      detail: formatNextRefresh(automaticRefresh),
    });
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
  if (!dashboardDocumentActive || preparationStatusPolling) return;
  preparationStatusPolling = true;
  preparationStatusPollTimer = null;
  try {
    const response = await fetch('/api/status', {
      headers: {Accept: 'application/json'},
      cache: 'no-store',
    });
    if (!response.ok) throw new Error('status-unavailable');
    const payload = await response.json();
    if (!dashboardDocumentActive) return;
    const preparationRunning = renderPreparationStatus(
      payload.preparation,
      payload.automatic_refresh,
    );
    renderRawStorageStatus(payload.raw_storage);
    const revision = payload.publication
      ? `${payload.publication.generation}:${payload.user_state_revision || 0}` : null;
    if (revision && revision !== lastPublishedRevision) {
      lastPublishedRevision = revision;
      document.dispatchEvent(new CustomEvent('reflect-snapshot-published', {
        cancelable:true, detail:{completion:payload.publication.completed_at, profile:'snapshot', revision},
      }));
    }
    document.dispatchEvent(new CustomEvent('reflect-status-polled', {detail:payload}));
    if (!dashboardDocumentActive) return;
    preparationStatusPollTimer = window.setTimeout(
      pollPreparationStatus,
      preparationRunning ? 1000 : DASHBOARD_REFRESH_MIN_INTERVAL_MS,
    );
  } catch {
    if (!dashboardDocumentActive) return;
    updatePreparationStatus({
      tone: 'warning',
      kicker: 'Status unavailable',
      message: 'Dashboard data remains available while Reflect reconnects.',
      detail: 'Reconnecting in 10s',
    });
    preparationStatusPollTimer = window.setTimeout(
      pollPreparationStatus,
      DASHBOARD_REFRESH_MIN_INTERVAL_MS,
    );
  } finally {
    preparationStatusPolling = false;
  }
}

function startPreparationStatusPolling(){
  if (!dashboardDocumentActive || !D?.sql_backed || !reportSupportsServerFiltering()) return;
  if (preparationStatusPollTimer) window.clearTimeout(preparationStatusPollTimer);
  pollPreparationStatus();
}

async function requestDashboardRefresh(){
  // The backend owns scheduling; visiting or focusing a tab never starts a refresh.
  startPreparationStatusPolling();
}

async function loadDashboardData(){
  if (!dashboardDocumentActive) return null;
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
      while (true) {
        if (!dashboardDocumentActive) return null;
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 30000);
        try {
          const options = {headers: {Accept: 'application/json'}, signal: controller.signal, cache: 'no-store'};
          const response = await fetch(reportUrlWithCurrentFilters(), options);
          if (response.ok) return await response.json();
          const pending = response.status === 503 ? await response.json() : null;
          if (!dashboardDocumentActive) return null;
          if (pending?.state !== 'preparing' && !(pending?.state === 'busy' && pending.reason === 'reading')) {
            throw new Error('report-fetch-failed');
          }
          // These sections own independent reads; a cold session snapshot must
          // not prevent their navigation and evidence from initializing.
          if (pending.reason !== 'reading' && ['inbox','skills','workflows','impact'].includes(params.get('tab')) && reportSupportsServerFiltering()) {
            return {sql_backed:true, _unpublishedNavigation:true, snapshot_completed_at:null,
              sessions:[], sqlite:{tabs:{}}};
          }
          // A session link can navigate the completed card cache while its
          // richer details refresh. Never reuse global totals as scoped data.
          const sessionId = params.get('session');
          if ((params.get('tab') === 'explore' || sessionId) && reportSupportsServerFiltering()) {
            const cacheUrl = new URL(report, window.location.href);
            cacheUrl.search = '';
            const cachedResponse = await fetch(cacheUrl.toString(), options);
            if (!dashboardDocumentActive) return null;
            if (cachedResponse.ok) {
              const cached = await cachedResponse.json();
              cached._navigationFromCache = true;
              cached._unfilteredNavigation = true;
              if (params.get('tab') !== 'explore') {
                updateUrlParams(values => { values.set('tab', 'sessions'); values.delete('view'); });
              }
              return cached;
            }
            if (sessionId && cachedResponse.status === 503 && (await cachedResponse.json()).state === 'preparing') {
              if (!dashboardDocumentActive) return null;
              const detailResponse = await fetch('/api/session/' + encodeURIComponent(sessionId), options);
              if (detailResponse.status === 404) throw new Error('Session not found in captured telemetry.');
              if (detailResponse.ok) {
                const detail = await detailResponse.json();
                if (detail.session?.id === sessionId) {
                  updateUrlParams(values => { values.set('tab', 'sessions'); values.delete('view'); });
                  return {
                    sql_backed:true, _unpublishedNavigation:true, snapshot_completed_at:null,
                    sessions:[{id:sessionId, full_id:sessionId, agent:detail.session.agent,
                      _navigationOnly:true, _initialDetail:detail}],
                    sqlite:{tabs:{}},
                  };
                }
              }
            }
          }
          if (pending.reason === 'reading') {
            showReportLoader(pending.error || 'Loading selected sessions in the background…');
          } else {
            if (!dashboardDocumentActive) return null;
            const statusResponse = await fetch('/api/status', options);
            if (!statusResponse.ok) throw new Error('Refresh status unavailable. Reload to retry.');
            const status = await statusResponse.json();
            if (status.preparation?.state === 'failed') {
              throw new Error(status.preparation.error || 'Report preparation failed. Check the server log.');
            }
            const elapsed = formatPreparationElapsed(status.preparation?.started_at);
            showReportLoader((status.preparation?.message || 'Preparing report…') + (elapsed ? ` · ${elapsed}` : ''));
          }
        } finally {
          window.clearTimeout(timeout);
        }
        await new Promise(resolve => window.setTimeout(resolve, 2000));
      }
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
  } finally {
    hideReportLoader();
  }
}

async function loadImprovementData(onResource = null){
  if (!dashboardDocumentActive || !D || !D.sql_backed || (!isStaticDemo() && !reportSupportsServerFiltering())) return IMPROVEMENT_DATA;
  const sequence = loadImprovementData.sequence = (loadImprovementData.sequence || 0) + 1;
  const result = {...IMPROVEMENT_DATA, resources:{...IMPROVEMENT_DATA.resources}};
  const requests = [
    ['findings', '/api/findings', payload => ({
      ...payload,
      observations:payload.findings || [],
      finding_total_count:Number(payload.finding_total_count ?? (payload.findings || []).length),
      observation_record_count:Number(payload.observation_record_count ?? 0),
      counts_by_status:payload.counts_by_status || {},
    })],
    ['workflows', '/api/workflows', payload => ({workflows:payload.workflows || []})],
    ['impact', '/api/impact', payload => ({measurements:payload.impact_checks || []})],
    ['rules', '/api/rules', payload => ({rules:payload.rules || [], rule_extension:payload.extension || {}})],
    ['loops', '/api/loops', payload => ({loops:payload.loops || []})],
    ['skills', '/api/skills?limit=500', payload => ({
      skills:payload.skills || [],
      skill_total_count:Number(payload.total_count ?? (payload.skills || []).length),
      skill_archived_count:Number(payload.archived_count || 0),
      skill_counts_by_lifecycle:payload.counts_by_lifecycle || {},
      skill_analysis_state:payload.analysis_state || 'ready',
      skill_evidence_state:payload.evidence_state || null,
    })],
  ];
  for (const [name] of requests) {
    result.resources[name] ||= {state:'preparing',loaded:false,loaded_at:null};
  }
  const publish = name => {
    if (sequence === loadImprovementData.sequence && onResource) {
      onResource({...result, resources:{...result.resources}}, name);
    }
  };
  publish(null);
  await Promise.all(requests.map(async ([name, url, project]) => {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 5000);
      const requestOptions = {headers:{Accept:'application/json'},signal:controller.signal};
      const loaded_at = IMPROVEMENT_DATA.resources?.[name]?.loaded_at ?? null;
      const previous = IMPROVEMENT_DATA.resources?.[name];
      const loaded = Boolean(previous?.loaded || loaded_at || ['ready','stale'].includes(previous?.state));
      try {
        const response = await reportReadResponse(url, requestOptions);
        const payload = await response.json();
        if (!response.ok) {
          result.resources[name] = {
            ...previous,
            state:response.status === 503 && payload.state === 'preparing' ? 'preparing' : 'error',
            loaded_at,
            loaded,
          };
          return;
        }
        Object.assign(result, project(payload));
        result.resources[name] = {
          state:['stale','recorded'].includes(payload.publication?.state) ? payload.publication.state : 'ready',
          loaded:true,
          loaded_at:payload.publication?.snapshot_completed_at || payload.snapshot_completed_at
            || payload.publication?.loaded_at || (payload.publication?.state === 'stale' ? null : new Date().toISOString()),
          publication:{...payload.publication,
            snapshot_completed_at:payload.publication?.snapshot_completed_at || payload.snapshot_completed_at || null},
        };
      } catch {
        result.resources[name] = {...previous, state:'error', loaded_at, loaded};
      } finally {
        window.clearTimeout(timeout);
        publish(name);
      }
    }));
  return sequence === loadImprovementData.sequence ? result : IMPROVEMENT_DATA;
}

function updateUrlParams(mutator, {mode = 'replace'} = {}){
  const url = new URL(window.location.href);
  mutator(url.searchParams);
  const query = url.searchParams.toString();
  const target = `${url.pathname}${query ? `?${query}` : ''}${url.hash}`;
  if (target === `${url.pathname}${url.search}${url.hash}`) return;
  window.history[mode === 'push' ? 'pushState' : 'replaceState']({}, '', target);
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
  try {
    configureReportSource(D);
    const report = new URLSearchParams(window.location.search).get('report') || '';
    if (/showcase\.json(?:$|\?)/.test(report) && !isStaticDemo()) throw new Error('This sample is outdated. Reload the latest public example.');
  } catch (error) {
    renderDashboardError('sample-unavailable', error.message);
    return;
  }
  let improvementSurfacesReady = false;
  // The core dashboard is usable immediately; each analysis panel publishes
  // independently as its bounded read completes.
  loadImprovementData(partial => {
    IMPROVEMENT_DATA = partial;
    if (improvementSurfacesReady) buildImprovementSurfaces();
  }).catch(() => {});
  const demoBadge = document.getElementById('demo-badge');
  const reportParam = new URLSearchParams(window.location.search).get('report') || '';
  const isLocalApiReport = ['localhost', '127.0.0.1', '::1'].includes(window.location.hostname)
    && reportParam.replace(/^\//, '') === 'api/data';
  if (demoBadge && isLocalApiReport) demoBadge.hidden = true;
