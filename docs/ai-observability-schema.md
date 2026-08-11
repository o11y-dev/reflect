# AI observability schema for `reflect`

This document defines Reflect's public analysis-side telemetry contract. It
describes the facts accepted at normalization boundaries and the guarantees of
the canonical SQLite model. Capture configuration belongs to agents, hooks, and
collectors; interpretation belongs here.

## Ownership and data flow

```text
source adapters -> raw_events -> canonical normalization -> SQLite
    -> derived state -> typed read models -> CLI, MCP, and browser
```

`src/reflect/agent_capabilities.py` is the source of truth for current,
partial, historical, and planned agent support. This document intentionally
does not duplicate that changing support matrix.

Raw source attributes and provenance remain available for evidence and
debugging. Canonical rows provide shared identities and semantics for
aggregation. Native conversation adapters may enrich session detail, but they
never feed aggregates.

## Normalization contract

Adapters map provider-native records into these shared inputs before or during
normalization:

| Concept | Accepted input | Canonical behavior |
|---|---|---|
| Agent | `gen_ai.client.name`, then `ide.name`, `agent.name`, or `service.name` | Resolve one agent identity; do not use the model provider as the agent |
| Session | Source session ID, then `session.id`, `gen_ai.client.session_id`, or `conversation.id` | Preserve a stable provider conversation container or derive a source-stable fallback |
| Provider | `gen_ai.system` | Store LLM provider metadata independently from agent identity |
| Model | `gen_ai.request.model`, `gen_ai.response.model` | Preserve requested and observed models separately |
| Tokens | `gen_ai.usage.*` input, output, cache, and reasoning fields | Store measured usage and its provenance; never infer exact usage from missing fields |
| Tool | Provider call ID, `tool.id`, `tool_call.id`, or `gen_ai.tool.call.id` | Merge invocation and result phases into one logical `tool_calls` row |
| MCP | Server, tool, transport, and protocol attributes | Extend the canonical logical tool call through one-to-one `mcp_calls` metadata |
| Source | Adapter source kind, source reference, and raw event ID | Retain provenance so reconciliation decisions remain explainable |

Provider-specific adapters may derive these inputs from native JSONL, SQLite,
OTLP traces or logs, and hook facts. Derivation happens at the adapter boundary;
shared normalization must not grow provider condition piles.

## Evidence semantics

- A lifecycle event is not an LLM call unless it contains model-exchange or
  usage evidence.
- A logical tool invocation is counted once even when a provider emits separate
  request and result records.
- Exact local tokens, transcript estimates, and unavailable usage remain
  distinct provenance states.
- Missing optional telemetry remains unavailable. It is not converted to zero
  for quality, cost, or impact claims.
- Richer native evidence may fill fields missing from OTLP; estimates never
  replace exact observed usage.
- Raw attributes support drill-down and repair; canonical tables support
  aggregation. Presentation cards and bounded top-N rows are never aggregation
  inputs.
- Execution units, not whole long-lived sessions, are the comparison samples
  for workflow adherence and impact.

No signal type wins globally. Traces commonly provide hierarchy and timing,
logs and hook facts provide lifecycle detail, and native stores may provide
conversation or usage records. Normalization resolves each fact using explicit
source provenance and field-level evidence.

## Cardinality and privacy

Session, conversation, prompt, tool-call, file, and user identifiers are useful
correlation keys but unsafe default metric dimensions. Aggregate only bounded
dimensions such as canonical agent, model, operation, and a controlled tool
set.

Prompt, response, tool input, paths, and identifiers must remain redacted,
hashed, normalized, or opt-in according to the source contract. Durable
repository facts should use repository-relative paths when a workspace root can
be established.

## Adding or changing a source

A source change should:

1. register current client capabilities in `agent_capabilities.py` when
   applicable
2. map vendor fields into the existing normalization contract at the adapter
   boundary
3. preserve stable source identity, provenance, and raw evidence
4. prove idempotent ingestion, logical-call reconciliation, token provenance,
   and optional-field behavior in tests
5. update this document only when the shared contract changes

Avoid adding a second support registry, session aggregate, workflow identity, or
presentation-derived telemetry path.
