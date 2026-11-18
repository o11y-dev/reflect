/* ── Tabs ── */
const tabButtons = Array.from(document.querySelectorAll('.tab'));
const PRODUCT_TABS = new Set(['sessions','inbox','workflows','skills','impact','explore']);
const EXPLORE_VIEWS = new Set(['usage','tools','graph','context']);
let activeExploreView = 'usage';

function canonicalDashboardLocation(tabName, viewName = ''){
  const requestedTab = String(tabName || '').trim().toLowerCase();
  const tab = PRODUCT_TABS.has(requestedTab) ? requestedTab : 'sessions';
  if (tab !== 'explore') return {tab};
  const normalizedView = String(viewName || '').trim().toLowerCase();
  return {tab, view: EXPLORE_VIEWS.has(normalizedView) ? normalizedView : 'usage'};
}

function updateTabOrientation(){
  const tabs = document.getElementById('tabs');
  if (tabs) tabs.setAttribute('aria-orientation', window.matchMedia('(max-width: 720px)').matches ? 'horizontal' : 'vertical');
}

let exploreMatchingSessionCount = null;

function updateExploreScope(){
  const label = document.getElementById('explore-scope-label');
  const change = document.getElementById('explore-session-change');
  const clear = document.getElementById('explore-session-clear');
  const fixed = isStaticDemo();
  const context = activeExploreView === 'context';
  const sessionId = fixed || context ? '' : exploreViewUrl(activeExploreView).sessionId;
  const params = currentParams();
  const range = params.get('range') || (sessionId ? 'all' : D.range);
  const rangeLabel = range === 'all' ? 'All time' : range === '24h' ? 'Last 24 hours'
    : /^\d+d$/.test(range || '') ? `Last ${parseInt(range, 10)} days` : 'Default date range';
  const agents = (params.get('agents') || params.get('agent') || '').split(',').filter(Boolean);
  const agentLabel = agents.length ? agents.map(formatAgentLabel).join(', ') : 'All agents';
  const session = (D.sessions || []).find(item => (item.full_id || item.id) === sessionId || item.id === sessionId);
  const matchingCount = exploreMatchingSessionCount;
  const sessionLabel = sessionId ? `Session: ${sessionListIdentity(session || {id:sessionId}).title}`
    : matchingCount === null ? 'Matching sessions'
    : `${fmt(matchingCount)} matching session${matchingCount === 1 ? '' : 's'}`;
  const saved = ['range','agents','agent','q','model','status','session'].some(key => params.has(key));
  const scope = fixed ? 'Full illustrative sample'
    : context ? (saved ? 'All captured context · Session filters saved and inactive' : 'All captured context')
    : [rangeLabel, agentLabel, sessionLabel].join(' · ');
  if (label.textContent !== scope) label.textContent = scope;
  change.hidden = fixed || context;
  change.textContent = sessionId ? 'Change session' : 'Choose session';
  clear.hidden = fixed || context || !sessionId;
}

function clearExploreSessionScope(){
  dashboardSelectedSessionId = '';
  updateUrlParams(params => { params.delete('session'); params.delete('evidence'); }, {mode:'push'});
  updateDashboardViewScope('explore');
  document.getElementById('explore-session-change')?.focus();
  return hydrateExploreView(activeExploreView).catch(() => {});
}

function chooseExploreSession(){
  activateTab('sessions', {clearSession:true});
  document.getElementById('session-table-heading')?.focus();
}

