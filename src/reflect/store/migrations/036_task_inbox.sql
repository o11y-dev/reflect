-- Receipts commit with canonical task changes, so a crash before inbox cleanup
-- can safely replay the same event without repeating its effects.
CREATE TABLE task_event_receipts (
  event_id TEXT PRIMARY KEY,
  task_run_id TEXT NOT NULL,
  applied_at TEXT NOT NULL
);
