# Reflect Skill — Observability Platform Specification

> **Version:** 0.1.0-draft
> **Last updated:** 2026-03-20
> **Repository:** [o11y-dev/reflect-skill](https://github.com/o11y-dev/reflect-skill)
> **Companion skill:** [o11y-dev/opentelemetry-skill](https://github.com/o11y-dev/opentelemetry-skill)

---

## 1. Product Vision

### Problem

Developers using AI coding agents (Claude Code, Copilot, Cursor, Gemini CLI, etc.) have no unified way to observe, analyze, and improve their AI-assisted workflows. Telemetry data is fragmented across vendors with incompatible formats, support levels vary from full OTel-native to zero instrumentation, and no tool synthesizes this data into actionable insights.

### Solution

The **Reflect skill** completes the o11y-dev platform by adding the analysis and reflection layer on top of the existing [opentelemetry-skill](https://github.com/o11y-dev/opentelemetry-skill) (which handles OTel configuration and education). Reflect collects telemetry from all AI coding agents on a developer's machine, normalizes it into a unified schema, vectorizes it for semantic search, and uses AI to generate developer workflow reports.

### Value Proposition

_"Understand how you and your AI agents work — find bottlenecks, reduce costs, improve outcomes."_

### Target Users

- Individual developers using one or more AI coding agents
- Engineering teams wanting visibility into AI-assisted development patterns
- Tech leads evaluating AI agent ROI and effectiveness

### Ecosystem Fit

| Component | Role |
|-----------|------|
| **opentelemetry-skill** | Configures OTel collection; teaches AI agents about observability best practices |
| **reflect-skill** (this repo) | Analyzes collected telemetry; generates insights, reports, and visualizations |

The opentelemetry-skill is the _input_ layer (getting telemetry flowing). The reflect-skill is the _output_ layer (making telemetry useful).

---

## 2. Installation & Agent Self-Discovery

### Install Flow

```bash
npx @o11y-dev/reflect-skill
```

On first run, the installer performs automatic discovery of AI coding agents installed on the developer's machine.

### Auto-Discovery Mechanism

The discovery engine scans for each supported agent using multiple detection methods:

| Agent | Vendor | Detection Method | OTel Support |
|-------|--------|-----------------|--------------|
| Claude Code | Anthropic | `which claude` + `~/.claude/` directory | Partial (metrics + logs, no traces) |
| Gemini CLI | Google | `which gemini` + `~/.gemini/` directory | Full native |
| GitHub Copilot VS Code | Microsoft | `code --list-extensions \| grep copilot` | Full native |
| GitHub Copilot CLI | Microsoft | `which github-copilot-cli` or `gh copilot` | Full native |
| OpenAI Codex CLI | OpenAI | `which codex` + `~/.codex/` directory | Partial (interactive mode only) |
| Cursor | Cursor | `which cursor` or app bundle detection | Hook-based |
| Windsurf | Codeium | `which windsurf` or app bundle | Hook-based |
| Amazon Q Developer | AWS | `which q` or `~/.aws/q/` directory | Hook-based |
| Aider | Open source | `which aider` or `pip show aider-chat` | Hook-based |
| OpenCode | Anomaly | `which opencode` | Hook-based |
| Qwen Code | Alibaba | `which qwen` + `~/.qwen/` directory | Planned (unshipped) |

> **Source of truth:** [o11y-dev/opentelemetry-skill/references/ai-agents.md](https://github.com/o11y-dev/opentelemetry-skill/blob/main/references/ai-agents.md)

### Post-Discovery Actions

After scanning, the installer:

1. **Displays** discovered agents with their OTel support level (native / partial / hook-based)
2. **Auto-configures** OTel endpoints for native agents (sets `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`)
3. **Installs hook wrappers** for agents without native OTel support (process-level observability)
4. **Starts** the local OTel Collector Docker container
5. **Registers** the `/reflect` slash command in each agent that supports custom commands
6. **Creates** the `~/.reflect/` data directory for local storage

### Example Output

```
 Reflect Skill — Agent Discovery

Found 4 AI coding agents:

  Claude Code (Anthropic)       partial   metrics + logs, synthetic traces
  Gemini CLI (Google)           native    traces, metrics, logs
  Cursor (Cursor)               hooks     process-level via otel-hooks wrapper
  Aider (open source)           hooks     process-level via otel-hooks wrapper

 Configuring OTel endpoints...
 Installing hook wrappers for Cursor, Aider...
 Starting OTel Collector (Docker)...
 Registered /reflect command

Ready. Run /reflect or reflect in terminal.
```

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     Developer Machine                         │
│                                                               │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐               │
│  │ Claude     │ │ Gemini     │ │ Copilot    │  ...agents     │
│  │ Code       │ │ CLI        │ │ VS Code    │               │
│  └─────┬──────┘ └─────┬──────┘ └─────┬──────┘               │
│        │ metrics       │ traces       │ OTLP                 │
│        │ logs          │ metrics      │ HTTP                  │
│        │               │ logs         │                       │
│        └───────────────┼──────────────┘                      │
│                        ▼                                      │
│  ┌───────────────────────────────────────┐                   │
│  │   OTel Collector (Docker)             │                   │
│  │   ─────────────────────────           │                   │
│  │   Receivers:  OTLP gRPC + HTTP        │                   │
│  │   Processors: batch, normalize, OTTL  │                   │
│  │   Exporters:  file (JSONL)            │                   │
│  └──────────────────┬────────────────────┘                   │
│                     ▼                                         │
│  ┌───────────────────────────────────────┐                   │
│  │   Local Storage (~/.reflect/data/)    │                   │
│  │   ─────────────────────────────       │                   │
│  │   traces.jsonl                        │                   │
│  │   metrics.jsonl                       │                   │
│  │   logs.jsonl                          │                   │
│  └──────────────────┬────────────────────┘                   │
│                     ▼                                         │
│  ┌───────────────────────────────────────┐                   │
│  │   Vectorization Engine                │                   │
│  │   ─────────────────────               │                   │
│  │   Embeds telemetry into semantic      │                   │
│  │   chunks; stores in local vector DB   │                   │
│  └──────────────────┬────────────────────┘                   │
│                     ▼                                         │
│  ┌───────────────────────────────────────┐                   │
│  │   AI Reflection Engine                │                   │
│  │   ─────────────────────               │                   │
│  │   RAG retrieval + LLM analysis        │                   │
│  │   Generates insight reports           │                   │
│  └──────────────────┬────────────────────┘                   │
│                     ▼                                         │
│  ┌───────────────────────────────────────┐                   │
│  │   Visualization (HTML/JS)             │                   │
│  │   ─────────────────────               │                   │
│  │   Unified timeline + dashboard        │                   │
│  │   Served on localhost                 │                   │
│  └───────────────────────────────────────┘                   │
└──────────────────────────────────────────────────────────────┘
```

### Data Flow Summary

1. **Collect** — AI agents emit telemetry to local OTel Collector via OTLP
2. **Store** — Collector normalizes and writes to JSONL files in `~/.reflect/data/`
3. **Vectorize** — Telemetry chunks are embedded and stored in a local vector DB
4. **Analyze** — RAG-style retrieval feeds relevant telemetry to the LLM for reflection
5. **Report** — Markdown reports generated; optional HTML dashboard for visualization

---

## 4. Data Model — Unified Telemetry Schema

### Core Challenge

Vendors emit fundamentally different telemetry shapes:
- **Gemini CLI / Copilot:** Full traces with spans, metrics, and logs
- **Claude Code:** Metrics and logs only — no traces
- **Cursor / Aider / etc.:** No native telemetry — hook wrappers capture process-level data

The unified schema must normalize all of these into a single queryable model while preserving data fidelity information.

### Unified Span Model

```json
{
  "id": "span-uuid",
  "trace_id": "trace-uuid",
  "parent_span_id": "span-uuid | null",
  "agent": "claude-code | gemini-cli | copilot-vscode | copilot-cli | codex-cli | cursor | windsurf | amazon-q | aider | opencode | qwen-code",
  "agent_vendor": "anthropic | google | microsoft | openai | cursor | codeium | aws | open-source | alibaba",
  "otel_support": "native | partial | hook-based",
  "timestamp_start": "2026-03-20T10:30:00.000Z",
  "timestamp_end": "2026-03-20T10:30:02.500Z",
  "duration_ms": 2500,
  "kind": "llm_request | tool_call | user_interaction | code_edit | file_read | file_write | shell_exec | test_run | search | unknown",
  "status": "ok | error",
  "attributes": {
    "gen_ai.system": "string",
    "gen_ai.request.model": "string",
    "gen_ai.usage.input_tokens": 0,
    "gen_ai.usage.output_tokens": 0,
    "gen_ai.request.temperature": 0.0,
    "gen_ai.request.max_tokens": 0
  },
  "vendor_attributes": {},
  "synthetic": false,
  "synthetic_source": "metrics | logs | hooks | null"
}
```

### Handling Partial Telemetry

| Scenario | Strategy |
|----------|----------|
| **No traces** (Claude Code) | Generate synthetic spans from metrics timestamps + log entries. Mark `synthetic: true`, `synthetic_source: "metrics"` |
| **Hook-based** (Cursor, Aider, etc.) | Wrap process start/stop as parent spans. Capture stdin/stdout/stderr as child events. Mark `synthetic_source: "hooks"` |
| **Interactive-only** (Codex CLI) | Only collect telemetry during interactive sessions; skip `codex exec` and `codex mcp-server` modes |
| **Missing attributes** | Set to `null`; never fabricate values. The visualization layer handles gaps gracefully |

### Attribute Mapping

All vendor-specific attributes map to [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) where possible. Unmapped vendor-specific attributes are preserved in `vendor_attributes`.

### Cost/Usage Model

```json
{
  "agent": "string",
  "session_id": "string",
  "period_start": "ISO8601",
  "period_end": "ISO8601",
  "tokens_in": 0,
  "tokens_out": 0,
  "total_tokens": 0,
  "estimated_cost_usd": 0.00,
  "cost_source": "telemetry | api | estimated",
  "model": "string",
  "api_key_required": true,
  "api_key_source": "env:ANTHROPIC_API_KEY | config:~/.claude/settings.json | none"
}
```

**Cost data sources** (per vendor):

| Agent | Cost Source | API Key Needed |
|-------|-----------|----------------|
| Claude Code | Token counts from metrics; pricing from Anthropic API | `ANTHROPIC_API_KEY` |
| Gemini CLI | Token counts from spans; pricing from Google AI API | `GOOGLE_API_KEY` |
| Copilot | Subscription-based; no per-token cost | None |
| Codex CLI | Token counts from telemetry; pricing from OpenAI API | `OPENAI_API_KEY` |
| Cursor | Subscription + usage tiers; API for overages | Cursor auth token |
| Others | Estimated from hook-observed session duration | Varies |

---

## 5. The `/reflect` Command

### Dual Entry Points

The reflect command works in two modes:

1. **Slash command** — type `/reflect` inside any supported AI agent (Claude Code, Cursor, etc.)
2. **Standalone CLI** — run `reflect` directly in terminal after npx install

Both modes produce the same output; the slash command variant renders inline in the agent's UI.

### Command Modes

| Command | Description |
|---------|-------------|
| `/reflect` | Summary report of recent sessions (default: last 24h) |
| `/reflect --deep` | Full analysis with vectorized search across all history |
| `/reflect --cost` | Cost/usage breakdown across all agents |
| `/reflect --compare` | Side-by-side agent effectiveness comparison |
| `/reflect --viz` | Open the HTML dashboard in default browser |
| `/reflect --since 7d` | Custom time range (supports `1h`, `24h`, `7d`, `30d`) |
| `/reflect --agent claude-code` | Filter to a specific agent |

### Report Output

**Format:** Markdown, rendered in-terminal or in the agent's UI.

**Report Sections:**

#### 1. Session Summary
- What was worked on (inferred from file paths, commit messages, project directories)
- Which agents were used and for how long
- Session count and total active time

#### 2. Workflow Patterns
- Tool usage frequency (file reads vs. writes vs. shell commands vs. searches)
- Interaction patterns (prompt length, response length, edit cycles)
- Time distribution across activities
- Most-touched files and directories

#### 3. Code Quality Signals
- Error rates and types (compilation errors, test failures, runtime exceptions)
- Debugging cycles (error → fix → retry loops)
- Test interaction patterns (how often tests are run, pass/fail ratios)
- Code review feedback cycles (if detectable from git activity)

#### 4. Agent Effectiveness
- Task completion quality per agent
- Token efficiency (tokens per successful outcome)
- Cost per task/session
- Throughput comparison across agents
- Where each agent excels vs. struggles

#### 5. Recommendations
- Actionable suggestions to improve workflow
- Agent selection recommendations per task type
- Cost optimization opportunities
- Patterns that correlate with higher productivity

---

## 6. Vectorization & AI Reflection Engine

### Embedding Strategy

| Aspect | Approach |
|--------|----------|
| **Chunking** | Group telemetry by session → task → agent. A "task" is inferred from temporal gaps, project switches, or explicit user markers |
| **Embedding model** | Local `all-MiniLM-L6-v2` via ONNX runtime (no external API calls; privacy-preserving) |
| **Vector store** | [LanceDB](https://lancedb.com/) — file-based, no server needed, stores in `~/.reflect/vectors/` |
| **Metadata** | Each vector stores: agent, timestamp range, session ID, task type, file paths touched |

### Reflection Pipeline

```
1. Collect    →  Raw JSONL files land in ~/.reflect/data/ from OTel Collector
2. Normalize  →  Apply unified schema; generate synthetic spans for partial agents
3. Chunk      →  Group by session/task/agent into semantic units
4. Embed      →  Vectorize chunks using local embedding model
5. Query      →  RAG-style retrieval for telemetry relevant to the reflect request
6. Analyze    →  LLM generates insights from retrieved context
7. Report     →  Formatted markdown output
```

### LLM for Reflection

The reflect command uses **the invoking agent's own LLM** to generate the report. When run as a slash command inside Claude Code, it uses Claude. When run inside Gemini CLI, it uses Gemini. When run as a standalone CLI, it prompts the user to select which configured agent to use for analysis.

This means: **no additional API keys needed** for the reflection step — it piggybacks on the agent the developer is already using.

---

## 7. Visualization — Unified Timeline

### Core Challenge

Different vendors provide different levels of trace detail. The visualization must present a coherent unified view without hiding this reality.

### Design Principles

- **Unified timeline** — all agents rendered on a single horizontal timeline
- **Grouped by agent** — each agent gets its own swim lane
- **Color-coded by vendor** — consistent colors for quick identification
- **Data fidelity indicators:**
  - Solid spans = real trace data from native OTel
  - Dashed/hatched spans = synthetic (inferred from metrics/logs/hooks)
  - Gray gaps = no data available
- **Hover for details** — raw vendor-specific attributes shown on hover
- **Missing data callouts** — explicit labels like "No trace data from Claude Code — showing inferred spans"

### Dashboard Sections

1. **Timeline View** — Gantt-style chart of all agent activity across time
2. **Agent Cards** — Per-agent summary with OTel support level badge, session count, token usage
3. **Cost Ticker** — Running cost across all vendors with per-agent breakdown
4. **Insights Panel** — AI-generated observations from the most recent `/reflect` run

### Tech Stack

- **Single-file HTML + vanilla JS** — no build step, no dependencies
- Served on `localhost:<port>` via the reflect skill's built-in HTTP server
- Data loaded via JSON API from the local storage
- Charts rendered with Canvas API (no external charting library)

### Visualization To-Do

The concept HTML/script files will be provided separately and integrated in Phase 3. The spec defines the data contracts and layout; the implementation will iterate on the concept files.

---

## 8. OTel Collector Configuration

### Docker Setup

Auto-created during `npx` install:

```yaml
# docker-compose.yml
services:
  reflect-collector:
    image: otel/opentelemetry-collector-contrib:latest
    container_name: reflect-otel-collector
    restart: unless-stopped
    ports:
      - "4317:4317"   # OTLP gRPC
      - "4318:4318"   # OTLP HTTP
    volumes:
      - ./collector-config.yaml:/etc/otelcol-contrib/config.yaml
      - ${HOME}/.reflect/data:/data
    environment:
      - REFLECT_DATA_DIR=/data
```

### Collector Pipeline

```yaml
# collector-config.yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 5s
    send_batch_size: 100

  attributes:
    actions:
      # Normalize vendor-specific attributes to GenAI semconv
      - key: gen_ai.system
        action: upsert
      - key: reflect.agent
        action: insert
      - key: reflect.synthetic
        value: false
        action: insert

  transform:
    # OTTL transformations for cross-vendor normalization
    trace_statements:
      - context: span
        statements: []
    log_statements:
      - context: log
        statements: []

exporters:
  file/traces:
    path: /data/traces.jsonl
    format: json
  file/metrics:
    path: /data/metrics.jsonl
    format: json
  file/logs:
    path: /data/logs.jsonl
    format: json

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch, attributes, transform]
      exporters: [file/traces]
    metrics:
      receivers: [otlp]
      processors: [batch, attributes]
      exporters: [file/metrics]
    logs:
      receivers: [otlp]
      processors: [batch, attributes]
      exporters: [file/logs]
```

---

## 9. Agent Integration Details

### Per-Agent Configuration

#### Agents with Native OTel Support

For Gemini CLI, Copilot VS Code, Copilot CLI — the installer sets:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

Plus agent-specific env vars:
- Gemini: `GEMINI_TELEMETRY_ENABLED=1`
- Copilot: `COPILOT_OTEL_ENABLED=1`
- Claude Code: `CLAUDE_CODE_ENABLE_TELEMETRY=1`

#### Agents with Partial Support

- **Claude Code:** Emits cumulative metrics and logs but no traces. Configure via `~/.claude/settings.json` or `OTEL_*` env vars. Synthetic traces generated post-collection.
- **Codex CLI:** Only emits in interactive mode. Configure in `~/.codex/config.toml`.

#### Agents Requiring Hook-Based Instrumentation

For Cursor, Windsurf, Amazon Q, Aider, OpenCode — install the `opentelemetry-hooks` wrapper:

```bash
# The hooks wrapper observes process-level signals:
# - Process start/stop times
# - stdin/stdout/stderr capture (configurable)
# - File system events during session
# - Network activity (OTLP-formatted)
```

This is coordinated with the opentelemetry-skill which defines the hook patterns.

### How opentelemetry-skill Feeds into reflect-skill

1. **opentelemetry-skill** teaches the AI agent how to configure OTel properly
2. **reflect-skill** installer calls on that knowledge to auto-configure each agent
3. Telemetry flows to the shared local OTel Collector
4. **reflect-skill** reads from the Collector's output files

They share the same Docker Collector instance — `opentelemetry-skill` defines the pipeline, `reflect-skill` consumes the output.

---

## 10. Implementation Phases

### Phase 1 — Foundation (MVP)

> Get telemetry flowing and produce a basic report.

- [ ] `npx` installer with agent auto-discovery (`bin/reflect.js`)
- [ ] Agent detection for all 11 supported agents
- [ ] Docker-based OTel Collector with JSONL file export
- [ ] Unified data model implementation (`src/normalize/schema.js`)
- [ ] Basic `/reflect` command — session summary from raw JSONL (no vectorization)
- [ ] CLI entry point (`reflect` terminal command)
- [ ] Slash command registration for Claude Code

### Phase 2 — Intelligence

> Add semantic understanding and AI-powered analysis.

- [ ] Vectorization engine: LanceDB + local ONNX embeddings (`src/vectorize/engine.js`)
- [ ] Synthetic span generation for partial-support agents
- [ ] AI reflection pipeline: RAG retrieval + LLM analysis (`src/reflect/analyze.js`)
- [ ] `/reflect --deep` with historical analysis
- [ ] Full report generation with all 5 sections
- [ ] `/reflect --since` time range filtering

### Phase 3 — Visualization

> Build the HTML dashboard and unified timeline.

- [ ] HTML/JS dashboard (`src/viz/dashboard.html`)
- [ ] Unified timeline with swim lanes per agent
- [ ] Agent cards with support level badges
- [ ] `/reflect --viz` to launch dashboard server
- [ ] Integrate concept HTML/scripts (to be provided by author)
- [ ] Data fidelity indicators (solid vs. dashed spans)

### Phase 4 — Cost & Multi-Vendor Intelligence

> Enable cost tracking and cross-agent comparison.

- [ ] `/reflect --cost` with per-vendor cost breakdown
- [ ] API key management for vendor usage/billing APIs
- [ ] `/reflect --compare` agent effectiveness comparison
- [ ] Cost ticker in dashboard
- [ ] Export/share reports (markdown file, clipboard)
- [ ] Recommendation engine improvements

---

## 11. Repository File Structure

```
reflect-skill/
├── SPEC.md                        # This specification
├── SKILL.md                       # Skill prompt (cognitive router for /reflect)
├── package.json                   # npm package — npx entry point
├── bin/
│   └── reflect.js                 # CLI entry — npx runner + standalone command
├── src/
│   ├── discovery/
│   │   └── agents.js              # Agent auto-discovery logic
│   ├── collector/
│   │   ├── docker-compose.yml     # OTel Collector container definition
│   │   └── config.yaml            # Collector pipeline configuration
│   ├── normalize/
│   │   └── schema.js              # Unified telemetry normalization
│   ├── vectorize/
│   │   └── engine.js              # Embedding + vector store (LanceDB)
│   ├── reflect/
│   │   └── analyze.js             # AI reflection pipeline (RAG + LLM)
│   └── viz/
│       ├── dashboard.html         # Single-file visualization
│       └── server.js              # localhost HTTP server for dashboard
├── references/
│   └── ai-agents.md               # Supported agents reference (synced from otel skill)
└── tests/
    ├── discovery.test.js
    ├── normalize.test.js
    └── reflect.test.js
```

---

## 12. Open Questions

1. **Visualization concept files** — To be provided by author; will be integrated in Phase 3
2. **Hook wrapper distribution** — Should `opentelemetry-hooks` be bundled in reflect-skill or remain a separate package in the o11y-dev org?
3. **Multi-machine** — Is there a future need to aggregate telemetry across multiple developer machines (team dashboards)?
4. **Data retention** — How long should JSONL files be kept? Default rotation policy TBD
5. **Qwen Code** — OTel support is documented but unshipped as of March 2026; treat as planned and re-evaluate
