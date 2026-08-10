# Reflect product direction

This is a decision-oriented product document for simplifying Reflect,
productizing the closed improvement loop, and earning developer trust. Every
remaining item names a product behavior, an acceptance signal, and a boundary.
Architecture details belong in `docs/current-architecture.md`.

## Product promise

Reflect should discover a repeated senior-engineering procedure, let the
developer review one bounded intervention, and prove whether the next
comparable tasks improved.

```text
discover procedure -> review exact workflow -> install with approval
    -> measure comparable future tasks -> keep, revise, or roll back
```

This happens locally, with inspectable evidence. Reflect is not a hidden agent
control plane and does not rank developers or agents.

## Product principles

1. **One canonical fact per concept.** A logical call, task execution, workflow
   contract, installation, exposure, and outcome each have one stable identity.
2. **Comparable tasks over global averages.** Impact is reported only for a
   bounded contract or high-confidence task archetype.
3. **Evidence before confidence.** Claims expose source, freshness, cohort,
   missing telemetry, and whether the conclusion is observation or attribution.
4. **Explicit mutation.** Discovery and measurement are read-only. Setup,
   installation, external writes, retention, and rollback require visible intent.
5. **Fresh-install truth.** A capability is supported only when the packaged
   product can exercise and verify it without inherited developer state.
6. **Delete accidental complexity.** Extend canonical stores and adapter
   contracts; do not add another ledger, lifecycle flag, provider branch, or
   renderer-specific metric.

## Implemented foundation on this branch

- Workflow contract semantics own procedure identity; exact content owns a
  revision. Review, deployment, installation, and measurement are distinct.
- Procedure evidence, adherence, and impact use eligible execution units.
  Sessions remain source navigation and aggregate usage containers.
- Impact freezes the baseline and evaluates the first bounded comparable
  post-install tasks. Missing or mixed evidence withholds the claim rather than
  rendering a zero baseline.
- `tool_calls` owns every logical invocation. `mcp_calls` is a one-to-one MCP
  metadata extension, so hooks, native telemetry, and transcripts cannot inflate
  call counts by representing the same operation twice.
- The gateway uses bounded active and immutable closed JSONL segments. Closed
  raw segments are removed only after successful normalization unless retention
  is explicitly requested. Combined trace/log admission stops at 4 GiB without
  deleting unprocessed evidence, and capture health is visible in the UI and
  diagnostics.
- One capability registry feeds setup, doctor, aliases, skills, MCP clients, and
  cross-agent fixtures. Antigravity replaces Gemini as the current headless
  target; historical Gemini evidence remains readable.
- Stateful snapshot preparation has one coordinator. CLI database commands,
  dashboard queries, route construction, and authored frontend modules have
  explicit owners. Refresh-enabled dashboards reuse that coordinator on a
  five-minute cadence, so freshness is a product behavior rather than a manual
  operator ritual.
- The browser has six product surfaces: Sessions, Inbox, Workflows, Skills,
  Impact, and Explore. Inbox owns evidence that still needs investigation; the
  durable Skills Registry remains visible between a reviewed workflow and its
  measured impact.
- Database evolution is one-way. New code does not maintain dual writes, old
  routes, aliases, or fallback readers for retired models.

## Next product gates

### 1. Prove capture state per configured agent

The product should expose this exact progression in setup completion, doctor,
and the UI:

```text
installed -> configured -> event observed -> session ingested -> calls reconciled
```

Acceptance:

- Every selected agent reports its last completed stage and timestamp.
- The diagnostic names the source class and a concrete repair action without
  exposing prompt text.
- “Configured” is never presented as “capturing.”
- An MCP-only Antigravity task is not shown as native token or hook coverage.

Boundary: derive this from the capability registry and ingestion state. Do not
create a second health registry.

### 2. Make the first useful result achievable in ten minutes

The onboarding path should be: install, configure one agent, verify one captured
task, ask one evidence-backed question, and open the source evidence.

