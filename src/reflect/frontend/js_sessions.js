/* ════════════════════ SESSION BROWSER ════════════════════ */
class SessionConversationPlayhead {
  constructor(panel, options = {}) {
    this.panel = panel;
    this.track = panel?.querySelector('.session-timeline-track') || null;
    this.playhead = panel?.querySelector('.session-timeline-playhead') || null;
    this.scroller = options.scroller || null;
    this.onSelectionChange = options.onSelectionChange || (() => {});
    this.onRequireConversation = options.onRequireConversation || (() => {});
    this.initialConversationIndex = Number.isInteger(options.initialConversationIndex)
      ? options.initialConversationIndex
      : null;
    this.points = this.track
      ? [...this.track.querySelectorAll('[data-timeline-event-index]')].map(element => ({
          element,
          conversationIndex: Number(element.dataset.timelineEventIndex),
          position: Number(element.dataset.timelinePosition || 0),
          label: element.dataset.timelineLabel || 'Event',
          summary: element.dataset.timelineSummary || '',
          time: element.dataset.timelineTime || '+0:00',
        }))
      : [];
    this.rows = this.scroller
      ? [...this.scroller.querySelectorAll('[data-conversation-event-index]')]
      : [];
    this.pointByConversationIndex = new Map(this.points.map((point, index) => [point.conversationIndex, index]));
    this.abortController = new AbortController();
    this.selectedPointIndex = 0;
    this.dragging = false;
    this.scrollFrame = 0;
    this.programmaticScrollUntil = 0;
    this.reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches || false;
  }

  mount() {
    if (!this.panel || !this.track || !this.playhead || !this.points.length) return false;
    const signal = this.abortController.signal;
    this.track.addEventListener('pointerdown', event => this.startDrag(event), {signal});
    this.track.addEventListener('pointermove', event => this.moveDrag(event), {signal});
    this.track.addEventListener('pointerup', event => this.finishDrag(event), {signal});
    this.track.addEventListener('pointercancel', event => this.finishDrag(event), {signal});
    this.playhead.addEventListener('keydown', event => this.handleKeydown(event), {signal});
    this.points.forEach((point, index) => {
      point.element.addEventListener('click', event => {
        if (event.detail !== 0) return;
        this.selectPoint(index, {scroll: true, behavior: 'smooth'});
        this.openConversationIfNeeded();
      }, {signal});
    });
    if (this.scroller && this.rows.length) {
      this.scroller.addEventListener('scroll', () => this.scheduleConversationSync(), {passive: true, signal});
    }
    const initialPoint = this.initialConversationIndex === null
      ? 0
      : (this.pointByConversationIndex.get(this.initialConversationIndex) ?? 0);
    this.selectPoint(initialPoint, {scroll: false});
    if (this.initialConversationIndex !== null && this.rows.length) {
      requestAnimationFrame(() => this.selectPoint(initialPoint, {scroll: true, behavior: 'auto'}));
    }
    return true;
  }

  destroy() {
    this.abortController.abort();
    if (this.scrollFrame) cancelAnimationFrame(this.scrollFrame);
  }

  pointIndexAt(clientX) {
    const bounds = this.track.getBoundingClientRect();
    const percent = bounds.width > 0
      ? Math.max(0, Math.min(100, ((clientX - bounds.left) / bounds.width) * 100))
      : 0;
    let nearest = 0;
    let distance = Number.POSITIVE_INFINITY;
    this.points.forEach((point, index) => {
      const nextDistance = Math.abs(point.position - percent);
      if (nextDistance < distance) {
        distance = nextDistance;
        nearest = index;
      }
    });
    return nearest;
  }

  startDrag(event) {
    if (event.button !== 0) return;
    event.preventDefault();
    this.dragging = true;
    this.panel.classList.add('is-scrubbing');
    this.track.setPointerCapture?.(event.pointerId);
    const eventButton = event.target.closest?.('[data-timeline-event-index]');
    const directIndex = eventButton
      ? this.points.findIndex(point => point.element === eventButton)
      : -1;
    this.selectPoint(directIndex >= 0 ? directIndex : this.pointIndexAt(event.clientX), {scroll: true, behavior: 'auto'});
  }

  moveDrag(event) {
    if (!this.dragging) return;
    event.preventDefault();
    this.selectPoint(this.pointIndexAt(event.clientX), {scroll: true, behavior: 'auto'});
  }

  finishDrag(event) {
    if (!this.dragging) return;
    this.dragging = false;
    this.panel.classList.remove('is-scrubbing');
    if (this.track.hasPointerCapture?.(event.pointerId)) this.track.releasePointerCapture(event.pointerId);
    this.selectPoint(this.selectedPointIndex, {scroll: true, behavior: 'smooth'});
    this.openConversationIfNeeded();
  }

  handleKeydown(event) {
    const key = event.key;
    let next = this.selectedPointIndex;
    if (key === 'ArrowLeft' || key === 'ArrowUp') next -= 1;
    else if (key === 'ArrowRight' || key === 'ArrowDown') next += 1;
    else if (key === 'PageUp') next -= 5;
    else if (key === 'PageDown') next += 5;
    else if (key === 'Home') next = 0;
    else if (key === 'End') next = this.points.length - 1;
    else return;
    event.preventDefault();
    this.selectPoint(Math.max(0, Math.min(this.points.length - 1, next)), {scroll: true, behavior: 'smooth'});
    this.openConversationIfNeeded();
  }

  openConversationIfNeeded() {
    if (this.rows.length) return;
    const point = this.points[this.selectedPointIndex];
    if (point) this.onRequireConversation(point.conversationIndex);
  }

  selectPoint(index, options = {}) {
    const point = this.points[index];
    if (!point) return;
    this.selectedPointIndex = index;
    this.playhead.style.setProperty('--playhead-left', `${point.position}%`);
    this.playhead.dataset.edge = point.position < 12 ? 'left' : (point.position > 88 ? 'right' : 'center');
    this.playhead.setAttribute('aria-valuenow', String(index + 1));
    this.playhead.setAttribute('aria-valuetext', `${point.time} · ${point.label}${point.summary ? ` · ${point.summary}` : ''}`);
    this.playhead.querySelector('.session-playhead-time').textContent = point.time;
    this.playhead.querySelector('.session-playhead-label').textContent = point.label;
    const summary = this.playhead.querySelector('.session-playhead-summary');
    summary.textContent = point.summary;
    summary.hidden = !point.summary;
    this.points.forEach((item, itemIndex) => item.element.classList.toggle('is-active', itemIndex === index));
    this.rows.forEach(row => row.classList.toggle(
      'is-playhead-active',
      Number(row.dataset.conversationEventIndex) === point.conversationIndex,
    ));
    this.onSelectionChange(point.conversationIndex);
    if (options.scroll) this.scrollToConversationEvent(point.conversationIndex, options.behavior || 'auto');
  }

  scrollToConversationEvent(conversationIndex, behavior) {
    if (!this.scroller) return;
    const row = this.rows.find(item => Number(item.dataset.conversationEventIndex) === conversationIndex);
    if (!row) return;
    const group = row.closest('details');
    if (group && !group.open) group.open = true;
    const scrollerBounds = this.scroller.getBoundingClientRect();
    const rowBounds = row.getBoundingClientRect();
    const targetTop = this.scroller.scrollTop
      + rowBounds.top
      - scrollerBounds.top
      - Math.max(0, (this.scroller.clientHeight - rowBounds.height) / 2);
    const finalBehavior = this.reducedMotion ? 'auto' : behavior;
    this.programmaticScrollUntil = performance.now() + (finalBehavior === 'smooth' ? 520 : 100);
    this.scroller.scrollTo({top: Math.max(0, targetTop), behavior: finalBehavior});
  }

  scheduleConversationSync() {
    if (this.dragging || performance.now() < this.programmaticScrollUntil || this.scrollFrame) return;
    this.scrollFrame = requestAnimationFrame(() => {
      this.scrollFrame = 0;
      this.syncFromConversation();
    });
  }

  syncFromConversation() {
    if (!this.scroller || !this.rows.length) return;
    const scrollerBounds = this.scroller.getBoundingClientRect();
    const center = scrollerBounds.top + scrollerBounds.height / 2;
    let nearestPoint = null;
    let nearestDistance = Number.POSITIVE_INFINITY;
    this.rows.forEach(row => {
      const conversationIndex = Number(row.dataset.conversationEventIndex);
      const pointIndex = this.pointByConversationIndex.get(conversationIndex);
      if (pointIndex === undefined) return;
      const bounds = row.getBoundingClientRect();
      if (!bounds.height || bounds.bottom < scrollerBounds.top || bounds.top > scrollerBounds.bottom) return;
      const distance = Math.abs((bounds.top + bounds.height / 2) - center);
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nearestPoint = pointIndex;
      }
    });
    if (nearestPoint !== null && nearestPoint !== this.selectedPointIndex) {
      this.selectPoint(nearestPoint, {scroll: false});
    }
  }
}

