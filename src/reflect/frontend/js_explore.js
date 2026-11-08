/* ════════════════════ ACTIVITY ════════════════════ */

function resetUsageChart(id){
  const canvas = document.getElementById(id);
  if (!canvas) return;
  Chart.getChart(canvas)?.destroy();
  canvas.hidden = false;
  canvas.parentElement.querySelector('[data-usage-chart-empty]')?.remove();
}

function renderUsageChartEmpty(id, message){
  const canvas = document.getElementById(id);
  if (!canvas) return;
  canvas.hidden = true;
  const note = document.createElement('div');
  note.dataset.usageChartEmpty = 'true';
  note.className = 'empty-note';
  note.textContent = message;
  canvas.parentElement.appendChild(note);
}

function renderMeasurementCoverage(){
  const el = document.getElementById('measurement-summary');
  if (!el) return;
  const data = D.measurements || {}, c = data.coverage || {}, m = data.metrics || {}, totals = data.totals || {};
  const git = D.git_activity || {}, buckets = c.token_bucket_records || {};
  const known = v => v != null && Number.isFinite(Number(v));
  const compact = v => !known(v) ? 'Unknown' : Number(v).toLocaleString(undefined, {notation:'compact', maximumFractionDigits:1});
  const decimal = v => !known(v) ? 'Unknown' : Number(v).toLocaleString(undefined, {maximumFractionDigits:2});
  const duration = v => !known(v) ? 'Unknown' : v >= 86400000 ? `${decimal(v/86400000)}d` : v >= 3600000 ? `${decimal(v/3600000)}h` : fmtDur(v);
  const percent = v => !known(v) ? 'Unknown' : `${decimal(v*100)}%`;
  const amount = v => !known(v) ? 'Unknown' : Number(v).toLocaleString(undefined, {minimumFractionDigits:2,maximumFractionDigits:2});
  const exact = v => known(v) ? Number(v).toLocaleString(undefined, {maximumFractionDigits:6}) : 'Not captured';
  const number = (v, format=compact, unit='') => `<strong title="${escHtml(exact(v)+(unit ? ' '+unit : ''))}">${escHtml(format(v))}</strong>`;
  const fact = (label, v, format=compact, unit='') => `<div><dt>${escHtml(label)}</dt><dd>${number(v,format,unit)}</dd></div>`;
  const ratioBar = (part,total,label) => known(part) && known(total) && total > 0
    ? `<div class="usage-proportion" role="img" aria-label="${escHtml(label)}"><span style="width:${Math.max(0,Math.min(100,part/total*100))}%"></span></div>` : '';
  const observed = c.observed_usage, fullyPriced = c.complete_priced_usage;
  const costKnown = known(totals.estimated_cost_usd) && c.cost_status !== 'unavailable' && (Number(c.priced_usage || 0)+Number(c.partially_priced_usage || 0)+Number(c.legacy_cost_usage || 0)>0);
  const cost = costKnown ? totals.estimated_cost_usd : null;
  const costLabel = c.cost_status === 'complete' ? 'Estimated cost' : 'Known cost subtotal';
  const tokenBuckets = [
    ['U','Uncached input','input_tokens'],['R','Cache read','cache_read_tokens'],
    ['W','Cache write','cache_creation_tokens'],['O','Output','output_tokens'],
  ].map(([code,label,key])=>({code,label,key,records:buckets[key],value:Number(buckets[key])>0 && known(totals[key]) ? totals[key] : null}));
  const tokenTotal = tokenBuckets.some(item=>known(item.value)) ? tokenBuckets.reduce((sum,item)=>sum+Number(item.value || 0),0) : null;
  const tokenComplete = tokenBuckets.every(item=>known(item.value) && item.records === observed);
  const completedTurns = m.completed_turns === 0 && !known(c.observed_prompt_turns) ? null : m.completed_turns;
  const scope = data.scope || {};
  const scopeEl = document.getElementById('measurement-scope');
  if (scopeEl) scopeEl.textContent = `${D.focused_session_id ? 'Selected session · ' : ''}${scope.kind === 'event_window' ? 'Selected event window' : 'Captured session lifetimes'}${scope.date_from || scope.date_to ? ` · ${scope.date_from ? fmtWorkflowDate(scope.date_from) : 'First event'} — ${scope.date_to ? fmtWorkflowDate(scope.date_to) : 'Latest event'} (local time)` : ''}. Coverage travels with every measurement.`;
  el.innerHTML = `
    <article class="usage-widget usage-headline usage-cost"><div class="usage-widget-head"><h3><span class="usage-code">M1</span> ${escHtml(costLabel)}</h3><span class="usage-unit">USD</span></div><div class="usage-big">${number(cost,amount,'USD')}</div><div class="usage-coverage"><span>${known(observed) && observed>0 ? `${decimal(fullyPriced)} / ${decimal(observed)} fully priced` : 'Pricing coverage unknown'}</span><b>${percent(observed>0 && known(fullyPriced) ? fullyPriced/observed : null)}</b></div>${ratioBar(fullyPriced,observed,'Fully priced usage coverage')}<p class="usage-copy">${c.cost_status === 'complete' ? 'All observed usage records are fully priced.' : costKnown ? 'Unpriced usage is excluded from this subtotal.' : 'Model or pricing evidence is missing.'} Estimates are not provider invoices.</p></article>
    <article class="usage-widget usage-headline"><div class="usage-widget-head"><h3><span class="usage-code">M2</span> Captured tokens</h3><span class="usage-unit">${tokenComplete ? 'Complete buckets' : 'Known buckets'}</span></div><div class="usage-big">${number(tokenTotal,compact,'tokens')}</div><dl class="usage-token-buckets">${tokenBuckets.map(item=>`<div title="${escHtml(item.label)} · ${escHtml(decimal(item.records))} of ${escHtml(decimal(observed))} records"><dt><b>${item.code}</b> ${escHtml(item.label)}</dt><dd>${number(item.value,compact,'tokens')}</dd><small>${decimal(item.records)} / ${decimal(observed)} records</small></div>`).join('')}</dl><div class="usage-inline-metric"><span>Cache-read share</span>${number(m.cache_read_ratio,percent)}</div>${ratioBar(m.cache_read_ratio,1,'Cache-read token share')}<p class="usage-copy">R / (U + R + W), from ${decimal(c.complete_input_usage)} complete-input records. A token share, not a provider cache hit rate.</p></article>
    <article class="usage-widget usage-headline"><div class="usage-widget-head"><h3><span class="usage-code">M3</span> Completed turns</h3></div><div class="usage-big">${number(completedTurns)}</div><dl class="usage-facts">${fact('Mean inclusive input',m.mean_input_tokens,compact,'tokens')}${fact('Eligible usage turns',c.eligible_usage_turns)}</dl><p class="usage-copy">Input includes U + R + W. A recorded turn ending is not verified task success.</p></article>
    <article class="usage-widget usage-headline"><div class="usage-widget-head"><h3><span class="usage-code">M4</span> Paired tool calls</h3></div><div class="usage-big">${number(m.completed_tool_calls)}</div><dl class="usage-facts">${fact('Mean per eligible turn',m.mean_tool_calls_per_turn,decimal)}${fact('Maximum in a turn',m.max_tool_calls_per_turn)}</dl><p class="usage-copy">Calls with results across ${decimal(c.eligible_tool_turns)} eligible completed turns.</p><button type="button" class="usage-link" data-usage-view="tools">Inspect tool activity <span aria-hidden="true">→</span></button></article>`;
  el.onclick = event => {
    const trigger=event.target.closest('[data-usage-view]');
    if (trigger) activateTab('explore',{view:trigger.dataset.usageView});
  };
  const timeEl = document.getElementById('measurement-time');
  if (timeEl) timeEl.innerHTML = `
    <section class="usage-widget"><div class="usage-widget-head"><h3><span class="usage-code">M5</span> Elapsed time</h3><span class="usage-unit">Includes waiting</span></div><div class="usage-time-row"><span>Observed session span<small>${decimal(c.eligible_session_elapsed)} eligible sessions</small></span><dl>${fact('Mean',m.mean_session_elapsed_ms,duration,'ms')}${fact('p95',m.p95_session_elapsed_ms,duration,'ms')}</dl></div><div class="usage-time-row"><span>Prompt to final stop<small>${decimal(c.eligible_turn_elapsed)} eligible turns</small></span><dl>${fact('Mean',m.mean_turn_elapsed_ms,duration,'ms')}${fact('p95',m.p95_turn_elapsed_ms,duration,'ms')}</dl></div><p class="usage-copy">Session span is first to last captured event in this scope. Elapsed time includes waiting; it does not measure active work.</p></section>
    <section class="usage-widget"><div class="usage-widget-head"><h3><span class="usage-code">M6</span> Between-turn pauses</h3></div><div class="usage-big usage-big-small">${number(m.idle_total_ms,duration,'ms')}<span>Total observed pause</span></div><dl class="usage-facts">${fact('Longest pause',m.idle_max_ms,duration,'ms')}${fact('Over five minutes',m.idle_over_five_minutes)}</dl><p class="usage-copy">${decimal(c.eligible_idle_intervals)} eligible intervals from a final stop to the next prompt. Pauses do not establish wasted time.</p></section>`;
  const modelSummary = document.getElementById('measurement-model-summary');
  const switchRatio = c.eligible_model_transitions > 0 && known(m.model_switches) ? m.model_switches/c.eligible_model_transitions : null;
  if (modelSummary) modelSummary.innerHTML = `<dl class="usage-facts usage-switches">${fact('Model switches',m.model_switches)}${fact('Eligible adjacent pairs',c.eligible_model_transitions)}${fact('Switch rate',switchRatio,percent)}</dl>${ratioBar(m.model_switches,c.eligible_model_transitions,'Model switches among eligible adjacent turns')}<p class="usage-copy">${decimal(c.known_model_usage)} / ${decimal(observed)} usage records identify a model. Switches compare adjacent observed turns with known models.</p>`;
  const modelsEl = document.getElementById('measurement-models');
  if (modelsEl) {
    const models = [...(data.model_mix || [])].sort((a,b)=>Number(b.calls || 0)-Number(a.calls || 0));
    const row = model => `<div class="usage-model-row"><div><strong title="${escHtml(model.model || 'Model unknown')}">${escHtml(model.model || 'Model unknown')}</strong><small>${decimal(model.calls)} usage records · ${decimal(model.cost_observations)} with cost</small></div><span title="${escHtml(exact(model.cost_usd))} USD">${Number(model.cost_observations)>0 ? `${amount(model.cost_usd)} <small>USD</small>` : 'Unknown'}</span></div>`;
    modelsEl.innerHTML = models.length ? `<div class="usage-ledger-heading"><span>Model / captured usage</span><span>Known cost</span></div>${models.slice(0,5).map(row).join('')}${models.length>5 ? `<details class="usage-evidence"><summary>Show ${models.length-5} more models</summary>${models.slice(5).map(row).join('')}</details>` : ''}` : '<p class="usage-empty">No model observations are available for this scope.</p>';
  }
  const gitEl = document.getElementById('measurement-git');
  if (gitEl) {
    const operations = (git.operations || []).slice(0,10);
    const operationRow = op => `<li><div><strong>${escHtml([op.provider,op.operation].filter(Boolean).join(' ') || 'Operation')}</strong><span class="usage-operation-state">${escHtml(op.state || 'unknown')}</span><p title="${escHtml(op.repository || '')}">${escHtml(op.repository || 'Repository unknown')} · ${escHtml(op.artifact_id || 'Artifact not identified')}</p></div>${op.session_id ? `<a class="usage-link" href="${escHtml(sessionInspectionUrl({session_id:op.session_id,evidence_focus_id:op.evidence_id}))}">Inspect session <span aria-hidden="true">↗</span></a>` : '<span class="usage-copy">Session unknown</span>'}</li>`;
    gitEl.innerHTML = `<div class="usage-widget-head"><h3>Git &amp; delivery</h3><span class="usage-unit">Confirmed distinct artifacts</span></div><dl class="usage-delivery-counts">${fact('Commits',git.distinct_commits)}${fact('PRs created',git.created_pull_requests)}${fact('PRs merged',git.merged_pull_requests)}${fact('MRs created',git.created_merge_requests)}${fact('MRs merged',git.merged_merge_requests)}</dl><div class="usage-delivery-note"><span>${decimal(git.total_operations)} captured operations</span><span>${decimal(git.states ? git.states.unresolved ?? 0 : null)} unresolved</span><span>${decimal(git.undated_operations)} undated</span></div><p class="usage-copy">Captured CLI and MCP operations only. A session’s AI cost is not allocated to its commits or PRs/MRs.</p>${operations.length ? `<details class="usage-evidence"><summary>Inspect recent operations <span>${operations.length} of ${decimal(git.total_operations)}</span></summary><ol class="usage-operations">${operations.map(operationRow).join('')}</ol></details>` : `<p class="usage-empty">${known(git.total_operations) ? 'No operation details were captured in this scope.' : 'Git activity is not available yet.'}</p>`}`;
  }
  const diagnostics = document.getElementById('usage-diagnostics');
  if (diagnostics) diagnostics.ontoggle = () => {
    if (diagnostics.open) requestAnimationFrame(()=>{
      for (const id of ['eventChart','eventBarChart']) Chart.getChart(document.getElementById(id))?.resize();
    });
  };
}

