-- Reclassify retained user-prompt evidence once, including older sessions.
INSERT INTO pending_refresh(phase,session_id,revision,deferred)
SELECT DISTINCT 'improvements',st.session_id,'task-context-inputs-v1',0
FROM steps st WHERE
    NULLIF(json_extract(st.raw_attrs_json,'$."gen_ai.client.prompt.text"'),'') IS NOT NULL
    OR NULLIF(json_extract(st.raw_attrs_json,'$."gen_ai.client.prompt"'),'') IS NOT NULL
    OR EXISTS(SELECT 1 FROM conversation_facts cf WHERE cf.step_id=st.id
        AND cf.kind='prompt' AND cf.role='user' AND NULLIF(cf.content_preview_redacted,'') IS NOT NULL)
ON CONFLICT(phase,session_id) DO UPDATE SET revision=excluded.revision,deferred=0;
