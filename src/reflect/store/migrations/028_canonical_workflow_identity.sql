ALTER TABLE workflow_candidates
  ADD COLUMN contract_signature TEXT NOT NULL DEFAULT '';

ALTER TABLE workflow_candidates
  ADD COLUMN revision_hash TEXT NOT NULL DEFAULT '';

UPDATE workflow_candidates
SET contract_signature = reflect_workflow_contract_signature(title, content_json),
    revision_hash = reflect_workflow_revision_hash(title, content_json),
    status = CASE
      WHEN status IN ('active', 'rolled_back') THEN 'approved'
      ELSE status
    END,
    checks_json = json_remove(COALESCE(NULLIF(checks_json, ''), '{}'), '$.applied');

ALTER TABLE workflow_candidates DROP COLUMN support_count;

CREATE INDEX idx_workflow_candidates_contract
  ON workflow_candidates(contract_signature, status, updated_at DESC);

DELETE FROM store_metadata
WHERE key = 'observation_session_ledger_v1';

DELETE FROM measurements
WHERE COALESCE(json_extract(cohort_json, '$.unit'), 'sessions') <> 'execution_units';

DROP TABLE session_task_archetypes;