/* Heatmap */
function buildHeatmap(){
  const WD = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const SHOW = [true,false,true,false,true,false,true];
  const activityTab = sqlTab('activity');
  const entries = Object.entries(activityTab.activity_by_day || {});

  const activeCounts = entries.map(e=>e[1]).filter(v=>v>0).sort((a,b)=>a-b);
  function quartile(arr, q){ return arr[Math.floor(arr.length*q)]||1; }
  const q1=quartile(activeCounts,.25), q2=quartile(activeCounts,.50), q3=quartile(activeCounts,.75);

  function cellColor(c){
    if(!c) return 'rgba(255,255,255,.04)';
    if(c<=q1) return 'rgba(242,138,26,.25)';
    if(c<=q2) return 'rgba(242,138,26,.45)';
    if(c<=q3) return 'rgba(242,138,26,.65)';
    return 'var(--blue)';
  }

  const actMap = Object.fromEntries(entries);
  const today = new Date(); today.setUTCHours(0,0,0,0);
  const allDays = [];
  for(let i=364;i>=0;i--){
    const d=new Date(today); d.setUTCDate(d.getUTCDate()-i);
    const key=d.toISOString().slice(0,10);
    allDays.push({date:key, count:actMap[key]||0});
  }
  const firstDate = new Date(allDays[0].date+'T00:00:00Z');
  const startDow  = firstDate.getUTCDay();
  const padded    = Array(startDow).fill(null).concat(allDays);
  const weeks=[];
  for(let i=0;i<padded.length;i+=7) weeks.push(padded.slice(i,i+7));

  const wdEl = document.getElementById('hm-weekdays');
  const outerEl = wdEl.closest('.panel');
  const availW = (outerEl ? outerEl.clientWidth : window.innerWidth) - 48 - 36;
  const GAP = 3;
  const cellSize = Math.max(10, Math.floor((availW - (weeks.length - 1) * GAP) / weeks.length));

  wdEl.style.width = (cellSize + 4) + 'px';
  wdEl.innerHTML = WD.map((w,i)=>`
    <div class="hm-wd-label" style="height:${cellSize}px;line-height:${cellSize}px">${SHOW[i]?w:''}</div>`).join('');

  const mEl = document.getElementById('hm-months');
  mEl.innerHTML='';
  let lastMo='';
  weeks.forEach(w=>{
    const first=w.find(c=>c);
    const mo = first ? new Date(first.date+'T00:00:00Z').toLocaleString(navigator.language || undefined,{month:'short',timeZone:'UTC'}) : '';
    const d=document.createElement('div');
    d.className='hm-month';
    d.style.width=(cellSize+GAP)+'px';
    d.textContent=(mo&&mo!==lastMo)?mo:'';
    if(mo&&mo!==lastMo) lastMo=mo;
    mEl.appendChild(d);
  });

  const gEl=document.getElementById('hm-grid');
  gEl.innerHTML='';
  gEl.style.gap=GAP+'px';
  weeks.forEach(w=>{
    const col=document.createElement('div');
    col.className='hm-week';
    col.style.gap=GAP+'px';
    for(let d=0;d<7;d++){
      const slot=w[d]??null;
      const cell=document.createElement('div');
      cell.className='hm-cell';
      cell.style.width=cell.style.height=cellSize+'px';
      cell.style.background=slot?cellColor(slot.count):'transparent';
      if(slot) cell.title=`${slot.date} (${WD[d]}): ${slot.count} events`;
      col.appendChild(cell);
    }
    gEl.appendChild(col);
  });
}

