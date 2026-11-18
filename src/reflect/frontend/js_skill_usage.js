/* Skill evidence is loaded independently from package and installation detail. */
function skillUsageGraphMarkup(payload) {
  const esc = escHtml;
  const nodes = (payload.nodes || []).slice(0,75);
  const columns = [[],[],[],[]];
  nodes.forEach(node => columns[node.kind === 'Skill' ? 0 : node.kind === 'Repo' ? 1 : node.kind === 'Evidence' ? 3 : 2].push(node));
  const height = Math.max(180, Math.max(...columns.map(items => items.length)) * 58 + 30);
  const positions = new Map();
  columns.forEach((items,column) => items.forEach((node,index) => positions.set(node.id, {x:20+column*220,y:20+(index+.5)*(height-40)/items.length})));
  const paths = (payload.edges || []).slice(0,150).map(edge => {
    const source=positions.get(edge.source),target=positions.get(edge.target);
    if (!source || !target) return '';
    return `<path class="skill-usage-edge ${edge.evidence_class === 'inferred' ? 'inferred' : ''}" d="M${source.x+170},${source.y} C${source.x+200},${source.y} ${target.x-30},${target.y} ${target.x},${target.y}"><title>${esc(edge.kind.replaceAll('_',' '))} · ${esc(edge.evidence_class)}</title></path>`;
  }).join('');
  const boxes = nodes.map(node => {
    const point=positions.get(node.id), data=node.data || {};
    const expandable=node.kind === 'Execution' || node.kind === 'Session';
    const control=expandable ? `role="button" tabindex="0" data-skill-graph-expand="${esc(node.id)}" aria-label="Explore ${esc(node.label)}"` : '';
    const label=String(node.label || node.id);
    return `<g class="skill-usage-node ${node.kind.toLowerCase()}" transform="translate(${point.x},${point.y-18})" ${control}><title>${esc(label)}${data.agent ? ` · ${esc(data.agent)}` : ''}</title><rect width="170" height="36" rx="6"/><text x="9" y="22">${esc(label.length>23 ? label.slice(0,22)+'…' : label)}</text></g>`;
  }).join('');
  const counts=payload.counts || {}, recorded=payload.analysis_state !== 'ready';
  const count=value=>value == null ? 'Unknown' : fmt(Number(value));
  const records=(payload.records || []).map(record=> {
    const sources=(record.evidence || []).map(item=>`<li><strong>${esc(item.relationship.replaceAll('_',' '))}</strong> · ${esc(item.evidence_class)} · ${item.skill_version_id ? 'captured version' : 'version unknown'}<br><span class="skill-source-ref" translate="no">${esc(item.evidence?.path || item.evidence?.tool_call_id || item.source_id)}</span></li>`).join('');
    const link=typeof sessionInspectionUrl === 'function' ? sessionInspectionUrl({session_id:record.session_id}) : `?tab=sessions&session=${encodeURIComponent(record.session_id)}`;
    return `<tr><td>${esc(record.title || record.id)}<br><span class="skill-source-ref">${record.execution_unit_id ? 'Execution' : 'Execution unknown'} · ${esc(record.agent || 'Agent unknown')}</span><br><a href="${esc(link)}" data-related-session-link>Open session</a></td><td>${esc(record.repository || 'Repository unknown')}</td><td><ul>${sources}</ul><button type="button" class="ledger-button" data-skill-graph-expand="${esc(record.id)}">Inspect ${count(record.occurrence_count)} references</button></td></tr>`;
  }).join('');
  const versionOptions=[{id:'',label:'All versions'},{id:'unknown',label:'Version unknown'},...(payload.versions || [])].map(item=>`<option value="${esc(item.id)}" ${item.id === (payload.scope?.version || '') ? 'selected' : ''}>${esc(item.label)}</option>`).join('');
  const scope=payload.scope || {};
  const scopeLabel=scope.days === null ? 'All time' : scope.days ? `Last ${scope.days} days` : 'Selected range';
  return `<div class="skill-usage-summary"><span>${count(counts.executions)} executions</span><span>${count(counts.sessions)} sessions</span><span>${count(counts.occurrences)} references</span><label>Version <select data-skill-graph-version>${versionOptions}</select></label></div>
    <p class="review-panel-copy">${esc(scopeLabel)} · ${esc(scope.agents?.length ? scope.agents.join(', ') : 'All agents')} · ${scope.repo_id ? 'Selected repository' : 'All repositories'}</p>
    <p class="review-panel-copy">${recorded ? 'Recorded evidence; current analysis is updating. ' : ''}Invoked, loaded, reported, and inferred references are distinct. A reference does not prove the procedure was followed or caused an outcome.</p>
    ${(payload.records || []).length ? `<div class="skill-usage-canvas"><svg viewBox="0 0 880 ${height}" aria-label="Where this skill was referenced" role="img">${paths}${boxes}</svg></div><details class="skill-usage-table" open><summary>Evidence table</summary><table><thead><tr><th>Execution / session</th><th>Repository</th><th>Captured references</th></tr></thead><tbody>${records}</tbody></table></details>` : `<div class="session-ledger-empty">${recorded ? 'No recorded references in this range; analysis is pending.' : 'No captured skill references in this range.'}</div>`}
    <div class="ledger-actions"><button type="button" class="ledger-button" data-skill-graph-reset>All uses in this scope</button>${payload.pagination?.next_offset != null ? `<button type="button" class="ledger-button" data-skill-graph-next="${payload.pagination.next_offset}">Next executions</button>` : ''}${payload.pagination?.next_evidence_offset != null ? `<button type="button" class="ledger-button" data-skill-evidence-next="${payload.pagination.next_evidence_offset}">Next references</button>` : ''}</div>`;
}

