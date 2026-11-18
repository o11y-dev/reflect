-- Product identity is independent of the service that emitted an event.
CREATE TEMP TABLE codex_agent_ids AS
SELECT id FROM agents WHERE reflect_agent_name(name) = 'codex';

-- Only file-derived sessions whose every step lacks a conversation identity are
-- reclassified. Explicit conversation IDs and ambiguous mixed sources survive.
CREATE TEMP TABLE codex_unassigned_sessions AS
SELECT s.id FROM sessions s
WHERE s.agent_id IN (SELECT id FROM codex_agent_ids)
  AND s.id = reflect_stable_id('session', s.source_ref)
  AND COALESCE(s.source_kind, '') <> 'native_session'
  AND EXISTS (SELECT 1 FROM steps st WHERE st.session_id = s.id)
  AND NOT EXISTS (
    SELECT 1 FROM steps st WHERE st.session_id = s.id
      AND reflect_unassigned_codex(st.raw_attrs_json) IS NULL
  );

CREATE TEMP TABLE codex_unassigned_raw AS
SELECT id, reflect_stable_id('step', id) AS step_id,
       reflect_unassigned_codex(attrs_json) AS classification
FROM raw_events WHERE session_id IS NULL
  AND reflect_unassigned_codex(attrs_json) IS NOT NULL;
CREATE INDEX codex_unassigned_raw_step ON codex_unassigned_raw(step_id);
CREATE UNIQUE INDEX codex_unassigned_raw_id ON codex_unassigned_raw(id);

UPDATE raw_events
SET normalized_status = 'ignored', normalization_error = NULL,
    attrs_json = json_set(attrs_json, '$."reflect.telemetry.classification"',
      (SELECT classification FROM codex_unassigned_raw r WHERE r.id = raw_events.id))
WHERE id IN (SELECT id FROM codex_unassigned_raw);

-- Raw retention can have expired while canonical steps survive. Preserve those
-- attributes with explicit recovery provenance before removing synthetic rows.
INSERT OR IGNORE INTO raw_events(
  id, source_id, source_type, event_type, observed_at, received_at,
  attrs_json, body_json, normalized_status, content_hash, created_at, origin_kind
)
SELECT 'recovered_' || st.id, COALESCE(s.source_ref, 'migration:038'),
       COALESCE(s.source_kind, 'otlp_traces_json'), COALESCE(st.summary, st.type),
       st.started_at, COALESCE(st.ended_at, st.started_at),
       json_set(st.raw_attrs_json,
         '$."reflect.telemetry.classification"', reflect_unassigned_codex(st.raw_attrs_json),
         '$."reflect.telemetry.recovered_from_step"', st.id),
       '{}', 'ignored', 'recovered_' || st.id, st.created_at, st.origin_kind
FROM steps st JOIN sessions s ON s.id = st.session_id
WHERE s.id IN (SELECT id FROM codex_unassigned_sessions)
  AND NOT EXISTS (SELECT 1 FROM codex_unassigned_raw r WHERE r.step_id = st.id);

DELETE FROM graph_edges WHERE session_id IN (SELECT id FROM codex_unassigned_sessions);
DELETE FROM graph_nodes WHERE session_id IN (SELECT id FROM codex_unassigned_sessions);
DELETE FROM session_rollups WHERE session_id IN (SELECT id FROM codex_unassigned_sessions);
DELETE FROM sessions WHERE id IN (SELECT id FROM codex_unassigned_sessions);

-- Consolidate only product nodes and their edges. Rebuilding every conversation
-- graph for a label change would make this migration unnecessarily expensive.
CREATE TEMP TABLE codex_agent_nodes AS
SELECT id FROM graph_nodes WHERE kind = 'Agent' AND reflect_agent_name(label) = 'codex';
INSERT OR IGNORE INTO graph_nodes(id, kind, label, attrs_json, created_at, updated_at)
SELECT reflect_stable_id('node', 'Agent:codex:'), 'Agent', 'codex',
       json_object('agent_id', reflect_stable_id('agent', 'codex')),
       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
WHERE EXISTS (SELECT 1 FROM codex_agent_nodes);

CREATE TEMP TABLE codex_agent_edges AS
SELECT e.*,
       CASE WHEN source_node_id IN (SELECT id FROM codex_agent_nodes)
         THEN reflect_stable_id('node', 'Agent:codex:') ELSE source_node_id END AS new_source,
       CASE WHEN target_node_id IN (SELECT id FROM codex_agent_nodes)
         THEN reflect_stable_id('node', 'Agent:codex:') ELSE target_node_id END AS new_target
FROM graph_edges e WHERE source_node_id IN (SELECT id FROM codex_agent_nodes)
  OR target_node_id IN (SELECT id FROM codex_agent_nodes);
INSERT OR IGNORE INTO graph_edges(
  id, source_node_id, target_node_id, kind, session_id, weight,
  first_seen_at, last_seen_at, attrs_json, created_at, updated_at
)
SELECT reflect_stable_id('edge', kind || ':' || new_source || ':' || new_target || ':' || COALESCE(session_id, '')),
       new_source, new_target, kind, session_id, weight,
       first_seen_at, last_seen_at, attrs_json, created_at, updated_at
FROM codex_agent_edges;
DELETE FROM graph_nodes WHERE id IN (SELECT id FROM codex_agent_nodes)
  AND id <> reflect_stable_id('node', 'Agent:codex:');
UPDATE graph_nodes
SET attrs_json = json_set(attrs_json, '$.agent_id', reflect_stable_id('agent', 'codex'))
WHERE kind = 'Session' AND session_id IN (
  SELECT id FROM sessions WHERE agent_id IN (SELECT id FROM codex_agent_ids)
);
DROP TABLE codex_agent_edges;
DROP TABLE codex_agent_nodes;

INSERT OR IGNORE INTO agents(id, name, raw_json, created_at, updated_at)
SELECT reflect_stable_id('agent', 'codex'), 'codex', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
WHERE EXISTS (SELECT 1 FROM codex_agent_ids);

UPDATE sessions SET agent_id = reflect_stable_id('agent', 'codex')
WHERE agent_id IN (SELECT id FROM codex_agent_ids);
UPDATE execution_units SET agent_id = reflect_stable_id('agent', 'codex')
WHERE agent_id IN (SELECT id FROM codex_agent_ids);
UPDATE pruned_sessions SET agent_id = reflect_stable_id('agent', 'codex'), agent_name = 'codex'
WHERE agent_id IN (SELECT id FROM codex_agent_ids);
DELETE FROM agents WHERE id IN (SELECT id FROM codex_agent_ids)
  AND id <> reflect_stable_id('agent', 'codex');

INSERT OR IGNORE INTO maintenance_tasks(task, requested_at)
SELECT 'rebuild_rollups_after_agent_identity', CURRENT_TIMESTAMP
WHERE EXISTS (SELECT 1 FROM codex_agent_ids);

DROP TABLE codex_unassigned_raw;
DROP TABLE codex_unassigned_sessions;
DROP TABLE codex_agent_ids;
