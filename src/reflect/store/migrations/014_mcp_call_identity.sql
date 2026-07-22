ALTER TABLE mcp_calls ADD COLUMN tool_call_id TEXT;

UPDATE mcp_calls
SET tool_call_id = COALESCE(
  json_extract(raw_attrs_json, '$."gen_ai.client.tool_use_id"'),
  json_extract(raw_attrs_json, '$."tool.call_id"'),
  json_extract(raw_attrs_json, '$.tool_call_id')
)
WHERE tool_call_id IS NULL;

CREATE INDEX idx_mcp_calls_session_tool_call
  ON mcp_calls(session_id, tool_call_id)
  WHERE tool_call_id IS NOT NULL AND tool_call_id <> '';
