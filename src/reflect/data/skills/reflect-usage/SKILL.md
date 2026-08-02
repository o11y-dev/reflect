---
name: reflect-usage
description: Inspect and explain exact local AI coding-agent usage for the current session, a selected session, or all captured sessions. Use for questions such as "where did my tokens go?", "why were so many tokens used?", "was the usage efficient or worth it?", or any token, cache, cost, model, tool, MCP, subagent, duration, failure, or usage-statistics analysis across Codex, Claude, Cursor, Copilot, Gemini, OpenCode, and other Reflect sources.
---

# reflect-usage

Use Reflect's canonical local SQLite telemetry as the source of truth for usage questions.

## Commands

Run the narrowest matching command and prefer JSON when preparing an answer:

```bash
reflect usage --json                         # current runtime session
reflect usage --session SESSION_ID --json    # selected session
reflect usage --global --period day --json   # all local usage in 24 hours
reflect usage --global --period week --json  # all local usage in 7 days
reflect usage --global --period month --json # all local usage in 30 days
reflect usage --global --period all --json   # all captured local usage
reflect usage --global --period week --agent codex --json
reflect usage --refresh --json                # ingest local sources first when freshness matters
```

`--agent` is valid only with `--global`. Global queries aggregate the complete matching SQLite cohort and do not inherit the browser's session-page limit.

Normal usage opens the prepared SQLite store in query-only mode. It never
migrates the schema or repairs rollups. A missing, outdated, empty, or
maintenance-stale snapshot returns an actionable error. Use `--refresh` when
the active session is missing or the operator explicitly needs newly captured
native sessions; use `reflect refresh` for a complete snapshot rebuild. Either
refresh can take longer on large local stores.

## Analysis workflow

1. Establish scope and freshness. Name the session or period and preserve any resolution or limitation warning.
2. Account for the spend. Show recorded input, output, cache-write, cache-read, and reasoning tokens separately; do not hide cache or reasoning inside one unexplained total.
3. Explain where it went. Attribute input/output tokens and estimated cost by model or agent when available. Treat tool, MCP, and subagent counts as workflow context, not token attribution: the usage report does not assign tokens to individual tools.
4. Explain likely drivers. Relate the spend to observed LLM calls, tool intensity, subagent fan-out, duration, failures, and recoveries. Mark causal explanations as `Inference`.
5. Judge the result. Use `productive`, `mixed`, `inefficient`, or `indeterminate`, followed by the strongest one or two reasons. A low token count is not automatically good, a high count is not automatically bad, and output-token volume is not a quality score.
6. Suggest one concrete next check or improvement only when the evidence supports it.

For a global scope, normalize raw totals with useful rates such as tokens or cost per session, failures per session, and recovery rate. Avoid comparing periods or agents with materially different session counts using totals alone.

Useful evidence for a `productive` judgment includes a completed session, few unresolved failures, successful recovery, and tool or subagent activity consistent with the task. Useful evidence for an `inefficient` judgment includes repeated failures, excessive retries or fan-out, long duration, or large context/cache creation without corresponding progress. If outcome evidence is absent, return `indeterminate` rather than inventing value.

## Reporting contract

- Lead with the requested total and verdict, then add only the breakdowns that explain the spend.
- Prefer this compact answer shape: `Spend`, `Where it went`, `Why`, and `Verdict`.
- Label the evidence `Local telemetry`. Estimated costs are not provider invoices, quotas, or billing records.
- Label behavioral explanations and value judgments `Inference`; never present tool counts as exact token ownership.
- Preserve any `limitations` from the JSON response. If `resolution` starts with `inferred_`, say that the current session was inferred because its telemetry was not yet present locally.
- Do not run `reflect setup`, install hooks, or change capture settings automatically. If no sessions exist, explain that limitation and offer the setup command.
- Use the general `$reflect` skill only when the question also needs workflow guidance, provider quota reconciliation, or broader telemetry diagnosis.
