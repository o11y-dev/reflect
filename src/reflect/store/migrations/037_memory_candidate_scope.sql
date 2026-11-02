-- Old graph candidates aggregated globally and then labeled the caller's path.
-- Preserve them for audit; regenerate from scoped evidence on the next request.
ALTER TABLE memory_candidates ADD COLUMN scope_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_candidates ADD COLUMN scope_id TEXT NOT NULL DEFAULT '';

UPDATE memories
SET validation_status = 'needs_review', validated_at = NULL,
    stale_reason = 'ambiguous_candidate_scope',
    validation_error = 'Legacy graph evidence was not repository scoped. Review before reuse.'
WHERE source = 'graph_candidate'
   OR id IN (SELECT promoted_memory_id FROM memory_candidates);

UPDATE memory_candidates SET status = 'invalidated';

CREATE INDEX idx_memory_candidates_scope
ON memory_candidates(scope_kind, scope_id, status, confidence DESC, updated_at DESC);
