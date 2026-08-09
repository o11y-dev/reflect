CREATE TEMP TABLE canonical_mcp_call_mapping AS
SELECT
  mc.id AS legacy_id,
  COALESCE(
    (
      SELECT tc.id
      FROM tool_calls AS tc
      WHERE tc.session_id = mc.session_id
        AND NULLIF(mc.tool_call_id, '') IS NOT NULL
        AND tc.logical_call_id = mc.tool_call_id
      ORDER BY tc.created_at, tc.id
      LIMIT 1
    ),
    (
      SELECT tc.id
      FROM tool_calls AS tc
      WHERE tc.step_id = mc.step_id
      ORDER BY tc.created_at, tc.id
      LIMIT 1
    ),
    'mcp_tool_' || mc.id
  ) AS canonical_tool_call_id
FROM mcp_calls AS mc;

INSERT OR IGNORE INTO tool_calls(
  id,
  step_id,
  session_id,
  logical_call_id,
  tool_name,
  tool_type,
  mcp_session_id,
  status,
  duration_ms,
  raw_attrs_json,
  created_at,
  updated_at
)
SELECT
  mapping.canonical_tool_call_id,
  mc.step_id,
  mc.session_id,
  COALESCE(NULLIF(mc.tool_call_id, ''), mc.id),
  CASE
    WHEN NULLIF(mc.server_name, '') IS NOT NULL
      AND NULLIF(mc.tool_name, '') IS NOT NULL
    THEN 'mcp__' || mc.server_name || '__' || mc.tool_name
    ELSE COALESCE(NULLIF(mc.tool_name, ''), 'mcp')
  END,
  'mcp',
  mc.mcp_session_id,
  mc.status,
  mc.duration_ms,
  mc.raw_attrs_json,
  mc.created_at,
  mc.updated_at
FROM mcp_calls AS mc
JOIN canonical_mcp_call_mapping AS mapping
  ON mapping.legacy_id = mc.id;

UPDATE tool_calls AS target
SET
  tool_name = COALESCE(
    (
      SELECT CASE
        WHEN NULLIF(mc.server_name, '') IS NOT NULL
          AND NULLIF(mc.tool_name, '') IS NOT NULL
        THEN 'mcp__' || mc.server_name || '__' || mc.tool_name
      END
      FROM mcp_calls AS mc
      JOIN canonical_mcp_call_mapping AS mapping
        ON mapping.legacy_id = mc.id
      WHERE mapping.canonical_tool_call_id = target.id
      ORDER BY mc.updated_at DESC, mc.id
      LIMIT 1
    ),
    target.tool_name
  ),
  tool_type = 'mcp',
  mcp_session_id = COALESCE(
    target.mcp_session_id,
    (
      SELECT mc.mcp_session_id
      FROM mcp_calls AS mc
      JOIN canonical_mcp_call_mapping AS mapping
        ON mapping.legacy_id = mc.id
      WHERE mapping.canonical_tool_call_id = target.id
        AND NULLIF(mc.mcp_session_id, '') IS NOT NULL
      ORDER BY mc.updated_at DESC, mc.id
      LIMIT 1
    )
  ),
  status = CASE
    WHEN EXISTS (
      SELECT 1
      FROM mcp_calls AS mc
      JOIN canonical_mcp_call_mapping AS mapping
        ON mapping.legacy_id = mc.id
      WHERE mapping.canonical_tool_call_id = target.id
        AND mc.status = 'error'
    ) THEN 'error'
    WHEN target.status = 'error' THEN 'error'
    WHEN target.status = 'ok' OR EXISTS (
      SELECT 1
      FROM mcp_calls AS mc
      JOIN canonical_mcp_call_mapping AS mapping
        ON mapping.legacy_id = mc.id
      WHERE mapping.canonical_tool_call_id = target.id
        AND mc.status = 'ok'
    ) THEN 'ok'
    ELSE 'unknown'
  END,
  duration_ms = MAX(
    COALESCE(target.duration_ms, 0),
    COALESCE(
      (
        SELECT MAX(mc.duration_ms)
        FROM mcp_calls AS mc
        JOIN canonical_mcp_call_mapping AS mapping
          ON mapping.legacy_id = mc.id
        WHERE mapping.canonical_tool_call_id = target.id
      ),
      0
    )
  )
WHERE target.id IN (
  SELECT canonical_tool_call_id
  FROM canonical_mcp_call_mapping
);

ALTER TABLE mcp_calls RENAME TO legacy_mcp_calls;

CREATE TABLE mcp_calls (
  tool_call_id TEXT PRIMARY KEY REFERENCES tool_calls(id) ON DELETE CASCADE,
  mcp_session_id TEXT,
  mcp_protocol_version TEXT,
  transport TEXT,
  server_name TEXT,
  tool_name TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

INSERT INTO mcp_calls(
  tool_call_id,
  mcp_session_id,
  mcp_protocol_version,
  transport,
  server_name,
  tool_name,
  created_at,
  updated_at
)
SELECT
  mapping.canonical_tool_call_id,
  MAX(mc.mcp_session_id),
  MAX(mc.mcp_protocol_version),
  MAX(mc.transport),
  MAX(mc.server_name),
  MAX(mc.tool_name),
  MIN(mc.created_at),
  MAX(mc.updated_at)
FROM legacy_mcp_calls AS mc
JOIN canonical_mcp_call_mapping AS mapping
  ON mapping.legacy_id = mc.id
GROUP BY mapping.canonical_tool_call_id;

DROP TABLE legacy_mcp_calls;
DROP TABLE canonical_mcp_call_mapping;

CREATE INDEX idx_mcp_calls_server_name
  ON mcp_calls(server_name);
