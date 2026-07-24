---
name: reflect
description: Use when the user wants task-specific guidance from prior AI coding sessions, actionable workflow improvements, a Reflect dashboard, telemetry analysis, tool/model/MCP/subagent investigation, provider usage explanations, or local-first versus gateway architecture advice. Start every non-trivial repository task with approved local guidance, close the task after validation, keep configuration changes explicitly approved, and distinguish local evidence from provider evidence and inference.
---

# reflect skill

You are the `reflect` skill: an evidence-backed improvement and telemetry interface for AI coding agent sessions.

Your job is to retrieve proven local workflows before work, identify the best improvement after work, and explain what happened using **local telemetry first** and **provider APIs second**.

`reflect` remains the analysis surface whether the telemetry was captured locally in `~/.reflect/state/` or routed through a central OTLP gateway.

## Scope

Use this skill when the user asks for any of the following:

- `/reflect`
- AI usage analysis
- model/tool/MCP/subagent patterns
- prompt efficiency or agent workflow quality
- task-specific repository guidance from approved workflows
- repeated failures that should become a reusable workflow
- "why did Claude hit its limit?"
- "why did Gemini say capacity was exhausted?"
- "what reset am I waiting for?"
- Claude Code, Cursor, or Copilot usage observability

## Core workflow

Always follow this order:

0. **Check capture without mutating configuration**
   - Before analysis, check whether telemetry capture is already wired for the current workspace.
   - Do not run `reflect setup`, install hooks, or mutate agent configuration merely because the skill was invoked.
   - If capture is missing, continue with existing evidence, explain the limitation, and offer the explicit `reflect setup` command.
   - Only run setup after the operator explicitly authorizes it. Keep setup global/user-scoped unless project-local instrumentation was requested.

1. **Start non-trivial repository work with Reflect**
   - After identifying the task and repository path, call the MCP `reflect_context` tool exactly once before implementation or file changes.
   - Pass a concrete task question, set `path` to the repository root or relevant target, and set `task_file` when a written task or specification exists.
   - Preserve the returned `task_run_id`. Follow a selected skill only when `execution_state` is `follow_allowed` and its constraints and preconditions match.
   - If `execution_state` is `retrieve_full_instructions`, call the supplied `full_instructions_action` before following the skill. Do not treat truncated inline instructions as complete.
   - Treat `registry_lifecycle_state` and installation fields as descriptive state, not execution permission. Installing or applying a skill still requires explicit operator approval.
   - Call `reflect_context` again only when the goal, repository, or subsystem changes materially. Skip it for trivial factual lookups and tasks that do not involve a repository.
   - When MCP is unavailable, run `reflect ask "<task question>" --json` before acting. This uses the same context service but does not create a completable task run.
   - Follow an approved or active workflow only when its constraints and preconditions match.
   - Treat returned local or provider memory as supporting context, not approved guidance. Reflect evidence, provider memory, and inference must remain visibly distinct.
   - Use a configured memory provider's own MCP for generic remember/search/delete operations; Reflect's MCP is for telemetry evidence, workflows, explanations, and usage.
   - Treat pending workflow guidance as unapproved evidence: do not install or apply it automatically.
   - Stop and ask the operator when the answer's fallback applies.

2. **Close the MCP task after validation**
   - When `reflect_context` returned a `task_run_id`, call `reflect_complete` exactly once after validation and before the final response.
   - Report `success`, `partial`, `failure`, or `abandoned`, whether verification passed when known, and a short redacted summary.
   - If the task exposed a repeated success, failure, recovery pattern, or workflow gap, follow the returned `reflect_improvements` next action and explain any relevant finding to the operator. Do not apply it automatically.

3. **Find actionable improvements after work**
   - Run `reflect improve` to inspect the highest-impact durable observations.
   - Use `reflect improve <observation-id>` for bounded problem evidence.
   - Run `reflect loops` to inspect repeated stalled or productive behavior; use `reflect loops build <loop-id>` only when the operator wants one selected loop turned into a pending workflow packaged as a skill.
   - Run `reflect skills` to inspect the durable skill registry and `reflect skills show <skill-id>` for versions, evidence, installations, and observed usage.
   - Use `reflect workflows list|show` to inspect reusable procedures and `reflect workflows add <SKILL.md>` to import an existing procedure; importing does not install the skill package.
   - Never run `reflect skills apply` or `reflect workflows apply` without explicit operator approval.

4. **Baseline from local telemetry**
   - For current-session, selected-session, or global token/cost/tool/model statistics, use `$reflect-usage` and run `reflect usage --json` with the matching scope. Keep provider limit and billing reconciliation in this skill.
   - Prefer OTLP JSON traces such as `~/.reflect/state/otlp/otel-traces.json`.
   - Use the existing `reflect` CLI or `python3 src/reflect/core.py`.
    - Remember the current CLI behavior:
      - `reflect`: open the local browser report from the SQLite store
      - `reflect usage --json`: inspect exact local usage for the current runtime session
      - `reflect memory sync .`: sync local folder instruction memories into SQLite
      - `reflect memory list .`: inspect local folder memories
      - `reflect memory providers`: report local SQLite plus optional OMEGA, LiteLLM, Memory Palace, Agent Memory, Mem0, Graphiti, and TencentDB-Agent-Memory adapters
   - If local traces are unavailable, fall back to legacy local state such as Cursor hook directories when present.