Acceptance:

- A clean-package test completes the path with isolated state.
- Setup ends with one next command based on observed state.
- A missing executable, permission, event, or session names the exact blocked
  stage.
- Before telemetry exists, validated local task context can still be returned,
  but the response clearly says outcome evidence is unavailable.

Boundary: onboarding must not require hosted state, a sample database, or a
developer checkout.

### 3. Produce one defensible five-task impact result

The strongest retention moment is not a dashboard of activity; it is proof that
a reviewed procedure improved later comparable tasks.

Acceptance:

- One installed workflow reaches five eligible post-install executions.
- The card exposes exact baseline and post-install unit IDs, archetype, agent and
  source composition, signal coverage, adherence, and evidence cutoff.
- The result states separately whether an outcome shifted and whether the
  intervention can reasonably receive attribution.
- `improved`, `no effect`, and `regressed` all lead to an explicit keep, revise,
  or rollback decision.

Boundary: do not fill missing metrics with estimates merely to complete the
window, and do not transfer impact between workflows that look similar.

### 4. Keep every evidence response bounded and inspectable

Agent-facing and browser APIs should return a summary and stable references,
then fetch exact rows only on drill-down.

Acceptance:

- Every evidence collection has a deterministic order and pagination contract.
- Default MCP responses remain within a documented payload budget.
- Evidence cutoff, last successful refresh, excluded units, and attribution
  completeness accompany the summary.
- No default response embeds complete transcripts or every source row.

Boundary: pagination extends existing evidence ledgers; it does not create a
parallel reporting store.

### 5. Make deletion and privacy behavior auditable

Developers must be able to understand what is captured, retained, removed, and
still provable afterward.

Acceptance:

- Doctor and the UI show the active text-capture mode and resolved local paths.
- A telemetry reset previews exact files and table families before mutation.
- Removing session evidence does not silently delete reviewed workflow versions
  or fabricate historical support counts.
- External-write workflows preserve target, preview fingerprint, approval,
  write fingerprint, and read-back result.
- Public exports redact repository names, paths, people, secrets, and prompts.

Boundary: privacy metadata belongs to the canonical evidence and change-review
contracts, not scattered renderer warnings.

## Packaged cross-agent proof

Keep one deterministic fresh-wheel scenario in which six headless clients —
Claude, Cursor, Antigravity, Copilot, Codex, and OpenCode — draft and revise a
public-safe Reflect post using the same validated project memory.

Required deterministic result:

- 12 sessions
- 12 completed and linked task runs
- 12 memory exposures
- 24 successful logical MCP calls
- exact aggregate token total from the fixture
- zero new logical events on an unchanged second refresh

Live-agent execution remains opt-in and must be reported separately from the
deterministic fixture. Windsurf stays outside the headless suite until it has a
supported non-interactive execution surface.

## Adoption proof

A public case study should contain:

- one uncomfortable finding across at least three comparable tasks
- exact anonymized evidence and contradictions
- the hidden procedure reconstructed as a task contract
- one bounded reviewed intervention
- the five-task validation result
- a clear split between proven outcome, plausible attribution, and remaining
  hypothesis

Do not publish generic productivity percentages. An ROI claim additionally
requires measured time or cost coverage and an explicit account of missing
telemetry.

## Deliberate non-goals

- Ranking developers or agents.
- Treating whole long-lived sessions as comparable tasks.
- Claiming capture from configuration alone.
- Inferring external-write success without read-back evidence.
- Automatically installing a discovered workflow or skill.
- Creating another workflow engine beside task runs, workflow versions,
  interventions, installations, and measurements.
- Adding abstractions that do not remove duplication or isolate a demonstrated
  variant.
- Coupling current client support to historical telemetry parsers.

## Review rule

At release review, promote at most one product gate to the active proof target.
It is complete only when the packaged fresh-install path demonstrates its
acceptance evidence. If delivery adds more concepts or runtime paths than it
removes, revise the design before implementation.
