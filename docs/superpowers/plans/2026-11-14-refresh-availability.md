# Refresh availability implementation ledger

Approved design: keep completed evidence readable throughout refresh and keep user actions independently writable. Existing six dirty CLI-fix files are preserved; no live database or installed runtime changes before review.

## Ownership and interfaces

| Component | Owns | Contract |
|---|---|---|
| PublishedSnapshotStore | Immutable generations, atomic manifest, leases, disk admission | PublishedSnapshot metadata and acquired generation path; working DB is never substituted implicitly |
| UserStateRepository | Human review, saved drafts, versions, installations, feedback | Independent short transactions and material revision; evidence remains generation-qualified |
| PreparationRunRegistry | Cross-process request/join, durable progress/result | PreparationLock is liveness authority; progress does not advance material user revision |
| Read context | One generation plus user-state read revision | Dashboard, CLI and MCP share provenance and cache keys |

## Required sequence

- [ ] Immutable snapshot publication and lease regressions.
- [ ] One-way user-state migration and action services, including crash recovery.
- [ ] Reader and discovery integration; dependency-scoped measurement invalidation.
- [ ] Durable refresh registry and completion-relative scheduling.
- [ ] UI freshness, stable navigation and status contract.
- [ ] Targeted/full tests, Ruff, artifact parity and independent review.
- [ ] Installed rollout after all old writers stop; actual concurrent read/action verification.

## Binding defaults

Reserve 2 GiB free disk; at most three completed generations including leased generations and one temporary build. Normally retain current and previous. Prefer COW only after verified WAL checkpoint under writer lock; fallback to space-checked SQLite backup. Never evict active reader generations or raw telemetry. Discovery releases its lease after materializing bounded evidence, before running an external agent. Automatic refresh waits 300 seconds after completion or failure, including publication.

User state owns manual review/suppression/dismissal/approval, draft overrides/self-contained drafts, approved workflow and skill versions, installation intent/expected hashes, audit/feedback/settings and approval records. Evidence owns telemetry, graph, generated suggestions, support/confidence, observed integrity, usage and measurements. Preserve contract-signature dismissal; approval is revision-specific. Source pruning never removes saved work. Only affected dependency changes invalidate current assessment; unrelated dismissals do not blank Impact.

## Progress

Implementation started on feat/guidance-efficiency. No live changes. Publication primitives are first; automatic routing before the user-state split is prohibited because it would hide newly committed actions.

### Implemented foundation

- Added PublishedSnapshotStore with explicit material `evidence_revision` input (readiness timestamps never identify material changes), COW after WAL checkpoint, SQLite backup fallback, atomic manifest, generation-directory durability before manifest replacement, short reader leases, capacity/disk admission and orphan partial cleanup. Caller MUST own PreparationLock; no automatic routing/integration yet.
- Added six publication tests, including active refresh isolation, no-op timestamp change, disk rejection, pinned-generation cap, orphan cleanup and fsync ordering.
- Scheduler now derives deadline from completion/failure timestamp; long-refresh regression demonstrated old implementation started after only 0.088 seconds of a requested 0.15-second interval, then passes after fix.
- Targeted validation: 25 tests passed (publication and preparation). Full integration and full suite remain outstanding.
- User-state migration, all reader/action wiring, durable run joining, UI and live rollout are NOT implemented. Do not treat isolated primitives as completed availability fix.

### Additional approved user requirement

Workflow creation needs a clear Create workflow entrypoint, editable draft, project/file preview and Enable workflow. Preserve draft after review failure and explain next action; do not expose unexplained Review/Retry review buttons. Reuse existing drafting/services after user-state separation. Read impeccable skill before UI work.

### User-state persistence primitive

Added UserStateStore with independently durable, short transactions for contract dismissals and self-contained editable drafts, per-draft CAS and a material revision that changes only on material edits. Three regressions pass, including a successful state mutation while the working database owns BEGIN IMMEDIATE. This store is not yet wired into WorkflowService or reader overlays; no migration or installed cutover has run.