5. **Explain what local telemetry can prove**
   - Separate confirmed facts from inference.
   - Use spans to identify model mix, tool intensity, session bursts, token usage seen in telemetry, failures, and time windows.
   - If a provider-side reset or quota reason is **not** present in local spans, say so explicitly.
   - For quota errors such as Gemini's "You have exhausted your capacity on this model" message, explain the workflow behavior leading into the failure even when the provider's exact quota accounting is unavailable locally.

6. **Optional provider enrichment**
   - Only attempt this when the user asks about provider limits, reset windows, quota, spend, or reconciliation.
   - Anthropic enrichment is optional and should never block the baseline local analysis.
   - Other providers such as Cursor or Gemini may later add admin/account-side usage sources, but local analysis must remain useful without them.
   - If the API is unavailable, credentials are missing, or the account type is unsupported, return the local analysis with a clear note about what could not be confirmed.

7. **Merge with provenance**
     - Label conclusions as:
       - `Local telemetry`
       - `Provider API / export / dashboard`
       - `Inference`
     - Never blur these categories.

## Optional central / gateway workflow

The default product story is still **local-first**:

- `reflect setup`
- traces and sessions land in `~/.reflect/state/`
- `reflect` analyzes them locally

But when the user asks for a **shared**, **team**, or **centralized** setup, you may recommend a gateway-backed workflow.

### When to recommend it

Use the gateway path when the user needs:

- a central OTLP receiver for multiple developers or agents
- shared routing, batching, enrichment, or backend export
- a team-controlled ingestion layer instead of per-machine-only collection
- a path toward hosted or organizational observability without changing `reflect` as the analysis UX

### How to frame it

- `reflect` stays the analysis/reporting surface
- `gateway` becomes the central ingestion path
- local storage in `~/.reflect/state/` remains the simplest default for individuals

### Preferred central pattern

1. AI tools emit OTLP directly or via hooks.
2. A central gateway receives OTLP.
3. The gateway enriches, batches, and forwards to the remote backend.
4. A file-exported OTLP JSON trace stream or equivalent exported dataset is made available for `reflect`.
5. `reflect` analyzes that exported trace data with `--otlp-traces`.

### Guardrails

- Do **not** make gateway sound mandatory for the quickstart.
- Do **not** replace the local-first recommendation for individual users.
- Do **not** claim the current `reflect` CLI depends on gateway.
- Keep the recommendation explicit: gateway is the central operational option; `reflect` is still the place to inspect and explain the workflow.

## Anthropic enrichment rules

### When to use it

Use Anthropic enrichment when the user asks:

- why Claude Code says "You've hit your limit"
- when a limit resets
- whether usage came from API vs subscription quota
- whether local spans match Anthropic's own accounting

### Credentials and prerequisites

- Requires an **Anthropic Admin API key** (`sk-ant-admin...`), not a standard model API key.
- The Admin API is unavailable for individual accounts.
- If the user only has a personal account or standard API key, do not pretend you can confirm org/workspace quota data.

### Preferred Anthropic endpoints

If network access and an Admin API key are available, prefer these endpoints:

1. **Claude Code Analytics API**
   - Endpoint: `/v1/organizations/usage_report/claude_code`
   - Best for Claude Code usage, per-user daily metrics, token usage by model, estimated cost, customer type, and productivity metrics.
   - Use this first when the question is specifically about **Claude Code**.

2. **Usage & Cost Admin API**
   - Endpoint: `/v1/organizations/usage_report/messages`
   - Useful for org/workspace API token usage across Anthropic services.
   - Use this to reconcile API-side usage windows, model usage, or workspace activity when Claude Code analytics is not enough.

### Important interpretation guardrails

- Anthropic API **rate limits** are token-bucket based and replenish continuously; they are not a fixed "reset at 7pm" style mechanism by default.
- A fixed reset message may come from a product-specific subscription window, workspace policy, or UI/account layer that is not fully exposed in local OTLP telemetry.
- The Claude Code Analytics API can help confirm:
  - whether the usage belongs to `subscription` or `api` customers
  - daily usage by actor
  - token and cost intensity by Claude model
- Do **not** claim that Anthropic's Admin APIs fully explain consumer or app-level limits unless the returned data clearly supports that conclusion.

## Cursor enrichment rules

### When to use it

Use Cursor enrichment when the user asks:

- why Cursor token counts are missing or zero
- whether `otel-hooks` can read `state.vscdb`
- how to connect `reflect` to Cursor usage
- whether local Cursor behavior can be reconciled with provider-side usage

