---
name: reflect-skills
description: Use when the user wants to inventory, inspect, discover, version, apply, or roll back reusable skills derived from AI coding sessions.
---

# reflect-skills

Use the Skills v2 registry as the durable source of truth for reusable behavior. A skill has a stable identity, version history, evidence links, installation state, and observed usage. Agent-assisted discovery creates pending versions; it does not install them.

## Usage

```bash
reflect skills                         # list the current registry without reconciling sources
reflect skills sync                    # reconcile files, drafts, usage, and measurements
reflect skills sync --path .agents/skills # include another skill root in reconciliation
reflect skills show <skill-id>         # inspect versions, evidence, installs, and usage
reflect skills discover --period week  # ask an agent to discover skill drafts from recent sessions
reflect skills discover --refresh      # refresh SQL graph evidence before discovery
reflect skills discover --agent codex  # choose the authoring agent CLI
reflect skills apply <skill-id>         # explicitly install a reviewed pending version
reflect skills rollback <skill-id>      # restore the prior repo-local file state
```

Use `reflect loops` separately to inspect repeated behavior. A loop does not become a workflow or skill automatically. When the operator selects a useful loop, `reflect loops build <loop-id> --agent <name>` asks the agent to author exactly one pending workflow packaged as a skill, with explicit state, iteration, exit, recovery, verification, and handoff contracts.

## Discovery flow

1. Load local session telemetry for the requested time range.
2. Build a bounded evidence bundle from quality scores, tool flows, commands, recovery chains, graph relationships, and selected high-signal session context.
3. Invoke the selected coding-agent CLI with the evidence bundle.
4. Validate the returned skill definitions and let the operator choose drafts.
5. Store each selected draft as a pending skill version with source-agent and source-session evidence.
6. Report the stable skill ID for `reflect skills show <skill-id>`.

Nothing is installed automatically. Never run `reflect skills apply` without explicit operator approval.

Plain `reflect skills` and `reflect skills show` are query-only. File edits,
new installations, missing files, workflow drafts, observed uses, and
measurements become new registry revisions only when `reflect skills sync` is
run. Discovery reuses existing SQL graph evidence and does not ingest telemetry
unless `--refresh` is explicit.

## Options for discovery

| Flag | Default | Description |
|------|---------|-------------|
| `--agent` | auto-detect | Agent CLI used to author drafts |
| `--yes` / `-y` | off | Stage all valid extracted drafts without the selection prompt |
| `--period day|week|month|all` | `week` | Session evidence range |
| `--demo` | off | Use bundled sample telemetry |
| `--refresh` | off | Explicitly prepare current SQL graph evidence before discovery |

Older individual period flags remain accepted with deprecation warnings. Older root
invocations such as `reflect skills --agent codex --week` also remain accepted in
compatibility mode, but new automation should use `reflect skills discover --period ...`.