function renderSkillUsageGraph(container, payload) {
  container.innerHTML = skillUsageGraphMarkup(payload);
}

async function loadSkillUsageGraph(skillId, container, filters = {}) {
  if (!container) return;
  container._skillUsageAbort?.abort();
  const controller=new AbortController(), requestId=(container._skillUsageRequest || 0)+1;
  container._skillUsageAbort=controller;container._skillUsageRequest=requestId;
  const timeout=window.setTimeout(()=>controller.abort(),10000);
  const params=typeof currentParams === 'function' ? currentParams() : new URLSearchParams(window.location.search);
  const range=params.get('range') || '7d';
  const query=new URLSearchParams({days:range === 'all' ? 'all' : range === '24h' ? '1' : /^\d+d$/.test(range) ? range.slice(0,-1) : '7'});
  if (params.get('agents')) query.set('agents',params.get('agents'));
  if (params.get('repo')) query.set('repo',params.get('repo'));
  Object.entries(filters).forEach(([key,value])=>{if(value != null && value !== '') query.set(key,String(value));});
  if (!container._skillUsagePayload) container.innerHTML='<p class="inbox-copy" role="status">Loading skill references…</p>';
  try {
    const response=await reportReadResponse(`/api/skills/${encodeURIComponent(skillId)}/usage-graph?${query}`,{headers:{Accept:'application/json'},signal:controller.signal});
    const payload=await response.json();
    if(!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    if(container._skillUsageRequest !== requestId || !container.isConnected) return;
    container._skillUsagePayload=payload;
    renderSkillUsageGraph(container,payload);
    if (isStaticDemo()) {
      container.querySelectorAll('select,button,[data-skill-graph-expand]').forEach(control => {
        control.setAttribute('aria-disabled','true');
        control.removeAttribute('tabindex');
        if ('disabled' in control) control.disabled = true;
      });
      const note = document.createElement('p'); note.className = 'review-panel-copy';
      note.textContent = 'Illustrative sample · all included references. Interactive graph filters are available in your local report.';
      container.prepend(note);
      return;
    }
    const expand=id=>{
      const record=(payload.records || []).find(item=>item.id===id);
      if(!record) return;
      loadSkillUsageGraph(skillId,container,{...filters,offset:0,evidence_offset:0,
        execution:record.execution_unit_id || '',session:record.execution_unit_id ? '' : record.session_id});
    };
    container.querySelectorAll('[data-skill-graph-expand]').forEach(button=>{
      button.onclick=()=>expand(button.dataset.skillGraphExpand);
      button.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();expand(button.dataset.skillGraphExpand);}};
    });
    container.querySelector('[data-skill-graph-next]')?.addEventListener('click',event=>loadSkillUsageGraph(skillId,container,{...filters,offset:event.currentTarget.dataset.skillGraphNext}));
    container.querySelector('[data-skill-evidence-next]')?.addEventListener('click',event=>loadSkillUsageGraph(skillId,container,{...filters,evidence_offset:event.currentTarget.dataset.skillEvidenceNext}));
    container.querySelector('[data-skill-graph-reset]')?.addEventListener('click',()=>{const {execution,session,offset,evidence_offset,...scope}=filters;loadSkillUsageGraph(skillId,container,scope);});
    container.querySelector('[data-skill-graph-version]')?.addEventListener('change',event=>loadSkillUsageGraph(skillId,container,{...filters,version:event.target.value,offset:0,evidence_offset:0}));
  } catch(error) {
    if(container._skillUsageRequest !== requestId || !container.isConnected) return;
    container.querySelector('[data-skill-graph-error]')?.remove();
    const notice=document.createElement('div');notice.className='inbox-copy';notice.setAttribute('role','status');
    notice.setAttribute('data-skill-graph-error','');
    notice.textContent=`Skill references could not update: ${error.message}. `;
    const retry=document.createElement('button');retry.className='ledger-button';retry.textContent='Retry';retry.onclick=()=>loadSkillUsageGraph(skillId,container,filters);
    notice.appendChild(retry);container.prepend(notice);
  } finally {window.clearTimeout(timeout);}
}
