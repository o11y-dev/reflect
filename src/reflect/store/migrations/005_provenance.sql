ALTER TABLE raw_events ADD COLUMN origin_kind TEXT;
ALTER TABLE steps ADD COLUMN origin_kind TEXT;

CREATE INDEX IF NOT EXISTS idx_raw_events_origin_kind ON raw_events(origin_kind, observed_at);
CREATE INDEX IF NOT EXISTS idx_steps_origin_kind ON steps(origin_kind, started_at);
