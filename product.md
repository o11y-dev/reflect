# Reflect product direction

This is a decision-oriented product backlog for making Reflect simpler to build,
easier to adopt, and worthy of developer trust. Add an item only when it names a
specific product behavior, an acceptance signal, and a boundary. Architecture
details belong in `docs/current-architecture.md`; speculative feature lists do
not belong here.

## Product promise

Reflect should discover a repeated engineering procedure, let the developer
review a bounded intervention, and prove whether the next comparable tasks
improved. It must do this locally, with inspectable evidence and without turning
agent telemetry into a hidden control plane.

## Product principles

1. One canonical fact per concept. A logical tool call, task run, memory
   exposure, workflow, and outcome each have one stable identity.
2. Current evidence over inferred certainty. Every claim exposes its source,
   freshness, cohort, missing telemetry, and confidence.
3. Explicit mutation. Discovery and measurement are read-only; installation,
   external writes, retention, and rollback require visible intent.
4. Comparable tasks over global averages. Impact is reported only for a bounded
   contract or high-confidence task archetype.
5. Fresh-install truth. A feature is supported only when a clean packaged
   install can exercise and verify it without developer-machine state.
6. Delete accidental complexity. Prefer extending the canonical store and
   shared adapter contracts over adding another ledger, renderer-specific
   calculation, or provider branch.

## Now: close the trust gap

### 1. Make capture state explicit per agent

Expose one matrix in `reflect doctor` and setup completion:

`installed -> configured -> event observed -> session ingested -> calls reconciled`

Acceptance evidence:

- Each supported agent reports the last observed event and last ingested
  session, or a precise missing stage.
- “Configured” is never presented as “capturing.”
- The diagnostic names the source path and remediation without exposing prompt
  content.

Simplification target: one capability registry should feed setup, doctor, docs,
and tests; remove duplicated supported-agent lists and status wording.

### 2. Make refresh source selection visible and controllable

`reflect refresh` currently discovers default OTLP, hook, and native sources.
Before work starts, print the selected source classes and resolved paths. Add
explicit `--no-otlp`, `--no-hook-spans`, and `--no-native-sessions` controls so a
temporary or repaired store cannot accidentally scan unrelated live history.

Acceptance evidence:

- JSON output contains every selected source, whether it existed, inserted
  rows, skipped rows, and unchanged files.
- A clean temporary-store test cannot read the developer's live Reflect state.
- Repeating refresh over unchanged sources produces zero new logical events.

### 3. Prove the packaged cross-agent contract

Keep one fresh-wheel scenario: agents draft a public-safe Reflect post from
validated project memory, then different agents revise it in new processes.
Verify exact task runs, memory selection, logical MCP calls, token totals, and
idempotent refresh. Treat Windsurf separately until it has a supported headless
execution surface.

Acceptance evidence:

- Deterministic fixture: 12 sessions, 12 completed task runs, 12 memory
  exposures, and 24 successful logical MCP calls for the six headless agents.
- Opt-in live test: two completed task runs per selected agent, with the same
  validated memory selected in both independent processes.
- Results label deterministic telemetry separately from paid live-agent proof.

### 4. Show why an impact claim is or is not trustworthy

Every Impact card should show baseline cohort size, post-install cohort size,
task contract/archetype, signal coverage, direction, and the reason a claim is
withheld. Never render missing baseline evidence as zero.

Acceptance evidence:

- A developer can open the exact compared task units from the card.
- Mixed cohorts and incomplete signal coverage visibly block an impact claim.
- “Observed improvement” and “attributed to this intervention” remain separate
  conclusions.

## Next: simplify the product surface

### 5. Center the product on three objects

Use these user-facing objects:

- **Task contract**: the bounded work and completion conditions.
- **Workflow**: the reviewed procedure applied to that contract.
- **Impact**: before/after evidence from comparable contract executions.

