-- Normalize retained telemetry and instruction scans into the memory source contract.
-- Existing canonical metadata wins; readers no longer need raw-attribute fallbacks.
UPDATE memories
SET source_metadata_json = json_patch(
  json_object(
    'source_kind', source,
    'source_ref', COALESCE(
      json_extract(raw_attrs_json, '$.path'),
      json_extract(raw_attrs_json, '$."gen_ai.memory.source_path"'), id),
    'path', COALESCE(
      json_extract(raw_attrs_json, '$.path'),
      json_extract(raw_attrs_json, '$."gen_ai.memory.source_path"'), ''),
    'workspace_root', COALESCE(
      json_extract(raw_attrs_json, '$.workspace_root'),
      json_extract(raw_attrs_json, '$."code.workspace.root"'), ''),
    'content_hash', content_hash
  ), source_metadata_json
);