/* Hour bars */
function buildHourBars(){
  const activityTab = sqlTab('activity');
  const hours=activityTab.activity_by_hour || {};
  const total = Object.values(hours).reduce((sum, value) => sum + Number(value || 0), 0);
  const max=Math.max(...Object.values(hours),1);
  const bEl=document.getElementById('hour-bars');
  const lEl=document.getElementById('hour-labels');
  bEl.innerHTML=''; lEl.innerHTML='';
  if (!total) {
    bEl.innerHTML = '<div class="empty-note" style="width:100%">No hourly activity matches the current selection.</div>';
    return;
  }
  for(let h=0;h<24;h++){
    const c=hours[String(h)]||0;
    const bar=document.createElement('div');
    bar.className='hour-bar';
    bar.style.height=Math.max((c/max)*100,2)+'%';
    bar.style.background=(h===Number(activityTab.peak_hour ?? -1))?'var(--blue)':'rgba(255,255,255,.08)';
    bar.title=`${h12(h)}: ${c.toLocaleString()} events`;
    bEl.appendChild(bar);

    const lbl=document.createElement('div');
    lbl.className='hour-label-tick';
    lbl.textContent=(h%6===0)?h12(h):'';
    lEl.appendChild(lbl);
  }
}

/* Recorded launches and lifecycle stops are observations, not success rates. */
function buildSubagentActivity(){
  const el = document.getElementById('subagent-effectiveness');
  const toolsTab = sqlTab('tools');
  const launches = toolsTab.subagent_types_by_count || {};
  const stops = toolsTab.subagent_stops_by_type || {};
  const allTypes = [...new Set([...Object.keys(launches), ...Object.keys(stops)])]
    .sort((a, b) => Number(launches[b] || 0) - Number(launches[a] || 0) || a.localeCompare(b));
  const types = allTypes.slice(0, 12);
  if (!types.length) {
    el.innerHTML = '<p class="empty-note">No recorded subagent launches or stops in this selection.</p>';
    return;
  }
  const totalLaunches = toolsTab.subagent_total_starts ?? Object.values(launches).reduce((a,b)=>a+b,0);
  const totalStops = toolsTab.subagent_total_stops ?? Object.values(stops).reduce((a,b)=>a+b,0);
  const rows = types.map(t => `<tr>
      <td>${escHtml(t === 'unknown' ? 'Role not recorded' : t)}</td>
      <td style="text-align:right">${Number(launches[t] || 0).toLocaleString()}</td>
      <td style="text-align:right">${Number(stops[t] || 0).toLocaleString()}</td>
    </tr>`).join('');
  el.innerHTML = `<table class="data-table">
    <thead><tr>
      <th>Role / task</th>
      <th style="text-align:right">Launches</th>
      <th style="text-align:right">Recorded stops</th>
    </tr></thead>
    <tbody>${rows}</tbody>
    <tfoot><tr style="border-top:1px solid rgba(255,255,255,.1);font-weight:600">
      <td>Total</td>
      <td style="text-align:right">${totalLaunches.toLocaleString()}</td>
      <td style="text-align:right">${totalStops.toLocaleString()}</td>
    </tr></tfoot>
  </table><p class="empty-note">Stops depend on harness capture and do not verify task success. A missing stop does not establish failure.</p>${allTypes.length > types.length ? `<div class="empty-note">Showing 12 of ${fmt(allTypes.length)} recorded roles or tasks.</div>` : ''}`;
}