Sessions, spans, calls, memories, and execution units remain evidence and
implementation concepts, not competing top-level product concepts.

Acceptance evidence:

- Navigation and copy use the same three nouns.
- A workflow review identifies its task contract and planned impact measure.
- Removing a presentation-only synonym does not require a database migration.

### 6. Collapse provider differences behind capability adapters

Provider adapters should declare native-session discovery, hook support, MCP
configuration, call-ID extraction, token/cost fidelity, and headless-test
support. Shared ingestion and product logic consume those capabilities.

Acceptance evidence:

- Adding an agent changes one adapter registration plus contract fixtures.
- Core orchestration gains no new provider `if/elif` branch.
- Setup, doctor, documentation tables, and local-agent tests derive from the
  same registry.
- Capability lifecycle is per surface: replacing Gemini CLI with Antigravity
  changes the MCP/headless target without deleting historical Gemini telemetry,
  and verified MCP execution does not imply native token or cost capture.

### 7. Bound every evidence API

Large evidence payloads should be summary-first with pagination and field
projection. Session lists and source ledgers should return stable references;
details should be fetched only on drill-down.

Acceptance evidence:

- Default MCP responses stay within a documented payload budget.
- No endpoint embeds every transcript or source row by default.
- Pagination preserves deterministic ordering and evidence cutoff metadata.

## Adoption and developer trust

### 8. Make the first useful result achievable in ten minutes

The onboarding path should be: install, configure one agent, verify one captured
session, run one evidence-backed question, and open its source sessions.

Acceptance evidence:

- A clean-machine script verifies this path from the published package.
- Validated project memory can produce bounded `reflect_context` and CLI
  fallback context before the first telemetry session exists; the response
  states that outcome evidence is not available yet instead of declaring the
  entire snapshot unusable.
- Setup ends with one concrete next command based on observed state.
- Failure at any stage reports the exact missing dependency or permission.

### 9. Publish a privacy and evidence contract

Document what stays local, what text capture is optional, what fingerprints are
stored, how retention works, and which actions can mutate files or external
systems. Surface the active capture mode in doctor and the dashboard.

Acceptance evidence:

- Users can audit source paths and delete session evidence without deleting
  reviewed workflows or aggregate proof unexpectedly.
- `reflect_context` may return bounded full text only for a validated local
  project instruction whose current file remains inside the requested workspace
  and still matches its stored hash; stale, user-wide, and provider memory stays
  preview-only.
- Validated project instruction files are path-applicable context, not ordinary
  keyword memories; text beyond the stored privacy preview must remain
  retrievable without persisting the full file in the search index.
- External-write workflows preserve preview, approval, write, and read-back
  evidence.
- No public case study exposes repository names, paths, people, or raw prompts.

### 10. Ship proof, not activity marketing

Public examples should state what current evidence proves, what remains a
hypothesis, the comparable cohort, and the next validation window. Avoid generic
productivity percentages until outcomes and intervention attribution are both
measured.

Acceptance evidence:

- Every case study includes an uncomfortable truth, exact anonymized evidence,
  the hidden procedure, the intervention, and a five-task validation result.
- ROI claims include measured time or cost and disclose missing telemetry.

## Deliberate non-goals

- Ranking developers or agents.
- Treating whole long-lived sessions as comparable tasks.
- Inferring external-write success without read-back evidence.
- Creating another workflow engine beside the existing contract, workflow,
  skill-version, task-run, and measurement lifecycle.
- Adding abstractions that do not delete duplication or isolate a demonstrated
  variant.
- Claiming support from configuration alone when no event has been observed.
- Coupling current client support to historical telemetry parsers; providers can
  replace a CLI without invalidating already captured sessions.

## Review rule

At each release review, promote at most one item from **Next** to **Now**. An
item is complete only when the packaged fresh-install path demonstrates its
acceptance evidence. If an item adds more concepts or code paths than it
removes, revise it before implementation.