function updateDashboardViewScope(tabName){
  const sessionsVisible = tabName === 'sessions';
  const filtersVisible = sessionsVisible || (tabName === 'explore' && activeExploreView !== 'context' && !isStaticDemo());
  const sectionTitle = document.getElementById('section-nav-title');
  const sectionScope = document.getElementById('section-scope-note');
  const sectionExploreNav = document.getElementById('section-explore-nav');
  const [title, note] = {
    sessions: ['Session scope', 'Filters apply to the session list.'],
    inbox: ['Finding scope', 'Findings link to the tasks that support them.'],
    workflows: ['Workflow filters', 'Review project changes before enabling a workflow.'],
    skills: ['Registry filters', 'Skill references cover all captured history.'],
    impact: ['Comparison scope', 'Each check uses its recorded task cohorts.'],
    explore: ['Explore views', ''],
  }[tabName];
  sectionTitle.textContent = title;
  sectionScope.textContent = note;
  sectionScope.hidden = !note;
  if (sectionExploreNav) sectionExploreNav.hidden = tabName !== 'explore';
  const sectionSidebar = document.getElementById('section-sidebar');
  sectionSidebar.dataset.view = tabName;
  sectionSidebar.hidden = sessionsVisible;
  const shell = document.getElementById('report-shell');
  if (shell) {
    shell.dataset.view = tabName;
    shell.dataset.exploreView = activeExploreView;
  }
  const params = currentParams();
  const hasSession = sessionsVisible && params.has('session');
  const exploreVisible = tabName === 'explore';
  const sessionListVisible = sessionsVisible && !hasSession;
  const filterDisclosure = document.getElementById('section-filters');
  document.getElementById('dashboard-scope').hidden = !exploreVisible && !sessionListVisible;
  document.getElementById('session-scope-summary').hidden = !sessionListVisible;
  document.getElementById('explore-scope-label').hidden = !exploreVisible;
  if (exploreVisible) updateExploreScope();
  else {
    document.getElementById('explore-session-change').hidden = true;
    document.getElementById('explore-session-clear').hidden = true;
  }
  filterDisclosure.hidden = !filtersVisible || hasSession;
  if (filterDisclosure.hidden) filterDisclosure.open = false;
  document.getElementById('workflow-scope-controls').hidden = tabName !== 'workflows';
  document.getElementById('skill-scope-controls').hidden = tabName !== 'skills';
  document.getElementById('hdr-range').hidden = !sessionListVisible;
  document.getElementById('session-sort-field').hidden = !sessionsVisible;

  const inactive = document.getElementById('inactive-scope');
  const inactiveSessionFilters = !filtersVisible || hasSession;
  const savedScope = [
    inactiveSessionFilters && params.has('range') && `range ${params.get('range')}`,
    inactiveSessionFilters && (params.get('agents') || params.get('agent')) && `agents ${params.get('agents') || params.get('agent')}`,
    inactiveSessionFilters && params.has('model') && 'model',
    inactiveSessionFilters && params.has('status') && 'status',
    inactiveSessionFilters && params.has('q') && 'session search',
    !sessionsVisible && params.has('sort') && 'session sort',
    !filtersVisible && params.has('session') && 'selected session',
    tabName !== 'workflows' && (params.has('workflow_type') || params.has('workflow_status')) && 'workflow filters',
    tabName !== 'skills' && params.has('skill_q') && 'skill search',
  ].filter(Boolean);
  inactive.hidden = exploreVisible || !savedScope.length;
  inactive.textContent = savedScope.length ? `Saved scope inactive here · ${savedScope.join(', ')}` : '';
  const analysisScope = document.getElementById('inbox-analysis-window');
  const analysisWindow = D.analysis_window;
  if (analysisScope) analysisScope.textContent = isStaticDemo() ? 'Fixed illustrative sample.' : analysisWindow?.date_from && analysisWindow?.date_to
    ? `Latest analysis window: ${fmtWorkflowDate(analysisWindow.date_from)} — ${fmtWorkflowDate(analysisWindow.date_to)}.`
    : analysisWindow?.days && analysisWindow?.date_to
      ? `Latest analysis window: ${analysisWindow.days} days ending ${fmtWorkflowDate(analysisWindow.date_to)}.`
      : 'Analysis window not supplied by this report.';
}

