-- Keep terminal-result measurement on compact facts after adding the terminal
-- flag. A changed json_extract argument list no longer matches an old SQLite
-- expression index and otherwise reopens every tool's command/result payload.
DROP INDEX idx_tool_calls_measurement_facts;
CREATE INDEX idx_tool_calls_measurement_facts ON tool_calls(
  session_id,logical_call_id,status,output_hash,
  json_extract(raw_attrs_json,'$."reflect.tool.identity_provenance"','$."reflect.tool.terminal"','$."gen_ai.client.hook.event"','$."process.exit.code"','$."gen_ai.client.turn_id"','$."turn.id"','$."turn_id"','$."generation.id"','$."gen_ai.client.generation_id"')
);
CREATE INDEX idx_agent_events_observed_counts ON agent_events(
  session_id,event_name,agent_type,event_id,agent_id,step_id
);