Foundation full-suite result: 2037 passed, 22 skipped, one existing Starlette deprecation warning in 173.39s (`/tmp/reflect-availability-foundation-tests.log`). The subsequent three user-state tests pass separately; combined targeted suite 28 passed. No claim of end-to-end refresh availability follows from these isolated tests.

### Migration integration ruling

Cross-store foreign keys must become generation-qualified provenance IDs. Existing migrate(commit=False) keeps its caller-owned atomicity contract. The separate user-state copy/verify/cutover orchestration must run at explicit initialization outside that transaction, retain authoritative originals until sidecar content is durable and verified, and resume idempotently after crashes. A normal reader must never initialize or migrate the sidecar. Parent approved this ruling.

WorkflowService.create_draft now accepts an explicit UserStateStore, validates content using its existing validator, and saves authored instructions with unverified provenance independently of an analysis write transaction. Regression validates durable read-back and rejects unsafe slugs. User-state plus existing workflow-action tests: 9 passed. This is a domain method only; no HTTP/UI entrypoint has been exposed before the remainder of the lifecycle is ready.

Repository integration now accepts a pinned UserStateSnapshot and overlays human draft edits/dismissal without mutating evidence or unrelated measurement state. Contract queries use the overlaid contract identity when that explicit state context is supplied. Regression proves an old review snapshot stays pending while a new response sees rejection, with unchanged confidence/measurement, during an actual SQLite write transaction. Combined user-state and existing action tests: 10 passed. Interactive factories still need cutover wiring; analysis repositories intentionally remain evidence-only without a supplied review context.

### Availability foundation status and immediate UI pivot

Implemented explicit one-way UserStateMigration copy/digest verification/cutover recovery; durable sidecar action gates; legacy dismissal import; owned rollback after source pruning; explicit rereview adoption of changed evidence; pinned shared request connections and attached review snapshots. Existing WorkflowService apply/rollback runs against command-owned context in tests while analysis owns its writer. SkillRegistryService now computes evidence outside short registry writes, validates review fingerprints, and keeps filesystem observation from replacing workflow installation intent. Latest targeted state/registry/identity run: 35 passed.

These foundations remain DISABLED at runtime: no startup migration invocation or publication routing was added. Full availability is NOT achieved. Remaining: read/action factories, pipeline publication/material revisions, authoritative measurement/integrity reads, durable run join, discovery/MCP integration, freshness UI and installed validation. No live migration/install/database changes occurred.

Parent relayed user priority pivot because implementation was taking too long: stop infrastructure expansion and deliver visible workflow creation using existing storage/services now. Added POST /api/workflows, validated instruction-only user_authored drafts (zero support and no claimed impact), Create workflow form, existing exact preview and Enable flow. Draft remains available after blocked preview; unsaved form fields remain on errors. Replaced unexplained Retry Review copy with Reload project preview and recovery explanation. Existing canonical coordination remains in force; long refresh may still block saving, which the UI reports honestly.

New real API regression creates/retries a draft, tests an invalid project, reads retained draft, previews, enables a real temporary SKILL.md and rolls it back. Validation rejects invalid paths/steps and fabricated evidence. Both tests pass. JS syntax and generated artifact build pass. Full suite now running in /tmp/reflect-availability-current-tests.log. Mechanical UI detector reports only the incumbent template flat-type-hierarchy warning; no authored color/spacing violations. Browser validation pending.

### Bounded workflow delivery verified (2026-11-14)

Independent review identified and fixed a delayed-submit race: submitted instructions/project are immutable, fields disable during save, stale responses cannot reopen a closed dialog or overwrite a newer draft, and successful saves immediately update workflow inventory. A delayed-response JavaScript regression exercises this behavior. User-authored drafts have an explicit source label rather than being mislabeled agent-authored. Blank instructions and injected evidence claims are rejected.

Final source suite: **2057 passed, 22 skipped**, one existing Starlette warning, 171.56s (`/tmp/reflect-availability-final-tests.log`). Full Ruff passed. JS syntax, git diff whitespace, and generated HTML byte parity passed. Earlier run exposed UI harness querySelector assumptions and renamed-copy assertions; both corrected and affected tests reran 51/51 before final full run. API/create race tests 3/3. Rendered visual verification could not run: IAB unavailable and browser inventory empty. Temporary fixture server stopped.

