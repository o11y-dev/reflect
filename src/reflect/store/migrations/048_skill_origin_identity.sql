-- Preserve the existing unique index as the source identity constraint. Names
-- remain display values and can occur at independent filesystem origins.
ALTER TABLE skills RENAME COLUMN slug TO identity_key;
ALTER TABLE skills ADD COLUMN slug TEXT NOT NULL DEFAULT '';
UPDATE skills SET slug = identity_key;

CREATE TEMP TABLE reflect_skill_identity AS
SELECT s.id,
  CASE
    WHEN (SELECT COUNT(DISTINCT wc.contract_signature)
          FROM skill_versions v JOIN workflow_candidates wc ON wc.id=v.workflow_candidate_id
          WHERE v.skill_id=s.id) = 1
      AND NOT EXISTS (SELECT 1 FROM skill_versions v WHERE v.skill_id=s.id AND v.workflow_candidate_id IS NULL)
    THEN 'workflow:' || (SELECT wc.contract_signature FROM skill_versions v
                         JOIN workflow_candidates wc ON wc.id=v.workflow_candidate_id
                         WHERE v.skill_id=s.id LIMIT 1)
    WHEN (SELECT COUNT(*) FROM skill_installations i WHERE i.skill_id=s.id) = 1
      AND NOT EXISTS (SELECT 1 FROM skill_versions v WHERE v.skill_id=s.id AND v.source_kind <> 'filesystem')
    THEN 'filesystem:' || (SELECT i.path FROM skill_installations i WHERE i.skill_id=s.id)
    ELSE 'legacy:' || s.id
  END AS identity_key
FROM skills s;

-- Multiple preexisting rows may themselves share one workflow contract. Keep
-- their history separate until an explicit source replay resolves it.
UPDATE skills SET identity_key = 'legacy:' || id;
UPDATE skills SET identity_key = (
  SELECT CASE WHEN (SELECT COUNT(*) FROM reflect_skill_identity other WHERE other.identity_key=chosen.identity_key)=1
              THEN chosen.identity_key ELSE 'legacy:' || chosen.id END
  FROM reflect_skill_identity chosen WHERE chosen.id=skills.id
);
DROP TABLE reflect_skill_identity;
CREATE INDEX idx_skills_slug ON skills(slug);
