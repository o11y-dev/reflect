# reflect

[![PyPI](https://img.shields.io/pypi/v/o11y-reflect)](https://pypi.org/project/o11y-reflect/)
[![Python](https://img.shields.io/pypi/pyversions/o11y-reflect)](https://pypi.org/project/o11y-reflect/)
[![License](https://img.shields.io/github/license/o11y-dev/reflect)](LICENSE)
[![CI](https://github.com/o11y-dev/reflect/actions/workflows/test.yml/badge.svg)](https://github.com/o11y-dev/reflect/actions/workflows/test.yml)

**See why your AI coding agents fail, stall, or burn budget.**

Local-first telemetry for Claude Code, GitHub Copilot, Gemini CLI, and Cursor. One install, real insight, no cloud required.

## Quickstart

```bash
pipx install o11y-reflect
reflect setup
# use your AI tool normally, then:
reflect
```

That's it. `reflect setup` installs hooks, wires your agents, and starts capturing telemetry to `~/.reflect/state/`. `reflect` renders an interactive terminal dashboard.

**No telemetry yet?** Try the demo:

```bash
reflect --demo
```

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
reflect --dashboard-artifact out.json  # JSON for hosted dashboards
reflect --publish              # open hosted dashboard in browser
reflect --demo                 # instant demo with sample data
```

## Health check

```bash
reflect doctor
reflect update
```

`reflect doctor` includes an update advisor for package drift, live pipx drift, skill-copy drift, and hook wiring drift. `reflect update --apply` upgrades the pipx package when a newer release is available.

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
- direct dashboard view: `https://reflect.o11y.dev/demo.html`

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

`reflect` ships with a portable skill at `skills/reflect/SKILL.md` for Claude Code. After `reflect setup`, the `/reflect` skill is available in your AI tool for in-session telemetry analysis.

## Analysis schema

See [`docs/ai-observability-schema.md`](docs/ai-observability-schema.md) for the canonical cross-tool analysis schema.

## Part of o11y.dev

`reflect` is the entry point to the [o11y.dev](https://o11y.dev) ecosystem:

- **[opentelemetry-hooks](https://github.com/o11y-dev/opentelemetry-hooks)** — instrumentation engine for AI coding agents
- **[opentelemetry-skill](https://github.com/o11y-dev/opentelemetry-skill)** — observability knowledge layer for AI assistants
- **[gateway](https://github.com/o11y-dev/gateway)** — optional OTLP gateway for team/hosted telemetry

## License

[Apache-2.0](LICENSE)
