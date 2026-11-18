-- Migration 51's bounded Python repair classifies retained runtime/container
-- spans only. Event counts are read directly from steps; no usage, execution,
-- graph or rollup rebuild is needed. Invalidate cached reads without publishing
-- an incomplete refresh or changing either profile's readiness state.
UPDATE store_metadata
SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE key IN ('prepared_snapshot', 'prepared_usage');
