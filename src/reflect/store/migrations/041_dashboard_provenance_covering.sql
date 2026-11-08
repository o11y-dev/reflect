-- The dashboard only needs capture-source counts. Cover the scoped projection
-- so it never fetches wide raw JSON rows to read the trailing origin column.
CREATE INDEX idx_raw_events_session_origin ON raw_events(session_id, origin_kind);
