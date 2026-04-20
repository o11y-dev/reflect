```
░▒▓███████▓▒░░▒▓████████▓▒░▒▓████████▓▒░▒▓█▓▒░      ░▒▓████████▓▒░▒▓██████▓▒░▒▓████████▓▒░
░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░      ░▒▓█▓▒░      ░▒▓█▓▒░      ░▒▓█▓▒░     ░▒▓█▓▒░░▒▓█▓▒░ ░▒▓█▓▒░
░▒▓███████▓▒░░▒▓██████▓▒░ ░▒▓██████▓▒░ ░▒▓█▓▒░      ░▒▓██████▓▒░░▒▓█▓▒░        ░▒▓█▓▒░
░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░      ░▒▓█▓▒░      ░▒▓█▓▒░      ░▒▓█▓▒░     ░▒▓█▓▒░░▒▓█▓▒░ ░▒▓█▓▒░
░▒▓█▓▒░░▒▓█▓▒░▒▓████████▓▒░▒▓█▓▒░      ░▒▓████████▓▒░▒▓████████▓▒░▒▓██████▓▒░  ░▒▓█▓▒░
```
# 

[![PyPI](https://img.shields.io/pypi/v/o11y-reflect)](https://pypi.org/project/o11y-reflect/)
[![Python](https://img.shields.io/pypi/pyversions/o11y-reflect)](https://pypi.org/project/o11y-reflect/)
[![License](https://img.shields.io/github/license/o11y-dev/reflect)](LICENSE)
[![CI](https://github.com/o11y-dev/reflect/actions/workflows/test.yml/badge.svg)](https://github.com/o11y-dev/reflect/actions/workflows/test.yml)

**Your AI agents are doing things you can't see. reflect shows you.**

Local-first telemetry for Claude Code, GitHub Copilot, Gemini CLI, and Cursor — token spend, tool failure rates, latency, and what's actually burning your budget. No cloud. No account. Runs on your machine.

```
$ reflect --demo

─────────── AI Usage Dashboard  All time  (2026-03-16 → 2026-03-23) ────────────

╭────────────────────────────────── Insights ──────────────────────────────────╮
│ ✓ Good prompt-to-action ratio — 4.2 tool calls per prompt, showing           │
│   effective task delegation.                                                 │
│ ✓ Effective subagent delegation — 1 Task subagent, keeping main context      │
│   focused.                                                                   │
│ ⚠ 7 tool failures (20.6% of tool calls). Path and schema validation up       │
│   front can reduce iteration cost.                                           │
│ ⚠ Top session consumed 42% of all tokens — context blowout pattern.          │
│ → Use a fixed prompt contract: Goal, Context, Constraints, Output, Done-when │
│ → Pin relevant files in the first prompt to reduce exploratory tool churn.   │
╰──────────────────────────────────────────────────────────────────────────────╯

╭── Quality Score ──╮ ╭─── Sessions ────╮ ╭── Active Days ──╮
│       75.0%       │ │        8        │ │        8        │
╰───────────────────╯ ╰─────────────────╯ ╰─────────────────╯
╭───── Prompts ─────╮ ╭── Tool/Prompt ──╮ ╭─── Failure % ───╮
│         8         │ │      4.2:1      │ │      20.6%      │
╰───────────────────╯ ╰─────────────────╯ ╰─────────────────╯

╭────────────────────────────── Agent Comparison ──────────────────────────────╮
│                                                        Top    In    Out  Fail │
│   Agent     Sess  Events  Quality      Top Model       Tool  Tok    Tok     % │
│  ──────────────────────────────────────────────────────────────────────────  │
│   claude       4      46  ████░ High   sonnet-4-5      Read  275K  44.5K  16% │
│   copilot      2      20  ████░ High   gpt-4o          Read   33K   6.3K  12% │
│   cursor       1      11  █░░░░ Low    —               Write  95K   8.0K  60% │
│   gemini       1       8  ████░ High   gemini-2.0-fla… Read   12K   2.5K   0% │
╰──────────────────────────────────────────────────────────────────────────────╯

╭───────────────────────────── Sessions (8 total) ─────────────────────────────╮
│   Session                    Agent     Started (UTC)      Score   In Tok      │
│  ──────────────────────────────────────────────────────────────────────────  │
│   implement the entire da…   claude    2026-03-16 20:10      60   180.0K      │
│   migrate the users table…   cursor    2026-03-20 17:25      20    95.0K      │
│   investigate the memory …   claude    2026-03-22 14:55      80    45.0K      │
│   refactor the auth modul…   claude    2026-03-23 10:10      90    28.0K      │
│   add cursor-based pagina…   copilot   2026-03-21 10:40      80    18.0K      │
│   fix the token expiry bu…   copilot   2026-03-17 09:40      90    15.0K      │
│   review PR #142 for secu…   gemini    2026-03-18 16:03      90    12.0K      │
╰──────────────────────────────────────────────────────────────────────────────╯

─────────────────────────────── reflect.o11y.dev ───────────────────────────────
```

> Run this yourself: `pipx install o11y-reflect && reflect --demo`

## Requirements

- Python 3.11+
- [pipx](https://pipx.pypa.io/stable/installation/) (recommended) or pip

## Quickstart

```bash
pipx install o11y-reflect
reflect setup
# use your AI tool normally for a bit, then:
reflect
```

`reflect setup` modifies config files for the agents with implemented telemetry wiring today (Claude Code, GitHub Copilot, Gemini CLI, and Codex native OTel), starts a local OTLP gateway on ports 4317 (gRPC) and 4318 (HTTP), and begins writing spans to `~/.reflect/state/`. `reflect` then reads those spans and renders an interactive terminal dashboard.

**No telemetry yet?** Try the demo:

```bash
reflect --demo
```

## What people actually find

Running `reflect` for the first time is usually surprising:

- One session consumed 30–40% of your total tokens (almost always a context blowout, not useful work)
- Your tool failure rate is higher than you thought — Bash failures often go unnoticed because the agent silently retries
- Cache hit rate varies dramatically by agent; switching prompt style can cut costs 30–50%
- If you use multiple agents, one is almost always measurably more efficient than the others for the same class of task

## How it works

`reflect` takes care of instrumentation and session data collection for the integrations that are implemented today. AI coding agents expose telemetry in two ways, and `reflect setup` uses whichever the verified integration supports:

- **Hooks** (Claude Code today) — scripts that fire at key lifecycle moments (session start, tool call, prompt, stop). `reflect setup` installs a small [opentelemetry-hooks](https://github.com/o11y-dev/opentelemetry-hooks) instrumentation layer into the agent's config file where that path is verified.
- **Native OpenTelemetry** (Claude Code, GitHub Copilot, Gemini CLI, OpenAI Codex CLI) — the agent has built-in OTLP export that just needs to be pointed at the local collector. `reflect setup` writes the relevant settings for each:
  - Claude Code: `env` block in `~/.claude/settings.json` (logs locally; traces still come from hooks/session stores)
  - GitHub Copilot VS Code: `github.copilot.chat.otel.*` keys in VS Code `settings.json`
  - GitHub Copilot CLI: `COPILOT_OTEL_ENABLED` / `COPILOT_OTEL_OTLP_ENDPOINT` env vars
  - Gemini CLI: `telemetry.*` keys in `~/.gemini/settings.json` (e.g. `telemetry.enabled`, `telemetry.otlpEndpoint`)
  - OpenAI Codex CLI: `[otel]` section in `~/.codex/config.toml` with explicit trace/log exporters (interactive mode only)

`reflect` records whichever verified local signal path is available. Hook-based flows emit OTLP spans for tool calls, token usage events, and session boundaries; native OTel flows write traces and/or logs depending on the agent.

When you run `reflect`, it:

1. **Reads spans** from `~/.reflect/state/` (or falls back to each agent's native session logs if hooks aren't available)
2. **Normalizes** them into a single cross-agent data model — so a Claude tool call and a Copilot tool call look the same
3. **Aggregates** per-session and cross-session metrics: token totals, tool failure rates, latency percentiles, subagent delegation patterns
4. **Renders** the results as a terminal dashboard, markdown report, or JSON artifact for a hosted web view

Nothing leaves your machine. There's no cloud backend, no account, no API key.

## What you get

- **Token economy** — input, output, cache hits, largest-session concentration
- **Tool efficiency** — failure rates, latency percentiles (p50/p90/p95/p99), tool-to-prompt ratio
- **Agent comparison** — side-by-side across Claude, Copilot, Gemini, Cursor
- **Model breakdown** — which models you're actually using and how much
- **MCP server tracking** — observed usage counts and completion gaps from recorded MCP events
- **Subagent patterns** — delegation frequency and types
- **Activity heatmaps** — by hour and day of week
- **Actionable recommendations** — based on your actual usage patterns

## Output modes

```bash
reflect                        # interactive terminal dashboard (default)
reflect --no-terminal          # markdown report
reflect --dashboard-artifact out.json  # JSON artifact for dashboards
reflect report                 # open local dashboard in browser
reflect skills                 # extract reusable skills from your sessions
reflect --demo                 # instant demo with sample data
```

## Local OTLP gateway

`reflect setup` automatically starts a lightweight OTLP gateway that listens for telemetry from all agents:

- **gRPC** on `127.0.0.1:4317` (Claude Code, Gemini CLI, Codex, otel-hook)
- **HTTP** on `127.0.0.1:4318` (GitHub Copilot)

The gateway writes received traces and logs as JSON lines to `~/.reflect/state/otlp/`, the same files `reflect` already reads. You can also manage the gateway manually:

```bash
reflect gateway start          # start as background daemon
reflect gateway stop           # stop the daemon
reflect gateway status         # check if running, show file sizes
reflect gateway --foreground   # run in foreground (for debugging)
```

## Health check

```bash
reflect doctor
reflect update
```

`reflect doctor` checks that your installation is healthy, shows which integrations are implemented vs still planned, and reports whether hooks are wired correctly, the OTLP gateway is running, the installed package matches the latest release, and skill files are up to date. `reflect update --apply` upgrades the pipx package when a newer release is available.

### Native OTel details by agent

- **Claude Code** — `reflect setup` writes a `settings.json` `env` block with `CLAUDE_CODE_ENABLE_TELEMETRY=1`, OTLP endpoint/protocol keys, and `OTEL_LOGS_EXPORTER=otlp`. Claude's native path does not currently give `reflect` local traces, so hook spans or local session stores still provide tool-call/session coverage.
- **GitHub Copilot VS Code** — `reflect setup` writes `github.copilot.chat.otel.enabled`, `github.copilot.chat.otel.otlpEndpoint`, `github.copilot.chat.otel.exporterType`, and `github.copilot.chat.otel.captureContent=false` in VS Code `settings.json`. The local gateway target is HTTP (`127.0.0.1:4318`) for Copilot.
- **GitHub Copilot CLI** — `reflect setup` also writes `COPILOT_OTEL_ENABLED=true` and `COPILOT_OTEL_OTLP_ENDPOINT=http://localhost:4318` into the same VS Code `env` block used by the CLI.
- **Gemini CLI** — `reflect setup` writes `telemetry.enabled`, `telemetry.target=local`, `telemetry.useCollector=true`, `telemetry.otlpEndpoint`, `telemetry.otlpProtocol`, and `telemetry.logPrompts=false`. If `~/.gemini/settings.json` does not exist yet, `reflect` leaves guidance only and `reflect doctor` will report the native path as missing.
- **OpenAI Codex CLI** — `reflect setup` writes a full `[otel]` section in `~/.codex/config.toml` with explicit `traces_exporter`, `traces_endpoint`, `logs_exporter`, `logs_endpoint`, and `log_user_prompt=false`, while preserving unrelated TOML sections.

### Why this differs from vendor OTLP guides

- `reflect` always points agents at a **local collector first**, not directly at a SaaS endpoint.
- The local setup path does **not** add auth headers or vendor-specific routing attributes.
- `reflect`'s bundled gateway currently persists **traces and logs only**. Even if an agent can emit OTLP metrics, metrics are not yet written into the local OTLP JSON cache.
- `reflect doctor` distinguishes between a native config that is absent, incomplete, unreadable, or ready so you can repair only the missing part.

## Agent instrumentation landscape

reflect's mission is to make every AI coding agent observable with zero manual instrumentation. Today, though, only a subset of integrations have verified telemetry collection. `reflect setup` detects agent homes for guidance, but it only starts collection where wiring and parsing are implemented.

| Agent | Instrumentation | What you get | Confidence |
|---|---|---|---|
| Claude Code | Native OTel + hooks | Native logs plus hook/session-based traces, tool calls, and sessions | High |
| GitHub Copilot VS Code | Native OTel | Traces + logs to the local gateway with content capture disabled | High |
| GitHub Copilot CLI | Native OTel + hooks | Traces + logs via native OTel, plus hook coverage where available | High |
| Gemini CLI | Native OTel + hooks | Traces + logs via native OTel, with prompt logging disabled by default | High |
| OpenAI Codex CLI | Native OTel (interactive) | Traces + logs in interactive mode only | Medium |
| Cursor | Session/log adapters | Tool calls, sessions, rough token estimates when exact usage is missing (`len(text) / 4`) | Medium |
| Windsurf, Trae, Cline, Roo Code, Goose, OpenHands, Amp, Continue, iFlow, Pi, OpenClaw | Not implemented yet | Detection, config snapshots, and skill distribution only | Planned |

**Why Cursor is only medium confidence:** local Cursor transcripts do not contain exact per-session usage, so reflect falls back to a rough `len(text) / 4` estimate when provider-side token usage is unavailable.

**Instrumentation paths:**
- **Native OTel** — agent has built-in OTLP export; reflect configures it to point at the local collector
- **Hooks** — `opentelemetry-hooks` intercepts agent lifecycle events (session start, tool calls, stop)
- **Session/log adapters** — reflect reads the agent's local session files directly when spans aren't available

When hook spans and OTLP traces are absent, `reflect` falls back to rich local session stores:

- Cursor: `~/.cursor/projects/**/agent-transcripts/**/*.jsonl`
- Copilot: `~/.copilot/session-state/*/events.jsonl`
- Claude Code: `~/.claude/projects/**/*.jsonl`
- Gemini: `~/.gemini/tmp/**/chats/session-*.json`

## Advanced usage

### Direct OTLP traces

If you already have OTLP JSON traces from a collector, skip setup:

```bash
reflect --otlp-traces path/to/otel-traces.json
```

A sibling `otel-logs.json` file is used automatically for enrichment when present.

### Hosted dashboard

Write a JSON artifact for GitHub Pages or a local server:

```bash
reflect --dashboard-artifact docs/reports/latest.json
```

For a safe public example, this repo also ships a curated GitHub Pages demo:

- `https://reflect.o11y.dev/showcase.html`

### All options

```
reflect [OPTIONS] [COMMAND]

Options:
  --sessions-dir PATH          Session metadata JSON directory
  --spans-dir PATH             Local span JSONL directory
  --otlp-traces PATH           OTLP JSON traces file
  --output PATH                Markdown report output path
  --terminal / --no-terminal   Terminal dashboard (default) or markdown report
  --dashboard-artifact PATH    Write dashboard JSON artifact
  --demo                       Run with bundled sample data
  --help                       Show help

Commands:
  setup    Install hooks, wire agents, configure telemetry, start gateway
  doctor   Check installation health and agent status
  update   Check release drift and optional package upgrade
  report   Open the AI usage dashboard in a browser
  skills   Extract reusable skills from your session history
  gateway  Manage the local OTLP gateway (start/stop/status)
```

## Data flow

```
reflect setup
    ├── installs opentelemetry-hooks
    ├── edits each agent's settings file to enable telemetry
    │       via hooks        Claude Code  → ~/.claude/settings.json
    │                        Codex CLI    → ~/.codex/config.toml
    │       via native otel  Claude Code  → ~/.claude/settings.json  (env block, metrics+logs)
    │                        Copilot VS Code → VS Code settings.json (otel.* keys)
    │                        Copilot CLI  → VS Code settings.json  (env block)
    │                        Gemini CLI   → ~/.gemini/settings.json  (telemetry.* keys)
    │                        Codex CLI    → ~/.codex/config.toml    ([otel] section)
    ├── starts local OTLP gateway (gRPC :4317, HTTP :4318)
    ├── distributes skill packages
    └── enables local span export to ~/.reflect/state/

Your AI tool → hooks -or- native OTLP → gateway → ~/.reflect/state/otlp/

reflect → reads traces → terminal dashboard / report / hosted view
```

## Skill package

`reflect` ships with a portable skill for Claude Code. After `reflect setup`, the `/reflect` skill is available in your Claude Code session for in-session telemetry analysis.

## Analysis schema

See [`docs/ai-observability-schema.md`](docs/ai-observability-schema.md) for the canonical cross-tool analysis schema.

## License

[Apache-2.0](LICENSE)
