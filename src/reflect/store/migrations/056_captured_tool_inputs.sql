-- Reproject captured wrapper commands and discover referenced local skills once.
INSERT INTO pending_refresh(phase,session_id,revision,deferred)
SELECT DISTINCT 'improvements',st.session_id,'captured-tool-inputs-v1',0
FROM tool_calls tc JOIN steps st ON st.id=tc.step_id
WHERE tc.tool_name IN ('exec','functions.exec')
   OR tc.raw_attrs_json LIKE '%SKILL.md%'
   OR tc.input_preview_redacted LIKE '%SKILL.md%'
   OR tc.tool_name LIKE '%merge_request%'
   OR tc.tool_name LIKE '%pull_request%'
ON CONFLICT(phase,session_id) DO UPDATE SET revision=excluded.revision,deferred=0;
