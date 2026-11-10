-- Migration 52 reconciles exact Codex OTLP span duplicates and restores
-- canonical dispatch calls through the existing pending-refresh pipeline.
UPDATE store_metadata
SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE key IN ('prepared_snapshot', 'prepared_usage');
