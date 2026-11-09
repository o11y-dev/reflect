/* ── Tabs ── */
let _graphsInited = false;
const tabButtons = Array.from(document.querySelectorAll('.tab'));
const tabsEl = document.getElementById('tabs');
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

function updateTabIndicator(activeButton){
  if (!tabsEl || !activeButton) return;
  const navRect = tabsEl.getBoundingClientRect();
  const activeRect = activeButton.getBoundingClientRect();
  const trackWidth = Math.max(1, tabsEl.clientWidth - 48);
  const center = activeRect.left - navRect.left + tabsEl.scrollLeft + (activeRect.width / 2) - 24;
  const originPct = Math.max(0, Math.min(100, (center / trackWidth) * 100));
  tabsEl.style.setProperty('--tab-indicator-origin', `${originPct}%`);
}

function animateTabIndicator(){
  if (!tabsEl) return;
  tabsEl.classList.remove('tab-rail-animate');
  void tabsEl.offsetWidth;
  tabsEl.classList.add('tab-rail-animate');
}

function updateDashboardViewScope(tabName){
  const sessionsVisible = tabName === 'sessions';
  const filtersVisible = sessionsVisible || tabName === 'explore';
  const shell = document.getElementById('report-shell');
  if (shell) shell.dataset.view = tabName;
  for (const id of ['session-rail', 'session-rail-open']) {
    const element = document.getElementById(id);
    if (element) element.hidden = !sessionsVisible;
  }
  for (const id of ['filter-rail', 'filter-sheet-open', 'report-panel-controls', 'hdr-range']) {
    const element = document.getElementById(id);
    if (element) element.hidden = !filtersVisible;
  }
  const sortField = document.getElementById('session-sort-field');
  if (sortField) sortField.hidden = !sessionsVisible;
  const filterTitle = document.getElementById('filter-scope-title');
  if (filterTitle) filterTitle.textContent = sessionsVisible ? 'Session filters' : 'Matching-session filters';
  const filterNote = document.getElementById('filter-scope-note');
  if (filterNote) filterNote.textContent = sessionsVisible
    ? 'Choose sessions to inspect. Session details cover their captured lifetime.'
    : 'Choose matching sessions. Each view labels its event-window or lifetime scope.';
  // Close temporary mobile panels without changing the saved session selection,
  // filters, or the desktop rail's collapsed preference.
  document.body.classList.remove('sessions-rail-open', 'filters-rail-open', 'rail-overlay-open');
  const backdrop = document.getElementById('filter-sheet-backdrop');
  if (backdrop) backdrop.hidden = true;
  const mobile = window.matchMedia('(max-width: 900px)').matches;
  for (const id of ['session-rail-open', 'session-rail-toggle']) {
    document.getElementById(id)?.setAttribute('aria-expanded', String(sessionsVisible && !mobile && !document.body.classList.contains('sessions-rail-collapsed')));
  }
  document.getElementById('filter-sheet-open')?.setAttribute('aria-expanded', String(filtersVisible && !mobile));
  const analysisScope = document.getElementById('inbox-analysis-window');
  const analysisWindow = D.analysis_window;
  if (analysisScope) analysisScope.textContent = analysisWindow?.date_from && analysisWindow?.date_to
    ? `Latest analysis window: ${fmtWorkflowDate(analysisWindow.date_from)} — ${fmtWorkflowDate(analysisWindow.date_to)}.`
    : analysisWindow?.days && analysisWindow?.date_to
      ? `Latest analysis window: ${analysisWindow.days} days ending ${fmtWorkflowDate(analysisWindow.date_to)}.`
      : 'Analysis window not supplied by this report.';
}

function activateTab(tabName, {view = '', updateUrl = true, focusButton = false} = {}){
  const location = canonicalDashboardLocation(tabName, view || activeExploreView);
  const activeTab = location.tab;
  if (activeTab === 'explore') activeExploreView = location.view || 'usage';
  const panelId = activeTab === 'explore' ? `tab-explore-${activeExploreView}` : `tab-${activeTab}`;
  const activeButton = document.querySelector(`.tab[data-tab="${activeTab}"]`) || tabButtons[0];
  tabButtons.forEach(button => {
    const isActive = button === activeButton;
    button.classList.toggle('active', isActive);
    button.setAttribute('aria-selected', isActive ? 'true' : 'false');
    button.tabIndex = isActive ? 0 : -1;
    if (focusButton && isActive) button.focus();
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    const isActive = panel.id === panelId;
    panel.classList.toggle('active', isActive);
    panel.hidden = !isActive;
  });
  updateDashboardViewScope(activeTab);
  updateTabIndicator(activeButton);
  animateTabIndicator();
  document.querySelectorAll('[data-explore-view]').forEach(button => {
    button.classList.toggle('active', button.dataset.exploreView === activeExploreView);
  });
  if (updateUrl) {
    updateUrlParams(params => {
      params.set('tab', activeTab);
      if (activeTab === 'explore') params.set('view', activeExploreView);
      else params.delete('view');
      if (activeTab !== 'workflows') {
        params.delete('workflow');
        params.delete('workflow_root');
        params.delete('workflow_type');
        params.delete('workflow_status');
      }
      if (activeTab !== 'inbox') {
        params.delete('inbox_view');
        params.delete('loop');
      }
      if (activeTab !== 'skills') params.delete('skill_q');
    });
  }
  if (activeTab === 'explore') hydrateExploreView(activeExploreView).catch(() => {});
}

tabButtons.forEach((button, index) => {
  button.addEventListener('click', () => activateTab(button.dataset.tab));
  button.addEventListener('keydown', e => {
    const keyToIndex = {
      ArrowRight: (index + 1) % tabButtons.length,
      ArrowLeft: (index - 1 + tabButtons.length) % tabButtons.length,
      Home: 0,
      End: tabButtons.length - 1,
    };
    if (!(e.key in keyToIndex)) return;
    e.preventDefault();
    const nextButton = tabButtons[keyToIndex[e.key]];
    activateTab(nextButton.dataset.tab, {focusButton: true});
  });
});
document.querySelectorAll('[data-explore-view]').forEach(button => {
  button.addEventListener('click', () => activateTab('explore', {view: button.dataset.exploreView || 'usage'}));
});
window.addEventListener('resize', () => updateTabIndicator(document.querySelector('.tab[aria-selected="true"]')));

const requestedInitialTab = currentParams().get('tab');
const requestedInitialView = currentParams().get('view');
const defaultProductTab = (IMPROVEMENT_DATA.observations || []).length || (IMPROVEMENT_DATA.loops || []).length
  ? 'inbox'
  : (IMPROVEMENT_DATA.workflows || []).length
    ? 'workflows'
    : (IMPROVEMENT_DATA.skills || []).length
      ? 'skills'
      : 'sessions';
activateTab(requestedInitialTab || defaultProductTab, {
  view: requestedInitialView || '',
  updateUrl: Boolean(requestedInitialTab || requestedInitialView),
});

/* ── Header ── */
const headerScope = D.measurements?.scope;
document.getElementById('hdr-range').textContent = `${fmtWorkflowDate(headerScope?.kind === 'event_window' ? headerScope.date_from : D.first_event_ts)} — ${fmtWorkflowDate(headerScope?.kind === 'event_window' ? headerScope.date_to : D.last_event_ts)}`;
document.getElementById('hdr-ts').textContent = 'Loaded ' + new Date().toLocaleString();
startPreparationStatusPolling();
window.setTimeout(requestDashboardRefresh, 250);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) requestDashboardRefresh();
});
