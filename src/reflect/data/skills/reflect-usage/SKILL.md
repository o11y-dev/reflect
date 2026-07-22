---
name: reflect-usage
description: Inspect exact local AI coding-agent usage for the current session, a selected session, or all captured sessions. Use for token, cache, cost, model, tool, MCP, subagent, duration, failure, or usage-statistics questions across Codex, Claude, Cursor, Copilot, Gemini, OpenCode, and other Reflect sources.
---

# reflect-usage

Use Reflect's canonical local SQLite telemetry as the source of truth for usage questions.

## Commands

Run the narrowest matching command and prefer JSON when preparing an answer:

```bash
reflect usage --json                         # current runtime session
reflect usage --session SESSION_ID --json    # selected session
reflect usage --global --day --json          # all local usage in 24 hours
reflect usage --global --week --json         # all local usage in 7 days
reflect usage --global --month --json        # all local usage in 30 days
reflect usage --global --all --json           # all captured local usage
reflect usage --global --week --agent codex --json
reflect usage --refresh --json                # ingest local sources first when freshness matters
```

`--agent` is valid only with `--global`. Global queries aggregate the complete matching SQLite cohort and do not inherit the browser's session-page limit.

Normal usage reads the prepared SQLite store immediately. Use `--refresh` when the active session is missing or the operator explicitly needs newly captured native sessions; this can take longer on large local stores.

## Reporting contract

- Lead with the requested total, then add only the most useful model, tool, agent, failure, or cost breakdown.
- Label the evidence `Local telemetry`. Estimated costs are not provider invoices, quotas, or billing records.
- Preserve any `limitations` from the JSON response. If `resolution` starts with `inferred_`, say that the current session was inferred because its telemetry was not yet present locally.
- Do not run `reflect setup`, install hooks, or change capture settings automatically. If no sessions exist, explain that limitation and offer the setup command.
- Use the general `$reflect` skill only when the question also needs workflow guidance, provider quota reconciliation, or broader telemetry diagnosis.
