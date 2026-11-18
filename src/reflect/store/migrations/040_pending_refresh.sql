-- Keep dirty scope independently of sessions so retractions/deletions survive.
-- A per-phase revision prevents an older refresh from acknowledging new input.
CREATE TABLE pending_refresh (
  phase TEXT NOT NULL CHECK (phase IN ('canonical', 'cost', 'rollup', 'graph', 'improvements')),
  session_id TEXT NOT NULL,
  revision TEXT NOT NULL,
  deferred INTEGER NOT NULL DEFAULT 0 CHECK (deferred IN (0, 1)),
  PRIMARY KEY (phase, session_id)
) WITHOUT ROWID;

-- Seed a one-time inventory build from the previous catalogue checkpoint. If
-- the catalogue is unchanged, existing persisted costs need no full rewrite.
INSERT INTO store_metadata(key, value, updated_at)
SELECT 'cost_pricing_inputs_v2', json_object('catalogue_fingerprint', value), updated_at
FROM store_metadata WHERE key = 'cost_pricing_inputs_v1';
DELETE FROM store_metadata WHERE key = 'cost_pricing_inputs_v1';

-- Bound window discovery before reading historical events. Normalize offsets
-- in the index so Z/UTC-offset/native timestamp forms share exact boundaries.
CREATE INDEX idx_sessions_analysis_start ON sessions(CASE WHEN started_at GLOB '[0-9][0-9][0-9][0-9]-*' THEN julianday(started_at) END, id);
CREATE INDEX idx_steps_analysis_start ON steps(CASE WHEN started_at GLOB '[0-9][0-9][0-9][0-9]-*' THEN julianday(started_at) END, session_id)
WHERE COALESCE(json_extract(raw_attrs_json,'$."reflect.time.provenance"'),'observed')
      NOT IN ('synthetic','inferred');
CREATE INDEX idx_steps_analysis_end ON steps(CASE WHEN ended_at GLOB '[0-9][0-9][0-9][0-9]-*' THEN julianday(ended_at) END, session_id)
WHERE COALESCE(json_extract(raw_attrs_json,'$."reflect.time.provenance"'),'observed')
      NOT IN ('synthetic','inferred');
