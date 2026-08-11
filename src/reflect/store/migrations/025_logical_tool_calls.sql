ALTER TABLE tool_calls ADD COLUMN logical_call_id TEXT;

UPDATE tool_calls
SET logical_call_id = COALESCE(
  NULLIF(json_extract(raw_attrs_json, '$."gen_ai.client.tool_use_id"'), ''),
  NULLIF(json_extract(raw_attrs_json, '$."tool.call_id"'), ''),
  NULLIF(json_extract(raw_attrs_json, '$.tool_call_id'), ''),
  NULLIF(json_extract(raw_attrs_json, '$."tool.id"'), ''),
  id
);

CREATE INDEX idx_tool_calls_logical_backfill
  ON tool_calls(session_id, logical_call_id);

CREATE TEMP TABLE logical_tool_call_reconciliation AS
SELECT
  id AS duplicate_id,
  FIRST_VALUE(id) OVER keeper_order AS keeper_id,
  FIRST_VALUE(tool_name) OVER tool_name_order AS tool_name,
  FIRST_VALUE(status) OVER status_order AS status,
  MAX(duration_ms) OVER logical_call AS duration_ms,
  FIRST_VALUE(output_hash) OVER output_hash_order AS output_hash,
  FIRST_VALUE(output_preview_redacted) OVER output_preview_order
    AS output_preview_redacted,
  FIRST_VALUE(error_type) OVER error_type_order AS error_type,
  FIRST_VALUE(error_message_redacted) OVER error_message_order
    AS error_message_redacted
FROM (
  SELECT tool_calls.*, steps.seq AS step_seq
  FROM tool_calls
  JOIN steps ON steps.id = tool_calls.step_id
)
WINDOW
  logical_call AS (PARTITION BY session_id, logical_call_id),
  keeper_order AS (
    PARTITION BY session_id, logical_call_id
    ORDER BY step_seq, id
  ),
  tool_name_order AS (
    PARTITION BY session_id, logical_call_id
    ORDER BY
      (tool_name GLOB 'mcp__*') DESC,
      CASE WHEN tool_name GLOB 'mcp__*' THEN updated_at END DESC,
      step_seq,
      id
  ),
  status_order AS (
    PARTITION BY session_id, logical_call_id
    ORDER BY
      CASE status WHEN 'error' THEN 0 WHEN 'ok' THEN 1 ELSE 2 END,
      updated_at DESC,
      step_seq,
      id
  ),
  output_hash_order AS (
    PARTITION BY session_id, logical_call_id
    ORDER BY (output_hash IS NOT NULL) DESC, updated_at DESC, step_seq, id
  ),
  output_preview_order AS (
    PARTITION BY session_id, logical_call_id
    ORDER BY
      (output_preview_redacted IS NOT NULL) DESC,
      updated_at DESC,
      step_seq,
      id
  ),
  error_type_order AS (
    PARTITION BY session_id, logical_call_id
    ORDER BY (error_type IS NOT NULL) DESC, updated_at DESC, step_seq, id
  ),
  error_message_order AS (
    PARTITION BY session_id, logical_call_id
    ORDER BY
      (error_message_redacted IS NOT NULL) DESC,
      updated_at DESC,
      step_seq,
      id
  );

CREATE UNIQUE INDEX idx_logical_tool_call_reconciliation_duplicate
  ON logical_tool_call_reconciliation(duplicate_id);

CREATE INDEX idx_logical_tool_call_reconciliation_keeper
  ON logical_tool_call_reconciliation(keeper_id);

UPDATE tool_calls AS keeper
SET (
  tool_name,
  status,
  duration_ms,
  output_hash,
  output_preview_redacted,
  error_type,
  error_message_redacted
) = (
  SELECT
    reconciled.tool_name,
    reconciled.status,
    reconciled.duration_ms,
    reconciled.output_hash,
    reconciled.output_preview_redacted,
    reconciled.error_type,
    reconciled.error_message_redacted
  FROM logical_tool_call_reconciliation AS reconciled
  WHERE reconciled.duplicate_id = keeper.id
)
WHERE keeper.id IN (
  SELECT keeper_id FROM logical_tool_call_reconciliation
);

UPDATE observation_evidence
SET tool_call_id = (
  SELECT keeper_id
  FROM logical_tool_call_reconciliation
  WHERE duplicate_id = observation_evidence.tool_call_id
)
WHERE tool_call_id IN (
  SELECT duplicate_id
  FROM logical_tool_call_reconciliation
  WHERE duplicate_id <> keeper_id
);

DELETE FROM tool_calls
WHERE id IN (
  SELECT duplicate_id
  FROM logical_tool_call_reconciliation
  WHERE duplicate_id <> keeper_id
);

DROP TABLE logical_tool_call_reconciliation;

DROP INDEX idx_tool_calls_logical_backfill;

CREATE UNIQUE INDEX idx_tool_calls_logical_identity
  ON tool_calls(session_id, logical_call_id)
  WHERE logical_call_id IS NOT NULL AND logical_call_id <> '';
