ALTER TABLE sessions
ADD COLUMN token_provenance TEXT NOT NULL DEFAULT 'unavailable'
CHECK(token_provenance IN ('local_telemetry', 'estimated_cursor_transcript', 'unavailable'));

UPDATE sessions
SET token_provenance = CASE
  WHEN EXISTS (
    SELECT 1
    FROM steps token_step
    WHERE token_step.session_id = sessions.id
      AND (
        json_type(token_step.raw_attrs_json, '$."gen_ai.usage.input_tokens"') IS NOT NULL
        OR json_type(token_step.raw_attrs_json, '$."gen_ai.usage.output_tokens"') IS NOT NULL
        OR json_type(
          token_step.raw_attrs_json,
          '$."gen_ai.usage.cache_creation.input_tokens"'
        ) IS NOT NULL
        OR json_type(
          token_step.raw_attrs_json,
          '$."gen_ai.usage.cache_read.input_tokens"'
        ) IS NOT NULL
        OR json_type(
          token_step.raw_attrs_json,
          '$."gen_ai.usage.reasoning_output_tokens"'
        ) IS NOT NULL
      )
  ) OR EXISTS (
    SELECT 1
    FROM llm_calls usage_call
    WHERE usage_call.session_id = sessions.id
      AND usage_call.input_tokens + usage_call.output_tokens
          + usage_call.cache_creation_input_tokens
          + usage_call.cache_read_input_tokens
          + usage_call.reasoning_output_tokens > 0
  ) THEN 'local_telemetry'
  WHEN EXISTS (
    SELECT 1
    FROM steps token_step
    WHERE token_step.session_id = sessions.id
      AND json_extract(
        token_step.raw_attrs_json,
        '$."reflect.token.source"'
      ) = 'estimated_cursor_transcript'
  ) THEN 'estimated_cursor_transcript'
  WHEN input_tokens + output_tokens + cache_creation_tokens
       + cache_read_tokens + reasoning_tokens > 0 THEN 'local_telemetry'
  ELSE 'unavailable'
END;

DELETE FROM llm_calls
WHERE (
    lower(operation_name) IN ('userpromptsubmit', 'sessionstart', 'sessionend')
    AND input_tokens + output_tokens + cache_creation_input_tokens
        + cache_read_input_tokens + reasoning_output_tokens = 0
  )
   OR (
     lower(operation_name) = 'stop'
     AND COALESCE(NULLIF(request_model, ''), NULLIF(response_model, '')) IS NULL
     AND input_tokens + output_tokens + cache_creation_input_tokens
         + cache_read_input_tokens + reasoning_output_tokens = 0
     AND COALESCE(
       json_extract(raw_attrs_json, '$."gen_ai.operation.name"'),
       ''
     ) = ''
   );

UPDATE steps
SET type = 'conversation_event'
WHERE lower(
  COALESCE(
    json_extract(raw_attrs_json, '$."gen_ai.client.hook.event"'),
    summary,
    ''
  )
) IN ('userpromptsubmit', 'user.message', 'user_message')
  AND NOT EXISTS (
    SELECT 1 FROM llm_calls usage_call WHERE usage_call.step_id = steps.id
  );

UPDATE steps
SET type = 'lifecycle'
WHERE lower(
  COALESCE(
    json_extract(raw_attrs_json, '$."gen_ai.client.hook.event"'),
    summary,
    ''
  )
) IN ('sessionstart', 'sessionend', 'session.start', 'session.end')
  AND NOT EXISTS (
    SELECT 1 FROM llm_calls usage_call WHERE usage_call.step_id = steps.id
  );

UPDATE steps
SET type = 'conversation_event'
WHERE lower(
  COALESCE(
    json_extract(raw_attrs_json, '$."gen_ai.client.hook.event"'),
    summary,
    ''
  )
) = 'stop'
  AND NOT EXISTS (
    SELECT 1 FROM llm_calls model_call WHERE model_call.step_id = steps.id
  );