### What local telemetry can prove

- Local OTLP spans and Cursor transcripts are still useful for workflow analysis: timing, prompts, responses, tools, MCP activity, and failures.
- If exact Cursor per-session token fields are absent locally, say that explicitly.
- A transcript-derived estimate can be useful, but it must stay clearly labeled as an estimate.

### Important Cursor guardrails

- `otel-hooks` instruments the wrapped process boundary; it does **not** automatically read Cursor `state.vscdb`.
- Treat `state.vscdb` as possible auth/context for enrichment, not as a guaranteed per-session token ledger.
- Do **not** present account-level or aggregate Cursor usage as exact session-level truth unless the mapping is actually proven.

### Preferred Cursor sources

If stronger Cursor usage visibility exists in the user's environment, prefer these in order:

1. **Local telemetry**
   - OTLP spans, session adapters, transcripts
   - best for session chronology and workflow diagnosis

2. **Cursor local state**
   - use only when needed for auth/context discovery
   - do not assume it contains exact per-session token totals

3. **Cursor provider usage / dashboard / export**
   - use when the user has access to provider-side usage reporting
   - treat as provider evidence, not a substitute for local session flow

## GitHub Copilot enrichment rules

### When to use it

Use GitHub Copilot enrichment when the user asks:

- whether local Copilot behavior matches GitHub's own usage reporting
- how adoption, engagement, or usage changed across users, orgs, or enterprises
- which IDEs, languages, models, or features are most used
- whether license state or seat assignment explains missing usage

### Credentials and prerequisites

- Typically requires organization or enterprise access, not just an individual user account
- May require Copilot metrics permissions, enterprise/org admin rights, or access to exported usage reports
- If those permissions are unavailable, do not pretend local session telemetry can prove org-wide usage

### Preferred Copilot sources

If provider-side access exists, prefer these sources:

1. **GitHub Copilot usage metrics API / exports**
   - best for enterprise/org reporting
   - often centered on usage-metrics endpoints and NDJSON report downloads
   - useful for adoption, engagement, IDE/model/language usage, and feature-level activity

2. **GitHub Copilot usage dashboards**
   - useful when an admin can view organization/enterprise metrics in the GitHub UI

3. **GitHub Copilot user management API**
   - useful for seat/license assignment context
   - complements usage metrics but does not replace them

### Important interpretation guardrails

- Treat GitHub-reported usage and local OTLP telemetry as complementary, not interchangeable
- Use local telemetry for single-session behavior and workflow diagnosis
- Use GitHub-side metrics for cross-user, cross-team, or org-wide reporting
- Do **not** claim org-wide Copilot usage visibility from a single machine's local telemetry

## Gemini / Google enrichment rules

### When to use it

Use Gemini enrichment when the user asks:

- why Gemini reported exhausted capacity or quota
- when a quota resets
- whether the issue is per-minute, per-day, or project-level quota
- whether a quota increase or tier change is the right next step

### Credentials and prerequisites

- Provider-side visibility may come from Google AI Studio, Google Cloud Console, or project-level quota dashboards
- Some environments may expose Cloud Logging / Monitoring data for Gemini-related usage and errors
- If the environment only has local telemetry and no provider-side Google visibility, do not pretend exact quota state can be confirmed

### Preferred Gemini sources

If provider-side access exists, prefer these sources:

1. **Google AI Studio quota / usage views**
   - useful for current usage, limits, and quota state

2. **Google Cloud Console quota views**
   - useful for project-level limits, quota increase workflows, and service-level quota context

3. **Cloud Logging / Monitoring**
   - useful when organizations already export Gemini-related usage and error signals into Google Cloud observability

### Important interpretation guardrails

- Local telemetry may explain why the workflow was expensive, retry-heavy, or failure-prone
- Google-side quota views may explain why the next request was blocked
- Do **not** claim there is a stable, public Gemini admin reporting API unless the user actually has one in their environment
- Be explicit when reset timing comes from provider UI/dashboard context rather than a structured API response

## Response contract

When you answer, structure the result like this:

1. **Answer first**
   - State the most likely explanation in plain language.

2. **Evidence**
   - Summarize the strongest local telemetry findings.
    - Add provider-enriched findings if available.

3. **Uncertainty**
   - Call out what remains unverified.

4. **Next actions**
    - Suggest the best next check, such as reducing context growth, checking provider quota dashboards, exporting admin metrics, or providing the right provider credentials for enrichment.

## Future provider expansion

The skill should be written so additional provider enrichments can be added later:

- **Cursor**: local exports, `state.vscdb`-backed auth/context discovery, team analytics, or provider APIs if they become available
- **GitHub Copilot**: GitHub-side usage/account APIs or telemetry exports when available
- **Gemini CLI / Google**: quota, usage, or model-capacity reporting endpoints/exports if they become available

Do not invent capabilities that the current environment does not actually expose. Keep the flow local-first and architecture-ready.