Parent explicitly authorized local rollout of the bounded flow after clean checks. Verified installed CLI is uv-managed, preserved existing dependencies/config, acquired the preparation writer lock before stopping, copied and hash-verified all 219 source package files, and restarted the dashboard with original port8765, refresh=true, interval300. Live served HTML byte-matches docs/report.html and includes Create workflow/form; OpenAPI advertises POST /api/workflows. No live workflow was created. No migration or immutable-publication routing was activated. Startup refresh was still running at first read-back, so full refresh availability remains unresolved as documented above.

Installed startup refresh subsequently completed successfully with no error at 2026-11-14T10:45:56.946235Z. Next automatic refresh is 10:50:56.946235Z, confirming the intended 300 seconds from completion. Independent reviewer approved the bounded Create flow after the async fixes, with no remaining blocking findings in that scope. This is installed proof of workflow creation delivery and scheduler timing, not proof of the unfinished concurrent-read/action architecture.

### Authorized follow-up: create from Inbox finding

Added a server-resolved finding draft endpoint and a finding_id option on the existing create endpoint. The existing observation remains the workflow's authoritative measurement link, preserving its repository, metric direction, metric name and baseline query without manufacturing a baseline. Grouped finding IDs are retained as server-owned provenance for evidence display; the measurement cohort remains scoped to the original observation. Existing candidates are reused even when omitted from the currently loaded UI or when finding grouping changes after creation. Repository-scoped finding workflows require a target from their canonical repository workspaces.

Inbox now offers Create workflow from finding when no linked workflow is loaded. Existing linked workflows keep Open Linked Workflow. The existing editable form loads finding context and an explicitly proposed review checklist, preserves edits per finding across save failures/context changes, and continues into exact preview/Enable. The source evidence supports the finding, not the effectiveness of the authored instructions.

A relevant measurement defect was found: unknown metric names fell back to failed-call counts. That fallback was removed so unsupported metrics remain unmeasurable. Targeted domain/API run: 89 passed, including real stored grouped findings, idempotent creation, invalid IDs, retained baseline query, blocked wrong-project apply, five real eligible pre-activation executions freezing the baseline and entering collecting state after Enable. UI regression and full suite pending. No installed update for this follow-up yet; unfinished availability migration remains disabled.

Finding-flow independent review resolved three cases before final validation: a concurrent different draft returns409 with a link to the existing workflow and preserves form edits; repo-less findings require one unambiguous canonical workspace before any impact cohort can be selected; multiple known roots with only one matching workspace remain explicitly unmeasurable. Tests use real evidence/units to prove an unrelated project's pre-activation executions cannot supply the baseline. Detector refresh cannot overwrite explicit user-authored finding instructions. Scoped final review approved with no remaining blockers. Final full suite is running against frozen source/artifacts; lint and parity pass. Local rollout remains gated on that result; no live finding/workflow has been created by validation.

Finding-flow final full suite: **2062 passed, 22 skipped**, one existing Starlette warning in173.95s (`/tmp/reflect-finding-workflow-final-tests.log`). Full Ruff, whitespace, JS syntax and generated artifact parity pass. An intermediate showcase test caught optional false scope metadata changing existing serialized samples; metadata is now emitted only when unavailable, preserving original samples (showcase+measurement94passed before the full rerun). Final review approved.

Installed follow-up through the previously reviewed guarded rollout:219 source files hash-verified, same uv environment/dependencies and dashboard config, PID3172. Read-only verification confirms live served artifact byte-matches source and includes Create workflow from finding; OpenAPI exposes the finding draft context route. No live candidates/workflows created; availability migration remains disabled. Startup refresh was running without error at first read-back; final readiness check follows.

Installed follow-up readiness verified: refresh completed without error at2026-11-14T11:17:36.706029Z; next refresh11:22:36.706029Z (300seconds after completion). A read-only request for a real existing finding returned matching finding identity and target metric with215 grouped observation references. No workflow was created or enabled on live data during verification. Bounded finding-to-workflow delivery is complete; broader concurrent refresh availability remains unfinished/disabled.
