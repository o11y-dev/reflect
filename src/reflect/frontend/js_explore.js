/* ════════════════════ ACTIVITY ════════════════════ */

/* Heatmap */
(function buildHeatmap(){
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
})();

/* Hour bars */
(function buildHourBars(){
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
})();

/* Subagent effectiveness table */
(function buildSubagentEffectiveness(){
  const el = document.getElementById('subagent-effectiveness');
  const toolsTab = sqlTab('tools');
  const launches = toolsTab.subagent_types_by_count || {};
  const stops = toolsTab.subagent_stops_by_type || {};
  const allTypes = Object.keys(launches)
    .sort((a, b) => Number(launches[b] || 0) - Number(launches[a] || 0) || a.localeCompare(b));
  const types = allTypes.slice(0, 12);
  if (!types.length) {
    el.innerHTML = '<p style="color:var(--text-3);font-size:14px;text-align:center;padding:40px 0">No subagent data yet</p>';
    return;
  }
  const totalLaunches = toolsTab.subagent_total_starts || Object.values(launches).reduce((a,b)=>a+b,0);
  const totalStops = toolsTab.subagent_total_stops || Object.values(stops).reduce((a,b)=>a+b,0);
  const rows = types.map(t => {
    const launched = launches[t] || 0;
    const done = Math.min(stops[t] || 0, launched);
    const rate = launched > 0 ? Math.round(done / launched * 100) : 0;
    const color = rate >= 90 ? 'var(--green)' : rate >= 70 ? 'var(--yellow)' : 'var(--red)';
    return `<tr>
      <td>${escHtml(t)}</td>
      <td style="text-align:right">${launched.toLocaleString()}</td>
      <td style="text-align:right">${done.toLocaleString()}</td>
      <td style="text-align:right"><span style="color:${color};font-weight:600">${rate}%</span></td>
    </tr>`;
  }).join('');
  const totalRate = totalLaunches > 0 ? Math.round(Math.min(totalStops,totalLaunches)/totalLaunches*100) : 0;
  const totalColor = totalRate >= 90 ? 'var(--green)' : totalRate >= 70 ? 'var(--yellow)' : 'var(--red)';
  el.innerHTML = `<table class="data-table">
    <thead><tr>
      <th>Agent Type</th>
      <th style="text-align:right">Launched</th>
      <th style="text-align:right">Completed</th>
      <th style="text-align:right">Rate</th>
    </tr></thead>
    <tbody>${rows}</tbody>
    <tfoot><tr style="border-top:1px solid rgba(255,255,255,.1);font-weight:600">
      <td>Total</td>
      <td style="text-align:right">${totalLaunches.toLocaleString()}</td>
      <td style="text-align:right">${Math.min(totalStops,totalLaunches).toLocaleString()}</td>
      <td style="text-align:right"><span style="color:${totalColor}">${totalRate}%</span></td>
    </tr></tfoot>
  </table>${allTypes.length > types.length ? `<div class="empty-note" style="padding-top:10px">Showing the 12 most-launched types of ${fmt(allTypes.length)}.</div>` : ''}`;
})();

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
      <div class="bar-lbl" title="${name}">${name}</div>
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
  const servers = Object.keys(before);
  if (!servers.length) {
    buildBarList('mcp-list', mcpTab.mcp_servers_by_count || {}, P.teal);
    return;
  }
  servers.sort((a,b) => (before[b]||0) - (before[a]||0));
  const rows = servers.map(s => {
    const calls = before[s] || 0;
    const done  = after[s]  || 0;
    const avail = calls > 0 ? Math.round(done / calls * 100) : 0;
    const color = avail >= 95 ? 'var(--green)' : avail >= 80 ? 'var(--yellow)' : 'var(--red)';
    return `<tr>
      <td title="${s}">${s}</td>
      <td style="text-align:right">${calls.toLocaleString()}</td>
      <td style="text-align:right">${done.toLocaleString()}</td>
      <td style="text-align:right"><span style="color:${color};font-weight:600">${avail}%</span></td>
    </tr>`;
  }).join('');
  document.getElementById('mcp-list').innerHTML = `<table class="data-table">
    <thead><tr>
      <th>Server</th>
      <th style="text-align:right">Calls</th>
      <th style="text-align:right">Completed</th>
      <th style="text-align:right">Avail</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

/* Weekly trends table */
(function buildWeeklyTrends(){
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
})();

/* Event distribution */
(function buildEventBar(){
  const activityTab = sqlTab('activity');
  const entries=Object.entries(activityTab.events_by_type || {});
  const n=entries.length;
  const barH=28, pad=40;
  document.getElementById('event-bar-box').style.height=(n*barH+pad)+'px';
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
})();

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
      <td><span class="mono" style="color:var(--teal)">${r.tool}</span></td>
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
  document.getElementById('cmd-unique').textContent='-- '+Number(toolsTab.unique_commands || 0)+' unique patterns';
  document.getElementById('cmd-tbody').innerHTML=commands.length ? commands.map((c,i)=>`
    <tr>
      <td style="color:var(--text-3);font-weight:600">${i+1}</td>
      <td><span class="mono" style="color:var(--teal)">${escHtml(c.command)}</span></td>
      <td style="text-align:right"><span class="run-badge">${c.count}x</span></td>
    </tr>`).join('') : '<tr><td colspan="3" class="empty-note">No command patterns match this cohort.</td></tr>';
}

function renderToolsView(){
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
    return filteredDashboardSessions || D.sessions || [];
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
      opt.textContent = `${s.id}  ${s.created_at?'  '+s.created_at:''}  ${fmt(s.event_count)} events`;
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