/* ════════════════════ TOOLS ════════════════════ */

function buildBarList(elId, obj, color){
  const entries=Object.entries(obj);
  if (!entries.length) {
    document.getElementById(elId).innerHTML = '<div class="empty-note">No activity yet.</div>';
    return;
  }
  const max=entries[0]?.[1]||1;
  document.getElementById(elId).innerHTML=entries.map(([name,count])=>`
    <div class="bar-item">
      <div class="bar-lbl" title="${escHtml(name)}">${escHtml(name)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${(count/max*100).toFixed(1)}%;background:${color}"></div></div>
      <div class="bar-cnt">${count.toLocaleString()}</div>
    </div>`).join('');
}
function buildToolsLists(){
  const toolsTab = sqlTab('tools');
  buildBarList('tools-list', toolsTab.tools_by_count || {}, P.blue);
  const sqlSkills = toolsTab.skills_by_count || {};
  const skillsOrSubagents = Object.keys(sqlSkills).length
    ? sqlSkills
    : (toolsTab.subagent_types_by_count || {});
  buildBarList('skills-list', skillsOrSubagents, P.purple);
}

/* MCP availability table */
function buildMcpPanel(){
  const mcpTab = sqlTab('mcp');
  const before = mcpTab.mcp_server_before || {};
  const after  = mcpTab.mcp_server_after  || {};
  const statusKnown = mcpTab.mcp_server_status_known || {};
  const servers = Object.keys(before);
  if (!servers.length) {
    buildBarList('mcp-list', mcpTab.mcp_servers_by_count || {}, P.teal);
    return;
  }
  servers.sort((a,b) => (before[b]||0) - (before[a]||0));
  const rows = servers.map(s => {
    const calls = before[s] || 0;
    const done  = after[s]  || 0;
    const known = Math.min(calls, statusKnown[s] || 0);
    const completeCoverage = calls > 0 && known === calls;
    const completionRate = completeCoverage ? Math.round(done / calls * 100) : null;
    const outcome = !known
      ? 'Not captured'
      : completeCoverage
        ? `${completionRate}% complete`
        : `${known.toLocaleString()}/${calls.toLocaleString()} captured`;
    const color = !known
      ? 'var(--text-3)'
      : !completeCoverage
        ? 'var(--yellow)'
        : completionRate >= 95
          ? 'var(--green)'
          : completionRate >= 80 ? 'var(--yellow)' : 'var(--red)';
    return `<tr>
      <td title="${s}">${s}</td>
      <td style="text-align:right">${calls.toLocaleString()}</td>
      <td style="text-align:right">${done.toLocaleString()}</td>
      <td style="text-align:right"><span style="color:${color};font-weight:600">${outcome}</span></td>
    </tr>`;
  }).join('');
  document.getElementById('mcp-list').innerHTML = `<table class="data-table">
    <thead><tr>
      <th>Server</th>
      <th style="text-align:right">Calls</th>
      <th style="text-align:right">Verified</th>
      <th style="text-align:right">Outcome</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

/* Weekly trends table */
function buildWeeklyTrends(){
  const wt = (sqlTab('activity').weekly_trends || []).slice(-12);
  const el = document.getElementById('weekly-trends-table');
  if (!wt.length) {
    el.innerHTML = '<div class="empty-note">At least one dated activity week is needed for a trend.</div>';
    return;
  }
  const rows = wt.map((w,i) => {
    const delta = w.delta > 0  ? `<span style="color:var(--green)">+${w.delta.toLocaleString()}</span>`
                : w.delta < 0  ? `<span style="color:var(--red)">${w.delta.toLocaleString()}</span>`
                : '<span style="color:var(--text-3)">--</span>';
    const pctV = w.delta_pct != null
      ? (w.delta_pct > 0  ? `<span style="color:var(--green)">+${w.delta_pct}%</span>`
        : w.delta_pct < 0 ? `<span style="color:var(--red)">${w.delta_pct}%</span>`
        : '<span style="color:var(--text-3)">0%</span>')
      : '<span style="color:var(--text-3)">--</span>';
    const isLatest = i === wt.length - 1;
    return `<tr${isLatest ? ' style="font-weight:600"' : ''}>
      <td>${w.week}${isLatest ? ' <span style="color:var(--blue);font-size:10px">current</span>' : ''}</td>
      <td style="text-align:right">${w.events.toLocaleString()}</td>
      <td style="text-align:right">${w.days_active}</td>
      <td style="text-align:right">${delta}</td>
      <td style="text-align:right">${pctV}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `<table class="data-table">
    <thead><tr>
      <th>Week</th>
      <th style="text-align:right">Events</th>
      <th style="text-align:right">Days Active</th>
      <th style="text-align:right">Delta Events</th>
      <th style="text-align:right">Delta %</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

/* Event distribution */
function buildEventBar(){
  const activityTab = sqlTab('activity');
  const entries=Object.entries(activityTab.events_by_type || {});
  const n=entries.length;
  const barH=28, pad=40;
  document.getElementById('event-bar-box').style.height=(n*barH+pad)+'px';
  if (!entries.length) {
    renderUsageChartEmpty('eventBarChart', 'No events match the current selection.');
    return;
  }
  new Chart(document.getElementById('eventBarChart').getContext('2d'),{
    type:'bar',
    data:{
      labels:entries.map(e=>e[0]),
      datasets:[{
        data:entries.map(e=>e[1]),
        backgroundColor:entries.map((_,i)=>COLORS[i%COLORS.length]),
        borderRadius:6,
        borderSkipped:false,
      }]
    },
    options:{
      indexAxis:'y',
      responsive:true,
      maintainAspectRatio:false,
      plugins:{
        legend:{display:false},
        tooltip:{callbacks:{label:c=>' '+c.parsed.x.toLocaleString()+' events'}}
      },
      scales:{
        x:{
          grid:{color:'rgba(255,255,255,.04)'},
          ticks:{color:'rgba(255,255,255,.4)',font:{size:11}},
        },
        y:{
          grid:{display:false},
          ticks:{color:'rgba(255,255,255,.6)',font:{size:12},maxRotation:0},
        }
      }
    }
  });
}

function renderUsageView(){
  if (D._unpublishedNavigation && exploreViewScopes.get('usage')?.scope !== exploreViewUrl('usage').url.toString()) return;
  ['modelChart', 'eventChart', 'agentCostChart', 'tokenChart', 'eventBarChart'].forEach(resetUsageChart);
  renderUsageSummary();
  renderMeasurementCoverage();
  buildCohortComparison();
  renderUsageMetrics();
  buildHeatmap();
  buildHourBars();
  buildSubagentActivity();
  buildWeeklyTrends();
  buildEventBar();
  buildAchievements();
}
renderUsageView();

/* ════════════════════ LATENCY (merged into Tools tab) ════════════════════ */

/* Percentiles table */
function buildPercentiles(){
  const pctl = sqlTab('tools').tool_percentiles || [];
  const tbody = document.getElementById('pctl-tbody');
  if (!pctl.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:var(--text-3);text-align:center">No duration data available</td></tr>';
    return;
  }
  function fmtMs(v) {
    if (v >= 1000) return (v/1000).toFixed(2) + 's';
    return v.toFixed(1);
  }
  function pctlColor(v) {
    if (v >= 10000) return 'var(--red)';
    if (v >= 5000) return 'var(--orange)';
    if (v >= 1000) return 'var(--yellow)';
    return 'var(--text)';
  }
  tbody.innerHTML = pctl.map(r => `
    <tr>
      <td><span class="mono" style="color:var(--teal)">${escHtml(r.tool)}</span></td>
      <td style="text-align:right;font-weight:600">${r.count.toLocaleString()}</td>
      <td style="text-align:right;color:${pctlColor(r.p50)}">${fmtMs(r.p50)}</td>
      <td style="text-align:right;color:${pctlColor(r.p90)}">${fmtMs(r.p90)}</td>
      <td style="text-align:right;color:${pctlColor(r.p95)}">${fmtMs(r.p95)}</td>
      <td style="text-align:right;color:${pctlColor(r.p99)};font-weight:600">${fmtMs(r.p99)}</td>
    </tr>`).join('');
}

function buildCommandPatterns(){
  const toolsTab = sqlTab('tools');
  const commands = toolsTab.top_commands || [];
  document.getElementById('cmd-unique').textContent='-- '+Number(toolsTab.unique_commands || 0)+' templates';
  const coverage = document.getElementById('cmd-coverage');
  if (coverage) coverage.textContent = toolsTab.commands_with_template == null ? ''
    : `${fmt(toolsTab.commands_with_template)} of ${fmt(toolsTab.shell_executions || 0)} recorded shell calls have command text available for templates.`;
  document.getElementById('cmd-tbody').innerHTML=commands.length ? commands.map((c,i)=>`
    <tr>
      <td style="color:var(--text-3);font-weight:600">${i+1}</td>
      <td><span class="mono" style="color:var(--teal)">${escHtml(c.command)}</span></td>
      <td style="text-align:right"><span class="run-badge">${c.count}x</span></td>
    </tr>`).join('') : '<tr><td colspan="3" class="empty-note">No captured command templates match this cohort.</td></tr>';
}

function renderToolsView(){
  if (D._unpublishedNavigation && exploreViewScopes.get('tools')?.scope !== exploreViewUrl('tools').url.toString()) return;
  buildToolsLists();
  buildMcpPanel();
  buildPercentiles();
  buildCommandPatterns();
}
renderToolsView();

/* ════════════════════ SESSION COMPARISON ════════════════════ */
(function initSessionComparison(){
  const selA = document.getElementById('cmp-a');
  const selB = document.getElementById('cmp-b');
  const result = document.getElementById('cmp-result');
  const clearBtn = document.getElementById('cmp-clear');
  const params = currentParams();

  let selIdA = null, selIdB = null;
  const initialA = params.get('cmpA');
  const initialB = params.get('cmpB');
  if (initialA) selIdA = initialA;
  if (initialB) selIdB = initialB;

  function availableSessions(){
    return (filteredDashboardSessions || D.sessions || []).filter(session => !session._navigationOnly);
  }

  function findSessionById(sessionId){
    if (!sessionId) return null;
    return (D.sessions || []).find(s => (s.full_id || s.id) === sessionId || s.id === sessionId) || null;
  }

  function buildOptions(sel, excludeId){
    const val = sel.value;
    sel.innerHTML = '<option value="">— select —</option>';
    availableSessions().forEach(s => {
      const sid = s.full_id || s.id;
      if(sid === excludeId) return;
      const opt = document.createElement('option');
      opt.value = sid;
      opt.textContent = `${s.id}  ${s.created_at ? `  ${fmtWorkflowDate(s.created_at)}` : ''}  ${fmt(s.event_count)} events`;
      if(String(sid)===String(val)) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  function syncDropdowns(){
    const availableIds = new Set(availableSessions().map(s => s.full_id || s.id));
    if (selIdA && !availableIds.has(selIdA)) selIdA = null;
    if (selIdB && !availableIds.has(selIdB)) selIdB = null;
    selA.value = selIdA!==null?selIdA:'';
    selB.value = selIdB!==null?selIdB:'';
    buildOptions(selA, selIdB);
    buildOptions(selB, selIdA);
    if(selIdA!==null) selA.value=selIdA;
    if(selIdB!==null) selB.value=selIdB;
    updateUrlParams(urlParams => {
      const sessionA = findSessionById(selIdA);
      const sessionB = findSessionById(selIdB);
      if (sessionA) urlParams.set('cmpA', sessionA.full_id || sessionA.id);
      else urlParams.delete('cmpA');
      if (sessionB) urlParams.set('cmpB', sessionB.full_id || sessionB.id);
      else urlParams.delete('cmpB');
    });
  }

  selA.addEventListener('change',()=>{
    selIdA=selA.value||null;
    syncDropdowns(); updateComparison();
  });
  selB.addEventListener('change',()=>{
    selIdB=selB.value||null;
    syncDropdowns(); updateComparison();
  });
  clearBtn.addEventListener('click',()=>{
    selIdA=null; selIdB=null;
    syncDropdowns(); updateComparison();
  });

  function updateComparison(){
    const sessionA = findSessionById(selIdA);
    const sessionB = findSessionById(selIdB);
    if(!sessionA||!sessionB){ result.innerHTML=''; return; }
    result.innerHTML = renderComparison(sessionA, sessionB);
  }

  document.addEventListener('dashboard-filters-changed', () => {
    syncDropdowns();
    updateComparison();
  });

  function deltaChip(a, b, lowerIsBetter){
    if(!a && !b) return '';
    const diff = b - a;
    if(diff === 0) return '<span style="color:var(--text-3);font-size:11px">+/-0</span>';
    const pctV = a ? Math.round(diff/a*100) : null;
    const better = lowerIsBetter ? diff < 0 : diff > 0;
    const color = better ? 'var(--green)' : 'var(--red)';
    const sign = diff > 0 ? '+' : '';
    const label = pctV !== null ? `${sign}${pctV}%` : (diff > 0 ? 'UP' : 'DOWN');
    return `<span style="color:${color};font-size:12px;font-weight:600">${label}</span>`;
  }

  function cmpTbl(headers, rows){
    const ths = headers.map((h,i) => `<th style="text-align:${i===0?'left':'right'};padding:7px 10px;font-size:12px;color:var(--text-3);font-weight:500;border-bottom:1px solid rgba(255,255,255,.07)">${h}</th>`).join('');
    const trs = rows.map(cells => `<tr>${cells.map((c,i)=>`<td style="text-align:${i===0?'left':'right'};padding:6px 10px;font-size:13px;border-bottom:1px solid rgba(255,255,255,.04);white-space:nowrap">${c}</td>`).join('')}</tr>`).join('');
    return `<table style="width:100%;border-collapse:collapse"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
  }

  function section(title, content){
    return `<div style="margin-bottom:20px">
      <div class="compare-section-title">${title}</div>
      ${content}
    </div>`;
  }

  function renderComparison(a, b){
    const cA = 'var(--blue)', cB = 'var(--purple)';
    const hA = `<span style="color:${cA};font-weight:700">A</span>`, hB = `<span style="color:${cB};font-weight:700">B</span>`;

    function card(label, vA, vB, fmt2, lowerIsBetter){
      const fvA = fmt2 ? fmt2(vA) : (typeof vA==='number' ? fmt(vA) : (vA||'--'));
      const fvB = fmt2 ? fmt2(vB) : (typeof vB==='number' ? fmt(vB) : (vB||'--'));
      const chip = (typeof vA==='number' && typeof vB==='number') ? deltaChip(vA,vB,lowerIsBetter) : '';
      return `<div class="compare-metric-card">
        <div style="font-size:11px;color:var(--signal-2);margin-bottom:6px;text-transform:uppercase;letter-spacing:.07em;font-weight:900">${label}</div>
        <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:2px">
          <span style="font-size:14px;font-weight:600;color:${cA}">${fvA}</span>
          ${chip}
        </div>
        <div style="font-size:14px;font-weight:600;color:${cB}">${fvB}</div>
      </div>`;
    }
    const inA = a.input_tokens||0, inB = b.input_tokens||0;
    const outA = a.output_tokens||0, outB = b.output_tokens||0;
    const evA = a.event_count||1, evB = b.event_count||1;
    const cards = [
      card('Events',     a.event_count, b.event_count),
      card('Duration',   a.duration_ms, b.duration_ms, fmtDur, true),
      card('Failures',   a.failure_count, b.failure_count, null, true),
      card('In Tokens',  inA, inB, fmt, true),
      card('Out Tokens', outA, outB, fmt),
      card('In/Event',   Math.round(inA/evA), Math.round(inB/evB), fmt, true),
      card('Out/Event',  Math.round(outA/evA), Math.round(outB/evB), fmt),
    ].join('');

    const allCmds = [...new Set([...Object.keys(a.commands||{}), ...Object.keys(b.commands||{})])];
    allCmds.sort((x,y) => ((b.commands[y]||0)+(a.commands[y]||0)) - ((b.commands[x]||0)+(a.commands[x]||0)));
    const cmdRows = allCmds.slice(0,15).map(c => {
      const cntA = a.commands[c]||0, cntB = b.commands[c]||0;
      const chip = deltaChip(cntA, cntB);
      const only = cntA===0 ? `<span style="color:${cB};font-size:10px">B only</span>` : cntB===0 ? `<span style="color:${cA};font-size:10px">A only</span>` : chip;
      return [`<span style="color:var(--text-2)">${c}</span>`, cntA||'--', cntB||'--', only];
    });
    const cmdTbl = cmdRows.length
      ? cmpTbl([`Command`, hA+' runs', hB+' runs', 'Delta'], cmdRows)
      : '<span style="color:var(--text-3);font-size:12px">No command data</span>';

    const allTools = [...new Set([...Object.keys(a.tool_p50||{}), ...Object.keys(b.tool_p50||{})])];
    allTools.sort((x,y) => {
      const deltaX = Math.abs((b.tool_p50||{})[x]||0) - Math.abs((a.tool_p50||{})[x]||0);
      const deltaY = Math.abs((b.tool_p50||{})[y]||0) - Math.abs((a.tool_p50||{})[y]||0);
      return deltaY - deltaX;
    });
    const latRows = allTools.slice(0,12).map(t => {
      const p50A = (a.tool_p50||{})[t], p50B = (b.tool_p50||{})[t];
      const chip = (p50A && p50B) ? deltaChip(p50A, p50B, true) : '';
      return [t, p50A ? fmtDur(p50A) : '--', p50B ? fmtDur(p50B) : '--', chip,
              a.tools[t]||'--', b.tools[t]||'--'];
    });
    const latTbl = latRows.length
      ? cmpTbl(['Tool', hA+' p50', hB+' p50', 'Delta latency', hA+' calls', hB+' calls'], latRows)
      : '<span style="color:var(--text-3);font-size:12px">No latency data</span>';

    const allModels = [...new Set([...Object.keys(a.models||{}), ...Object.keys(b.models||{})])];
    const modelRows = allModels.map(m => {
      const mA = a.models[m]||0, mB = b.models[m]||0;
      return [m.replace(/^claude-/,'').replace(/-\d{8}$/,''), mA||'--', mB||'--', deltaChip(mA,mB)];
    });
    const modelTbl = modelRows.length
      ? cmpTbl(['Model', hA, hB, 'Delta'], modelRows)
      : '<span style="color:var(--text-3);font-size:12px">No model data</span>';

    return `
      <div style="display:flex;gap:2px;margin-bottom:16px;font-size:12px">
        <span style="background:${cA};color:#100903;padding:3px 12px;border-radius:5px 0 0 5px;opacity:.9;font-weight:600">A: ${a.id}</span>
        <span style="background:${cB};color:#100903;padding:3px 12px;border-radius:0 5px 5px 0;opacity:.9;font-weight:600">B: ${b.id}</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:24px">${cards}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
        <div>
          ${section('Commands -- runs per session', cmdTbl)}
          ${section('Model usage', modelTbl)}
        </div>
        <div>
          ${section('Tool latency -- p50 per session', latTbl)}
        </div>
      </div>`;
  }

  syncDropdowns();
  updateComparison();
})();