(function initSessionBrowser(){
  const sessions = D.sessions || [];
  if (!sessions.length) return;

  const params = currentParams();
  const initialSessionId = params.get('session');
  let selectedIdx = initialSessionId ? sessions.findIndex(s => (s.full_id || s.id) === initialSessionId || s.id === initialSessionId) : -1;
  if (selectedIdx < 0) selectedIdx = null;
  let activeConversationPlayhead = null;

  function conversationSearchText(event){
    return [
      event?.preview,
      event?.tool_name,
      event?.server,
      event?.model,
      event?.type,
      event?.subagent_type,
    ].filter(Boolean).join('\n');
  }

  function conversationMatchIndexes(conversation, query){
    const needle = String(query || '').trim().toLocaleLowerCase();
    if (!needle) return [];
    const matches = [];
    (conversation || []).forEach((event, index) => {
      if (conversationSearchText(event).toLocaleLowerCase().includes(needle)) matches.push(index);
    });
    return matches;
  }

  function highlightConversationText(value, query){
    const text = String(value || '');
    const needle = String(query || '').trim();
    if (!text || !needle) return escHtml(text);
    const lowerText = text.toLocaleLowerCase();
    const lowerNeedle = needle.toLocaleLowerCase();
    let cursor = 0;
    let output = '';
    let matches = 0;
    while (cursor < text.length && matches < 100) {
      const index = lowerText.indexOf(lowerNeedle, cursor);
      if (index < 0) break;
      output += escHtml(text.slice(cursor, index));
      output += `<mark class="conversation-mark">${escHtml(text.slice(index, index + needle.length))}</mark>`;
      cursor = index + needle.length;
      matches += 1;
    }
    return output + escHtml(text.slice(cursor));
  }

  function selectConversationEvent(session, conversationIndex, behavior = 'smooth'){
    if (!Number.isInteger(conversationIndex) || conversationIndex < 0) return;
    session._playheadEventIndex = conversationIndex;
    session._detailTab = 'conversation';
    renderSessionDetail(session);
    requestAnimationFrame(() => {
      const pointIndex = activeConversationPlayhead?.pointByConversationIndex?.get(conversationIndex);
      if (pointIndex !== undefined) {
        activeConversationPlayhead.selectPoint(pointIndex, {scroll:true, behavior});
      }
    });
  }

  function renderConversationReaderToolbar(session, conversation){
    const query = String(session._conversationQuery || '');
    const matches = conversationMatchIndexes(conversation, query);
    const mode = session._conversationMode === 'full' ? 'full' : 'focused';
    const activeCursor = matches.indexOf(session._playheadEventIndex);
    const storedCursor = Number(session._conversationMatchCursor || 0);
    const cursor = activeCursor >= 0
      ? activeCursor
      : (matches.length ? Math.max(0, Math.min(matches.length - 1, storedCursor)) : 0);
    session._conversationMatchCursor = cursor;
    const resultLabel = query
      ? (activeCursor >= 0 ? `${activeCursor + 1} / ${matches.length}` : `${matches.length} ${matches.length === 1 ? 'match' : 'matches'}`)
      : 'Find';
    const hasFailures = conversation.some(event => event.type === 'tool_result' && event.success === false);
    return `<div class="conversation-reader-toolbar" role="region" aria-label="Conversation controls">
      <div class="conversation-mode-switch" role="group" aria-label="Conversation detail">
        <button type="button" class="conversation-mode-button${mode === 'focused' ? ' is-active' : ''}" data-conversation-mode="focused" aria-pressed="${mode === 'focused'}">Readable</button>
        <button type="button" class="conversation-mode-button${mode === 'full' ? ' is-active' : ''}" data-conversation-mode="full" aria-pressed="${mode === 'full'}">Full activity</button>
      </div>
      <div class="conversation-search-wrap">
        <input class="conversation-search" type="search" value="${escHtml(query)}" placeholder="Search prompts, responses, tools…" aria-label="Search this conversation" aria-keyshortcuts="Meta+f Control+f /" autocomplete="off" spellcheck="false">
        <span class="conversation-search-count" aria-live="polite">${resultLabel}</span>
        <button type="button" class="conversation-nav-button" data-conversation-search-nav="previous" aria-label="Previous conversation match" ${matches.length ? '' : 'disabled'}>↑</button>
        <button type="button" class="conversation-nav-button" data-conversation-search-nav="next" aria-label="Next conversation match" ${matches.length ? '' : 'disabled'}>↓</button>
        ${query ? '<button type="button" class="conversation-nav-button" data-conversation-search-clear aria-label="Clear conversation search">Clear</button>' : ''}
      </div>
      ${hasFailures ? '<button type="button" class="conversation-nav-button" data-conversation-failure>Next failure</button>' : ''}
    </div>`;
  }
  const filterState = {
    q: (params.get('q') || '').trim(),
    agents: new Set((params.get('agents') || '').split(',').filter(Boolean)),
    model: params.get('model') || 'all',
    status: params.get('status') || 'all',
    range: params.get('range') || 'all',
    sort: params.get('sort') || 'recent',
  };

  document.addEventListener('keydown', event => {
    const session = selectedIdx === null ? null : sessions[selectedIdx];
    if (!session || session._detailTab !== 'conversation') return;
    const target = event.target;
    const isEditable = target instanceof HTMLInputElement
      || target instanceof HTMLTextAreaElement
      || target?.isContentEditable;
    const findShortcut = (event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 'f';
    const slashShortcut = event.key === '/' && !isEditable && !event.metaKey && !event.ctrlKey && !event.altKey;
    if (!findShortcut && !slashShortcut) return;
    event.preventDefault();
    document.querySelector('#sb-detail .conversation-search')?.focus({preventScroll:true});
  });

  // Detect which agents exist
  const agentTab = sqlTab('agents');
  const agentSet = new Set([
    ...sessions.map(s => s.agent).filter(Boolean),
    ...(agentTab.agent_comparison || []).map(a => a.name).filter(Boolean),
    ...Object.keys(agentTab.agents || {}),
  ]);
  const pillContainer = document.getElementById('sb-agent-pills');
  const listHead = document.getElementById('sb-list-head');
  const metaEl = document.getElementById('global-filter-meta');
  const launcherMetaEl = document.getElementById('session-filter-launcher-meta');
  const chipsEl = document.getElementById('filter-chips');
  const clearBtn = document.getElementById('filter-clear');
  const filterShell = document.getElementById('global-filter-shell');
  const compactToggleBtn = document.getElementById('filter-compact-toggle');
  const filterSheetOpenBtn = document.getElementById('filter-sheet-open');
  const filterSheetCloseBtn = document.getElementById('filter-sheet-close');
  const filterSheetBackdrop = document.getElementById('filter-sheet-backdrop');
  const sessionRailToggleBtn = document.getElementById('session-rail-toggle');
  const sessionRailOpenBtn = document.getElementById('session-rail-open');
  const searchInput = document.getElementById('sb-search');
  const modelSelect = document.getElementById('filter-model');
  const statusSelect = document.getElementById('filter-status');
  const rangeSelect = document.getElementById('filter-range');
  const sortSelect = document.getElementById('filter-sort');
  let reloadTimer = null;
  let filterExpanded = true;
  const agentNames = [...agentSet].sort((a,b) => a.localeCompare(b));
  const modelNames = [...new Set([
    ...sessions.map(s => s.primary_model).filter(Boolean),
    ...Object.keys(sqlTab('models').models_by_count || {}),
  ])].sort((a,b) => a.localeCompare(b));
  modelNames.forEach(model => {
    const opt = document.createElement('option');
    opt.value = model;
    opt.textContent = model;
    modelSelect.appendChild(opt);
  });
  agentNames.forEach(a => {
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'sb-pill';
    pill.dataset.agent = a;
    pill.innerHTML = `<span class="sb-agent-icon">${agentIconSvg(a)}</span><span>${escHtml(formatAgentLabel(a))}</span>`;
    pillContainer.appendChild(pill);
  });

  function sessionTimeMs(session){
    const value = Date.parse(session.created_at || '');
    return Number.isFinite(value) ? value : 0;
  }

  function persistFilters(){
    dashboardSelectedSessionId = '';
    updateUrlParams(urlParams => {
      if (filterState.q) urlParams.set('q', filterState.q);
      else urlParams.delete('q');
      if (filterState.agents.size) urlParams.set('agents', [...filterState.agents].join(','));
      else urlParams.delete('agents');
      if (filterState.model !== 'all') urlParams.set('model', filterState.model);
      else urlParams.delete('model');
      if (filterState.status !== 'all') urlParams.set('status', filterState.status);
      else urlParams.delete('status');
      if (filterState.range !== 'all') urlParams.set('range', filterState.range);
      else urlParams.delete('range');
      if (filterState.sort !== 'recent') urlParams.set('sort', filterState.sort);
      else urlParams.delete('sort');
      urlParams.delete('session');
    });
  }

  function scheduleDashboardReload(){
    if (!reportSupportsServerFiltering()) return false;
    if (reloadTimer) window.clearTimeout(reloadTimer);
    showReportLoader('Filtering sessions...');
    reloadTimer = window.setTimeout(() => window.location.reload(), 120);
    return true;
  }

  function isMobileShell(){
    return window.matchMedia('(max-width: 900px)').matches;
  }

  function syncRailState(){
    const mobile = isMobileShell();
    const filtersOpen = mobile ? document.body.classList.contains('filters-rail-open') : true;
    const sessionsOpen = document.body.classList.contains('sessions-rail-open');
    const sessionsCollapsed = document.body.classList.contains('sessions-rail-collapsed');
    const overlayOpen = mobile && (filtersOpen || sessionsOpen);
    document.body.classList.toggle('rail-overlay-open', overlayOpen);
    if (filterSheetBackdrop) filterSheetBackdrop.hidden = !overlayOpen;
    sessionRailToggleBtn?.setAttribute('aria-expanded', String(mobile ? sessionsOpen : !sessionsCollapsed));
    sessionRailOpenBtn?.setAttribute('aria-expanded', String(mobile ? sessionsOpen : !sessionsCollapsed));
    filterSheetOpenBtn?.setAttribute('aria-expanded', String(filtersOpen));
    if (!mobile) document.body.classList.remove('sessions-rail-open');
  }

  function toggleSessionRail(){
    if (isMobileShell()) {
      document.body.classList.toggle('sessions-rail-open');
    } else {
      document.body.classList.toggle('sessions-rail-collapsed');
    }
    syncRailState();
  }

  function closeFilterRail(){
    if (isMobileShell()) document.body.classList.remove('filters-rail-open');
    syncRailState();
  }

  function toggleFilterRail(){
    if (!isMobileShell()) return;
    document.body.classList.toggle('filters-rail-open');
    syncRailState();
  }

  function closeShellOverlays(){
    document.body.classList.remove('sessions-rail-open');
    document.body.classList.remove('filters-rail-open');
    syncRailState();
  }

  function syncFilterLayout(){
    filterShell.classList.toggle('expanded', filterExpanded);
    if (compactToggleBtn) {
      compactToggleBtn.setAttribute('aria-expanded', String(filterExpanded));
      compactToggleBtn.textContent = filterExpanded ? 'Fewer filters' : 'More filters';
    }
    syncRailState();
  }

  function updateAgentPills(){
    pillContainer.querySelectorAll('.sb-pill').forEach(p => {
      const isActive = p.dataset.agent === 'all'
        ? filterState.agents.size === 0
        : filterState.agents.has(p.dataset.agent);
      p.classList.toggle('active', isActive);
      p.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    });
  }

  function setSelectedSession(idx, {updateUrl = true} = {}){
    selectedIdx = idx;
    const selected = selectedIdx === null ? null : sessions[selectedIdx];
    dashboardSelectedSessionId = selected ? (selected.full_id || selected.id || '') : '';
    renderSessionList();
    renderSessionDetail(selectedIdx === null ? null : sessions[idx]);
    if (updateUrl) {
      updateUrlParams(urlParams => {
        const selected = selectedIdx === null ? null : sessions[selectedIdx];
        if (selected) urlParams.set('session', selected.full_id || selected.id);
        else urlParams.delete('session');
      });
      if (selected && reportSupportsServerFiltering()) {
        scheduleDashboardReload();
      }
    }
  }

  // Pill click
  pillContainer.addEventListener('click', e => {
    const pill = e.target.closest('.sb-pill');
    if (!pill) return;
    const agent = pill.dataset.agent;
    if (agent === 'all') filterState.agents.clear();
    else if (filterState.agents.has(agent)) filterState.agents.delete(agent);
    else filterState.agents.add(agent);
    persistFilters();
    if (scheduleDashboardReload()) return;
    renderSessionList();
  });

  // Search
  searchInput.value = filterState.q;
  searchInput.addEventListener('input', () => {
    filterState.q = searchInput.value.trim();
    persistFilters();
    if (scheduleDashboardReload()) return;
    renderSessionList();
  });
  modelSelect.value = modelNames.includes(filterState.model) ? filterState.model : 'all';
  statusSelect.value = filterState.status;
  rangeSelect.value = filterState.range;
  sortSelect.value = filterState.sort;
  modelSelect.addEventListener('change', () => { filterState.model = modelSelect.value; persistFilters(); if (scheduleDashboardReload()) return; renderSessionList(); });
  statusSelect.addEventListener('change', () => { filterState.status = statusSelect.value; persistFilters(); if (scheduleDashboardReload()) return; renderSessionList(); });
  rangeSelect.addEventListener('change', () => { filterState.range = rangeSelect.value; persistFilters(); if (scheduleDashboardReload()) return; renderSessionList(); });
  sortSelect.addEventListener('change', () => { filterState.sort = sortSelect.value; persistFilters(); if (scheduleDashboardReload()) return; renderSessionList(); });
  clearBtn.addEventListener('click', () => {
    filterState.q = '';
    filterState.agents.clear();
    filterState.model = 'all';
    filterState.status = 'all';
    filterState.range = 'all';
    filterState.sort = 'recent';
    searchInput.value = '';
    modelSelect.value = 'all';
    statusSelect.value = 'all';
    rangeSelect.value = 'all';
    sortSelect.value = 'recent';
    persistFilters();
    if (scheduleDashboardReload()) return;
    renderSessionList();
  });
  compactToggleBtn?.addEventListener('click', () => {
    filterExpanded = !filterExpanded;
    syncFilterLayout();
  });
  sessionRailToggleBtn?.addEventListener('click', () => toggleSessionRail());
  sessionRailOpenBtn?.addEventListener('click', () => toggleSessionRail());
  filterSheetOpenBtn?.addEventListener('click', () => toggleFilterRail());
  filterSheetCloseBtn?.addEventListener('click', () => closeFilterRail());
  filterSheetBackdrop?.addEventListener('click', () => closeShellOverlays());
  window.addEventListener('resize', syncRailState);
  document.addEventListener('keydown', evt => {
    if (evt.key === 'Escape') closeShellOverlays();
  });
  syncFilterLayout();

  function removeFilterChip(kind, value){
    if (kind === 'q') filterState.q = '';
    if (kind === 'agent') filterState.agents.delete(value);
    if (kind === 'model') filterState.model = 'all';
    if (kind === 'status') filterState.status = 'all';
    if (kind === 'range') filterState.range = 'all';
    if (kind === 'sort') filterState.sort = 'recent';
    searchInput.value = filterState.q;
    modelSelect.value = filterState.model;
    statusSelect.value = filterState.status;
    rangeSelect.value = filterState.range;
    sortSelect.value = filterState.sort;
    persistFilters();
    if (scheduleDashboardReload()) return;
    renderSessionList();
  }

  function renderFilterChips(){
    const chips = [];
    if (filterState.q) chips.push({kind:'q', value:'', label:`Search: ${filterState.q}`});
    [...filterState.agents].forEach(agent => chips.push({kind:'agent', value:agent, label:`Agent: ${agent}`}));
    if (filterState.model !== 'all') chips.push({kind:'model', value:'', label:`Model: ${filterState.model}`});
    if (filterState.status !== 'all') chips.push({kind:'status', value:'', label:`Status: ${statusSelect.options[statusSelect.selectedIndex]?.textContent || filterState.status}`});
    if (filterState.range !== 'all') chips.push({kind:'range', value:'', label:`Range: ${rangeSelect.options[rangeSelect.selectedIndex]?.textContent || filterState.range}`});
    if (filterState.sort !== 'recent') chips.push({kind:'sort', value:'', label:`Sort: ${sortSelect.options[sortSelect.selectedIndex]?.textContent || filterState.sort}`});
    chipsEl.innerHTML = chips.map(chip => `
      <span class="filter-chip">${escHtml(chip.label)}<button type="button" data-kind="${chip.kind}" data-value="${escHtml(chip.value)}" aria-label="Remove ${escHtml(chip.label)}">×</button></span>
    `).join('');
    chipsEl.querySelectorAll('button').forEach(btn => {
      btn.addEventListener('click', () => removeFilterChip(btn.dataset.kind, btn.dataset.value || ''));
    });
  }

  function filteredSessions(){
    const searchText = filterState.q.toLowerCase();
    const reportNow = Math.max(...sessions.map(sessionTimeMs).filter(Boolean), Date.now());
    const rangeMs = filterState.range === '24h' ? 24 * 60 * 60 * 1000
      : filterState.range === '7d' ? 7 * 24 * 60 * 60 * 1000
      : filterState.range === '30d' ? 30 * 24 * 60 * 60 * 1000
      : 0;
    const filtered = sessions.map((s,i) => ({...s, _idx:i})).filter(s => {
      if (filterState.agents.size && !filterState.agents.has(s.agent || '')) return false;
      if (filterState.model !== 'all' && (s.primary_model || '') !== filterState.model) return false;
      if (filterState.status === 'completed' && !s.is_completed) return false;
      if (filterState.status === 'active' && s.is_completed) return false;
      if (filterState.status === 'recovered' && !(Number(s.recovered_failures || 0) > 0)) return false;
      if (filterState.status === 'failing' && !(Number(s.failure_count || 0) > 0)) return false;
      if (rangeMs > 0) {
        const createdMs = sessionTimeMs(s);
        if (!createdMs || (reportNow - createdMs) > rangeMs) return false;
      }
      if (searchText) {
        const haystack = [
          s.first_prompt || '',
          s.id || '',
          s.full_id || '',
          s.primary_model || '',
          s.agent || '',
        ].join(' ').toLowerCase();
        if (!haystack.includes(searchText)) return false;
      }
      return true;
    });
    const sorters = {
      recent: (a,b) => sessionTimeMs(b) - sessionTimeMs(a),
      quality: (a,b) => Number(b.quality_score || 0) - Number(a.quality_score || 0),
      tokens: (a,b) => (Number(b.input_tokens || 0) + Number(b.output_tokens || 0)) - (Number(a.input_tokens || 0) + Number(a.output_tokens || 0)),
      cost: (a,b) => Number(b.total_cost_usd || b.total_cost || 0) - Number(a.total_cost_usd || a.total_cost || 0),
      duration: (a,b) => Number(b.duration_ms || 0) - Number(a.duration_ms || 0),
      events: (a,b) => Number(b.event_count || 0) - Number(a.event_count || 0),
    };
    filtered.sort(sorters[filterState.sort] || sorters.recent);
    return filtered;
  }

  function renderSessionList(){
    const listEl = document.getElementById('sb-list');
    const filtered = filteredSessions();
    filteredDashboardSessions = filtered.map(s => sessions[s._idx]);
    updateAgentPills();
    renderFilterChips();
    const hasFilters = Boolean(filterState.q || filterState.agents.size || filterState.model !== 'all' || filterState.status !== 'all' || filterState.range !== 'all' || filterState.sort !== 'recent');
    const totalSessions = Number(D.session_list_total || sessions.length || 0);
    metaEl.textContent = hasFilters
      ? `Showing ${fmt(filtered.length)} of ${fmt(totalSessions)} sessions`
      : `Showing ${fmt(filtered.length)} of ${fmt(totalSessions)} sessions`;
    if (launcherMetaEl) launcherMetaEl.textContent = metaEl.textContent;
    listHead.textContent = `${fmt(filtered.length)} session${filtered.length === 1 ? '' : 's'} in view`;
    document.dispatchEvent(new CustomEvent('dashboard-filters-changed', {
      detail: { sessions: filteredDashboardSessions.slice(), hasFilters, selectedSessionId: dashboardSelectedSessionId, filters: {...filterState, agents:[...filterState.agents]} }
    }));
    if (selectedIdx !== null && !filtered.some(s => s._idx === selectedIdx)) {
      selectedIdx = null;
      dashboardSelectedSessionId = '';
      updateUrlParams(urlParams => urlParams.delete('session'));
    }
    if (!filtered.length) {
      listEl.innerHTML = '<div class="sb-empty" style="height:auto;padding:24px 12px"><div class="sb-empty-icon" aria-hidden="true">&#128269;</div><div>No sessions match this filter</div></div>';
      renderSessionDetail(selectedIdx === null ? null : sessions[selectedIdx]);
      return;
    }
    if (selectedIdx === null) {
      selectedIdx = filtered[0]._idx;
    }
    listEl.innerHTML = filtered.map(s => {
      const agentColor = colorForAgent(s.agent);
      const agentName = formatAgentLabel(s.agent);
      const prompt = s.first_prompt ? escHtml(s.first_prompt.slice(0, 80)) + (s.first_prompt.length > 80 ? '…' : '') : 'No prompt data';
      const isActive = s._idx === selectedIdx;
      const estimatedTokens = getEstimatedSessionTokens(s);
      const tokens = sessionTokenTotal(s, estimatedTokens);
      const costPresentation = sessionCostPresentation(s, estimatedTokens);
      const cost = costPresentation.cost;
      const unit = String(s.pricing_unit || sqlTab('usage').pricing_unit || 'usd').toUpperCase();
      return `<button type="button" class="sb-card${isActive ? ' active' : ''}" data-idx="${s._idx}" aria-pressed="${isActive ? 'true' : 'false'}">
        <div class="sb-card-header">
          <div class="sb-card-agent-icon">${agentIconSvg(s.agent)}</div>
          <div class="sb-card-agent">${escHtml(agentName)}</div>
          <div class="sb-card-time">${escHtml(fmtWorkflowDate(s.created_at))}</div>
        </div>
        <div class="sb-card-prompt">${prompt}</div>
        <div class="sb-card-badges">
          <div class="sb-card-badge">${fmt(s.event_count || 0)} events</div>
          ${tokens > 0 ? `<div class="sb-card-badge">${estimatedTokens ? '~' : ''}${fmtTokenShort(tokens)} tok</div>` : ''}
          ${cost > 0
            ? `<div class="sb-card-badge">${fmtCost(cost)} ${escHtml(unit)}</div>`
            : `<div class="sb-card-badge" style="color:var(--text-3)" title="${escHtml(costPresentation.reason)}">${escHtml(costPresentation.label)}</div>`}
          ${s.quality_available ? `<div class="sb-card-badge" style="color:${Number(s.quality_score || 0) > 70 ? 'var(--green)' : 'var(--yellow)'}">${Number(s.quality_score || 0).toFixed(0)}%</div>` : '<div class="sb-card-badge" style="color:var(--text-3)">No score</div>'}
        </div>
      </button>`;
    }).join('');

    // Click handler
    listEl.querySelectorAll('.sb-card').forEach(card => {
      card.addEventListener('click', () => {
        const idx = parseInt(card.dataset.idx);
        setSelectedSession(idx);
        if (isMobileShell()) closeShellOverlays();
      });
    });
    renderSessionDetail(sessions[selectedIdx]);
  }

  function renderSessionDetail(session){
    const detailEl = document.getElementById('sb-detail');
    activeConversationPlayhead?.destroy();
    activeConversationPlayhead = null;
    if (!session) {
      detailEl.innerHTML = `<div class="sb-empty"><div class="sb-empty-icon" aria-hidden="true">&#9776;</div><div>Select a session to view its conversation timeline</div></div>`;
      return;
    }

    const agentColor = colorForAgent(session.agent);
    const agentName = formatAgentLabel(session.agent);
    const estimatedTokens = getEstimatedSessionTokens(session);
    const inTok = estimatedTokens?.input ?? session.input_tokens ?? 0;
    const outTok = estimatedTokens?.output ?? session.output_tokens ?? 0;
    const cacheTok = session.cache_read_tokens || 0;
    const detailCostPresentation = sessionCostPresentation(session, estimatedTokens);
    const sessionCost = detailCostPresentation.cost;
    const sessionUnit = String(session.pricing_unit || sqlTab('usage').pricing_unit || 'usd').toUpperCase();
    const tokenMeta = estimatedTokens
      ? (session._fullLoaded ? 'Estimated from full Cursor transcript content.' : 'Estimated from available Cursor transcript preview.')
      : (session.token_note || '');
    const tokenNote = estimatedTokens
      ? (session._fullLoaded
        ? 'Token counts are estimated from Cursor transcript text because exact per-session usage is not present in local telemetry.'
        : 'Token counts are estimated from the available Cursor transcript preview because exact per-session usage is not present in local telemetry. Load the full transcript to refine this estimate.')
      : (session.token_note || '');
    const linkedEvidenceId = currentParams().get('evidence') || '';
    if (linkedEvidenceId && !session._linkedEvidenceInitialized) {
      session._linkedEvidenceInitialized = true;
      session._detailTab = 'evidence';
      session._telemetryFocusId = linkedEvidenceId;
    }
    const activeDetailTab = session._detailTab || 'summary';

    let headerHtml = `<div class="sb-detail-header">
      <div class="sb-detail-header-row">
        <div class="sb-detail-sid" title="${escHtml(session.full_id || session.id)}">${escHtml(session.full_id || session.id)}</div>
        <div class="sb-detail-agent-badge" style="background:${agentColor}">${escHtml(agentName)}</div>
        ${session.primary_model ? `<div class="sb-detail-model">${escHtml(session.primary_model)}</div>` : ''}
      </div>
      <div class="sb-detail-stats">
        <span>Duration: <strong>${fmtDur(session.duration_ms)}</strong></span>
        <span>Events: <strong>${fmt(session.event_count || 0)}</strong></span>
        <span title="${tokenMeta}">In: <strong>${estimatedTokens ? '~' : ''}${fmtTokenShort(inTok)}</strong></span>
        <span title="${tokenMeta}">Out: <strong>${estimatedTokens ? '~' : ''}${fmtTokenShort(outTok)}</strong></span>
        ${cacheTok > 0 ? `<span>Cache: <strong>${fmtTokenShort(cacheTok)}</strong></span>` : ''}
        ${sessionCost > 0
          ? `<span title="Cost basis: ${escHtml(session.pricing_source || sqlTab('usage').pricing_source || 'unknown')}">Cost: <strong>${fmtCost(sessionCost)} ${escHtml(sessionUnit)}</strong></span>`
          : `<span title="${escHtml(detailCostPresentation.reason)}">Cost: <strong>${escHtml(detailCostPresentation.label)}</strong></span>`}
        ${session.failure_count > 0 ? `<span style="color:var(--red)">Failures: <strong>${session.failure_count}</strong></span>` : ''}
      </div>
      <div class="feedback-row" aria-label="Record session outcome">
        <span class="feedback-label">Outcome</span>
        <button type="button" class="ledger-button" data-session-feedback="good">Good</button>
        <button type="button" class="ledger-button" data-session-feedback="bad">Bad</button>
        <button type="button" class="ledger-button" data-session-feedback="no-change-correct">No change was correct</button>
        <button type="button" class="ledger-button" data-session-feedback="corrected">Corrected</button>
        <span class="inbox-copy" data-feedback-status></span>
      </div>
      ${tokenNote ? `<div class="sb-detail-model" style="margin-top:8px">${escHtml(tokenNote)}</div>` : ''}
    </div>`;

    const conversation = session.conversation || [];
    const sessionStartTs = conversation.length > 0 ? conversation[0].ts : 0;

    const loadBtnHtml = ''; // sessions auto-load on selection — see auto-load below

    const isActivityType = t => ['tool_call','tool_result','mcp_call','mcp_result'].includes(t);
    const timelineHtml = renderConversationThread(session, conversation, sessionStartTs, isActivityType);

    const detailTabsHtml = `<div class="sb-detail-tabs">
      <button type="button" class="sb-detail-tab${activeDetailTab === 'summary' ? ' active' : ''}" data-tab="summary">Summary</button>
      <button type="button" class="sb-detail-tab${activeDetailTab === 'conversation' ? ' active' : ''}" data-tab="conversation">Conversation</button>
      <button type="button" class="sb-detail-tab${activeDetailTab === 'execution' ? ' active' : ''}" data-tab="execution">Execution</button>
      <button type="button" class="sb-detail-tab${activeDetailTab === 'changes' ? ' active' : ''}" data-tab="changes">Changes</button>
      <button type="button" class="sb-detail-tab${activeDetailTab === 'evidence' ? ' active' : ''}" data-tab="evidence">Evidence</button>
    </div>`;
    const sessionTimelineHtml = renderSessionTimelinePanel(session, conversation);
    const bodyHtml = activeDetailTab === 'evidence'
      ? renderTelemetryCockpit(session)
      : activeDetailTab === 'summary'
        ? renderQualityPanel(session)
        : activeDetailTab === 'execution'
          ? renderSessionToolsPanel(session)
          : activeDetailTab === 'changes'
            ? renderSessionChangesPanel(session)
            : renderConversationCasefile(session, conversation, loadBtnHtml, timelineHtml);

    detailEl.innerHTML = headerHtml
      + sessionTimelineHtml
      + detailTabsHtml
      + `<div class="sb-detail-body">${bodyHtml}</div>`;

    detailEl.querySelectorAll('.sb-detail-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        session._detailTab = tab.dataset.tab || 'summary';
        renderSessionDetail(session);
        if ((session._detailTab === 'evidence' || session._detailTab === 'execution' || session._detailTab === 'changes') && session.full_id && !session._fullLoaded && !session._loadingDetail) {
          tryLoadFullDetail(session.full_id || session.id, null);
        }
      });
    });

    detailEl.querySelectorAll('[data-session-feedback]').forEach(button => {
      button.addEventListener('click', () => submitSessionFeedback(
        session.full_id || session.id,
        button.dataset.sessionFeedback || '',
        button,
      ));
    });

    detailEl.querySelectorAll('[data-conversation-mode]').forEach(button => {
      button.addEventListener('click', () => {
        session._conversationMode = button.dataset.conversationMode === 'full' ? 'full' : 'focused';
        renderSessionDetail(session);
      });
    });

    const conversationSearch = detailEl.querySelector('.conversation-search');
    if (conversationSearch) {
      conversationSearch.addEventListener('input', () => {
        session._conversationQuery = conversationSearch.value;
        session._conversationMatchCursor = 0;
        const cursor = conversationSearch.selectionStart ?? conversationSearch.value.length;
        renderSessionDetail(session);
        requestAnimationFrame(() => {
          const nextInput = detailEl.querySelector('.conversation-search');
          nextInput?.focus({preventScroll:true});
          nextInput?.setSelectionRange(cursor, cursor);
        });
      });
      conversationSearch.addEventListener('keydown', event => {
        if (event.key === 'Escape' && session._conversationQuery) {
          event.preventDefault();
          session._conversationQuery = '';
          session._conversationMatchCursor = 0;
          renderSessionDetail(session);
          return;
        }
        if (event.key !== 'Enter') return;
        event.preventDefault();
        const matches = conversationMatchIndexes(conversation, session._conversationQuery);
        if (!matches.length) return;
        const direction = event.shiftKey ? -1 : 1;
        const active = matches.indexOf(session._playheadEventIndex);
        const current = active >= 0 ? active : (direction > 0 ? -1 : 0);
        session._conversationMatchCursor = (current + direction + matches.length) % matches.length;
        selectConversationEvent(session, matches[session._conversationMatchCursor]);
      });
    }

    detailEl.querySelectorAll('[data-conversation-search-nav]').forEach(button => {
      button.addEventListener('click', () => {
        const matches = conversationMatchIndexes(conversation, session._conversationQuery);
        if (!matches.length) return;
        const direction = button.dataset.conversationSearchNav === 'previous' ? -1 : 1;
        const active = matches.indexOf(session._playheadEventIndex);
        const current = active >= 0 ? active : (direction > 0 ? -1 : 0);
        session._conversationMatchCursor = (current + direction + matches.length) % matches.length;
        selectConversationEvent(session, matches[session._conversationMatchCursor]);
      });
    });

    detailEl.querySelector('[data-conversation-search-clear]')?.addEventListener('click', () => {
      session._conversationQuery = '';
      session._conversationMatchCursor = 0;
      renderSessionDetail(session);
      requestAnimationFrame(() => detailEl.querySelector('.conversation-search')?.focus({preventScroll:true}));
    });

    detailEl.querySelector('[data-conversation-failure]')?.addEventListener('click', () => {
      const failures = conversation
        .map((event, index) => ({event, index}))
        .filter(item => item.event.type === 'tool_result' && item.event.success === false)
        .map(item => item.index);
      if (!failures.length) return;
      const current = Number.isInteger(session._playheadEventIndex) ? session._playheadEventIndex : -1;
      selectConversationEvent(session, failures.find(index => index > current) ?? failures[0]);
    });

    detailEl.querySelectorAll('[data-copy-conversation]').forEach(button => {
      button.addEventListener('click', async event => {
        event.stopPropagation();
        const index = Number(button.dataset.copyConversation);
        const text = String(conversation[index]?.preview || '');
        if (!text) return;
        try {
          await navigator.clipboard.writeText(text);
          button.textContent = 'Copied';
          window.setTimeout(() => { button.textContent = 'Copy'; }, 1200);
        } catch (_) {
          button.textContent = 'Unavailable';
        }
      });
    });

    // Expand/collapse previews
    detailEl.querySelectorAll('.ev-preview.expandable').forEach(el => {
      el.addEventListener('click', () => {
        if (!(session._expandedConversationEvents instanceof Set)) {
          session._expandedConversationEvents = new Set();
        }
        const index = Number(el.dataset.eidx);
        const expanded = !session._expandedConversationEvents.has(index);
        if (expanded) session._expandedConversationEvents.add(index);
        else session._expandedConversationEvents.delete(index);
        const fullText = String(conversation[index]?.preview || '');
        el.innerHTML = highlightConversationText(
          fullText.slice(0, expanded ? 20000 : 280),
          session._conversationQuery,
        );
        el.classList.toggle('expanded', expanded);
        el.setAttribute('aria-expanded', String(expanded));
        const hint = el.nextElementSibling;
        if (hint?.classList.contains('ev-preview-hint')) {
          hint.textContent = expanded ? 'Tap to collapse' : 'Tap to expand';
        }
      });
    });

    const toggleSpanKey = (spanKey) => {
      if (!(session._collapsedSpanIds instanceof Set)) session._collapsedSpanIds = new Set();
      if (!spanKey) return;
      if (session._collapsedSpanIds.has(spanKey)) session._collapsedSpanIds.delete(spanKey);
      else session._collapsedSpanIds.add(spanKey);
      renderSessionDetail(session);
    };

    detailEl.querySelectorAll('.telemetry-span-toggle').forEach(el => {
      el.addEventListener('click', evt => {
        evt.stopPropagation();
        toggleSpanKey(el.dataset.spanToggle || '');
      });
    });

    detailEl.querySelectorAll('.telemetry-span-stack[data-span-toggle]').forEach(el => {
      el.addEventListener('click', evt => {
        if (evt.target.closest('.telemetry-span-toggle')) return;
        toggleSpanKey(el.dataset.spanToggle || '');
      });
    });

    detailEl.querySelectorAll('.telemetry-trace-action').forEach(el => {
      el.addEventListener('click', () => {
        if (!(session._collapsedSpanIds instanceof Set)) session._collapsedSpanIds = new Set();
        const action = el.dataset.telemetryAction || '';
        const spans = (session.telemetry?.spans || []).slice(0, 120);
        const treeRows = buildTelemetryTreeRows(spans, session, null, 0);
        const keys = treeRows.filter(row => row.hasChildren).map(row => row.key);
        if (action === 'collapse-all') session._collapsedSpanIds = new Set(keys);
        if (action === 'expand-all') session._collapsedSpanIds = new Set();
        renderSessionDetail(session);
      });
    });

    const timelinePanel = detailEl.querySelector('.session-timeline-panel');
    if (timelinePanel?.querySelector('.session-timeline-playhead')) {
      activeConversationPlayhead = new SessionConversationPlayhead(timelinePanel, {
        scroller: detailEl.querySelector('.sb-detail-body'),
        initialConversationIndex: Number.isInteger(session._playheadEventIndex)
          ? session._playheadEventIndex
          : null,
        onSelectionChange: conversationIndex => {
          session._playheadEventIndex = conversationIndex;
        },
        onRequireConversation: conversationIndex => {
          session._playheadEventIndex = conversationIndex;
          if (session._detailTab === 'conversation') return;
          session._detailTab = 'conversation';
          renderSessionDetail(session);
          requestAnimationFrame(() => activeConversationPlayhead?.playhead?.focus({preventScroll: true}));
        },
      });
      activeConversationPlayhead.mount();
    }

    // Auto-load full detail silently on selection — no button required
    if (session.full_id && !session._fullLoaded && !session._loadingDetail) {
      tryLoadFullDetail(session.full_id || session.id, null);
    }
  }

  function getEventNodeColor(ev){
    if (ev.type === 'prompt') return EVENT_COLORS.prompt;
    if (ev.type === 'response') return EVENT_COLORS.response;
    if (ev.type === 'tool_call') return EVENT_COLORS.tool_call;
    if (ev.type === 'tool_result') return ev.success ? EVENT_COLORS.tool_result_ok : EVENT_COLORS.tool_result_fail;
    if (ev.type === 'subagent_start' || ev.type === 'subagent_stop') return EVENT_COLORS.subagent_start;
    if (ev.type === 'mcp_call' || ev.type === 'mcp_result') return EVENT_COLORS.mcp_call;
    return EVENT_COLORS.session_start;
  }

  function getEventMeta(ev){
    if (ev.type === 'response') {
      const parts = [];
      if (ev.model) parts.push(ev.model);
      if (ev.input_tokens) parts.push(fmtTokenShort(ev.input_tokens) + ' in');
      if (ev.output_tokens) parts.push(fmtTokenShort(ev.output_tokens) + ' out');
      return parts.join(' · ');
    }
    if (ev.type === 'mcp_call' || ev.type === 'mcp_result') {
      return ev.server || '';
    }
    return '';
  }

  function renderConversationMessage(session, ev, index, sessionStartTs, turnIndex){
    const relTime = fmtRelTime(ev.ts - sessionStartTs);
    const nodeColor = getEventNodeColor(ev);
    const eventKind = `${ev.type || 'event'}${ev.type === 'tool_result' && ev.success === false ? ' fail' : ''}`;
    if (!(session._expandedConversationEvents instanceof Set)) {
      session._expandedConversationEvents = new Set();
    }
    const isExpanded = session._expandedConversationEvents.has(index);
    const previewLimit = isExpanded ? 20000 : 280;
    const rawPreview = ev.preview ? ev.preview.slice(0, previewLimit) : '';
    const query = String(session._conversationQuery || '').trim();
    const preview = rawPreview ? highlightConversationText(rawPreview, query) : '';
    const isLong = ev.preview && ev.preview.length > 280;
    const rail = `<div class="ev-rail"><span class="ev-ts">${relTime}</span><span class="ev-dot" aria-hidden="true"></span></div>`;
    const isUser = ev.type === 'prompt';
    const who = isUser ? 'User' : (ev.model ? ev.model.replace(/^claude-/,'').replace(/-\d{8}$/,'') : 'Agent');
    const meta = getEventMeta(ev);
    const matches = query && conversationSearchText(ev).toLocaleLowerCase().includes(query.toLocaleLowerCase());
    const copyButton = rawPreview
      ? `<button type="button" class="chat-copy-button" data-copy-conversation="${index}" aria-label="Copy ${isUser ? 'prompt' : 'response'}">Copy</button>`
      : '';
    return `<div class="chat-msg ${isUser ? 'is-user' : 'is-agent'}${matches ? ' is-search-match' : ''}" data-kind="${escHtml(eventKind)}" data-conversation-event-index="${index}" style="--node-color:${nodeColor}">${rail}<div class="chat-bubble"><div class="chat-header"><span class="chat-who">${escHtml(who)}</span><span class="chat-turn-label">Turn ${turnIndex + 1}</span>${meta ? `<span class="chat-header-meta">${escHtml(meta)}</span>` : '<span class="chat-header-meta"></span>'}${copyButton}</div>${preview ? `<div class="chat-content"><button type="button" class="ev-preview${session._fullLoaded ? ' full-content' : ''}${isLong ? ' expandable' : ''}${isExpanded ? ' expanded' : ''}" data-eidx="${index}" aria-expanded="${isExpanded}">${preview}</button>${isLong ? `<div class="ev-preview-hint">${isExpanded ? 'Tap to collapse' : 'Tap to expand'}</div>` : ''}</div>` : `<div class="chat-content" style="color:var(--text-3);font-size:13px;font-style:italic">No content captured</div>`}</div></div>`;
  }

  function renderConversationActivity(session, ev, sessionStartTs){
    const relTime = fmtRelTime(ev.ts - sessionStartTs);
    const nodeColor = getEventNodeColor(ev);
    const eventKind = `${ev.type || 'event'}${ev.type === 'tool_result' && ev.success === false ? ' fail' : ''}`;
    const query = String(session._conversationQuery || '').trim();
    const matches = query && conversationSearchText(ev).toLocaleLowerCase().includes(query.toLocaleLowerCase());
    let icon = '→', name = '', meta2 = '', statusHtml = '';
    const dur = ev.duration_ms ? `<span class="chip-dur">${fmtDur(ev.duration_ms)}</span>` : '';
    if (ev.type === 'tool_call') {
      icon = '▶'; name = ev.tool_name || 'Tool';
      meta2 = ev.preview ? ev.preview.slice(0,72) : 'input not captured';
    } else if (ev.type === 'tool_result') {
      icon = ev.success !== false ? '✓' : '✗'; name = ev.tool_name || 'Result';
      meta2 = ev.preview ? ev.preview.slice(0,72) : (ev.success === false ? 'failure details not captured' : '');
      statusHtml = ev.success !== false ? '<span class="chip-ok">ok</span>' : '<span class="chip-fail">failed</span>';
    } else if (ev.type === 'mcp_call') {
      icon = '⬡'; name = ev.tool_name || 'MCP';
      meta2 = ev.server || '';
    } else if (ev.type === 'mcp_result') {
      icon = '⬡'; name = (ev.tool_name || 'MCP') + ' ↩';
      meta2 = ev.server || '';
    }
    return `<div class="chat-activity${matches ? ' is-search-match' : ''}" data-kind="${escHtml(eventKind)}" data-conversation-event-index="${ev._conversationIndex}" style="--node-color:${nodeColor}"><div class="ev-rail"><span class="ev-ts">${relTime}</span><span class="ev-dot" aria-hidden="true"></span></div><div class="chat-activity-chip"><em class="chip-icon">${icon}</em><span class="chip-name">${highlightConversationText(name, query)}</span>${meta2 ? `<span class="chip-meta">${highlightConversationText(meta2, query)}</span>` : ''}${dur}${statusHtml}</div></div>`;
  }

  function renderConversationDivider(ev, sessionStartTs){
    const relTime = fmtRelTime(ev.ts - sessionStartTs);
    const nodeColor = getEventNodeColor(ev);
    const eventKind = ev.type || 'event';
    const label = ev.type === 'session_start' ? 'session start'
      : ev.type === 'session_end' ? 'session end'
      : ev.type === 'subagent_start' ? `subagent · ${ev.subagent_type || 'start'}`
      : ev.type === 'subagent_stop' ? 'subagent done'
      : (ev.type || 'event');
    return `<div class="chat-divider" data-kind="${escHtml(eventKind)}" data-conversation-event-index="${ev._conversationIndex}" style="--node-color:${nodeColor}"><div class="ev-rail"><span class="ev-ts" style="font-size:10px">${relTime}</span></div><div class="chat-divider-inner"><div class="chat-divider-line"></div><span class="chat-divider-label">${escHtml(label)}</span><div class="chat-divider-line"></div></div></div>`;
  }

  function renderActivityGroup(session, events, sessionStartTs, turnIndex, hiddenResponses = 0){
    if (!events.length && !hiddenResponses) return '';
    if (!events.length) {
      return `<div class="chat-activity-summary-note" style="margin-left:62px">${fmt(hiddenResponses)} intermediate ${hiddenResponses === 1 ? 'response' : 'responses'} hidden · use Full activity to inspect</div>`;
    }
    const failures = events.filter(ev => ev.type === 'tool_result' && ev.success === false).length;
    const tools = events.filter(ev => ev.type === 'tool_call' || ev.type === 'mcp_call').length;
    const toolNames = [...new Set(events
      .filter(ev => ev.type === 'tool_call' || ev.type === 'mcp_call')
      .map(ev => ev.type === 'mcp_call'
        ? `${ev.server ? `${ev.server}/` : ''}${ev.tool_name || 'MCP'}`
        : (ev.tool_name || 'Tool')))
    ];
    const failureSummaries = events
      .filter(ev => ev.type === 'tool_result' && ev.success === false)
      .map(ev => {
        const name = ev.tool_name || 'Tool';
        const detail = String(ev.preview || '').trim();
        return detail ? `${name}: ${detail.slice(0, 96)}` : `${name} failed`;
      });
    const toolSummary = toolNames.length
      ? toolNames.slice(0, 3).join(', ') + (toolNames.length > 3 ? ` +${toolNames.length - 3} more` : '')
      : '';
    const failureSummary = failureSummaries.length
      ? `<span class="chip-fail">${escHtml(failureSummaries[0])}${failureSummaries.length > 1 ? ` +${failureSummaries.length - 1} more` : ''}</span>`
      : '';
    const failedLabel = failures ? `<span class="chip-fail">${fmt(failures)} failed</span>` : '';
    const openAttr = failures || session._conversationMode === 'full' || session._conversationQuery ? ' open' : '';
    return `<details class="chat-activity-group"${openAttr}>
      <summary class="chat-activity-summary">
        <span>Turn activity</span>
        <span class="chip-meta">${fmt(tools)} calls · ${fmt(events.length)} events${toolSummary ? ` · ${escHtml(toolSummary)}` : ''}</span>
        ${hiddenResponses ? `<span class="chat-activity-summary-note">${fmt(hiddenResponses)} intermediate ${hiddenResponses === 1 ? 'response' : 'responses'} hidden</span>` : ''}
        ${failedLabel}
        ${failureSummary}
      </summary>
      <div class="chat-activity-list">
        ${events.map(ev => renderConversationActivity(session, ev, sessionStartTs)).join('')}
      </div>
    </details>`;
  }

  function renderConversationThread(session, conversation, sessionStartTs, isActivityType){
    if (!conversation.length) {
      if (session.full_id && !session._fullLoaded) {
        return `<div class="chat-loading"><div class="chat-loading-dot"></div><div class="chat-loading-dot"></div><div class="chat-loading-dot"></div><span>Loading conversation…</span></div>`;
      }
      return '<div style="color:var(--text-3);text-align:center;padding:40px">No conversation data for this session</div>';
    }
    const turns = [];
    let current = {events: [], activity: [], index: 0};
    conversation.forEach((ev, index) => {
      if (ev.type === 'prompt' && current.events.length) {
        turns.push(current);
        current = {events: [], activity: [], index: turns.length};
      }
      const event = {...ev, _conversationIndex: index};
      if (isActivityType(ev.type)) current.activity.push(event);
      else current.events.push(event);
    });
    if (current.events.length || current.activity.length) turns.push(current);
    const showFull = session._conversationMode === 'full' || Boolean(String(session._conversationQuery || '').trim());
    return `<div class="chat-thread">${turns.map((turn, turnIndex) => {
      const lastResponseIndex = turn.events.findLastIndex(ev => ev.type === 'response');
      const visibleEvents = showFull
        ? turn.events
        : turn.events.filter((ev, index) => ev.type !== 'response' || index === lastResponseIndex);
      const hiddenResponses = turn.events.filter(ev => ev.type === 'response').length
        - visibleEvents.filter(ev => ev.type === 'response').length;
      return `
      <section class="chat-turn" data-turn-index="${turnIndex}">
        ${visibleEvents.map(ev => {
          if (ev.type === 'prompt' || ev.type === 'response') return renderConversationMessage(session, ev, ev._conversationIndex, sessionStartTs, turnIndex);
          return renderConversationDivider(ev, sessionStartTs);
        }).join('')}
        ${renderActivityGroup(session, turn.activity, sessionStartTs, turnIndex, hiddenResponses)}
      </section>
    `;}).join('')}</div>`;
  }

  function extractSkillFromPreview(preview){
    const text = String(preview || '').trim();
    if (!text) return '';
    try {
      const payload = JSON.parse(text);
      if (payload && typeof payload.skill === 'string') return payload.skill.trim();
    } catch (_) {
      const match = text.match(/"skill"\s*:\s*"([^"]+)"/);
      if (match) return match[1].trim();
    }
    return '';
  }

  function extractSubagentFromPreview(toolName, preview){
    const normalizedTool = String(toolName || '').trim().toLowerCase();
    if (!['subagent','agent','task','read_agent'].includes(normalizedTool)) return '';
    let payload = {};
    try { payload = JSON.parse(String(preview || '').trim() || '{}'); }
    catch (_) { payload = {}; }
    const keys = normalizedTool === 'task' || normalizedTool === 'read_agent'
      ? ['agent_id','name','agent_type']
      : ['subagent_type','agent_type','name','agent_id','description'];
    for (const key of keys) {
      const value = String(payload[key] || '').trim();
      if (value && !value.includes('REDACTED') && !value.startsWith('[')) return value.slice(0,80);
    }
    return '';
  }

  function deriveToolInventory(session){
    const provided = session.tool_inventory || {};
    const hasProvided = Boolean((provided.tools || []).length || (provided.skills || []).length || (provided.mcp_tools || []).length || (provided.subagents || []).length);
    const toolMap = new Map();
    const skillMap = new Map();
    const mcpMap = new Map();
    const subagentMap = new Map();
    const addTool = (name, count, failures, duration, examples) => {
      const key = name || 'unknown';
      const row = toolMap.get(key) || {name:key,count:0,failures:0,avg_duration_ms:0,examples:[]};
      row.count += Number(count || 0);
      row.failures += Number(failures || 0);
      if (duration) row.avg_duration_ms = Number(duration || 0);
      (examples || []).forEach(ex => {
        if (ex && row.examples.length < 3) row.examples.push(String(ex));
      });
      toolMap.set(key, row);
    };
    (provided.tools || []).forEach(row => addTool(row.name, row.count, row.failures, row.avg_duration_ms, row.examples));
    (provided.skills || []).forEach(row => {
      const name = row.name || '';
      if (!name) return;
      const current = skillMap.get(name) || {name, count:0, source:row.source || ''};
      current.count += Number(row.count || 0);
      skillMap.set(name, current);
    });
    (provided.mcp_tools || []).forEach(row => {
      if (!row.name) return;
      mcpMap.set(row.name, {name: row.name, count: Number(row.count || 0)});
    });
    (provided.subagents || []).forEach(row => {
      if (!row.name) return;
      subagentMap.set(row.name, {
        name: row.name,
        count: Number(row.count || 0),
        stops: Number(row.stops || 0),
        source: row.source || '',
      });
    });
    if (!hasProvided) {
      (session.conversation || []).forEach(ev => {
        if (ev.type === 'tool_call') {
          addTool(ev.tool_name || 'unknown', 1, 0, ev.duration_ms || 0, [ev.preview || '']);
          if (ev.tool_name === 'skill') {
            const skillName = extractSkillFromPreview(ev.preview);
            if (skillName) {
              const current = skillMap.get(skillName) || {name:skillName,count:0,source:'tool'};
              current.count += 1;
              skillMap.set(skillName, current);
            }
          }
          const subagentName = extractSubagentFromPreview(ev.tool_name, ev.preview);
          if (subagentName) {
            const current = subagentMap.get(subagentName) || {name:subagentName,count:0,stops:0,source:'tool'};
            current.count += 1;
            subagentMap.set(subagentName, current);
          }
        } else if (ev.type === 'tool_result' && ev.success === false && ev.tool_name) {
          addTool(ev.tool_name, 0, 1, ev.duration_ms || 0, []);
        } else if (ev.type === 'mcp_call') {
          const label = ev.server && ev.tool_name ? `${ev.server}/${ev.tool_name}` : (ev.tool_name || ev.server || '');
          if (label) {
            const current = mcpMap.get(label) || {name:label,count:0};
            current.count += 1;
            mcpMap.set(label, current);
          }
        } else if (ev.type === 'subagent_start' || ev.type === 'subagent_stop') {
          const name = ev.subagent_type || 'unknown';
          const current = subagentMap.get(name) || {name,count:0,stops:0,source:'lifecycle'};
          if (ev.type === 'subagent_stop') current.stops += 1;
          else current.count += 1;
          subagentMap.set(name, current);
        }
      });
      Object.entries(session.tools || {}).forEach(([name,count]) => addTool(name, count, 0, 0, []));
    }
    Object.entries(session.skills || {}).forEach(([name,count]) => {
      if (hasProvided && skillMap.has(name)) return;
      const current = skillMap.get(name) || {name,count:0,source:'summary'};
      current.count += Number(count || 0);
      skillMap.set(name, current);
    });
    return {
      tools: Array.from(toolMap.values()).sort((a,b) => b.count - a.count || a.name.localeCompare(b.name)),
      skills: Array.from(skillMap.values()).sort((a,b) => b.count - a.count || a.name.localeCompare(b.name)),
      mcp_tools: Array.from(mcpMap.values()).sort((a,b) => b.count - a.count || a.name.localeCompare(b.name)),
      subagents: Array.from(subagentMap.values()).sort((a,b) => b.count - a.count || a.name.localeCompare(b.name)),
    };
  }

  function renderSessionToolsPanel(session){
    const inventory = deriveToolInventory(session);
    const toolTotal = inventory.tools.reduce((sum,row) => sum + Number(row.count || 0), 0);
    const skillTotal = inventory.skills.reduce((sum,row) => sum + Number(row.count || 0), 0);
    const mcpTotal = inventory.mcp_tools.reduce((sum,row) => sum + Number(row.count || 0), 0);
    const subagentTotal = inventory.subagents.reduce((sum,row) => sum + Number(row.count || 0), 0);
    const renderRows = rows => rows.length ? rows.map(row => `
      <tr>
        <td>
          <div class="tool-inventory-name">${escHtml(row.name || 'unknown')}</div>
          ${(row.examples || []).slice(0,1).map(ex => `<div class="tool-inventory-example">${escHtml(ex)}</div>`).join('')}
        </td>
        <td style="text-align:right">${fmt(row.count || 0)}</td>
        <td style="text-align:right">${fmt(row.failures || 0)}</td>
        <td style="text-align:right">${row.avg_duration_ms ? fmtDur(row.avg_duration_ms) : '--'}</td>
      </tr>
    `).join('') : '<tr><td colspan="4" style="color:var(--text-3);text-align:center">No entries captured for this session.</td></tr>';
    const renderSimpleRows = rows => rows.length ? rows.map(row => `
      <tr><td><div class="tool-inventory-name">${escHtml(row.name || 'unknown')}</div>${row.source ? `<div class="tool-inventory-example">${escHtml(row.source)}</div>` : ''}</td><td style="text-align:right">${fmt(row.count || 0)}</td></tr>
    `).join('') : '<tr><td colspan="2" style="color:var(--text-3);text-align:center">No entries captured for this session.</td></tr>';
    return `<div class="tool-inventory-shell">
      <div class="tool-inventory-summary">
        <div class="tool-inventory-card"><div class="tool-inventory-num">${fmt(toolTotal)}</div><div class="tool-inventory-label">Tool calls</div></div>
        <div class="tool-inventory-card"><div class="tool-inventory-num">${fmt(inventory.tools.length)}</div><div class="tool-inventory-label">Distinct tools</div></div>
        <div class="tool-inventory-card"><div class="tool-inventory-num">${fmt(skillTotal)}</div><div class="tool-inventory-label">Skill uses</div></div>
        <div class="tool-inventory-card"><div class="tool-inventory-num">${fmt(subagentTotal)}</div><div class="tool-inventory-label">Subagents</div></div>
        <div class="tool-inventory-card"><div class="tool-inventory-num">${fmt(mcpTotal)}</div><div class="tool-inventory-label">MCP calls</div></div>
      </div>
      <section class="tool-inventory-section">
        <h4>Tools</h4>
        <table class="tool-inventory-table"><thead><tr><th>Tool</th><th style="text-align:right">Calls</th><th style="text-align:right">Failed</th><th style="text-align:right">Avg</th></tr></thead><tbody>${renderRows(inventory.tools)}</tbody></table>
      </section>
      <section class="tool-inventory-section">
        <h4>Skills</h4>
        <table class="tool-inventory-table"><thead><tr><th>Skill</th><th style="text-align:right">Uses</th></tr></thead><tbody>${renderSimpleRows(inventory.skills)}</tbody></table>
      </section>
      <section class="tool-inventory-section">
        <h4>Subagents</h4>
        <table class="tool-inventory-table"><thead><tr><th>Subagent</th><th style="text-align:right">Starts</th></tr></thead><tbody>${renderSimpleRows(inventory.subagents)}</tbody></table>
      </section>
      <section class="tool-inventory-section">
        <h4>MCP tools</h4>
        <table class="tool-inventory-table"><thead><tr><th>Server / tool</th><th style="text-align:right">Calls</th></tr></thead><tbody>${renderSimpleRows(inventory.mcp_tools)}</tbody></table>
      </section>
    </div>`;
  }

  function renderSessionChangesPanel(session){
    const events = [...(session.conversation || []), ...(session.spans || [])];
    const changes = events.filter(event => {
      const name = String(event.tool_name || event.name || event.type || '').toLowerCase();
      const attrs = event.attrs || {};
      const path = attrs['gen_ai.client.file_path'] || attrs['gen_ai.client.tool.input.file_path'] || event.file_path || '';
      return name.includes('write') || name.includes('edit') || name.includes('patch') || Boolean(path);
    }).map(event => {
      const attrs = event.attrs || {};
      return {
        action: event.tool_name || event.name || event.type || 'file event',
        path: attrs['gen_ai.client.file_path'] || attrs['gen_ai.client.tool.input.file_path'] || event.file_path || 'path unavailable',
        status: event.status || (event.success === false ? 'failed' : 'observed'),
      };
    });
    const unique = Array.from(new Map(changes.map(item => [`${item.action}:${item.path}:${item.status}`, item])).values());
    if (!unique.length) {
      return `<div class="tool-inventory-shell"><section class="tool-inventory-section"><h4>Changes</h4><div style="color:var(--text-3)">No file-change evidence was captured for this session. This does not prove that no change occurred.</div></section></div>`;
    }
    return `<div class="tool-inventory-shell"><section class="tool-inventory-section"><h4>Observed changes</h4><table class="tool-inventory-table"><thead><tr><th>Action</th><th>Path</th><th>Status</th></tr></thead><tbody>${unique.slice(0,100).map(item => `<tr><td>${escHtml(String(item.action))}</td><td><code>${escHtml(String(item.path))}</code></td><td>${escHtml(String(item.status))}</td></tr>`).join('')}</tbody></table></section></div>`;
  }

  function renderConversationCasefile(session, conversation, loadBtnHtml, timelineHtml){
    const promptCount = conversation.filter(ev => ev.type === 'prompt').length;
    const responseCount = conversation.filter(ev => ev.type === 'response').length;
    const toolCallCount = conversation.filter(ev => ev.type === 'tool_call' || ev.type === 'mcp_call').length;
    const failureCount = conversation.filter(ev => ev.type === 'tool_result' && ev.success === false).length;
    const totalPromptCount = Number(session.prompt_count || promptCount || 0);
    const totalToolCallCount = Number(session.tool_calls || session.tool_call_count || toolCallCount || 0);
    const totalFailureCount = Number(session.failure_count || failureCount || 0);
    const promptLabel = totalPromptCount > promptCount
      ? `<span class="conv-stat-num">${fmt(promptCount)}</span> shown / ${fmt(totalPromptCount)} prompts`
      : `<span class="conv-stat-num">${fmt(promptCount)}</span> prompts`;
    const toolLabel = totalToolCallCount > toolCallCount
      ? `<span class="conv-stat-num">${fmt(toolCallCount)}</span> shown / ${fmt(totalToolCallCount)} tool calls`
      : `<span class="conv-stat-num">${fmt(toolCallCount)}</span> tool calls`;
    const failLabel = totalFailureCount > failureCount
      ? `<span class="conv-stat-num" style="color:var(--red)">${fmt(failureCount)}</span> shown / ${fmt(totalFailureCount)} failed`
      : `<span class="conv-stat-num" style="color:var(--red)">${fmt(failureCount)}</span> failed`;
    const failPart = totalFailureCount
      ? `<span class="conv-stat-sep">·</span><div class="conv-stat-item" style="color:var(--red)">${failLabel}</div>`
      : '';
    const statsBar = `<div class="conv-stats-bar">
      <div class="conv-stat-item">${promptLabel}</div>
      <span class="conv-stat-sep">·</span>
      <div class="conv-stat-item"><span class="conv-stat-num">${fmt(responseCount)}</span> responses</div>
      <span class="conv-stat-sep">·</span>
      <div class="conv-stat-item">${toolLabel}</div>
      ${failPart}
    </div>`;
    const readerToolbar = renderConversationReaderToolbar(session, conversation);
    return `<div class="conversation-shell">${readerToolbar}${statsBar}${timelineHtml}</div>`;
  }

  function sessionTimelineEventLabel(ev){
    if (ev.type === 'prompt') return 'Prompt';
    if (ev.type === 'response') return ev.model ? `Response · ${ev.model}` : 'Response';
    if (ev.type === 'tool_call') return ev.tool_name || 'Tool call';
    if (ev.type === 'tool_result') return `${ev.success === false ? 'Failed' : 'Done'} · ${ev.tool_name || 'Tool'}`;
    if (ev.type === 'mcp_call') return `MCP · ${ev.tool_name || ev.server || 'Call'}`;
    if (ev.type === 'mcp_result') return `MCP result · ${ev.tool_name || ev.server || 'Call'}`;
    if (ev.type === 'subagent_start') return `Subagent · ${ev.subagent_type || 'start'}`;
    if (ev.type === 'subagent_stop') return `Subagent done · ${ev.subagent_type || 'subagent'}`;
    return ev.type || 'Event';
  }

  function renderSessionTimelinePanel(session, conversation){
    const events = (conversation || [])
      .map((ev, index) => ({...ev, _conversationIndex: index}))
      .filter(ev =>
        ['prompt','response','tool_call','tool_result','mcp_call','mcp_result','subagent_start','subagent_stop'].includes(ev.type)
      );
    const totalShown = events.length;
    const totalKnown = Math.max(
      totalShown,
      Number(session.prompt_count || 0) + Number(session.tool_calls || session.tool_call_count || 0)
    );
    if (!events.length) {
      const copy = session.full_id && !session._fullLoaded ? 'Loading session timeline...' : 'No timeline events available for this session.';
      return `<div class="session-timeline-panel"><div class="session-timeline-head"><div class="session-timeline-title">Session Timeline</div></div><div class="session-timeline-empty">${copy}</div></div>`;
    }
    const startTs = Number(events[0].ts || 0);
    const rawPositioned = events.map((ev, index) => {
      const ts = Number(ev.ts || 0) || (startTs + index * 1000);
      const rel = Math.max(0, ts - startTs);
      const dur = Math.max(1, Number(ev.duration_ms || 0) || (ev.type === 'tool_result' ? 300 : 120));
      return {...ev, _rel: rel, _dur: dur, _index: index};
    });
    const gapThresholdMs = 2 * 60 * 1000;
    const compressedGapMs = 20 * 1000;
    const gapMarkers = [];
    let compressedRel = 0;
    const positioned = rawPositioned.map((ev, index) => {
      if (index > 0) {
        const previous = rawPositioned[index - 1];
        const gap = Math.max(0, Number(ev._rel || 0) - Number(previous._rel || 0));
        if (gap > gapThresholdMs) {
          compressedRel += compressedGapMs;
          gapMarkers.push({ rel: compressedRel, rawGap: gap });
        } else {
          compressedRel += gap;
        }
      }
      return {...ev, _displayRel: compressedRel};
    });
    const maxRel = Math.max(
      1,
      ...positioned.map(ev => Number(ev._displayRel || 0) + Number(ev._dur || 0))
    );
    const tooltipAttr = value => escHtml(value).replace(/"/g, '&quot;');
    const tooltipAlign = left => left < 16 ? 'left' : (left > 84 ? 'right' : 'center');
    const bars = positioned.map(ev => {
      const left = Math.max(0, Math.min(100, (Number(ev._displayRel || 0) / maxRel) * 100));
      const width = Math.max(0.35, Math.min(100 - left, (Number(ev._dur || 1) / maxRel) * 100));
      const label = sessionTimelineEventLabel(ev);
      const preview = ev.preview ? ` - ${String(ev.preview).slice(0, 220)}` : '';
      const compressedNote = ev._displayRel !== ev._rel ? ' · pauses compressed' : '';
      const title = `${label} at ${fmtRelTime(ev._rel)}${compressedNote}${ev.duration_ms ? `, ${fmtDur(ev.duration_ms)}` : ''}${preview}`;
      const summary = String(ev.preview || '').replace(/\s+/g, ' ').trim().slice(0, 96);
      const safeTip = tooltipAttr(title);
      const failureClass = ev.type === 'tool_result' && ev.success === false ? ' is-failure' : '';
      return `<button type="button" class="session-timeline-span${failureClass}" data-timeline-event-index="${ev._conversationIndex}" data-timeline-position="${left.toFixed(3)}" data-timeline-time="${fmtRelTime(ev._rel)}" data-timeline-label="${tooltipAttr(label)}" data-timeline-summary="${tooltipAttr(summary)}" data-tip-align="${tooltipAlign(left)}" style="left:${left.toFixed(3)}%;width:${width.toFixed(3)}%;--span-color:${getEventNodeColor(ev)}" data-tip="${safeTip}" aria-label="${safeTip}"></button>`;
    }).join('');
    const gaps = gapMarkers.map(gap => {
      const left = Math.max(0, Math.min(100, (Number(gap.rel || 0) / maxRel) * 100));
      const safeTip = tooltipAttr(`Idle gap compressed: ${fmtDur(gap.rawGap)}`);
      return `<span class="session-timeline-gap" data-tip-align="${tooltipAlign(left)}" style="left:${left.toFixed(3)}%" data-tip="${safeTip}" aria-label="${safeTip}" tabindex="0"></span>`;
    }).join('');
    const failureTotal = positioned.filter(ev => ev.type === 'tool_result' && ev.success === false).length;
    const rangeLabel = `${fmtRelTime(rawPositioned[rawPositioned.length - 1]?._rel || maxRel)}${gapMarkers.length ? ` · ${fmt(gapMarkers.length)} pauses compressed` : ''}`;
    const countLabel = totalKnown > totalShown ? `${fmt(totalShown)} shown / ${fmt(totalKnown)} events` : `${fmt(totalShown)} events`;
    const errorLabel = failureTotal ? ` · ${fmt(failureTotal)} shown failed` : '';
    const initialPointIndex = Number.isInteger(session._playheadEventIndex)
      ? Math.max(0, positioned.findIndex(ev => ev._conversationIndex === session._playheadEventIndex))
      : 0;
    const initialPoint = positioned[initialPointIndex] || positioned[0];
    const initialLeft = Math.max(0, Math.min(100, (Number(initialPoint._displayRel || 0) / maxRel) * 100));
    const initialLabel = sessionTimelineEventLabel(initialPoint);
    const initialSummary = String(initialPoint.preview || '').replace(/\s+/g, ' ').trim().slice(0, 96);
    const initialTime = fmtRelTime(initialPoint._rel);
    const initialEdge = initialLeft < 12 ? 'left' : (initialLeft > 88 ? 'right' : 'center');
    const initialValueText = `${initialTime} · ${initialLabel}${initialSummary ? ` · ${initialSummary}` : ''}`;
    const playhead = `<button type="button" class="session-timeline-playhead" role="slider" aria-label="Conversation position" aria-valuemin="1" aria-valuemax="${positioned.length}" aria-valuenow="${initialPointIndex + 1}" aria-valuetext="${tooltipAttr(initialValueText)}" data-edge="${initialEdge}" style="--playhead-left:${initialLeft.toFixed(3)}%">
      <span class="session-playhead-bubble" aria-hidden="true"><span class="session-playhead-time">${initialTime}</span><span class="session-playhead-copy"><span class="session-playhead-label">${escHtml(initialLabel)}</span><span class="session-playhead-summary"${initialSummary ? '' : ' hidden'}>${escHtml(initialSummary)}</span></span></span>
      <span class="session-playhead-line" aria-hidden="true"></span><span class="session-playhead-grip" aria-hidden="true"></span>
    </button>`;
    return `<div class="session-timeline-panel">
      <div class="session-timeline-head">
        <div class="session-timeline-title">Session Timeline</div>
        <div class="session-timeline-meta">${countLabel}${errorLabel} · ${rangeLabel}</div>
        <div class="session-timeline-hint">Drag to scan conversation</div>
      </div>
      <div class="session-timeline-track" aria-label="Session timeline">${gaps}${bars}${playhead}</div>
    </div>`;
  }

  function pickTelemetryMoments(session){
    const important = (session.conversation || []).filter(ev =>
      ['prompt','response','tool_call','tool_result','mcp_call','mcp_result','subagent_start'].includes(ev.type)
    );
    if (important.length <= 24) {
      return important.map((ev, idx) => ({...ev, _beatId:`beat-${idx}`}));
    }
    const step = Math.ceil(important.length / 24);
    return important
      .filter((_, idx) => idx % step === 0)
      .slice(0, 24)
      .map((ev, idx) => ({...ev, _beatId:`beat-${idx}`}));
  }

  function telemetryAbsMs(item, anchorMs){
    return anchorMs + Number(item.rel_ms || 0);
  }

  function parseEventTs(value){
    if (typeof value === 'number' && Number.isFinite(value) && value > 0) return value;
    if (typeof value === 'string' && value) {
      const parsed = Date.parse(value);
      if (Number.isFinite(parsed) && parsed > 0) return parsed;
    }
    return 0;
  }

  function stringifyEventContent(value){
    if (typeof value === 'string') return value;
    if (value === null || value === undefined) return '';
    if (Array.isArray(value)) {
      return value.map(item => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          if (typeof item.text === 'string') return item.text;
          if (typeof item.content === 'string') return item.content;
          try { return JSON.stringify(item); }
          catch (_) { return String(item); }
        }
        return String(item || '');
      }).filter(Boolean).join('\n');
    }
    if (value && typeof value === 'object') {
      if (typeof value.text === 'string') return value.text;
      if (typeof value.content === 'string') return value.content;
      try { return JSON.stringify(value); }
      catch (_) { return String(value); }
    }
    return String(value);
  }

  function normalizeLoadedConversation(events, existingConversation){
    const existing = Array.isArray(existingConversation) ? existingConversation : [];
    return (events || []).map((ev, idx) => {
      const fallback = existing[idx] || {};
      return {
        type: ev.type,
        ts: parseEventTs(ev.ts || ev.timestamp || ev.time) || Number(fallback.ts || 0) || 0,
        preview: stringifyEventContent(
          ev.content !== undefined ? ev.content : (ev.preview !== undefined ? ev.preview : ev.input)
        ) || '',
        tool_name: ev.tool_name || '',
        model: ev.model || '',
        input_tokens: ev.input_tokens || 0,
        output_tokens: ev.output_tokens || 0,
        cache_read_tokens: ev.cache_read_tokens || 0,
        duration_ms: ev.duration_ms || 0,
        success: ev.success !== undefined ? ev.success : true,
        subagent_type: ev.subagent_type || '',
        server: ev.server || '',
      };
    });
  }

  function telemetryMatchBeat(beat, item, anchorMs){
    if (!beat) return false;
    const beatMs = Number(beat.ts || 0);
    const itemMs = telemetryAbsMs(item, anchorMs);
    const near = Math.abs(itemMs - beatMs) <= 5000;
    const beatTool = beat.tool_name || '';
    const itemTool = item.tool_name || item.mcp_tool || '';
    if (beatTool && itemTool && beatTool === itemTool) return true;
    if (beat.server && item.mcp_server && beat.server === item.mcp_server) return true;
    return near;
  }

  function telemetryPhaseColor(phase){
    if (phase === 'mcp') return 'rgba(255,177,86,.75)';
    if (phase === 'shell') return 'rgba(255,159,10,.75)';
    if (phase === 'subagent') return 'rgba(215,209,198,.78)';
    if (phase === 'conversation') return 'rgba(48,209,88,.72)';
    return 'rgba(242,138,26,.75)';
  }

  function renderTelemetryAttrs(attrs){
    const entries = Object.entries(attrs || {}).slice(0, 6);
    if (!entries.length) return '';
    return `<div class="telemetry-chip-row">${entries.map(([key, value]) =>
      `<span class="telemetry-chip" title="${escHtml(String(value))}">${escHtml(key)}: ${escHtml(String(value))}</span>`
    ).join('')}</div>`;
  }

  function buildTelemetryTreeRows(spans, session, activeBeat, anchorMs){
    const byId = new Map();
    const children = new Map();
    const ordered = spans.slice().sort((a, b) => Number(a.rel_ms || 0) - Number(b.rel_ms || 0));
    ordered.forEach((span, index) => {
      const key = span.id || `span-${index}`;
      span._treeKey = key;
      byId.set(key, span);
      children.set(key, []);
    });
    const visualParentByKey = new Map();
    const roots = [];
    const traceStacks = new Map();
    ordered.forEach(span => {
      const key = span._treeKey;
      const parentId = span.parent_id || '';
      const explicitParent = parentId && byId.has(parentId) ? parentId : '';
      const traceKey = span.trace_id || '';
      const stack = traceStacks.get(traceKey) || [];
      const spanStart = Number(span.rel_ms || 0);
      const spanEnd = spanStart + Number(span.duration_ms || 0);
      while (stack.length) {
        const top = stack[stack.length - 1];
        const topEnd = Number(top.rel_ms || 0) + Number(top.duration_ms || 0);
        if (topEnd >= spanStart) break;
        stack.pop();
      }
      let visualParent = explicitParent;
      if (!visualParent && traceKey && stack.length) {
        const candidate = stack[stack.length - 1];
        const candidateEnd = Number(candidate.rel_ms || 0) + Number(candidate.duration_ms || 0);
        if (candidate._treeKey !== key && candidateEnd >= spanEnd && Number(candidate.duration_ms || 0) >= Number(span.duration_ms || 0)) {
          visualParent = candidate._treeKey;
        }
      }
      if (visualParent) {
        visualParentByKey.set(key, visualParent);
        children.get(visualParent).push(span);
      } else {
        roots.push(span);
      }
      if (traceKey) {
        stack.push(span);
        traceStacks.set(traceKey, stack);
      }
    });
    const sortChildren = list => list.sort((a, b) => {
      const rel = Number(a.rel_ms || 0) - Number(b.rel_ms || 0);
      if (rel) return rel;
      return Number(b.duration_ms || 0) - Number(a.duration_ms || 0);
    });
    sortChildren(roots);
    children.forEach(sortChildren);
    if (!(session._collapsedSpanIds instanceof Set)) session._collapsedSpanIds = new Set();
    const rows = [];
    function visit(span, depth){
      const key = span._treeKey;
      const descendants = children.get(key) || [];
      const active = activeBeat && telemetryMatchBeat(activeBeat, span, anchorMs);
      const collapsed = session._collapsedSpanIds.has(key);
      rows.push({
        span,
        key,
        depth,
        hasParent: visualParentByKey.has(key),
        hasChildren: descendants.length > 0,
        collapsed,
        active,
      });
      if (!collapsed) descendants.forEach(child => visit(child, depth + 1));
    }
    roots.forEach(root => visit(root, 0));
    return rows;
  }

  function telemetrySpanServiceLabel(span){
    if (span.service) return span.service;
    if (span.agent) return `${span.agent} agent`;
    if (span.mcp_server) return `mcp:${span.mcp_server}`;
    if (span.tool_name) return `tool:${span.tool_name}`;
    if (span.mcp_tool) return `tool:${span.mcp_tool}`;
    if (span.model) return span.model;
    if (span.phase) return span.phase;
    return '';
  }

  function renderQualityPanel(session){
    const rules = D.quality_rules || [];
    const hasScore = session.quality_available === true;
    const score = Number(session.quality_score || 0);
    const breakdown = session.quality_breakdown || [];
    const status = hasScore
      ? (score >= 80 ? 'Strong' : score >= 60 ? 'Watch' : 'Needs attention')
      : 'Not scored';
    const note = hasScore
      ? (session.quality_missing_reason || 'This score is computed from local telemetry spans, token usage, completion markers, tool failures, repeated tool loops, duration, recovery, tool diversity, and edit activity.')
      : (session.quality_missing_reason || 'No quality score is available for this session because reflect did not load enough scored telemetry spans for it.');
    const sessionSignals = [
      ['Status', session.is_completed ? 'completed' : 'not completed'],
      ['Failures', fmt(session.failure_count || session.failures || 0)],
      ['Recovered', fmt(session.recovered_failures || 0)],
      ['Tool Calls', fmt(sessionToolCallTotal(session))],
      ['Total Tokens', fmtTokenShort(sessionTokenTotal(session))],
    ];
    const formatMetricValue = value => {
      if (typeof value === 'number') return Number.isInteger(value) ? fmt(value) : String(Math.round(value * 100) / 100);
      if (typeof value === 'boolean') return value ? 'yes' : 'no';
      if (value === null || value === undefined || value === '') return 'none';
      return String(value);
    };
    const inputEntries = item => {
      const inputs = Array.isArray(item.inputs) ? item.inputs : [];
      if (inputs.length) return inputs.map(input => [input.name || 'input', input.value]);
      const metrics = Object.entries(item.metrics || {});
      return metrics.length ? metrics.map(([key, value]) => [String(key).replace(/_/g, ' '), value]) : [['none', 'none']];
    };
    const renderBreakdownRows = item => {
      const entries = inputEntries(item);
      const rowSpan = entries.length;
      return entries.map(([key, value], index) => `
        <tr>
          ${index === 0 ? `<td class="quality-breakdown-name" rowspan="${rowSpan}">${escHtml(item.name || '')}</td>` : ''}
          ${index === 0 ? `<td class="quality-breakdown-score" rowspan="${rowSpan}">${Number(item.earned || 0).toFixed(1)} / ${Number(item.max || 0).toFixed(0)}</td>` : ''}
          <td class="quality-breakdown-input">${escHtml(key)}</td>
          <td class="quality-breakdown-value">${escHtml(formatMetricValue(value))}</td>
          ${index === 0 ? `<td rowspan="${rowSpan}"><div class="quality-breakdown-summary">${escHtml(item.summary || '')}</div></td>` : ''}
        </tr>
      `).join('');
    };
    const breakdownTotal = breakdown.reduce((sum, item) => sum + Number(item.earned || 0), 0);
    const breakdownMax = breakdown.reduce((sum, item) => sum + Number(item.max || 0), 0);
    return `<div class="quality-shell">
      <div class="quality-summary">
        <div class="quality-score-card">
          <div class="quality-score-value">${hasScore ? score.toFixed(0) + '%' : '--'}</div>
          <div class="quality-score-label">${escHtml(status)}</div>
        </div>
        <div class="quality-note">
          <div style="font-weight:800;color:var(--text);margin-bottom:8px">Why this session ${hasScore ? 'has a score' : 'does not have a score'}</div>
          <div>${escHtml(note)}</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
            ${sessionSignals.map(([label, value]) => `<span class="sb-card-badge">${escHtml(label)}: ${escHtml(value)}</span>`).join('')}
          </div>
        </div>
      </div>
      <div class="panel-title" style="margin-top:2px">Score Breakdown</div>
      ${breakdown.length ? `<div class="quality-breakdown-table-wrap">
        <table class="quality-breakdown-table">
          <thead>
            <tr>
              <th>Rule</th>
              <th style="text-align:right">Score</th>
              <th>Input</th>
              <th>Value</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            ${breakdown.map(item => renderBreakdownRows(item)).join('')}
            <tr class="quality-breakdown-total">
              <td>Total</td>
              <td class="quality-breakdown-score">${breakdownTotal.toFixed(1)} / ${breakdownMax.toFixed(0)}</td>
              <td colspan="3">Final displayed score: ${hasScore ? score.toFixed(0) + '%' : '--'}</td>
            </tr>
          </tbody>
        </table>
      </div>` : `<div class="quality-note">No score breakdown is available because this session was not scored from detailed telemetry.</div>`}
      <div class="panel-title" style="margin-top:2px">Quality Rules</div>
      <div class="quality-rules">
        ${rules.map(rule => `
          <div class="quality-rule">
            <div class="quality-rule-top">
              <div class="quality-rule-name">${escHtml(rule.name || '')}</div>
              <div class="quality-rule-points">${fmt(rule.points || 0)} pts</div>
            </div>
            <div class="quality-rule-desc">${escHtml(rule.description || '')}</div>
            <div class="quality-rule-signals">${escHtml((rule.signals || []).join(' / '))}</div>
          </div>
        `).join('')}
      </div>
    </div>`;
  }

  function renderTelemetryCockpit(session){
    const telemetry = session.telemetry;
    const summary = telemetry?.summary || {};
    if (!telemetry || (!(summary.spans || 0) && !(summary.logs || 0))) {
      return `<div class="telemetry-empty">
        <div style="font-size:16px;color:var(--text);font-weight:700;margin-bottom:8px">Telemetry cockpit</div>
        <div style="margin-bottom:14px;line-height:1.6">Load the selected session’s OTLP traces and logs to see a synchronized trace waterfall, live log stream, and correlated session moments.</div>
        ${session.full_id ? `<button class="sb-load-btn" id="sb-load-full" data-sid="${escHtml(session.full_id)}">Load telemetry</button>` : ''}
      </div>`;
    }

    const spans = (telemetry.spans || []).slice(0, 120);
    const logs = (telemetry.logs || []).slice(0, 120);
    const anchorMs = Number(summary.anchor_ns || 0) / 1e6;
    const traceBaseMs = Math.min(
      ...spans.map(span => Number(span.rel_ms || 0)),
      ...logs.map(log => Number(log.rel_ms || 0)),
      0,
    );
    const traceWindowMs = Math.max(
      1,
      ...spans.map(span => (Number(span.rel_ms || 0) - traceBaseMs) + Number(span.duration_ms || 0)),
      ...logs.map(log => Number(log.rel_ms || 0) - traceBaseMs),
    );
    const beats = pickTelemetryMoments(session);
    if (!session._telemetryFocusId && beats.length) {
      session._telemetryFocusId = beats[0]._beatId;
    }
    const activeBeat = beats.find(beat => beat._beatId === session._telemetryFocusId) || beats[0] || null;
    const matchedSpans = activeBeat ? spans.filter(span => telemetryMatchBeat(activeBeat, span, anchorMs)) : [];
    const matchedLogs = activeBeat ? logs.filter(log => telemetryMatchBeat(activeBeat, log, anchorMs)) : [];
    const treeRows = buildTelemetryTreeRows(spans, session, activeBeat, anchorMs);
    const orderedLogs = logs.slice().sort((a, b) => {
      const aActive = activeBeat && telemetryMatchBeat(activeBeat, a, anchorMs) ? 1 : 0;
      const bActive = activeBeat && telemetryMatchBeat(activeBeat, b, anchorMs) ? 1 : 0;
      if (aActive !== bActive) return bActive - aActive;
      return Number(a.rel_ms || 0) - Number(b.rel_ms || 0);
    });
    const rowSpans = treeRows.slice(0, 120);
    const collapsibleKeys = treeRows.filter(row => row.hasChildren).map(row => row.key);
    const collapsedCount = treeRows.filter(row => row.collapsed).length;
    const rulerTicks = [0, .25, .5, .75, 1].map(fraction => ({
      left: fraction * 100,
      label: fmtDur(traceWindowMs * fraction),
    }));

    const summaryCards = [
      { kicker: 'Spans', value: fmt(summary.spans || 0), sub: `${rowSpans.length} rows visible` },
      { kicker: 'Logs', value: fmt(summary.logs || 0), sub: `${logs.length} records loaded` },
      { kicker: 'Errors', value: fmt(summary.errors || 0), sub: `${fmt(summary.warnings || 0)} warnings in trace` },
      { kicker: 'Trace window', value: fmtDur(traceWindowMs), sub: `${fmt(summary.services || 0)} services correlated` },
    ];
    if ((summary.hook_schema_versions || []).length || (summary.telemetry_sources || []).length) {
      const schemas = (summary.hook_schema_versions || []).map(version => `v${version}`).join(', ');
      const adapters = (summary.provider_adapters || []).join(', ');
      summaryCards.push({
        kicker: 'Hook contract',
        value: schemas || 'legacy',
        sub: `${adapters || (summary.telemetry_sources || []).join(', ') || 'unknown'} · ${fmt(summary.native_linked_spans || 0)} native links`,
      });
    }

    return `
      <div class="telemetry-shell">
        <div class="telemetry-summary">
          ${summaryCards.map(card => `
            <div class="telemetry-stat">
              <div class="telemetry-stat-kicker">${card.kicker}</div>
              <div class="telemetry-stat-value">${card.value}</div>
              <div class="telemetry-stat-sub">${card.sub}</div>
            </div>
          `).join('')}
        </div>
        <div class="telemetry-grid">
          <div class="telemetry-panel">
            <div class="telemetry-panel-title">
              <div class="telemetry-panel-head">
                <span>Trace waterfall</span>
                <span class="telemetry-panel-sub">Jaeger-style waterfall with nested spans aligned to trace start</span>
              </div>
              <div class="telemetry-panel-actions">
                ${collapsedCount ? `<button type="button" class="telemetry-trace-action" data-telemetry-action="expand-all">Expand all</button>` : ''}
                ${collapsibleKeys.length ? `<button type="button" class="telemetry-trace-action" data-telemetry-action="collapse-all">Collapse spans</button>` : ''}
              </div>
            </div>
            <div class="telemetry-ruler">
              <div class="telemetry-column-heads">
                <span class="telemetry-column-head">Service &amp; operation</span>
                <span class="telemetry-column-head">Phase</span>
                <span class="telemetry-column-head">Dur</span>
              </div>
              <div class="telemetry-ruler-track">
                ${rulerTicks.map(tick => `
                  <span class="telemetry-ruler-tick" style="left:${tick.left}%">${escHtml(tick.label)}</span>
                `).join('')}
              </div>
            </div>
            <div class="telemetry-waterfall">
              ${rowSpans.length ? rowSpans.map(row => {
                const span = row.span;
                const relMs = Number(span.rel_ms || 0);
                const durationMs = Number(span.duration_ms || 0);
                const laneStartMs = relMs - traceBaseMs;
                const left = Math.max(0, Math.min(100, (laneStartMs / traceWindowMs) * 100));
                const width = Math.max(2, Math.min(100 - left, (durationMs / traceWindowMs) * 100));
                const active = row.active;
                const title = span.tool_name || span.mcp_tool || span.event || span.name || 'Span';
                const serviceLabel = telemetrySpanServiceLabel(span);
                const startedAt = span.started_at ? fmtWorkflowDate(span.started_at) : '';
                const sub = [serviceLabel, span.trace_id ? `trace ${String(span.trace_id).slice(0, 8)}` : '', startedAt, fmtRelTime(relMs)].filter(Boolean).join(' · ');
                return `
                  <div class="telemetry-span-row${active ? ' active' : ''}">
                    <div class="telemetry-span-main">
                      <div class="telemetry-span-stack${row.hasParent ? ' has-parent' : ''}" style="--tree-depth:${row.depth}"${row.hasChildren ? ` data-span-toggle="${escHtml(row.key)}"` : ''}>
                        ${row.hasChildren
                          ? `<button type="button" class="telemetry-span-toggle" data-span-toggle="${escHtml(row.key)}" data-collapsed="${row.collapsed ? 'true' : 'false'}" aria-label="${row.collapsed ? 'Expand' : 'Collapse'} ${escHtml(title)}">${row.collapsed ? '+' : '−'}</button>`
                          : `<span class="telemetry-span-spacer" aria-hidden="true"></span>`}
                        <div class="telemetry-span-title">${escHtml(title)}</div>
                        <div class="telemetry-span-meta">${escHtml(sub)}</div>
                      </div>
                      <div class="telemetry-span-phase">
                        <span class="telemetry-phase-dot" style="background:${telemetryPhaseColor(span.phase)}"></span>
                        <span>${escHtml(span.phase || 'span')}</span>
                      </div>
                      <div class="telemetry-span-duration">${fmtDur(durationMs || 0)}</div>
                    </div>
                    <div class="telemetry-span-lane">
                      <div class="telemetry-span-start" style="left:${left}%">${escHtml(fmtDur(laneStartMs))}</div>
                      <div class="telemetry-span-track">
                        <div class="telemetry-span-bar" style="left:${left}%;width:${width}%;background:${telemetryPhaseColor(span.phase)}"></div>
                      </div>
                    </div>
                    ${active && span.attrs && Object.keys(span.attrs).length ? `<div style="grid-column:1 / -1;padding:0 14px 10px 14px">${renderTelemetryAttrs(span.attrs)}</div>` : ''}
                  </div>`;
              }).join('') : `<div class="telemetry-empty">No OTLP spans matched this session.</div>`}
            </div>
          </div>
        </div>
        <div class="telemetry-panel telemetry-log-panel">
          <div class="telemetry-panel-title">
            <span>Log dock</span>
            <span class="telemetry-panel-sub">OTLP log records for the selected session, placed below the trace waterfall</span>
          </div>
          <div class="telemetry-log-list">
            ${orderedLogs.length ? orderedLogs.map(log => {
              const active = activeBeat && telemetryMatchBeat(activeBeat, log, anchorMs);
              const meta = [log.service, log.event || log.tool_name || log.mcp_tool || log.mcp_server, log.trace_id ? `trace ${String(log.trace_id).slice(0, 8)}` : ''].filter(Boolean).join(' · ');
              return `
                <div class="telemetry-log${active ? ' active' : ''}">
                  <div class="telemetry-log-top">
                    <span class="telemetry-log-severity" data-severity="${escHtml(log.severity || 'TRACE')}">${escHtml(log.severity || 'TRACE')}</span>
                    <span class="telemetry-log-time">${fmtRelTime(Number(log.rel_ms || 0))}</span>
                    ${meta ? `<span class="telemetry-log-meta">${escHtml(meta)}</span>` : ''}
                  </div>
                  <div class="telemetry-log-body">${escHtml(log.body || 'No log body')}</div>
                  ${renderTelemetryAttrs(log.attrs)}
                </div>`;
            }).join('') : `<div class="telemetry-empty">No OTLP logs matched this session.</div>`}
          </div>
        </div>
      </div>`;
  }

  function roughTokenCount(text){
    const normalized = typeof text === 'string' ? text.trim() : '';
    if (!normalized) return 0;
    return Math.max(1, Math.round(normalized.length / 4));
  }

  function getEstimatedSessionTokens(session){
    if (!session || session.agent !== 'cursor') return null;
    if (session._estimatedTokens && ((session.input_tokens || 0) > 0 || (session.output_tokens || 0) > 0)) {
      return { input: session.input_tokens || 0, output: session.output_tokens || 0 };
    }
    if ((session.input_tokens || 0) > 0 || (session.output_tokens || 0) > 0) return null;
    const conv = session.conversation || [];
    if (!conv.length) return null;
    let input = 0;
    let output = 0;
    conv.forEach(ev => {
      if (ev.type === 'prompt') input += roughTokenCount(ev.preview || '');
      if (ev.type === 'response') output += roughTokenCount(ev.preview || '');
    });
    if (!input && !output) return null;
    return { input, output };
  }

  function tryLoadFullDetail(sessionId, btn){
    const idx = sessions.findIndex(s => (s.full_id || s.id) === sessionId);
    if (idx >= 0) sessions[idx]._loadingDetail = true;
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Loading…';
    }
    fetch('/api/session/' + encodeURIComponent(sessionId))
      .then(r => {
        if (!r.ok) throw new Error('not-found');
        return r.json();
      })
      .then(data => {
        const events = data.events || data.conversation || [];
        const telemetry = data.telemetry || null;
        const toolInventory = data.tool_inventory || null;
        if (idx >= 0) {
          const conv = normalizeLoadedConversation(events, sessions[idx].conversation);
          if (conv.length > 0) sessions[idx].conversation = conv;
          if (telemetry) sessions[idx].telemetry = telemetry;
          if (toolInventory) sessions[idx].tool_inventory = toolInventory;
          sessions[idx]._fullLoaded = true;
          sessions[idx]._loadingDetail = false;
          const estimated = getEstimatedSessionTokens(sessions[idx]);
          if (estimated) {
            sessions[idx].input_tokens = estimated.input;
            sessions[idx].output_tokens = estimated.output;
            sessions[idx]._estimatedTokens = true;
            sessions[idx].token_source = 'estimated_cursor_transcript';
            sessions[idx].token_note = 'Token counts are estimated from Cursor transcript text because exact per-session usage is not present in local telemetry.';
          }
          renderSessionDetail(sessions[idx]);
        } else if (btn) {
          btn.textContent = 'No conversation data for this session';
        }
      })
      .catch(err => {
        if (idx >= 0) sessions[idx]._loadingDetail = false;
        if (err.message === 'not-found') {
          if (btn) {
            btn.disabled = false;
            btn.textContent = 'Session not found';
          }
        } else {
          if (btn) {
            btn.disabled = false;
            btn.textContent = 'Retry telemetry load';
          }
        }
      });
  }

  renderSessionList();
  if (selectedIdx !== null) {
    renderSessionDetail(sessions[selectedIdx]);
  }
})();
