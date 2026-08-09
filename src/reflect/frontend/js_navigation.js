/* ── Tabs ── */
let _graphsInited = false;
const tabButtons = Array.from(document.querySelectorAll('.tab'));
const tabsEl = document.getElementById('tabs');
const PRODUCT_TABS = new Set(['sessions','workflows','impact','explore']);
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
  updateTabIndicator(activeButton);
  animateTabIndicator();
  document.querySelectorAll('[data-explore-view]').forEach(button => {
    button.classList.toggle('active', button.dataset.exploreView === activeExploreView);
  });
  if (activeTab === 'explore' && activeExploreView === 'graph') {
    hydrateExploreView(activeExploreView)
      .catch(() => {})
      .finally(() => {
        _graphsInited = true;
        requestAnimationFrame(initGraphs);
      });
  } else {
    if (activeTab === 'explore') hydrateExploreView(activeExploreView).catch(() => {});
  }
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
      if (activeTab !== 'workflows') params.delete('skill_q');
    });
  }
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
window.addEventListener('resize', () => updateTabIndicator(document.querySelector('.tab.active')));

const requestedInitialTab = currentParams().get('tab');
const requestedInitialView = currentParams().get('view');
const defaultProductTab = (IMPROVEMENT_DATA.observations || []).length || (IMPROVEMENT_DATA.loops || []).length ? 'workflows' : 'sessions';
activateTab(requestedInitialTab || defaultProductTab, {
  view: requestedInitialView || '',
  updateUrl: Boolean(requestedInitialTab || requestedInitialView),
});

/* ── Header ── */
document.getElementById('hdr-range').textContent = D.first_event_ts + ' — ' + D.last_event_ts;
document.getElementById('hdr-ts').textContent = 'Loaded ' + new Date().toLocaleString();
startPreparationStatusPolling();
window.setTimeout(requestDashboardRefresh, 250);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) requestDashboardRefresh();
});
