CREATE TEMP TABLE reflect_noisy_codex_desktop_sessions AS
SELECT s.id
FROM sessions s
JOIN agents a ON a.id = s.agent_id
WHERE lower(trim(a.name)) = 'codex desktop'
  AND s.source_kind = 'otlp_traces_json'
  AND NOT EXISTS (
    SELECT 1
    FROM steps st
    WHERE st.session_id = s.id
      AND (
        json_extract(st.raw_attrs_json, '$."gen_ai.client.hook.event"') IS NOT NULL
        OR json_extract(st.raw_attrs_json, '$."gen_ai.client.session_id"') IS NOT NULL
        OR json_extract(st.raw_attrs_json, '$."session.id"') IS NOT NULL
        OR json_extract(st.raw_attrs_json, '$."conversation.id"') IS NOT NULL
        OR json_extract(st.raw_attrs_json, '$."event.name"') IS NOT NULL
      )
  );

DELETE FROM graph_edges
WHERE session_id IN (SELECT id FROM reflect_noisy_codex_desktop_sessions);

DELETE FROM graph_nodes
WHERE session_id IN (SELECT id FROM reflect_noisy_codex_desktop_sessions);

DELETE FROM session_rollups
WHERE session_id IN (SELECT id FROM reflect_noisy_codex_desktop_sessions);

DELETE FROM sessions
WHERE id IN (SELECT id FROM reflect_noisy_codex_desktop_sessions);

UPDATE raw_events
SET session_id = NULL,
    normalized_status = 'ignored',
    normalization_error = NULL,
    attrs_json = json_set(
      attrs_json,
      '$."reflect.telemetry.classification"',
      'runtime_internal'
    )
WHERE source_type = 'otlp_traces_json'
  AND lower(trim(json_extract(attrs_json, '$."service.name"'))) = 'codex desktop'
  AND json_extract(attrs_json, '$."gen_ai.client.hook.event"') IS NULL
  AND json_extract(attrs_json, '$."gen_ai.client.session_id"') IS NULL
  AND json_extract(attrs_json, '$."session.id"') IS NULL
  AND json_extract(attrs_json, '$."conversation.id"') IS NULL
  AND json_extract(attrs_json, '$."event.name"') IS NULL;

DELETE FROM source_ingestion_state
WHERE source_type IN ('otlp_traces_json', 'otlp_logs_json');

CREATE TABLE IF NOT EXISTS maintenance_tasks (
  task TEXT PRIMARY KEY,
  requested_at TEXT NOT NULL
);

INSERT OR IGNORE INTO maintenance_tasks(task, requested_at)
SELECT 'rebuild_rollups_after_codex_desktop_otel', CURRENT_TIMESTAMP
WHERE EXISTS (SELECT 1 FROM reflect_noisy_codex_desktop_sessions);

DROP TABLE reflect_noisy_codex_desktop_sessions;