function activateTab(tabName, {view = '', updateUrl = true, focusButton = false, clearSession = false} = {}){
  const previousTab = tabButtons.find(button => button.getAttribute('aria-selected') === 'true')?.dataset.tab || '';
  const previousExploreView = activeExploreView;
  const location = canonicalDashboardLocation(tabName, view || activeExploreView);
  const activeTab = location.tab;
  if (activeTab === 'explore') activeExploreView = location.view || 'usage';
  const panelId = activeTab === 'explore' ? `tab-explore-${activeExploreView}` : `tab-${activeTab}`;
  const activeButton = document.querySelector(`.tab[data-tab="${activeTab}"]`) || tabButtons[0];
  tabButtons.forEach(button => {
    const isActive = button === activeButton;
    button.classList.toggle('active', isActive);
    button.setAttribute('aria-selected', isActive ? 'true' : 'false');
    const controlledPanel = button.dataset.tab === 'explore'
      ? activeTab === 'explore' && isActive ? panelId : 'tab-explore-usage'
      : `tab-${button.dataset.tab}`;
    button.setAttribute('aria-controls', controlledPanel);
    button.tabIndex = isActive ? 0 : -1;
    if (focusButton && isActive) button.focus();
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    const isActive = panel.id === panelId;
    panel.classList.toggle('active', isActive);
    panel.hidden = !isActive;
  });
  document.querySelectorAll('#section-explore-nav [data-explore-view]').forEach(button => {
    const active = button.dataset.exploreView === activeExploreView;
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  if (updateUrl) {
    const routeChanged = activeTab !== previousTab
      || (activeTab === 'explore' && previousExploreView !== activeExploreView);
    updateUrlParams(params => {
      params.set('tab', activeTab);
      if (clearSession) {
        params.delete('session');
        params.delete('evidence');
      }
      if (activeTab === 'explore') params.set('view', activeExploreView);
      else params.delete('view');
      if (activeTab !== previousTab) {
        for (const key of ledgerSelectionParams) params.delete(key);
        params.delete('workflow_root');
      }
    }, {mode: routeChanged ? 'push' : 'replace'});
  }
  updateDashboardViewScope(activeTab);
  document.dispatchEvent(new CustomEvent('dashboard-route-changed', {detail:{tab:activeTab}}));
  if (activeTab === 'explore') hydrateExploreView(activeExploreView).catch(() => {});
}

tabButtons.forEach((button, index) => {
  button.addEventListener('click', () => activateTab(button.dataset.tab));
  button.addEventListener('keydown', e => {
    const keyToIndex = {
      ArrowRight: (index + 1) % tabButtons.length,
      ArrowDown: (index + 1) % tabButtons.length,
      ArrowLeft: (index - 1 + tabButtons.length) % tabButtons.length,
      ArrowUp: (index - 1 + tabButtons.length) % tabButtons.length,
      Home: 0,
      End: tabButtons.length - 1,
    };
    if (!(e.key in keyToIndex)) return;
    e.preventDefault();
    const nextButton = tabButtons[keyToIndex[e.key]];
    activateTab(nextButton.dataset.tab, {focusButton: true});
  });
});
updateTabOrientation();
window.addEventListener('resize', updateTabOrientation);
const dashboardFilters = document.getElementById('section-filters');
document.addEventListener('pointerdown', event => {
  if (dashboardFilters.open && !dashboardFilters.contains(event.target)) dashboardFilters.open = false;
});
dashboardFilters.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || !dashboardFilters.open) return;
  event.preventDefault();
  dashboardFilters.open = false;
  dashboardFilters.querySelector('summary').focus();
});
document.getElementById('explore-session-change')?.addEventListener('click', chooseExploreSession);
document.getElementById('explore-session-clear')?.addEventListener('click', clearExploreSessionScope);
document.querySelectorAll('#section-explore-nav [data-explore-view]').forEach(button => {
  button.addEventListener('click', () => activateTab('explore', {view: button.dataset.exploreView || 'usage'}));
});
const defaultProductTab = (IMPROVEMENT_DATA.observations || []).length || (IMPROVEMENT_DATA.loops || []).length
  ? 'inbox'
  : (IMPROVEMENT_DATA.workflows || []).length
    ? 'workflows'
    : (IMPROVEMENT_DATA.skills || []).length
      ? 'skills'
      : 'sessions';

function restoreDashboardRoute(){
  const params = currentParams();
  const inferred = [
    ['session', 'sessions'],
    ['workflow', 'workflows'],
    ['finding', 'inbox'],
    ['finding_draft', 'inbox'],
    ['loop', 'inbox'],
    ['skill', 'skills'],
    ['impact', 'impact'],
    ['view', 'explore'],
  ].find(([key]) => params.has(key))?.[1];
  const defaultTab = isStaticDemo() ? 'inbox' : isLocalDashboard() ? 'sessions' : defaultProductTab;
  activateTab(params.get('tab') || inferred || defaultTab, {
    view:params.get('view') || 'usage',
    updateUrl:false,
  });
}
restoreDashboardRoute();
window.addEventListener('popstate', () => {
  restoreDashboardRoute();
  buildImprovementSurfaces();
});

/* ── Header ── */
function updateDashboardHeaderRange(report){
  const scope = report.measurements?.scope;
  const from = scope?.kind === 'event_window' ? scope.date_from : report.first_event_ts;
  const to = scope?.kind === 'event_window' ? scope.date_to : report.last_event_ts;
  document.getElementById('hdr-range').textContent = from && to ? `${fmtWorkflowDate(from)} — ${fmtWorkflowDate(to)}` : '';
}
document.getElementById('hdr-ts').textContent = isStaticDemo()
  ? 'Illustrative sample · fixed dates · all sample usage'
  : 'Loaded ' + new Date().toLocaleString();
startPreparationStatusPolling();
window.setTimeout(requestDashboardRefresh, 250);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) requestDashboardRefresh();
});
