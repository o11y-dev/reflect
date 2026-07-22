# Reflect. Actionable Improvement Product and Implementation Plan

Date: 12 July 2026

Implementation alignment update: 16 July 2026. The shipped model now treats loops as observed cycles, workflows as first-class reusable procedures, and skills as durable package outputs. The browser information architecture below was revised to preserve those boundaries.

Repository reviewed: [o11y-dev/reflect](https://github.com/o11y-dev/reflect), release v0.8.6 at commit `5dd7d2f`

Public surface reviewed: [reflect.o11y.dev](https://reflect.o11y.dev/)

## Executive decision

Reflect should become **the improvement loop for AI coding agents**.

The product promise should be:

> Your coding agents should get better every session. Reflect finds where they struggle, discovers the workflows that work, turns them into approved guidance, and proves whether future sessions improved.

This is more memorable and useful than leading with telemetry, a graph, memory, or a generic quality score. Those are important internal capabilities, but the user buys the outcome: better future work.

The complete loop is:

1. Observe real sessions.
2. Identify a repeated behavior or failure.
3. Explain its impact with evidence.
4. Propose one concrete change.
5. Let the human review, approve, reject, or edit it.
6. Make the approved change available to agents.
7. Measure comparable sessions before and after.
8. Keep, revise, or roll back the change.

The single unforgettable command should be:

```bash
reflect improve
```

It should show the highest-value improvement available now, not another dashboard summary.

## The bigger product opportunity

Reflect can serve four moments in the agent lifecycle.

| Moment | Agent need | Human need | Reflect product |
| --- | --- | --- | --- |
| Before work | What should I know and which workflow should I use? | Are the right constraints and proven practices available? | `reflect ask` and task brief |
| During work | Am I stuck, drifting, or repeating a known failure? | Is intervention needed, and is the agent acting safely? | Opt-in live signals and bounded nudges |
| After work | What happened and what should be reused next time? | Was the task successful, efficient, and trustworthy? | Session review and workflow discovery |
| Over time | Which remembered guidance is still valid? | Did our agents, skills, models, and practices improve? | Improvement ledger and before/after measurement |

This creates a useful progression:

```text
Observe -> Explain -> Recommend -> Approve -> Apply -> Measure -> Learn
```

Most adjacent products stop at Observe or Explain. Reflect can own the last five steps.

## What the current repository already has

Reflect has unusually strong foundations for this direction.

- Local-first collection across multiple coding agents.
- A canonical SQLite schema for sessions, steps, LLM calls, tool calls, MCP calls, files, workspaces, repositories, evidence, memories, and graph relationships. Workspace identity is resolved independently from optional Git repository identity so sessions can share workspace-relative folder/path nodes without false cross-repository merges.
- Session-level token, cost, tool failure, loop, completion, recovery, and quality signals.
- Distribution-aware baselines instead of only fixed thresholds.
- A graph linking sessions, parent/child session lineage, canonical workspaces, shared workspace-relative folders and paths, tools, skills, memories, outcomes, and other entities through weighted evidence relationships.
- Evidence bundles for extracting skills from session history.
- A bundled Reflect skill that agents can receive during setup.
- Memory candidates, validation, provenance, and provider routing.
- A local dashboard, session autopsy, and session filtering foundation.

This means Reflect does not need a new technical identity. It needs a product layer that connects its existing pieces.

## The most important current gaps

### 1. Observations disappear instead of becoming work

The `Insight` model is structured, but it is still mainly a rendered result. It has a title, body, severity, confidence, and evidence. It does not have a durable identity, owner, state, hypothesis, proposed action, target metric, or measured result.

An observation such as “Elevated failure rate” should become an item that can move through a lifecycle:

```text
new -> acknowledged -> action proposed -> approved -> active -> improved
                                                   -> inconclusive
                                                   -> regressed
                                                   -> rolled back
```

### 2. Advice is generic when the evidence can support a specific patch

Current recommendations often say things like “validate paths and schemas up front” or “pin relevant files.” Useful, but not enough. Reflect should identify the exact task archetype, command, files, error chain, and successful alternative, then propose the smallest reusable change.

Example:

```text
Finding
Release tasks failed in 7 of 11 comparable sessions. Five failures started
with a missing changelog or an incorrect release command.

Proposed change
Add a repo-local release-validation workflow that checks version, changelog,
working tree, lint, tests, and release notes before publishing.

Target
Reduce release-task tool failures from 18% to below 8% in the next 10 runs.
```

### 3. Skill extraction writes too quickly

`reflect skills` already builds a useful evidence bundle, but the product should not jump from LLM output to installation. Generated skills must be stored as pending candidates by default.

Each candidate needs:

- Supporting and contradicting sessions.
- Trigger conditions.
- Preconditions.
- Ordered steps and decision points.
- Stop conditions, including when no change is the correct answer.
- Verification requirements.
- Expected impact and target metric.
- Scope: task, repository, team, or user.
- Generated content hash and source versions.
- Review status, applied version, rollback data, and observed effect.

The current `--yes` path should never silently install generated behavior. It can skip display prompts for read-only analysis, but applying generated skills or instructions must remain an explicit write operation.

### 4. The graph is presented as the product rather than used as the engine

“Behavioral Memory Graph for engineering systems” is technically interesting but not an immediate user outcome. The graph should power answers such as:

- What usually goes wrong in release tasks in this repository?
- Which workflow succeeds most often for CI failures?
- Which instructions are repeatedly ignored?
- Which agent and model combination performs best for this task type?
- Did the new skill reduce retries?
- Why does this repository use this workflow?

The graph remains important, but it should sit behind `reflect ask`, `reflect improve`, and the evidence view.

### 5. Quality is partly inferred from activity, not confirmed outcomes

A clean stop event is not necessarily success. High tool diversity is not necessarily quality. Editing many files can be harmful. A no-change result can be correct.

Reflect needs explicit and derived outcomes:

- Tests passed or failed.
- Build passed or failed.
- User accepted, corrected, abandoned, or reverted the result.
- PR or commit created.
- Required verification completed.
- No change was required and the agent correctly abstained.
- Follow-up session repeated the same failure.

The existing 0 to 100 quality score can remain as a diagnostic composite, but it should not be the north-star metric.

### 6. The current Reflect skill is an analyst, not a learning interface

The bundled skill explains telemetry and provider limits. It should also let an agent ask for task-specific operational knowledge before acting.

The skill should use a stable answer contract:

```json
{
  "answer": "Use the repository release-validation workflow before publishing.",
  "workflow_id": "workflow_release_validation_v3",
  "confidence": 0.88,
  "freshness": "verified in the last 30 days",
  "constraints": ["Do not publish from a dirty working tree"],
  "verification": ["ruff", "pytest", "release notes check"],
  "evidence_refs": ["session://...", "memory://...", "skill://..."],
  "fallback": "If the repository has no release metadata, stop and ask the operator."
}
```

The default human rendering should be short. Agents can request JSON.

### 7. The repository documentation has architecture drift

The current `AGENTS.md` still describes `TelemetryStats` as the source of truth, while the newer direction makes SQLite the runtime source of truth. The public and internal documentation also mix browser-default and TUI-default behavior.

This must be corrected early. Product work built on two competing runtime paths will become slow and unreliable.

## What people are looking for

The external signals cluster into a few consistent needs.

### Searchable session knowledge

Users want to find where a previous conversation, file, command, tool call, or workflow occurred. Agenttrace tracks this as an adjacent product surface in [issue 101](https://github.com/luoyuctl/agenttrace/issues/101). Reflect should not build a separate generic search product. It should make historical search answer-oriented and evidence-backed through `reflect ask`.

### Live multi-agent understanding

Users running several agents want to know which agent is active, expensive, noisy, or stuck before a session ends. This appears in [Agenttrace issue 102](https://github.com/luoyuctl/agenttrace/issues/102). Reflect should add live state after its async SQLite ingestion is complete, but prioritize actionable status such as “stuck in the same failed tool loop” over an htop-style activity display.

### Persistent memory that improves future sessions

People want past work to carry forward safely, not merely remain searchable. This is described in [Agenttrace issue 105](https://github.com/luoyuctl/agenttrace/issues/105). Reflect already has memory infrastructure. Its differentiation should be validated, measured memory rather than automatic transcript summarization.

### Commit and decision provenance

Users want to know why a line, commit, or design decision exists and which agent session produced it. This appears in [Agenttrace issue 60](https://github.com/luoyuctl/agenttrace/issues/60). Reflect should link sessions, files, commits, requirements, workflows, and memories. The answer must include source links and freshness.

### Verification of apparently successful tool actions

Tool calls can report success while the intended file or result is absent. [Agenttrace issue 58](https://github.com/luoyuctl/agenttrace/issues/58) describes the need for an audit trail. Reflect should distinguish tool success from task outcome and require an observable postcondition for high-impact workflows.

### Trace to evaluation and reusable test cases

Phoenix users asked to turn reviewed traces into golden datasets and manual evaluations in [issue 3249](https://github.com/Arize-ai/phoenix/issues/3249). Reflect should convert confirmed failures into lightweight behavioral regression cases. This is more valuable than only preserving the trace.

### Filtering and drill-down by operational context

Users repeatedly ask for session metadata filtering, saved views, and detailed event filtering. Examples include [Langfuse issue 6091](https://github.com/langfuse/langfuse/issues/6091), [Phoenix issue 6435](https://github.com/Arize-ai/phoenix/issues/6435), and [Phoenix issue 7077](https://github.com/Arize-ai/phoenix/issues/7077). This reinforces the decision to finish session-level SQL filtering and saved scopes first.

### Cost that connects to a decision

Cost dashboards alone are common. Users also want model cost comparisons and end-to-end tool or external-service cost. See [Phoenix issue 4102](https://github.com/Arize-ai/phoenix/issues/4102) and [issue 8431](https://github.com/Arize-ai/phoenix/issues/8431). Reflect should answer “what should I change?” and “did it work?” rather than adding more cost charts.

## Research-backed product principles

Recent coding-agent research strongly supports this direction.

- [Wink](https://arxiv.org/abs/2602.17037) reports specification drift, reasoning problems, and tool call failures in about 30% of studied production trajectories. A lightweight asynchronous intervention resolved 90% of misbehaviors that required one intervention and reduced tool failures, tokens, and engineer interventions in a live test. Reflect should support bounded, evidence-backed nudges, but only after the operator controls the policy.
- A study of [20,574 real coding-agent sessions](https://arxiv.org/abs/2605.29442) found misalignment across project reading, intent interpretation, rule following, action boundaries, implementation, execution, and progress reporting. Most visible resolutions still required explicit user correction. Reflect should treat correction events and repeated pushback as first-class signals.
- [ABTest](https://arxiv.org/abs/2604.03362) turns real failure reports into reusable repository-grounded behavioral tests. Reflect should let an operator promote a confirmed failure into a regression case linked to a workflow or skill.
- [FixedBench](https://arxiv.org/abs/2605.07769) shows that coding agents often modify code when no change is needed. Reflect workflows need explicit abstention criteria and should score correct no-change outcomes as success.

## Deterministic core and coding-agent boundary

Reflect should use a hybrid architecture with a strict trust boundary.

The deterministic Reflect core remains the source of truth. A coding agent is an optional synthesis layer that can explain evidence and draft improvements, but it must never decide what happened, whether an improvement worked, or whether generated behavior should be installed.

### Deterministic responsibilities

Reflect must handle these without an LLM or coding agent:

- Ingestion, normalization, deduplication, and session reconstruction.
- Rule execution and observation detection.
- Token, cost, latency, failure, retry, correction, verification, and outcome calculations.
- Baseline and comparable-cohort selection.
- Evidence collection and provenance.
- Severity, confidence, impact, and priority calculation.
- Known-pattern recommendation templates.
- Candidate schema validation and safety checks.
- Approval state, scope enforcement, and policy evaluation.
- Idempotent application, versioning, hashing, rollback, and audit history.
- Before and after measurement.
- Improved, regressed, unchanged, and inconclusive verdicts.

This lets `reflect improve` produce useful results in offline and deterministic-only mode.

Example:

```text
Repeated release preflight failure
7 of 11 comparable sessions affected
Tool failure rate: 18%
Known remediation template: release validation workflow
```

### Coding-agent responsibilities

When Claude, Codex, Gemini, or another supported coding agent is available, Reflect may use it to:

- Explain deterministic findings in natural language.
- Answer `reflect ask` questions from a bounded evidence packet.
- Draft a workflow, skill, instruction, evaluation, or remediation patch.
- Adapt a known recommendation template to repository-specific commands and files.
- Summarize supporting and contradicting evidence.
- Propose alternative hypotheses when deterministic rules find a problem but no known remediation exists.

The coding agent receives a bounded evidence bundle rather than unrestricted access to all session history. Its output is always a proposal.

### Actions a coding agent must never own

A coding agent must never:

- Invent or modify source telemetry.
- Select evidence without Reflect recording the selection method.
- Assign its own confidence as the authoritative confidence value.
- Mark its own workflow as validated.
- Approve or install generated behavior.
- Change project or user instructions silently.
- Decide that an intervention improved results.
- Remove contradicting evidence.
- Bypass scope, privacy, approval, or rollback controls.

### Required processing pipeline

```text
Telemetry
  -> deterministic normalization
  -> deterministic detection and evidence
  -> deterministic known remediation, when available
  -> optional coding-agent explanation or workflow draft
  -> deterministic schema and policy validation
  -> human review and approval
  -> deterministic application and exposure tracking
  -> deterministic before and after measurement
```

### Three operating modes

| Mode | Behavior | Intended use |
| --- | --- | --- |
| Deterministic only | Rules, evidence, known remediation templates, approval, application, and measurement work without an agent | Offline use, CI, privacy-sensitive environments, reliable baseline |
| Local coding-agent assisted | An installed agent drafts explanations and repository-specific workflow candidates from bounded local evidence | Individual developer workflow |
| Team controlled | Approved models may synthesize proposals while shared policies, ownership, and measurement remain deterministic | Organizational deployment |

### Proposal validation contract

All coding-agent output must conform to a versioned schema containing:

- Proposal type and target scope.
- Observation IDs being addressed.
- Supporting and contradicting evidence references.
- Trigger, preconditions, ordered steps, decision points, and stop conditions.
- Verification requirements.
- Expected metric and target.
- Files or configuration paths that would change.
- Sensitivity classification.
- Model, agent, prompt-template version, output hash, and generation time.

Invalid proposals are rejected. Valid proposals remain `pending` until explicitly reviewed.

### Product communication

Reflect should describe this boundary clearly:

> Reflect uses deterministic analysis to decide what happened and whether results improved. A coding agent may help explain the evidence or draft a proposed workflow. Humans remain in control of every behavioral change.

This boundary is central to trust. Reflect is not an agent grading itself. It is an independent evidence and measurement system that can use an agent as a constrained author.

## Product surfaces

Keep the user-facing surface small.

### 1. `reflect`

The interactive home. It should open to an Improvement Inbox, not an overview grid.

Top questions:

- What needs attention?
- What changed since last time?
- Which proposed improvement is ready for review?
- Which applied improvement worked or regressed?

Tabs can include Inbox, Sessions, Workflows, Skills, Impact, and Explore.

### 2. `reflect improve`

The memorable vertical slice.

```text
Highest-impact recurring problem

Release tasks fail during preflight validation
7 of 11 comparable sessions. 18% tool failure rate. 312k avoidable tokens.

Proposed improvement
Create a repo-local release-validation workflow with six ordered checks.

Expected result
Failure rate below 8% across the next 10 release sessions.

[Review evidence] [Review diff] [Approve] [Edit] [Reject]
```

After application:

```text
Measured result
Failure rate: 18% -> 7%
Median tokens: 92k -> 66k
Operator corrections: 4/11 -> 1/10
Verdict: improved, medium confidence
```

### 3. `reflect ask`

The query surface for humans and agents.

Examples:

```bash
reflect ask "How should I debug CI failures in this repo?"
reflect ask "What repeatedly breaks release tasks?" --json
reflect ask "Which workflow should I use for this task?" --task-file task.md
reflect ask "Why is this implemented this way?" --path src/reflect/store/normalize.py
```

Answers should combine validated memory, successful workflows, recent failures, repository rules, current git context, and evidence links. They should never present weak graph frequency as a fact.

### 4. `reflect loops`, first-class workflows, and Skills v2

Keep observed repetition separate from durable intervention artifacts.

```bash
reflect loops
reflect loops show <loop-id>
reflect loops build <loop-id> [--agent NAME]
reflect workflows list
reflect workflows show <workflow-id>
reflect workflows add <SKILL.md>
reflect workflows apply <workflow-id>
reflect workflows rollback <workflow-id>
reflect skills
reflect skills show <skill-id>
reflect skills discover [--agent NAME]
reflect skills apply <skill-id>
reflect skills rollback <skill-id>
```

The public concepts are deliberately separate:

- **Observations** describe evidence-backed problems or opportunities.
- **Loops** describe observed repeated cycles and are classified as stalled or productive. A loop is evidence, not an intervention.
- **Workflows** are reusable, reviewable procedures with ordered steps, state, iteration, exit, recovery, verification, and handoff contracts. They can exist independently and can be proposed by deterministic rules, authored from bounded evidence by an agent, or imported by an operator.
- **Skills** are durable, versioned, installable packages with provenance, installations, observed usage, and measurement history. A skill is one delivery format for a workflow, not the definition of a workflow.

`reflect loops build` promotes only one operator-selected loop by giving its bounded evidence to an authoring agent and requiring exactly one pending workflow packaged as a skill. This creates a reviewable workflow candidate and a linked pending skill version; it does not approve or install either one automatically.

Stalled-loop detection uses consecutive same-input runs, not repeated frequency anywhere in a session. Approval metadata and wait/poll transport events are excluded; failure-free patterns require recurrence across sessions, while a single-session pattern must carry recorded failure evidence.

The Workflows surface is the decision boundary. It shows source evidence, ordered behavior, stop conditions, verification, the selected delivery target, the exact diff, and the before/after metric. Selecting a repository chooses where the current renderer will package the workflow; it does not change the evidence scope. The first renderer targets `.agents/skills/<slug>/SKILL.md`, but the domain model must remain ready for guidance, checklist, evaluation, policy, and opt-in nudge renderers.

The Skills v2 registry keeps stable identities, immutable versions, source-agent and source-loop provenance, source-session evidence, installations, telemetry-observed usage, and measurements. The explicit `skills apply` and `workflows apply` commands are approval boundaries for the current skill renderer. Reflect records one active owner for the exact target to protect apply and rollback; this is ledger ownership, not a Git or filesystem lock.

`reflect workflows list|show|add|apply|rollback` is therefore a first-class product surface, not merely a compatibility layer. New user journeys should still start from evidence in the Inbox or from an intentional import; repeated frequency alone must never create an implied recommendation.

The initial implementation intentionally leaves live nudges unwired. It may prepare a disabled, metadata-only local `nudges/` exchange contract for a future hook reader, but `reflect setup` must not configure `opentelemetry-hooks` to consume it until the operator-facing policy flow is designed and explicitly enabled.

Skills are durable intervention packages and can contain an agentic workflow. Other workflow outputs can include AGENTS.md guidance, a checklist, a future hook nudge, an evaluation, or a policy. This prevents Reflect from converting every repeated pattern into a skill.

Every workflow candidate records its authorship boundary and suggested artifact. A `rule_blueprint` is deterministic known remediation and can be rendered without an agent. An `agent_authored` draft was synthesized from a bounded evidence bundle and records the selected agent. A `manual_skill_file` was imported by the operator. These origins share review, application, rollback, and measurement infrastructure but must never be presented as equivalent authorship.

### 5. `reflect feedback`

Provide cheap outcome labels.

```bash
reflect feedback <session-id> --outcome good
reflect feedback <session-id> --outcome bad --reason "ignored existing implementation"
reflect feedback <session-id> --outcome no-change-correct
reflect feedback <session-id> --outcome corrected
```

The TUI and browser session view should expose the same actions.

## The Improvement Ledger

The Improvement Ledger is the core new domain model. Every recommendation must become traceable.

### Observation

Required fields:

- Stable observation ID.
- Rule ID and rule version.
- Scope: session, repository, agent, model, workflow, user, or team.
- Category: specification drift, reasoning, tool failure, context, verification, cost, governance, correction, or outcome.
- Metric name, value, unit, and direction.
- Baseline query and baseline value.
- Impact estimate.
- Severity and confidence.
- First seen, last seen, occurrence count, and affected sessions.
- Evidence references.
- Status and suppression state.

### Proposed action

Required fields:

- Observation ID.
- Human-readable hypothesis.
- Action type: workflow, skill, instruction, config, nudge, evaluation, policy, or no action.
- Proposed patch or generated artifact.
- Scope and risk.
- Expected benefit.
- Target metric, target value, and measurement window.
- Approval state and reviewer.

### Intervention

Required fields:

- Exact artifact version and hash.
- Applied target and path.
- Previous version for rollback.
- Applied time and actor.
- Rollout state.
- Sessions exposed to the intervention.

### Measurement

Required fields:

- Intervention ID.
- Comparable cohort definition.
- Baseline and post-period windows.
- Sample sizes.
- Before and after values.
- Absolute and relative effect.
- Confidence and known confounders.
- Verdict: improved, regressed, no material change, or inconclusive.

## Workflow discovery design

Workflow discovery should compare successful and unsuccessful examples of the same task, not merely count repeated tool sequences.

### Step 1. Derive task archetypes

Use metadata-first features so the default remains private:

- Repository and folder.
- File roles and extensions.
- Tool and MCP sequence.
- Shell command families.
- Agent, model, and permission mode.
- Outcome signals.
- Errors, retries, user corrections, and verification.
- Optional local text features only in masked or full local modes.

Examples of archetypes:

- Fix failing test.
- Investigate production alert.
- Release package.
- Review PR.
- Add feature to existing module.
- Migrate configuration.
- Explain unfamiliar code.
- Confirm no code change is required.

### Step 2. Build a workflow fingerprint

A fingerprint should include:

- Preconditions.
- Ordered phases, not only raw tools.
- Decision points.
- Error recovery branches.
- Verification steps.
- Stop or abstain condition.
- Outcome.

### Step 3. Compare positive and negative examples

For each archetype, identify which behaviors correlate with better confirmed outcomes while controlling for repository, agent, model, and task size where possible.

Do not call a pattern “best practice” when it only occurred twice or when all examples failed. Label early candidates as hypotheses.

### Step 4. Generate the smallest useful artifact

Choose the artifact based on the problem:

| Problem | Best artifact |
| --- | --- |
| Repeated task with stable steps | Skill or workflow |
| Missing repository fact | Validated memory |
| Agent repeatedly ignores a constraint | Project instruction or policy |
| Known transient stuck pattern | Bounded live nudge |
| Confirmed failure that must not return | Behavioral evaluation |
| Model or agent performs poorly for task type | Routing recommendation |
| One-off anomaly | No durable artifact, keep observation only |

### Step 5. Measure actual use

An installed skill is not success. Reflect must detect whether the workflow was selected, followed, partially followed, ignored, or caused regression.

## Agent-facing Reflect skill

The bundled skill should become a thin client over local Reflect knowledge.

### New trigger cases

- The agent is about to start a task in a known repository.
- The user asks what worked before.
- The agent encounters a repeated tool failure or loop.
- The agent needs a workflow, constraint, or repository decision.
- The user asks to inspect or improve agent behavior.

### Agent workflow

1. Resolve current repository, branch, task, and privacy mode.
2. Run a read-only `reflect ask` query.
3. Return only the top relevant workflow, constraints, stop conditions, and evidence.
4. During execution, record whether the workflow was used.
5. At completion, emit outcome and verification signals.
6. Never install or mutate a skill automatically.

### Token budget

The default answer packet should remain under roughly 600 to 1,000 tokens. Agents need decisions, not a report dump.

### Setup behavior

The skill should not mutate configuration merely because it was invoked for analysis. If capture is missing, it should explain the limitation and offer the explicit setup command. `reflect setup` remains the authorized mutation point.

## Improvement detection taxonomy

Start with rules that can produce a specific action and measurement.

### P0 rules

1. Repeated tool failure chain.
2. Repeated retry loop with no state change.
3. Missing or late verification.
4. User correction after claimed completion.
5. Context explosion for a repeated task archetype.
6. Repeated project exploration before touching the same known files.
7. Constraint or instruction ignored.
8. Successful recovery sequence worth codifying.
9. High-performing repeated workflow worth preserving.
10. Action taken when no change was required.

### P1 rules

1. Agent or model routing opportunity for a task archetype.
2. Excessive handoff or subagent coordination cost.
3. Stale or contradictory memory.
4. Skill exists but is not selected or followed.
5. Tool reports success without verified postcondition.
6. Approval, sandbox, or permission policy friction.
7. Repeated human intervention at the same workflow step.

### P2 rules

1. Live specification drift.
2. Live action-boundary violation.
3. Cross-agent handoff loss.
4. Team-wide regression after a skill, model, or policy change.

## SQLite implementation

SQLite remains the only runtime source of truth. The graph and optional vectors are derived indexes.

Add these logical tables through migrations and Pydantic models:

### `rule_definitions`

Stores rule identity, version, category, detector configuration, required signals, and lifecycle state.

### `observations`

Stores durable rule results and their lifecycle state.

### `observation_evidence`

Links observations to sessions, steps, tool calls, LLM calls, files, memories, and graph entities.

### `session_outcomes`

Stores derived and explicit outcomes, source, confidence, and verification evidence.

### `task_archetypes`

Stores reusable task classifications and their matching features.

### `workflow_candidates`

Stores discovered workflow hypotheses, structured steps, evidence, support, confidence, and review status.

### `workflow_versions`

Stores immutable reviewed workflow content and render targets.

### `interventions`

Stores application, rollout, hashes, previous state, rollback, and exposure data.

### `measurements`

Stores before and after cohort statistics and verdicts.

### `operator_feedback`

Stores accept, correct, reject, no-change, and free-form reason signals with privacy handling.

Graph nodes and edges should then be derived for Observation, TaskArchetype, Workflow, Intervention, Outcome, and Measurement.

## Measurement model

### North-star metric

**Verified Improvement Rate**

```text
interventions with a measured positive effect
------------------------------------------------
interventions with enough post-application evidence
```

This measures whether Reflect creates actual value.

### Product funnel

1. Time to first useful answer.
2. Time to first actionable observation.
3. Observation to proposal rate.
4. Proposal review rate.
5. Proposal approval rate.
6. Applied workflow use rate.
7. Measurement completion rate.
8. Verified improvement rate.
9. Regression and rollback rate.

### Outcome metrics

- Operator corrections per comparable session.
- Tool failures per comparable session.
- Failed retries before recovery.
- Tokens and cost per successful task.
- Time to verified completion.
- Verification pass rate.
- Correct abstention rate.
- Workflow adherence.
- Repeated failure recurrence.
- Human intervention time.

### Cohort rules

Compare sessions by task archetype, repository, agent, model class, and rough task size. Do not claim improvement from raw global before and after averages.

For small local samples:

- Fewer than 5 comparable sessions after application: early signal only.
- 5 to 9: directional result with low confidence.
- 10 or more: measured result with confounder checks.
- Show sample size and cohort definition everywhere.

## UI restructuring

The UI should move from an analytics dashboard organized by telemetry dimensions to an operating interface organized by human decisions.

The design can reuse familiar GitHub and GitLab interaction patterns without copying their visual identity:

- A session behaves like a GitHub Actions workflow run or GitLab pipeline.
- An observation behaves like an issue or security alert.
- A proposed workflow change behaves like a pull request or merge request.
- Deterministic validation behaves like required checks.
- Human approval behaves like review approval.
- Applying a workflow behaves like merging.
- Before and after measurement behaves like post-merge verification.
- Dismissing an observation requires a reason and leaves a permanent audit record.

Reflect should retain its black, warm-white, graphite, and signal-orange visual system. The inspiration is the workflow and information hierarchy, not GitHub or GitLab styling.

### Top-level information architecture

Replace the current analytics-oriented navigation:

```text
Sessions / Activity / Compare / Observations / Tools / Graphs / Context
```

With a smaller product-oriented navigation:

```text
Inbox / Sessions / Workflows / Skills / Impact / Explore
```

| Area | Purpose |
| --- | --- |
| Inbox | Findings and observed loops requiring evidence review |
| Sessions | Searchable session history and evidence drilldown |
| Workflows | Reusable procedure proposals, exact review, delivery targets, activation state, and rollback |
| Skills | Durable package identities, versions, installations, usage, provenance, and measurements |
| Impact | Before and after results for applied interventions |
| Explore | Cross-agent, model, tool, MCP, cost, activity, graph, and advanced comparison analysis |

The default screen should be Inbox when an open observation or detected loop exists. When the inbox is empty, it should show recent sessions, recently measured improvements, and capture health rather than a blank state.

Existing Activity, Compare, Tools, Graphs, and Context functionality moves under Explore. Existing Observations become durable Inbox items.

Inbox must not become a second analytics dashboard. Generic telemetry summaries, prompt examples, token-economy guidance, and rule-administration controls stay under Explore. The current Improvement Rule registry belongs under Explore → Context & system so users can still inspect thresholds and extension contracts without adding implementation detail to daily triage.

### Inbox

The Inbox is the primary human-operator surface. It contains only durable findings and observed loops that need evidence review.

Each row or card should show:

- Observation title and status.
- Affected scope and task archetype.
- Impact and severity.
- Supporting session count.
- Confidence and data sufficiency.
- Agent concentration when relevant.
- Proposed action state.
- Owner and age for team use.

Primary filters:

```text
Status | Severity | Repository | Task | Agent | Workflow | Owner | Time
```

Supported states:

```text
New -> Acknowledged -> Proposal ready -> Approved -> Active -> Measured
                                               -> Rejected
                                               -> Dismissed
                                               -> Regressed
                                               -> Rolled back
```

Observations are never silently deleted. Dismissal requires a reason such as false positive, accepted behavior, insufficient evidence, duplicate, or not actionable. The reason becomes input to rule tuning and suppression.

### Sessions remain a first-class evidence surface

Do not remove the existing session drilldown. It is required for trust, debugging, and provenance. Every observation, workflow proposal, and measurement must link back to the exact session evidence.

Sessions are not the product homepage. They are the detailed execution record behind the improvement system.

The Sessions page keeps:

- Search and session list.
- Repository, agent, model, task, outcome, workflow, and time filters.
- Stable deep links.
- Selection state in the URL.
- Large-session lazy loading and pagination.
- Compare-with-similar-session actions.

### Session detail structure

Restructure the current Conversation, Tools, Quality, and Telemetry tabs into:

```text
Summary | Conversation | Execution | Changes | Evidence
```

This creates a clear investigation sequence:

```text
Intent -> Execution -> Consequence -> Judgment
```

#### Summary

The default session tab should answer what happened without requiring telemetry expertise.

Header fields:

- Confirmed or inferred outcome and confidence.
- Task archetype.
- Agent, model, and wrapper or engine provenance.
- Repository, branch, commit, and working-tree state.
- Duration, tokens, cost, failures, and retries.
- Workflow selected and adherence.
- Verification and human-correction status.
- Privacy mode and telemetry confidence.

Summary content:

- Short deterministic explanation of the session.
- Important findings and strengths.
- Claimed outcome versus verified outcome.
- Comparison with similar sessions.
- Highest-value next action.

Primary actions:

- Mark outcome.
- Compare similar sessions.
- Create or open an improvement proposal.
- Create a behavioral evaluation.
- Open the applied workflow.
- Copy an evidence link.

#### Conversation

Show the human and agent interaction when the active privacy mode permits it.

Include:

- Prompts and responses.
- User corrections and pushback.
- Agent progress claims.
- Compaction and continuation boundaries.
- Attachments and pasted-context size metadata.
- Links from messages to resulting execution events.

In metadata-only mode, show the conversation structure, lengths, hashes, timestamps, model, and related actions without raw text. Never present an empty tab without explaining why content is unavailable.

#### Execution

Execution contains secondary views rather than adding many crowded session tabs:

```text
Timeline | Spans | Tools | MCP & Subagents
```

##### Timeline

The unified chronological story should combine:

- Prompts and responses when available.
- LLM calls.
- Tool and MCP calls.
- File reads and writes.
- Subagent starts, handoffs, and completions.
- Failures, retries, recoveries, and long gaps.
- Context compaction and continuation.
- Verification commands.
- Human corrections.
- Reflect observations anchored to exact events.

Add search and filters for errors, retries, slow operations, file events, agent boundaries, and observations.

##### Spans

Provide a real trace investigation experience rather than a raw attribute list:

- Parent-child span tree.
- Waterfall timeline.
- Critical path.
- Parallel activity.
- Agent and subagent boundaries.
- Context handoffs.
- Long gaps, stalls, and failed spans.
- Duration, status, token, and cost contribution.

Selecting a span opens a detail drawer containing:

- Attributes, events, and logs.
- Input and output preview according to privacy mode.
- Related tool, LLM, MCP, file, or subagent entity.
- Related observations and evidence.
- Raw JSON as the final advanced view.

Do not load unbounded span sets. Load the initial bounded tree and paginate or progressively fetch additional spans.

##### Tools

Keep per-session Tools as a first-class view.

Summary columns:

| Tool | Calls | Failed | Retried | Unverified | Duration | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |

Useful filters:

```text
All | Failed | Retried | Slow | Looped | Unverified
```

Clicking a tool shows every invocation with:

- Input and output preview.
- Error and attempt number.
- Previous and next action.
- Recovery chain.
- File or resource affected.
- Postcondition verification.
- Related span.
- Whether the invocation participated in a loop.

Tool success must remain distinct from verified task success.

##### MCP and Subagents

Show:

- MCP server, tool, transport, calls, latency, failure rate, and payload size.
- Subagent purpose, parent, agent and model, requested deliverable, handoff context, cost, and outcome.
- Context lost or repeated across handoffs.
- Whether the subagent result was verified and used.

Hide this view when a session has no MCP or subagent activity.

#### Changes

Execution does not prove consequence. Changes should show what the session actually affected.

Include:

- Files read, created, modified, moved, and deleted.
- Unified diff or available change summary.
- Commands executed.
- Tests, builds, lint, and verification results.
- Git status before and after.
- Commit, branch, and pull-request links.
- Produced artifacts.
- Expected versus observed postconditions.
- A clear valid no-change outcome.

#### Evidence

Replace the current Quality tab with Evidence because a heuristic score should not be presented as truth.

Include:

- Confirmed and inferred outcomes.
- Deterministic observations.
- Supporting and contradicting evidence.
- Rule versions and triggering metrics.
- Confidence explanation.
- Comparable-session baseline.
- Human feedback and corrections.
- Verification evidence.
- Quality-score breakdown, clearly labeled as a heuristic diagnostic.

### Improvement review should resemble a pull request

The improvement detail page is the core approval surface.

Its inner tabs should be:

```text
Overview | Changes | Evidence | Checks | Measurement | History
```

#### Overview

- Problem and affected scope.
- Deterministic impact.
- Proposed behavior.
- Expected result and target metric.
- Owner, reviewer, and status.

#### Changes

- Exact workflow, skill, instruction, policy, evaluation, or configuration diff.
- Target files and scope.
- Previous version and rollback preview.

#### Evidence

- Supporting and contradicting sessions.
- Task archetype and cohort.
- Failure and success examples.
- Direct links to session events and spans.

#### Checks

Deterministic checks should include:

- Proposal schema valid.
- Evidence sufficient.
- No secrets copied.
- Scope permitted.
- Paths safe.
- Write idempotent.
- Rollback available.
- Required reviewer present.
- Target metric defined.

Approval remains disabled when a required check fails.

#### Measurement

After application, show:

- Baseline, exposed, followed, and comparable session counts.
- Before and after outcome metrics.
- Absolute and relative effect.
- Agent and model breakdown.
- Workflow adherence.
- Confidence, confounders, and verdict.

#### History

Permanent audit history of generation, edits, approvals, application, exposure, measurement, dismissal, regression, and rollback.

### Cross-agent widgets

Cross-agent analysis remains important. Move the full surface to:

```text
Explore -> Agents
```

Cross-agent widgets should also appear contextually in Inbox observations, session comparisons, workflow evidence, and Impact.

Global filters:

```text
Time | Repository | Task archetype | Workflow | Agent | Model | Outcome
```

Primary cross-agent widgets:

- Successful outcomes by agent and task archetype.
- Cost per successful task.
- Human corrections per session.
- Verification pass rate.
- Tool failure and retry rate.
- Median time to verified completion.
- Workflow adherence.
- Context efficiency.
- Handoff cost and lost-context indicators.
- Telemetry coverage and confidence.

The center of the page should be a task-by-agent performance matrix. Each cell must show sample size and confidence and open the filtered supporting sessions.

Do not create a global best-agent leaderboard. Agents often receive different work. Compare like-for-like cohorts by repository, task archetype, model class, workflow, and task size.

Keep raw volume widgets such as sessions, tokens, cost, tool calls, and active hours under a separate Usage view so volume is not confused with performance.

Recommended Explore structure:

```text
Explore
  Agents
    Performance
    Usage
    Reliability
    Coverage
  Models
  Tools
  MCP & Subagents
  Cost
  Activity
  Graph
```

### Contextual cross-agent placement

#### Inbox

Show an agent breakdown only when the observation is concentrated or differs materially across agents.

#### Session Summary

Show one compact comparison against similar sessions and link to the full cross-agent view.

#### Workflow candidate

Show which agents supplied successful and failed evidence and whether the workflow should be cross-agent or agent-specific.

#### Measurement

Break down before and after impact by agent and model to determine whether the intervention transfers.

### Adaptive visibility and privacy

- Hide unavailable secondary views rather than showing misleading empty tabs.
- Explain when content is absent because of metadata-only capture.
- Label exact and estimated token or cost data.
- Distinguish native OTel, hook, and session-adapter provenance.
- Show telemetry-confidence indicators for cross-agent comparisons.
- Treat no code changes as a valid outcome.
- Preserve selected session, active inner tab, filters, and comparison state in stable URLs.

Example deep links:

```text
/sessions/<id>/summary
/sessions/<id>/execution/spans
/sessions/<id>/execution/tools?status=failed
/sessions/<id>/evidence
/improvements/<id>/changes
/improvements/<id>/measurement
```

### TUI and browser responsibilities

Both interfaces must use the same SQLite-backed view models and state transitions.

The Textual TUI should optimize for:

- Inbox triage.
- Recent sessions.
- Fast session summary and timeline.
- Workflow review summaries.
- Keyboard-driven filtering and approval.

The browser report should optimize for:

- Span waterfall and large timelines.
- Conversation review.
- Diffs and proposal checks.
- Cross-agent matrices and comparisons.
- Measurement details and history.

The browser is a richer renderer, not a separate analytics implementation.

### UI migration order

1. Introduce shared SQLite view models and stable entity URLs.
2. Add Summary and restructure the existing session tabs without removing underlying capabilities.
3. Merge detailed execution into Timeline, Spans, Tools, and MCP/Subagents.
4. Replace Quality with Evidence.
5. Add Changes and outcome feedback.
6. Convert Observations into the Inbox lifecycle.
7. Add pull-request-style improvement review.
8. Move legacy analytics into Explore.
9. Add cross-agent task matrix and measurement breakdowns.
10. Remove legacy duplicate widgets only after parity tests pass.

### UI acceptance criteria

1. Every observation can open the exact supporting session event or span.
2. Every session exposes intent, execution, changes, and evidence without raw telemetry being required.
3. Tools and spans remain fully drillable.
4. Every generated change has an exact diff, checks, approval, and rollback.
5. Every applied change gains a measurement view.
6. Cross-agent comparisons use comparable cohorts and display sample size and confidence.
7. Privacy mode and telemetry provenance are visible wherever they affect interpretation.
8. Common filtered pages meet the report latency targets.
9. TUI and browser show consistent states and metrics from the same SQLite queries.
10. Existing session, tool, span, conversation, comparison, and graph capabilities are not lost during restructuring.

## Public site changes

The current site has good visual quality but an unclear and partly over-broad promise.

### Current credibility problems

- The hero says Reflect connects code, telemetry, incidents, deployments, ownership, and architecture. The reviewed release does not yet deliver all of that as a complete product.
- “Behavioral Memory Graph” describes the internal engine, not why someone should install Reflect.
- The site says 183 sessions across 4 agents while the README says the demo includes five agent families.
- The site mentions OpenCode and Antigravity in ways that can be read as current observability support, while the README support matrix marks several agents as planned.
- The demo highlights counts and a score, but does not show a completed improvement loop.

### Proposed homepage copy

Eyebrow:

```text
THE IMPROVEMENT LOOP FOR CODING AGENTS
```

Headline:

```text
Your coding agents should get better every session.
```

Subheadline:

```text
Reflect finds where agents get stuck, discovers workflows that work,
turns them into approved skills and guidance, and measures the result.
Local-first. Evidence-backed. Agent-agnostic.
```

Primary CTA:

```text
Run the improvement demo
```

Command:

```bash
pipx install o11y-reflect
reflect improve --demo
```

### The hero demo

Show one before and after story. The bundled demo can use clearly labeled example data.

```text
Detected
Release workflow failed in 7 of 11 runs.

Proposed
Add a six-step release validation workflow.

Approved
Project skill v1 installed with rollback available.

Measured
Tool failures down 61%. Tokens per successful release down 28%.
```

Do not fabricate these as real project results. Label them as demo data until Reflect has anonymized user-approved case studies.

### Site analytics

Track:

- Improvement demo opened.
- Install command copied.
- GitHub clicked.
- Demo observation reviewed.
- Demo workflow approved.
- Demo measurement viewed.

The site should optimize for completed demo loops, not only visits.

## 90-day roadmap

### Month 1. Ship the unforgettable vertical slice

Goal: `reflect improve --demo` and a real local improvement inbox.

#### Week 1. Finish the foundation

- Complete continuous local ingestion with a bounded async queue.
- Keep all raw events, canonical data, rollups, observations, and report queries in SQLite.
- Complete session-level SQL filtering.
- Make ingestion independent from report startup.
- Align `AGENTS.md`, README, architecture docs, and actual default UI behavior.
- Preserve performance targets: under 2 seconds cold and under 250 ms warm for common report queries.

#### Week 2. Persist observations

- Add rule definitions, observations, evidence, status, and suppression.
- Version every rule.
- Recompute incrementally during ingestion.
- Build the Improvement Inbox query and API.
- Add operator feedback and session outcome fields.

#### Week 3. Add proposal and review

- Add proposed action, target metric, and measurement plan.
- Convert the top five P0 observations into specific action generators.
- Store generated workflows as pending candidates.
- Add review diff, approval, rejection, edit, and rollback metadata.

#### Week 4. Complete the demo loop

- Implement `reflect improve` and `reflect improve --demo`.
- Add one seeded before and after workflow story.
- Rewrite the site hero and README around improvement.
- Publish a 30 to 45 second recording of the complete loop.

Month 1 exit criteria:

- A new user understands the product in one sentence.
- The demo reaches a concrete proposed change in under 30 seconds.
- No generated behavior is installed without explicit approval.
- Every displayed observation has evidence, state, and a target metric.

### Month 2. Make Reflect useful to agents

Goal: a coding agent can retrieve proven task guidance and Reflect can discover real workflows.

#### Week 5. Task archetypes and outcomes

- Implement metadata-first task archetype classification.
- Add explicit outcome and correction capture.
- Treat correct no-change outcomes as success.
- Add comparable cohort queries.

#### Week 6. Workflow discovery

- Compare successful and failed sessions within archetypes.
- Generate structured workflow candidates.
- Include trigger, preconditions, steps, branches, verification, and stop conditions.
- Require supporting and contradicting evidence.

#### Week 7. Agent answer surface

- Implement `reflect ask` with optional task-file and path context.
- Add human and JSON renderers.
- Update the bundled Reflect skill to query these commands.
- Keep default answer packets concise and source-linked.
- Make setup explicit and read-only queries non-mutating.

#### Week 8. Safe application

- Render approved workflows to project skills, user skills, instructions, evaluations, and nudges.
- Add hashes, idempotent writes, version history, and rollback.
- Detect whether agents selected and followed applied workflows.

Month 2 exit criteria:

- An agent can ask how to handle a known task and receive a relevant, evidence-backed workflow.
- A human can approve one workflow and see exactly what will change.
- Reflect can distinguish installed, invoked, followed, ignored, and stale workflows.

### Month 3. Prove value and prepare team use

Goal: show measured improvement and make the loop usable by a small team.

#### Week 9. Measurement engine

- Add baseline and post-intervention cohort selection.
- Calculate before and after metrics with sample size and confidence.
- Add improved, regressed, unchanged, and inconclusive verdicts.
- Detect regression and offer rollback.

#### Week 10. Behavioral evaluations

- Promote confirmed failures into repository-grounded regression cases.
- Link evaluations to observations and workflows.
- Run evaluations manually first. Keep automatic execution optional.

#### Week 11. Bounded live intervention

- Add opt-in live detection for repeated failure loops, specification drift proxies, and missing verification.
- Allow a maximum of one low-risk nudge per rule per session with cooldown.
- Require operator-approved policies.
- Record every intervention and outcome.

#### Week 12. Team beta

- Add team aggregation without changing the local-first default.
- Support role and ownership for observations and proposals.
- Add shareable redacted improvement reports.
- Recruit 3 to 5 design partners and publish one measured case study with permission.

Month 3 exit criteria:

- At least one real intervention has a measurable result.
- The team view answers where humans intervene and which workflows improve results.
- Reflect can show proof, not only claims.

## Prioritized implementation backlog

### P0. Must ship first

1. Async SQLite ingestion and query-only report path.
2. Durable observations with rule versions and evidence.
3. Session outcomes and operator feedback.
4. Improvement Inbox.
5. Proposed action and target metric.
6. Pending workflow candidates.
7. Explicit approval, hashes, diff, and rollback.
8. `reflect improve --demo`.
9. Public positioning rewrite.

### P1. Differentiation

1. Task archetypes.
2. Success versus failure workflow comparison.
3. `reflect ask` and answer packets.
4. Agent-facing skill integration.
5. Workflow application and adherence.
6. Before and after measurement.
7. Behavioral evaluation generation.
8. Commit and decision provenance.

### P2. Expansion

1. Opt-in live nudges.
2. Agent and model routing recommendations.
3. Team ownership and rollups.
4. Saved views and scheduled improvement reports.
5. Central gateway and hosted organizational tier.
6. Optional semantic index for local full-text modes.

## What not to build yet

- A general-purpose vector memory platform.
- A hosted raw-prompt backend.
- Another broad LLM trace viewer.
- A large workflow marketplace before local workflows prove value.
- Automatic skill installation.
- Automatic code or policy mutation.
- A universal quality score presented as truth.
- Complex team billing before local Verified Improvement Rate works.
- Incident, deployment, ownership, and architecture promises that are not yet supported end to end.

## Acceptance tests for the product promise

Reflect is becoming actionable when these tests pass:

1. Given repeated release failures, `reflect improve` identifies the exact pattern, affected sessions, impact, and one proposed workflow.
2. The proposal remains pending until the operator explicitly approves it.
3. Applying the workflow is idempotent and produces an exact diff and rollback version.
4. An agent starting a similar task receives the approved workflow through `reflect ask` or the bundled skill.
5. Reflect records whether the workflow was selected and followed.
6. After enough comparable sessions, Reflect shows before and after metrics with cohort and sample size.
7. A regression is detected and the operator can roll back.
8. Metadata-only mode never needs raw prompt or response text.
9. Report filtering and common queries meet the latency targets.
10. The public demo communicates the full loop in under 45 seconds.

## Final product statement

Reflect is not merely where humans inspect agent telemetry. It is where humans and agents build a shared, evidence-backed operating system for better engineering work.

The human gets control, explanations, approval, ownership, and proof.

The agent gets the right workflow, constraints, memory, stop conditions, and verification before it repeats an old mistake.

The memorable moment is not seeing that a session wasted tokens. It is seeing Reflect detect why, propose a safe reusable fix, and later prove that the next ten sessions were better.
