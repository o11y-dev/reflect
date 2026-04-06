# AI observability schema for `reflect`

This document is the canonical analysis-side schema for `reflect`.

It defines how `reflect` should interpret, normalize, and merge telemetry from multiple AI tools after the data has already been captured.

## Why this lives in `reflect`

Configuring OpenTelemetry and analyzing OpenTelemetry are different responsibilities:

- capture/configuration belongs in agent setup, skills, collectors, and hook tooling
- interpretation/normalization belongs in `reflect`

`reflect` is where we answer questions like:

- which fields are safe for shared dashboards
- which vendor-specific signals should be preserved for drill-down
- how Claude, Gemini, Copilot, Codex, and future tools line up semantically
- how traces, logs, and metrics should be merged into one analysis model

## Core analysis model

`reflect` should treat incoming telemetry as two layers:

1. **Portable semantic layer**
   - standard OpenTelemetry and GenAI semantic-convention fields
   - used for shared dashboards, comparisons, alerts, and longitudinal analysis

2. **Vendor-native operational layer**
   - tool-specific fields and event names
   - used for product-specific drill-down, debugging, and explanation

The rule is:

- normalize to shared fields for rollups
- preserve native fields for forensics

## Preferred shared attributes

These are the preferred cross-tool attributes `reflect` should use internally when they are available or can be derived safely.

| Concept | Preferred shared field | Notes |
|--------|-------------------------|-------|
| Agent identity | `gen_ai.system` | Primary dimension for cross-tool comparisons |
| Conversation/session correlation | `gen_ai.conversation.id` | Shared conceptual field; keep vendor-native source too |
| Prompt/turn correlation | `gen_ai.prompt.id` | Use only when the source exposes a stable prompt-level identifier |
| Request model | `gen_ai.request.model` | Model requested by the agent |
| Response model | `gen_ai.response.model` | Model actually used/returned |
| Operation name | `gen_ai.operation.name` | High-level action such as chat, completion, tool call |
| Tool name | `gen_ai.tool.name` | Standardized tool/function name |
| Tool call id | `gen_ai.tool.call_id` | Only when the source exposes a stable identifier |
| Input tokens | `gen_ai.usage.input_tokens` | Prefer semconv attrs/metrics where available |
| Output tokens | `gen_ai.usage.output_tokens` | Prefer semconv attrs/metrics where available |
| Operation latency | `gen_ai.client.operation.duration` | Shared latency measure for GenAI agents |
| Finish reason | `gen_ai.response.finish_reason` | Used for stop/error/limit analysis |

## Cross-tool mapping

This table defines how current tools should map into the shared model.

| Concept | Claude Code | Gemini CLI | GitHub Copilot | OpenAI Codex CLI |
|--------|-------------|------------|----------------|------------------|
| Agent identity | derive `gen_ai.system=claude_code` from `service.name` or `claude_code.*` | native `gen_ai.system`; also `gen_ai.agent.name=gemini-cli` | native `gen_ai.system` | derive `gen_ai.system=codex_cli` from `service.name` or `codex.*` |
| Conversation/session correlation | `session.id` | native `gen_ai.conversation.id` | `gen_ai.thread.id` | `session.id` |
| Prompt/turn correlation | `prompt.id` | `prompt_id` | use trace/thread linkage; no single universal public prompt field | vendor/session-local fields only |
| Request model | map `model` to `gen_ai.request.model` | native `gen_ai.request.model` | native request model fields | map `model` to `gen_ai.request.model` |
| Response model | map `model` to `gen_ai.response.model` when only one model field exists | native `gen_ai.response.model` | native response model fields | map `model` to `gen_ai.response.model` |
| Operation name | derive from event/span type | native `gen_ai.operation.name` | native `gen_ai.operation.name` | derive from event/span type |
| Tool name | map `tool.name` to `gen_ai.tool.name` | map `function_name` or native span attr to `gen_ai.tool.name` | native tool-related attrs where emitted | map vendor tool field to `gen_ai.tool.name` |
| Tool call id | derive only if a stable id exists | native `gen_ai.tool.call_id` on spans | vendor-dependent | vendor-dependent |
| Input tokens | map from `claude_code.tokens.input` | native GenAI attrs/metrics | native GenAI attrs/metrics | map from `codex.tokens.used{direction=input}` |
| Output tokens | map from `claude_code.tokens.output` | native GenAI attrs/metrics | native GenAI attrs/metrics | map from `codex.tokens.used{direction=output}` |
| Latency | map from `claude_code.api.request.duration` | native `gen_ai.client.operation.duration` | native `gen_ai.client.operation.duration` | map from `codex.request.latency` |
| Finish reason | derive when exposed | map from `finish_reasons` or native attrs | native `gen_ai.response.finish_reason` | derive when exposed |

## Vendor-native operational layer

`reflect` should preserve vendor-native fields because they often carry the most explanatory detail.

Examples:

- `claude_code.*` for Claude-specific usage, cache, and request behavior
- `gemini_cli.*` for routing, tool decisions, retries, startup, agent lifecycle, and extensions
- `codex.*` for Codex-specific session and execution behavior

These should not be discarded during normalization.

Instead:

- promote shared equivalents when safe
- retain the original fields alongside the normalized record

## Metric-dimension safety rules

Some fields are excellent correlation keys and terrible metric dimensions.

| Field | Use in metrics? | Use in logs/traces? | Notes |
|------|------------------|---------------------|-------|
| `session.id` | No | Yes | Unbounded cardinality |
| `prompt.id` | No | Yes | Unbounded cardinality |
| `gen_ai.thread.id` | No | Yes | Unbounded cardinality |
| `gen_ai.conversation.id` | No | Yes | Treat as correlation field, not metric dimension |
| `user.id` | Sometimes | Yes | Only as metric dimension for small, bounded populations |
| `gen_ai.system` | Yes | Yes | Safe shared grouping field |
| `model` / `gen_ai.request.model` | Yes | Yes | Safe shared grouping field |
| `gen_ai.operation.name` | Yes | Yes | Safe shared grouping field |
| `gen_ai.tool.name` | Usually | Yes | Safe if the tool set is bounded |

## Merge rules for traces, logs, and metrics

`reflect` should merge signals with a clear precedence model:

### 1. Traces

Use traces as the primary structure for:

- execution hierarchy
- step ordering
- tool nesting
- latency attribution
- agent-to-tool flow reconstruction

### 2. Logs/events

Use logs/events to enrich traces with:

- prompt-level detail
- approval decisions
- routing rationale
- retries, fallbacks, and truncation
- lifecycle milestones not represented as spans

### 3. Metrics

Use metrics for:

- longitudinal rollups
- cost/token trend analysis
- latency percentiles
- comparative dashboards
- health and volume summaries

### Precedence rule

When the same concept appears in multiple signals:

- use **traces** for structure and sequencing
- use **logs/events** for explanatory detail
- use **metrics** for aggregate rollups

## Guidance for future tool onboarding

When adding a new tool, `reflect` should document four things:

1. the tool's **portable semantic layer**
2. the tool's **vendor-native operational layer**
3. the mapping between vendor fields and shared fields
4. any cardinality or privacy caveats

This keeps analysis consistent even when telemetry maturity varies widely across tools.

## Current position

Today:

- OpenTelemetry skills and hook tooling should explain how to **configure and emit** telemetry
- `reflect` should define how to **interpret and merge** that telemetry

That separation keeps the capture side flexible and the analysis side coherent.
