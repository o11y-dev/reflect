# reflect

[![PyPI](https://img.shields.io/pypi/v/o11y-reflect)](https://pypi.org/project/o11y-reflect/)
[![Python](https://img.shields.io/pypi/pyversions/o11y-reflect)](https://pypi.org/project/o11y-reflect/)
[![License](https://img.shields.io/github/license/o11y-dev/reflect)](LICENSE)
[![CI](https://github.com/o11y-dev/reflect/actions/workflows/test.yml/badge.svg)](https://github.com/o11y-dev/reflect/actions/workflows/test.yml)

**See why your AI coding agents fail, stall, or burn budget.**

Local-first telemetry for Claude Code, GitHub Copilot, Gemini CLI, and Cursor. One install, real insight, no cloud required.

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

`reflect setup` modifies your agent config files to install OpenTelemetry hooks (e.g. `~/.claude/settings.json` for Claude Code, `~/.config/github-copilot/` for Copilot) and starts writing spans to `~/.reflect/state/`. `reflect` then reads those spans and renders an interactive terminal dashboard.

**No telemetry yet?** Try the demo:

```bash
reflect --demo
```

## How it works

AI coding agents like Claude Code and Copilot support **hooks** — scripts that fire at key lifecycle moments (session start, tool call, prompt, stop). `reflect setup` installs a small OpenTelemetry instrumentation layer into each agent's config. From that point on, every tool call, token usage event, and session boundary is recorded as an **OTLP span** and written locally to `~/.reflect/state/`.

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
- **MCP server tracking** — usage counts and availability gaps
- **Subagent patterns** — delegation frequency and types
- **Activity heatmaps** — by hour and day of week
- **Actionable recommendations** — based on your actual usage patterns

## Output modes

```bash
reflect                        # interactive terminal dashboard (default)
reflect --no-terminal          # markdown report
reflect --dashboard-artifact out.json  # JSON artifact for dashboards
reflect --publish              # open local dashboard in browser
reflect --demo                 # instant demo with sample data
```

## Health check

```bash
reflect doctor
reflect update
```

`reflect doctor` checks that your installation is healthy: hooks are wired correctly, the installed package matches the latest release, and skill files are up to date. `reflect update --apply` upgrades the pipx package when a newer release is available.

## Supported agents

| Agent | Path | Confidence |
|---|---|---|
| Claude Code | Native logs + hooks | High |
| GitHub Copilot CLI | Native OTel + hooks | High |
| VS Code Copilot | Native OTel | High |
| Gemini CLI | Native OTel + hooks | High |
| Codex | Native OTel + hooks | Medium |
| Cursor | Session/log adapters | Medium |
| OpenCode | Hooks/plugin | Medium |

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
reflect --dashboard-artifact docs/reports/latest.json --publish
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
  --publish                    Open dashboard in browser
  --demo                       Run with bundled sample data
  --help                       Show help

Commands:
  setup    Install hooks, wire agents, configure telemetry
  doctor   Check installation health and agent status
  update   Check release drift and optional package upgrade
```

## Data flow

```
reflect setup
    ├── installs opentelemetry-hooks
    ├── wires agent configs (Claude, Copilot, Gemini)
    ├── distributes skill packages
    └── enables local span export to ~/.reflect/state/

Your AI tool → hooks capture spans → ~/.reflect/state/

reflect → reads traces → terminal dashboard / report / hosted view
```

## Skill package

`reflect` ships with a portable skill for Claude Code. After `reflect setup`, the `/reflect` skill is available in your Claude Code session for in-session telemetry analysis.

## Analysis schema

See [`docs/ai-observability-schema.md`](docs/ai-observability-schema.md) for the canonical cross-tool analysis schema.

## Part of o11y.dev

`reflect` is the entry point to the [o11y.dev](https://o11y.dev) ecosystem. `reflect setup` handles the required pieces automatically; the rest are optional:

- **[opentelemetry-hooks](https://github.com/o11y-dev/opentelemetry-hooks)** — instrumentation engine that captures spans from AI coding agents *(installed by `reflect setup`)*
- **[opentelemetry-skill](https://github.com/o11y-dev/opentelemetry-skill)** — observability knowledge layer for AI assistants *(optional)*
- **[gateway](https://github.com/o11y-dev/gateway)** — OTLP gateway for team or hosted telemetry *(optional)*

## License

[Apache-2.0](LICENSE)
