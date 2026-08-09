/* ════════════════════ GRAPHS ════════════════════ */

/* Tool color map */
const TOOL_COLORS = {};
(function assignToolColors(){
  const graphTab = sqlTab('graph');
  const co = graphTab.graph_cooccurrence || {};
  const dep = graphTab.graph_dep || {};
  const toolList = (co.tools || []).concat(
    (dep.nodes || []).map(n => n.id)
  );
  toolList.forEach((t, i) => {
    if (!(t in TOOL_COLORS)) TOOL_COLORS[t] = COLORS[i % COLORS.length];
  });
})();

function svgWidth(el) {
  return el.clientWidth > 0 ? el.clientWidth : (window.innerWidth - 96);
}

function initGraphs() {

/* ── 1. Tool Flow — manual arc diagram ── */
(function buildSankey(){
  const el = document.getElementById('sankey-svg');
  const trans = sqlTab('graph').graph_tool_transitions || [];
  if (!el || !trans.length) {
    if (el) d3.select(el).selectAll('*').remove();
    return;
  }
  const W = svgWidth(el.parentElement) - 48;
  const H = 360;
  el.setAttribute('viewBox', `0 0 ${W} ${H}`);
  el.setAttribute('width', W);
  el.setAttribute('height', H);

  const flow = {};
  trans.forEach(({from, to, count}) => {
    flow[from] = (flow[from] || 0) + count;
    flow[to]   = (flow[to]   || 0) + count;
  });
  const nodes = Object.entries(flow).sort((a,b) => b[1]-a[1]).slice(0,16).map(([n]) => n);
  const nodeSet = new Set(nodes);
  const filtered = trans.filter(t => nodeSet.has(t.from) && nodeSet.has(t.to));
  if (!filtered.length) return;

  const nodeH = Math.max(12, Math.min(28, (H - 40) / nodes.length - 4));
  const nodeW = 12;
  const labelW = 110;
  const leftX  = labelW + 8;
  const rightX = W - labelW - nodeW - 8;

  const sources = [...new Set(filtered.map(t => t.from))].filter(n => nodeSet.has(n));
  const targets = [...new Set(filtered.map(t => t.to))].filter(n => nodeSet.has(n));

  const leftNodes  = sources.slice(0, 12);
  const rightNodes = targets.slice(0, 12);

  function yPos(list, name) {
    const idx = list.indexOf(name);
    if (idx < 0) return -1;
    const spacing = (H - 40) / Math.max(list.length, 1);
    return 20 + idx * spacing + spacing / 2;
  }

  const svg = d3.select(el);
  svg.selectAll('*').remove();

  const maxCount = Math.max(...filtered.map(t => t.count), 1);

  filtered.slice(0, 30).forEach(({from, to, count}) => {
    const y1 = yPos(leftNodes, from);
    const y2 = yPos(rightNodes, to);
    if (y1 < 0 || y2 < 0) return;
    const x1 = leftX + nodeW;
    const x2 = rightX;
    const mx  = (x1 + x2) / 2;
    const strokeW = Math.max(1, (count / maxCount) * 12);
    const color = TOOL_COLORS[from] || P.blue;
    svg.append('path')
      .attr('d', `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`)
      .attr('fill', 'none')
      .attr('stroke', color)
      .attr('stroke-width', strokeW)
      .attr('stroke-opacity', 0.4)
      .append('title').text(`${from} -> ${to}: ${count}`);
  });

  const truncate = (s, max) => s.length > max ? s.slice(0, max - 1) + '…' : s;

  leftNodes.forEach(name => {
    const y = yPos(leftNodes, name);
    const color = TOOL_COLORS[name] || P.blue;
    svg.append('rect').attr('x', leftX).attr('y', y - nodeH/2)
      .attr('width', nodeW).attr('height', nodeH)
      .attr('fill', color).attr('rx', 3);
    svg.append('text').attr('x', leftX - 6).attr('y', y).attr('dy','0.35em')
      .attr('text-anchor','end').attr('font-size', 11)
      .attr('fill','rgba(255,255,255,.7)').text(truncate(name, 14));
  });

  rightNodes.forEach(name => {
    const y = yPos(rightNodes, name);
    const color = TOOL_COLORS[name] || P.blue;
    svg.append('rect').attr('x', rightX).attr('y', y - nodeH/2)
      .attr('width', nodeW).attr('height', nodeH)
      .attr('fill', color).attr('rx', 3);
    svg.append('text').attr('x', rightX + nodeW + 6).attr('y', y).attr('dy','0.35em')
      .attr('text-anchor','start').attr('font-size', 11)
      .attr('fill','rgba(255,255,255,.7)').text(truncate(name, 14));
  });
})();

/* ── 2. Dependency Graph (force-directed) ── */
(function buildDepGraph(){
  const dep = sqlTab('graph').graph_dep;
  const noteEl = document.getElementById('dep-note');
  if (!dep) return;

  const isolated = dep.isolated_agents || [];
  if (isolated.length) {
    const names = isolated.map(agent => `${agent.id} (${fmt(agent.events)} events)`).join(', ');
    noteEl.textContent = `No dependency activity captured for ${names}. They are omitted from the graph until tool or MCP usage is observed.`;
  } else {
    noteEl.textContent = '';
  }

  if (!dep.nodes.length || !dep.links.length) {
    document.getElementById('dep-svg').outerHTML =
      '<div style="height:440px;display:flex;align-items:center;justify-content:center;color:var(--text-3);font-size:13px">No agent-to-tool activity captured for this report.</div>';
    return;
  }

  const el = document.getElementById('dep-svg');
  const W = svgWidth(el.parentElement) - 40;
  const agentNodes = dep.nodes
    .filter(n => n.type === 'agent')
    .sort((a, b) => b.size - a.size);
  const toolNodes = dep.nodes
    .filter(n => n.type === 'tool')
    .sort((a, b) => b.size - a.size);
  const mcpToolNodes = dep.nodes
    .filter(n => n.type === 'mcp_tool')
    .sort((a, b) => b.size - a.size);
  const mcpServerNodes = dep.nodes
    .filter(n => n.type === 'mcp_server')
    .sort((a, b) => b.size - a.size);
  const maxRows = Math.max(agentNodes.length, toolNodes.length, mcpToolNodes.length, mcpServerNodes.length, 1);
  const H = Math.max(440, 120 + maxRows * 36);
  el.style.height = `${H}px`;
  el.setAttribute('viewBox', `0 0 ${W} ${H}`);

  const typeColor = { agent: P.purple, tool: P.blue, mcp_tool: P.teal, mcp_server: P.orange };
  const labelFor = (id) => id.replace(/^MCP:/, '').replace(/^mcp:/, '');
  const pillWidth = (d) => Math.max(110, Math.min(220, 34 + labelFor(d.id).length * 7 + String(fmt(d.size)).length * 7));
  const pillHeight = 24;
  const columnLayout = (items, x, top, bottom) => {
    const span = Math.max(bottom - top, 1);
    const step = span / Math.max(items.length, 1);
    return items.map((item, index) => ({
      ...item,
      x,
      y: top + step * index + step / 2,
      w: pillWidth(item),
      h: pillHeight,
      label: labelFor(item.id),
    }));
  };

  const svg = d3.select(el);
  svg.selectAll('*').remove();

  svg.append('rect')
    .attr('x', 0).attr('y', 0).attr('width', W).attr('height', H)
    .attr('rx', 18)
    .attr('fill', 'rgba(255,255,255,.015)');

  const top = 86;
  const bottom = H - 32;
  const hasMcpTools = mcpToolNodes.length > 0;
  const hasMcpServers = mcpServerNodes.length > 0;
  const agentX = 120;
  const toolX = hasMcpTools ? Math.round(W * 0.36) : Math.round(W * 0.68);
  const mcpToolX = hasMcpTools ? (hasMcpServers ? Math.round(W * 0.62) : Math.round(W - 150)) : Math.round(W - 150);
  const mcpServerX = Math.round(W - 120);

  const metricForNode = (node) => {
    const raw = node && (node.size ?? node.value ?? node.count ?? node.events ?? 0);
    return Number.isFinite(Number(raw)) ? Number(raw) : 0;
  };

  const laidOut = [
    ...columnLayout(agentNodes, agentX, top, bottom),
    ...columnLayout(toolNodes, toolX, top, bottom),
    ...columnLayout(mcpToolNodes, mcpToolX, top, bottom),
    ...columnLayout(mcpServerNodes, mcpServerX, top, bottom),
  ];
  const byId = new Map(laidOut.map(node => [node.id, node]));
  const links = dep.links
    .map(link => ({ ...link, sourceNode: byId.get(link.source), targetNode: byId.get(link.target) }))
    .filter(link => link.sourceNode && link.targetNode);

  const headings = [
    { label: 'Agents', x: agentX },
    { label: 'Tools', x: toolX },
  ];
  if (hasMcpTools) headings.push({ label: 'MCP Tools', x: mcpToolX });
  if (hasMcpServers) headings.push({ label: 'MCP Servers', x: mcpServerX });
  svg.append('g')
    .selectAll('text')
    .data(headings)
    .join('text')
      .attr('x', d => d.x)
      .attr('y', 34)
      .attr('text-anchor', 'middle')
      .attr('fill', 'rgba(255,255,255,.32)')
      .attr('font-size', 11)
      .attr('font-weight', 700)
      .attr('letter-spacing', '.18em')
      .text(d => d.label.toUpperCase());

  svg.append('g')
    .selectAll('path')
    .data(links)
    .join('path')
      .attr('fill', 'none')
      .attr('stroke', d => {
        if (d.targetNode.type === 'mcp_server') return 'rgba(255,159,10,.28)';
        if (d.targetNode.type === 'mcp_tool') return 'rgba(255,177,86,.28)';
        return 'rgba(255,177,86,.26)';
      })
      .attr('stroke-width', d => Math.max(1.2, Math.min(5, 1 + Math.log2(d.value + 1))))
      .attr('stroke-linecap', 'round')
      .attr('d', d => {
        const sx = d.sourceNode.x + d.sourceNode.w / 2 - 6;
        const sy = d.sourceNode.y;
        const tx = d.targetNode.x - d.targetNode.w / 2 + 6;
        const ty = d.targetNode.y;
        const cx = (sx + tx) / 2;
        return `M ${sx} ${sy} C ${cx} ${sy}, ${cx} ${ty}, ${tx} ${ty}`;
      });

  const nodeEl = svg.append('g')
    .selectAll('g')
    .data(laidOut)
    .join('g')
      .attr('transform', d => `translate(${d.x - d.w / 2}, ${d.y - d.h / 2})`);

  nodeEl.append('rect')
    .attr('width', d => d.w)
    .attr('height', d => d.h)
    .attr('rx', 12)
    .attr('fill', d => {
      if (d.type === 'agent') return 'rgba(215,209,198,.18)';
      if (d.type === 'mcp_tool') return 'rgba(255,177,86,.14)';
      if (d.type === 'mcp_server') return 'rgba(255,159,10,.14)';
      return 'rgba(242,138,26,.14)';
    })
    .attr('stroke', d => typeColor[d.type] || P.blue)
    .attr('stroke-width', d => d.type === 'agent' ? 1.6 : 1.2);

  nodeEl.append('text')
    .attr('x', 12)
    .attr('y', 15)
    .attr('fill', 'rgba(255,255,255,.9)')
    .attr('font-size', 11.5)
    .attr('font-weight', 600)
    .text(d => d.label.slice(0, 24));

  nodeEl.append('text')
    .attr('x', d => d.w - 10)
    .attr('y', 15)
    .attr('text-anchor', 'end')
    .attr('fill', 'rgba(255,255,255,.42)')
    .attr('font-size', 10.5)
    .text(d => fmt(metricForNode(d)));

  nodeEl.append('title')
    .text(d => `${d.label} -- ${fmt(metricForNode(d))} ${d.type === 'agent' ? 'events' : 'calls'}`);

  const serverSummary = dep.top_mcp_servers || [];
  if (serverSummary.length) {
    const serverText = serverSummary
      .map(server => {
        const label = server.id ?? server.server ?? '';
        const count = server.events ?? server.count ?? server.value ?? 0;
        return label ? `${label} (${fmt(count)})` : '';
      })
      .filter(Boolean)
      .join(', ');
    noteEl.textContent = `${noteEl.textContent ? `${noteEl.textContent} ` : ''}Top MCP servers: ${serverText}.`;
  }
})();

/* ── 3. Behavioral Memory Graph ── */
(function buildSemanticGraph(){
  const initialGraph = sqlTab('graph').graph_semantic || { nodes: [], edges: [], sessions: [], legend: [] };
  const shell = document.getElementById('semantic-graph-shell');
  const svgEl = document.getElementById('semantic-graph-svg');
  const filter = document.getElementById('semantic-session-filter');
  const mode = document.getElementById('semantic-graph-mode');
  const context = document.getElementById('semantic-graph-context');
  const legend = document.getElementById('semantic-graph-legend');
  const tip = document.getElementById('semantic-graph-tip');
  if (!shell || !svgEl || !filter || !mode || !context || !legend || !tip) return;
  if (!initialGraph.nodes.length) {
    shell.outerHTML = '<div class="semantic-graph-empty">No semantic graph rows found. Run reflect, then use reflect memory sync . to add local folder memories.</div>';
    filter.innerHTML = '<option value="">All sessions</option>';
    mode.disabled = true;
    legend.innerHTML = '';
    return;
  }

  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  let allNodes = [];
  let allEdges = [];
  let nodeById = new Map();
  let adjacency = new Map();
  const knownSessions = new Set(initialGraph.sessions || []);
  const edgeColor = {
    ran_in_workspace: 'rgba(242,138,26,.58)',
    ran_session: 'rgba(255,177,86,.42)',
    spawned_session: 'rgba(255,209,102,.48)',
    touched_folder: 'rgba(100,199,161,.34)',
    contains_touched_path: 'rgba(149,213,178,.26)',
    touched_path: 'rgba(149,213,178,.24)',
    described_by_path: 'rgba(255,141,125,.30)',
    used_skill: 'rgba(199,156,255,.34)',
    spawned_subagent: 'rgba(255,209,102,.36)',
    achieved_outcome: 'rgba(255,107,107,.38)',
  };

  function sessionAgentName(sessionId) {
    const sessionNode = allNodes.find(node => node.kind === 'Session' && node.session_id === sessionId);
    if (!sessionNode) return '';
    for (const edge of adjacency.get(sessionNode.id) || []) {
      if (edge.kind !== 'ran_session') continue;
      const neighbor = nodeById.get(edge.source === sessionNode.id ? edge.target : edge.source);
      if (neighbor?.kind === 'Agent') return String(neighbor.label || '');
    }
    return '';
  }

  function syncSessionOptions(selectedSession = '') {
    const options = [...knownSessions].sort().map(session => {
      const agent = sessionAgentName(session);
      const label = agent ? `${agent} · ${session}` : session;
      return `<option value="${esc(session)}">${esc(label)}</option>`;
    });
    filter.innerHTML = ['<option value="">All sessions</option>', ...options].join('');
    filter.value = knownSessions.has(selectedSession) ? selectedSession : '';
  }

  function useGraphData(nextGraph, selectedSession = '') {
    allNodes = (nextGraph.nodes || []).map(node => ({...node}));
    allEdges = (nextGraph.edges || []).map(edge => ({...edge}));
    nodeById = new Map(allNodes.map(node => [node.id, node]));
    adjacency = new Map();
    for (const edge of allEdges) {
      if (!adjacency.has(edge.source)) adjacency.set(edge.source, []);
      if (!adjacency.has(edge.target)) adjacency.set(edge.target, []);
      adjacency.get(edge.source).push(edge);
      adjacency.get(edge.target).push(edge);
    }
    for (const session of nextGraph.sessions || []) knownSessions.add(session);
    syncSessionOptions(selectedSession);
    legend.innerHTML = (nextGraph.legend || []).map(item =>
      `<div class="graph-legend-item"><div class="graph-legend-dot" style="background:${esc(item.color)}"></div>${esc(item.kind)}</div>`
    ).join('');
  }

  useGraphData(initialGraph);

  function workspaceSessionIds(sessionId) {
    const sessionIds = new Set([sessionId]);
    const sessionNodes = allNodes.filter(node => node.kind === 'Session' && node.session_id === sessionId);
    const workspaceIds = new Set();
    for (const sessionNode of sessionNodes) {
      for (const edge of adjacency.get(sessionNode.id) || []) {
        if (edge.kind !== 'ran_in_workspace') continue;
        workspaceIds.add(edge.source === sessionNode.id ? edge.target : edge.source);
      }
    }
    for (const workspaceId of workspaceIds) {
      for (const edge of adjacency.get(workspaceId) || []) {
        if (edge.kind !== 'ran_in_workspace') continue;
        const neighborId = edge.source === workspaceId ? edge.target : edge.source;
        const neighbor = nodeById.get(neighborId);
        if (neighbor?.kind === 'Session' && neighbor.session_id) sessionIds.add(neighbor.session_id);
      }
    }
    return sessionIds;
  }

  function visibleIdsForSessions(sessionIds) {
    if (!sessionIds.size) return new Set(allNodes.map(node => node.id));
    const nodeIds = new Set();
    const frontier = [];
    for (const node of allNodes) {
      if (sessionIds.has(node.session_id)) {
        nodeIds.add(node.id);
        frontier.push(node.id);
      }
    }
    for (const edge of allEdges) {
      if (sessionIds.has(edge.session_id)) {
        if (!nodeIds.has(edge.source)) frontier.push(edge.source);
        if (!nodeIds.has(edge.target)) frontier.push(edge.target);
        nodeIds.add(edge.source);
        nodeIds.add(edge.target);
      }
    }
    while (frontier.length) {
      const current = frontier.pop();
      for (const edge of adjacency.get(current) || []) {
        if (edge.session_id && !sessionIds.has(edge.session_id)) continue;
        const neighbor = edge.source === current ? edge.target : edge.source;
        if (nodeIds.has(neighbor)) continue;
        nodeIds.add(neighbor);
        frontier.push(neighbor);
      }
    }
    return nodeIds;
  }

  function agentSummary(sessionIds) {
    const counts = new Map();
    for (const sessionId of sessionIds) {
      const agent = sessionAgentName(sessionId);
      if (agent) counts.set(agent, (counts.get(agent) || 0) + 1);
    }
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([agent, count]) => `${agent} ${count}`)
      .join(' · ');
  }

  function nodeRadius(kind) {
    return {Agent:12, Session:9, Workspace:11, Repo:10, Folder:9, Path:5, Memory:10, Tool:9, ToolCall:5, MCPServer:10, Skill:11, Subagent:11, Outcome:12, Step:4}[kind] || 7;
  }

  function nodeCharge(kind) {
    return {Agent:-520, Session:-360, Workspace:-460, Repo:-420, Folder:-260, Path:-80, Memory:-300, Tool:-260, ToolCall:-70, MCPServer:-280, Skill:-340, Subagent:-340, Outcome:-360}[kind] || -160;
  }

  function shortLabel(node) {
    const label = String(node.label || '');
    if (label.length <= 26) return label;
    const parts = label.split('/');
    return parts.length > 1 ? parts.slice(-2).join('/') : `${label.slice(0, 25)}...`;
  }

  function showTip(node, event) {
    const attrs = node.attrs || {};
    const rows = [
      ['kind', node.kind],
      ['session', node.session_id],
      ['workspace', attrs.workspace_id],
      ['relative path', attrs.relative_path],
      ['type', attrs.type || attrs.kind || attrs.tool_type],
      ['status', attrs.status],
      ['scope', attrs.scope],
      ['source', attrs.source],
      ['first seen', node.first_seen_at],
      ['last seen', node.last_seen_at],
    ].filter(([, value]) => value !== undefined && value !== null && String(value) !== '');
    tip.innerHTML = `<strong>${esc(node.label)}</strong><dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
    const bounds = shell.getBoundingClientRect();
    tip.style.left = `${Math.min(Math.max(event.clientX - bounds.left + 14, 12), bounds.width - 280)}px`;
    tip.style.top = `${Math.min(Math.max(event.clientY - bounds.top + 14, 12), bounds.height - 120)}px`;
    tip.classList.add('is-visible');
  }

  const svg = d3.select(svgEl);
  const viewport = svg.append('g');
  const linkLayer = viewport.append('g');
  const nodeLayer = viewport.append('g');
  const zoom = d3.zoom()
    .scaleExtent([.18, 3.5])
    .on('zoom', event => viewport.attr('transform', event.transform));
  svg.call(zoom);

  let simulation = null;
  let activeSession = '';
  let activeMode = 'workspace';

  function render(sessionId, relationshipMode = activeMode) {
    activeSession = sessionId || '';
    activeMode = relationshipMode || 'workspace';
    const focusedSessionIds = !activeSession
      ? new Set()
      : activeMode === 'workspace'
        ? workspaceSessionIds(activeSession)
        : new Set([activeSession]);
    const selectedIds = visibleIdsForSessions(focusedSessionIds);
    const nodes = allNodes.filter(node => selectedIds.has(node.id));
    const visibleNodeIds = new Set(nodes.map(node => node.id));
    const links = allEdges
      .filter(edge => visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target))
      .filter(edge => !activeSession || !edge.session_id || focusedSessionIds.has(edge.session_id))
      .map(edge => ({...edge, source: nodeById.get(edge.source), target: nodeById.get(edge.target)}))
      .filter(edge => edge.source && edge.target);
    context.textContent = !activeSession
      ? 'All bounded graph relationships. Select a session to inspect its workspace peers.'
      : activeMode === 'workspace'
        ? `${focusedSessionIds.size} session(s) connected through the same canonical workspace${agentSummary(focusedSessionIds) ? ` · ${agentSummary(focusedSessionIds)}` : ''}.`
        : 'Only relationships attributed to the selected session are shown.';

    const rect = shell.getBoundingClientRect();
    const W = Math.max(rect.width, 640);
    const H = Math.max(rect.height, 420);
    svgEl.setAttribute('viewBox', `0 0 ${W} ${H}`);

    if (simulation) simulation.stop();
    linkLayer.selectAll('*').remove();
    nodeLayer.selectAll('*').remove();

    const linksSel = linkLayer.selectAll('line')
      .data(links)
      .join('line')
      .attr('class', 'semantic-graph-link')
      .attr('stroke', d => edgeColor[d.kind] || 'rgba(245,242,234,.16)')
      .attr('stroke-width', d => Math.max(.8, Math.min(3.4, 1 + Math.log2((d.weight || d.value || 1) + 1) * .45)));

    linksSel.append('title').text(d => `${d.kind || 'related'}: ${d.source.label || d.source.id} -> ${d.target.label || d.target.id}`);

    const nodeSel = nodeLayer.selectAll('g')
      .data(nodes, d => d.id)
      .join('g')
      .attr('class', 'semantic-graph-node')
      .call(d3.drag()
        .on('start', (event, d) => {
          if (!event.active) simulation.alphaTarget(.25).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on('drag', (event, d) => {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on('end', (event, d) => {
          if (!event.active) simulation.alphaTarget(0);
          d.fx = null;
          d.fy = null;
        }));

    nodeSel.append('circle')
      .attr('r', d => nodeRadius(d.kind))
      .attr('fill', d => d.color || '#d7d1c6');

    nodeSel.filter(d => ['Agent','Session','Workspace','Repo','Folder','Skill','Subagent','Outcome','Memory'].includes(d.kind))
      .append('text')
      .attr('x', d => nodeRadius(d.kind) + 5)
      .attr('y', 4)
      .text(shortLabel);

    nodeSel.append('title').text(d => `${d.kind}: ${d.label}`);
    nodeSel
      .on('pointerenter', function(event, d) {
        d3.select(this).classed('is-hovered', true).raise();
        showTip(d, event);
      })
      .on('pointermove', (event, d) => showTip(d, event))
      .on('pointerleave', function() {
        d3.select(this).classed('is-hovered', false);
        tip.classList.remove('is-visible');
      });

    simulation = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id(d => d.id).distance(d => {
        if (d.kind === 'contains_touched_path') return 34;
        if (d.kind === 'touched_folder') return 46;
        if (d.kind === 'used_skill' || d.kind === 'spawned_subagent') return 70;
        return 56;
      }).strength(.42))
      .force('charge', d3.forceManyBody().strength(d => nodeCharge(d.kind)))
      .force('center', d3.forceCenter(W / 2, H / 2))
      .force('collide', d3.forceCollide().radius(d => nodeRadius(d.kind) + 8).iterations(2))
      .force('x', d3.forceX(d => {
        const lanes = {Agent:.09, Session:.19, Workspace:.29, Repo:.37, Folder:.44, Path:.51, Memory:.58, Tool:.66, ToolCall:.72, MCPServer:.78, Skill:.84, Subagent:.90, Outcome:.94};
        return W * (lanes[d.kind] || .5);
      }).strength(.045))
      .force('y', d3.forceY(H / 2).strength(.035))
      .on('tick', () => {
        linksSel
          .attr('x1', d => d.source.x)
          .attr('y1', d => d.source.y)
          .attr('x2', d => d.target.x)
          .attr('y2', d => d.target.y);
        nodeSel.attr('transform', d => `translate(${d.x},${d.y})`);
      });

    setTimeout(() => {
      const bounds = viewport.node()?.getBBox?.();
      if (!bounds || !Number.isFinite(bounds.width) || !bounds.width) return;
      const scale = Math.min(1.1, .88 / Math.max(bounds.width / W, bounds.height / H));
      const tx = W / 2 - (bounds.x + bounds.width / 2) * scale;
      const ty = H / 2 - (bounds.y + bounds.height / 2) * scale;
      svg.transition().duration(450).call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
    }, 380);
  }

  let workspaceRequest = 0;
  filter.addEventListener('change', async event => {
    const selectedSession = event.target.value;
    const requestId = ++workspaceRequest;
    mode.disabled = !selectedSession;
    if (!selectedSession || !D.sql_backed || !reportSupportsServerFiltering()) {
      useGraphData(initialGraph, selectedSession);
      render(selectedSession, mode.value);
      return;
    }
    context.textContent = 'Loading every session in this canonical workspace...';
    try {
      const url = new URL('/api/explore/graph', window.location.href);
      url.searchParams.set('session', selectedSession);
      const response = await fetch(url.toString(), {headers: {Accept: 'application/json'}});
      if (!response.ok) throw new Error('workspace-graph-fetch-failed');
      const payload = await response.json();
      if (requestId !== workspaceRequest) return;
      useGraphData(payload.graph_semantic || initialGraph, selectedSession);
      mode.disabled = false;
      render(selectedSession, mode.value);
    } catch (error) {
      if (requestId !== workspaceRequest) return;
      useGraphData(initialGraph, selectedSession);
      render(selectedSession, mode.value);
      context.textContent = 'Workspace expansion was unavailable; showing the bounded local graph snapshot.';
    }
  });
  mode.addEventListener('change', event => render(activeSession, event.target.value));
  mode.disabled = true;
  window.addEventListener('resize', () => render(activeSession, activeMode));
  render('');
})();
/* ── 3. Session Timeline ── */
(function buildTimeline(){
  const sessions = sqlTab('graph').graph_session_timeline || [];
  if (!sessions.length) {
    document.getElementById('timeline-wrap').innerHTML =
      '<p style="color:var(--text-3);font-size:13px">No timeline data</p>';
    return;
  }

  const allTools = new Set();
  sessions.forEach(s => s.spans.forEach(sp => allTools.add(sp.tool)));
  const toolList = [...allTools];
  const tColor = {};
  toolList.forEach((t, i) => tColor[t] = COLORS[i % COLORS.length]);

  const maxT = Math.max(...sessions.map(s => {
    const last = s.spans[s.spans.length - 1];
    return last ? last.t + (last.dur || 0) : 0;
  }), 1);

  const wrap = document.getElementById('timeline-wrap');
  wrap.innerHTML = '';

  sessions.forEach(sess => {
    const lane = document.createElement('div');
    lane.className = 'timeline-lane';

    const sid = document.createElement('div');
    sid.className = 'timeline-sid';
    sid.textContent = sess.session;
    lane.appendChild(sid);

    const track = document.createElement('div');
    track.className = 'timeline-track';

    sess.spans.forEach(sp => {
      const bar = document.createElement('div');
      bar.className = 'timeline-span';
      const left = (sp.t / maxT * 100).toFixed(3) + '%';
      const width = Math.max(2, (Math.max(sp.dur || 1, 1) / maxT * 100));
      bar.style.left = left;
      bar.style.width = width.toFixed(3) + '%';
      bar.style.background = sp.ok ? (tColor[sp.tool] || P.blue) : P.red;
      bar.style.opacity = '0.75';
      bar.title = `${sp.tool} @ +${sp.t}ms (${sp.dur}ms)${sp.ok ? '' : ' FAIL'}`;
      track.appendChild(bar);
    });
    lane.appendChild(track);
    wrap.appendChild(lane);
  });

  const legend = document.getElementById('timeline-legend');
  legend.innerHTML = toolList.slice(0, 10).map(t =>
    `<div class="graph-legend-item"><div class="graph-legend-dot" style="background:${tColor[t]}"></div>${t}</div>`
  ).join('');
})();

/* ── 4. Co-occurrence Matrix ── */
(function buildMatrix(){
  const co = sqlTab('graph').graph_cooccurrence;
  if (!co || !co.tools.length) return;

  const tools = co.tools;
  const matrix = co.matrix;
  const n = tools.length;

  let maxVal = 1;
  for (let i = 0; i < n; i++)
    for (let j = 0; j < n; j++)
      if (i !== j && matrix[i][j] > maxVal) maxVal = matrix[i][j];

  function cellBg(i, j) {
    if (i === j) return 'rgba(255,255,255,.04)';
    const t = matrix[i][j] / maxVal;
    const alpha = (0.1 + t * 0.7).toFixed(2);
    return `rgba(242,138,26,${alpha})`;
  }

  const tbl = document.getElementById('matrix-table');
  let html = '<tr><th></th>' + tools.map(t =>
    `<th title="${t}" style="writing-mode:vertical-lr;transform:rotate(180deg);height:60px;vertical-align:bottom">${t.slice(0,8)}</th>`
  ).join('') + '</tr>';

  for (let i = 0; i < n; i++) {
    html += `<tr><th style="text-align:right;padding-right:6px;font-weight:500;color:var(--text-2)">${tools[i].slice(0,10)}</th>`;
    for (let j = 0; j < n; j++) {
      const v = matrix[i][j];
      html += `<td><div class="matrix-cell" style="width:30px;height:30px;background:${cellBg(i,j)};margin:auto;line-height:30px;font-size:11px;color:rgba(255,255,255,.5)" title="${tools[i]} <-> ${tools[j]}: ${v} sessions">${v > 0 ? v : ''}</div></td>`;
    }
    html += '</tr>';
  }
  tbl.innerHTML = html;
})();

/* ── 5. Latency Histograms ── */
(function buildHistograms(){
  const hist = sqlTab('graph').graph_latency_histograms;
  if (!hist || !Object.keys(hist.tools).length) return;

  const labels = hist.labels;
  const bucketColors = [P.green, P.teal, P.blue, P.yellow, P.orange, P.red];
  const grid = document.getElementById('hist-grid');
  grid.innerHTML = '';

  Object.entries(hist.tools).forEach(([tool, buckets]) => {
    const maxVal = Math.max(...buckets, 1);
    const item = document.createElement('div');
    item.className = 'hist-item';

    const lbl = document.createElement('div');
    lbl.className = 'hist-label';
    lbl.textContent = tool;
    item.appendChild(lbl);

    const bars = document.createElement('div');
    bars.className = 'hist-bars';

    const bLabels = document.createElement('div');
    bLabels.className = 'hist-bLabels';

    buckets.forEach((v, i) => {
      const bar = document.createElement('div');
      bar.className = 'hist-bar';
      bar.style.height = Math.max((v / maxVal) * 80, 2) + 'px';
      bar.style.background = bucketColors[i];
      bar.style.opacity = '0.8';
      bar.title = `${labels[i]}: ${v} calls`;
      bars.appendChild(bar);

      const bl = document.createElement('div');
      bl.className = 'hist-bLabel';
      bl.textContent = labels[i].replace('ms','').replace('-','-');
      bLabels.appendChild(bl);
    });

    item.appendChild(bars);
    item.appendChild(bLabels);
    grid.appendChild(item);
  });
})();

} // end initGraphs

function currentAgentComparison(){
  return (sqlTab('agents').agent_comparison || []).slice();
}

/* ════════════════════ CHART HELPERS ════════════════════ */
function renderChartEmpty(id, message){
  const canvas = document.getElementById(id);
  if (!canvas?.parentElement) return;
  canvas.parentElement.innerHTML = `<div class="empty-note" style="display:grid;place-items:center;height:100%;text-align:center">${escHtml(message)}</div>`;
}

function makeDoughnut(id, labels, values){
  new Chart(document.getElementById(id).getContext('2d'),{
    type:'doughnut',
    data:{
      labels,
      datasets:[{data:values,backgroundColor:COLORS,borderWidth:0}]
    },
    options:{
      responsive:true,
      maintainAspectRatio:false,
      cutout:'65%',
      plugins:{
        legend:{
          position:'right',
          labels:{color:'rgba(255,255,255,.5)',font:{size:11},boxWidth:10,padding:8}
        },
        tooltip:{callbacks:{label:c=>{
          const total=c.dataset.data.reduce((a,b)=>a+b,0);
          return ` ${c.label}: ${c.parsed.toLocaleString()} (${(100*c.parsed/total).toFixed(1)}%)`;
        }}}
      }
    }
  });
}

function makeHBar(id, labels, values, color){
  new Chart(document.getElementById(id).getContext('2d'),{
    type:'bar',
    data:{
      labels,
      datasets:[{data:values,backgroundColor:color,borderRadius:4,borderSkipped:false}]
    },
    options:{
      indexAxis:'y',
      responsive:true,
      maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>' '+c.parsed.x.toLocaleString()}}},
      scales:{
        x:{grid:{color:'rgba(255,255,255,.04)'},ticks:{color:'rgba(255,255,255,.35)',font:{size:10}}},
        y:{grid:{display:false},ticks:{color:'rgba(255,255,255,.5)',font:{size:11},maxRotation:0}}
      }
    }
  });
}

function makeLine(id, labels, datasets, costAxis=false){
  new Chart(document.getElementById(id).getContext('2d'),{
    type:'line',
    data:{labels,datasets:datasets.map(dataset=>({
      ...dataset,
      fill:false,
      tension:.25,
      borderWidth:2,
      pointRadius:2,
      pointHoverRadius:3
    }))},
    options:{
      responsive:true,
      maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{
        legend:{
          position:'bottom',
          labels:{color:'rgba(255,255,255,.5)',font:{size:11},boxWidth:10,padding:10}
        },
        tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ${costAxis ? fmtCost(c.parsed.y || 0) : (c.parsed.y || 0).toLocaleString()}`}}
      },
      scales:{
        x:{grid:{color:'rgba(255,255,255,.04)'},ticks:{color:'rgba(255,255,255,.35)',font:{size:10}}},
        y:{
          beginAtZero:true,
          grid:{color:'rgba(255,255,255,.04)'},
          ticks:{
            color:'rgba(255,255,255,.35)',
            font:{size:10},
            callback:value=>costAxis ? fmtCost(Number(value || 0)) : Number(value || 0).toLocaleString()
          }
        }
      }
    }
  });
}

function urlHasDashboardFilters(){
  const params = currentParams();
  return Boolean(
    (params.get('q') || '').trim()
    || (params.get('session') || '').trim()
    || (params.get('agents') || '').trim()
    || ((params.get('model') || 'all') !== 'all')
    || ((params.get('status') || 'all') !== 'all')
    || ((params.get('range') || 'all') !== 'all')
    || ((params.get('sort') || 'recent') !== 'recent')
  );
}

dashboardFilterActive = urlHasDashboardFilters();
buildCohortComparison();

hideReportLoader();

}

initApp().catch(() => {
  hideReportLoader();
});
