# Unified asynchronous backend implementation ledger

Approved goal: one local FastAPI backend for dashboard, CLI, MCP; automatic startup;
long commands return durable jobs by default, --wait attaches. Preserve shared domain
services and eliminate replaced client storage/readiness/locking paths.

## Binding design

- Local-only, one lifecycle owner per canonical database, default port8765; isolated
  endpoint discovery for alternate databases. Host/Origin guards and instance token.
- Typed resource APIs and shared async HTTP client; no generic command RPC or direct
  store fallback. Setup/update/gateway/server/schema export remain local.
- Durable jobs in user-state sidecar initialized before migration. Progress does not
  bump material state. One canonical writer worker and separate external agent worker.
  Queued/running/succeeded/failed/interrupted; idempotency and active refresh joining.
- PublishedSnapshotStore/SnapshotReadContext/UserStateMigration become authoritative.
  Complete generations remain readable during writes. Short user actions independent
  of preparation lock. Disk reserve2GiB, <=3 completed generations plus one temporary.
- Preserve evidence provenance, exact review/file hashes, authored/finding drafts,
  and durable task events until publication. No invented impact or empty missing data.
- All product CLI/MCP paths call backend. Jobs list/show/wait, --wait for long commands;
  discovery stages unverified drafts, never installs. UI shared 1s active/10s idle poller.
- Remove replaced factories/readiness/retry routes and disabled branches, not merely
  layer another facade. Update README, packaged skills and changelog; artifact parity.

## Sequence and status

- [x] Shared backend contracts, durable job registry and process lifecycle.
- [x] User-state cutover, publication, pinned read/action integration.
- [x] Dashboard API/runtime integration and authorization.
- [x] CLI/MCP client migration and asynchronous operation parity.
- [x] Frontend polling, cleanup, documentation.
- [x] Targeted/full regression tests, Ruff, generated parity, independent review.
- [x] Installed validation and guarded live migration after explicit approval.

Baseline: clean feat/guidance-efficiency at2920208a59210bf1cd30563a6dece531f0b5ab1d.
The implementation began in isolated stages; live rollout was performed only after
the operator explicitly approved it.

## Implementation progress

The handoff notes below are chronological checkpoints; later entries supersede
earlier status, test and rollout statements.

- Added JobRegistry and typed job states; atomic submit/join, persistent idempotency
  aliases, worker claim/ownership, progress/result, interrupted-state reconciliation.
- Added shared async HTTP client with bounded requests, no mutation replay or redirect
  following, typed job polling, and private authorization headers.
- Added job process runner and canonical job/status/refresh API integration helper,
  local Host/Origin/token enforcement and browser session cookie.
- Added explicit-generation snapshot lease acquisition. Repository workflow navigation
  merges owned sidecar-only candidates while canonical aggregates stay unchanged.
  User-authored workflows now retain ownership through user-state migration.
- Validation:6 backend tests pass;111 service/state/snapshot regressions pass;25 latest
  state/snapshot tests pass, including authored workflow source-pruning survival and
  explicit old-generation acquisition. Ruff on touched modules passed before latest
  runtime endpoint additions. Test runner is .venv/bin/python (Poetry default env lacks
  pytest); lint uses existing py3.13 Poetry environment ruff.
- Runtime/autostart wiring, publication integration, full CLI/MCP migration, UI and
  docs remain in progress. No live installation, migration, commit or push performed.

Ruling: initialize job tables before user-state cutover; never trigger material revision
on progress. Ruling: merge retained workflow rows only in repository workflow queries,
not aggregate SQL, so source-pruned evidence cannot reappear as observed activity.

### Foundation handoff

Parent requested sequential integration handoff after scoped foundation review.
Report: /tmp/reflect-backend-foundation-report.md (interfaces and unresolved items).
Final combined targeted suite:143 passed,1 existing Starlette warning,10.84seconds.
Touched-file Ruff clean; git diff --check clean. No full-suite or installed validation.
Pool-death recovery now recreates pool; OS worker leases protect live orphan jobs;
real refresh completes both before and after cutover on a fixture. Application saves
and reads a new draft while the canonical evidence writer owns BEGIN IMMEDIATE.

IMPORTANT: production server/client migration remains incomplete. Publisher material
revision currently misses derivation-only changes; resolve before activation. API status
fields are provisional until real dashboard lifecycle integration. Full pending list is
in the handoff report. This is a phase boundary, not completion of the approved plan.

### Server and frontend integration handoff

Production report server now uses durable backend runner, pinned published reads and
independent user action contexts. Old in-process coordinator routing and unused
DashboardDataCache removed. UI polls status once (1s active/10s idle), refreshes by
material revisions without document reload, and retains session/filter state. Canonical
mutation revision tracks derivation-only changes. Lifetime database ownership enforced.
Snapshot-only startup publishes existing completed evidence without ingestion.
Report: /tmp/reflect-backend-server-report.md.
Validation:146 combined focused tests passed; later connection isolation9passed and
snapshot-only bootstrap/jobs9passed. Touched Ruff, whitespace, artifact parity and
built JavaScript syntax pass. No full suite, performance gate, live install or migration.
Ruling: intermediate partial-usage publication is replaced by complete immutable
publication; initial evidence endpoints report preparing until first complete generation.
CLI/MCP and non-refresh jobs remain unmigrated. Do not activate live cutover yet.

Server/UI review fixes: async detail completion resolves ID/request token after navigation
reorder and rejects old scope/publication; navigation503/stale200 retries on shared status
polls without requiring another revision; summary merge retains loaded detail. Snapshot-
only startup validates completed publication rather than interrupted working evidence.
Validation69 focused tests passed plus strengthened actual navigation-reorder regression.


### CLI/MCP and operations integration handoff

Product CLI/MCP now share the authenticated async HTTP client and specific typed
resources; shell completion uses an existing backend without starting a daemon.
Added async discovery, loop build, maintenance, jobs CLI/MCP, separate agent lane,
durable task projection receipts and file action recovery journal. Saved finding
provenance and measurements include independent review state. Preserved CLI human
renderers and isolated demo behavior. README, packaged skill and lockfile updated.

Review regressions cover active rollback approval, post-cutover finding scope,
snapshot-only successive task publication, accepted-after-drain completion, >100
receipt continuation, existing-empty database refresh, draft completions and demo
discovery sources. Combined focused gate54passed; subsequent human-rendering gate
21passed. Full Ruff passes; browser artifacts byte-identical.

Full isolated suite:215failed,1862passed,22skipped,6errors. Legacy client fixtures
and assertions still require migration and real failures must be resolved before
completion. Handoff report /tmp/reflect-backend-clients-report.md, failures
/tmp/reflect-client-suite-failures.txt. No live install/migration or commits.

### Regression convergence (in progress)

Migrated explicit dashboard, usage, impact, scope and task fixtures to published
snapshots and isolated in-process authenticated HTTP; no daemon or live database
is started by the helper. Dashboard SQL API42, usage22, impact9, showcase6 and
inbox/jobs25 tests pass in focused runs. Fixed dependency idempotency before
side effects (409 on conflicting key), durable feedback accepted during canonical
writer locks with idempotent outcome projection, and preparation revision setup
transaction ownership before inbox drain. Usage reads again reject structurally
incomplete rollups; scoped refresh carries exact native session IDs through the
writer instead of silently ingesting all native history. Showcase publishes its
synthetic database explicitly and deterministic UUID imports remain outside fixture
patching. Remaining legacy client/cache/maintenance/MCP fixtures are still being
migrated; full regression gate is not yet green. No live cutover performed.

Regression convergence milestone: service89, publication/accounting33,
cache/product46, provider-discovery/product44, memory-MCP13, loop/drafting41,
retention12, CLI/report33, Explore7, recorded/concurrency/reliability30 and
cross-process action5 focused checks pass (overlapping groups, not a suite total).
Restored workflow type and provenance parity; skill detail metadata handling;
loop promotion identity; cold explicit-source discovery dependency; missing-store
maintenance; retention dry-run nonmutation and backup-before-migration ordering.
All product CLI/MCP/shell-completion imports now have an AST guard against SQLite
and store/preparation application ownership. Capture doctor reads existing backend
checkpoints, reporting offline files as unclassified. Removed unused synchronous
skill selection/install helpers. Ruff and generated dashboard parity pass.
The first complete post-migration suite is running; its mid-suite failures are
being investigated before any completion claim or live cutover.

First frozen full regression gate:2103passed,22skipped in178.87s; all417
source/test/artifact SHA-256 entries remained unchanged. Independent review then
found two concrete gaps: usage refresh omitted the default hook trace source;
retention could project pending feedback before its dry-run/backup. Added three
regressions that failed before the fixes. Usage now carries the resolved trace
path, and retention leaves independently queued feedback projection untouched.
Focused usage/backend/retention gate60passed; final frozen full rerun pending.

### Completed isolated regression gate

After the independent review fixes, the frozen full suite passed: **2106 passed,
22 skipped**, one Starlette/httpx deprecation warning, in 178.67 seconds. All 417
source/test/script/artifact/dependency hashes remained unchanged during the run.
Ruff, diff whitespace and generated dashboard byte parity pass. Synthetic showcase
checks and client SQLite ownership guards are included in the suite. Final evidence:
`/tmp/reflect-regressions-reviewed-final.log` and
`/tmp/reflect-backend-regressions-report.md`. No live installation/migration or
commit/push performed; installed large-store performance remains unverified.

### Installed rollout approval gate

Reviewed frozen source gate: 2106 passed,22 skipped; Ruff, whitespace, artifact parity and417-file manifest verification passed. Wheel built at /tmp/reflect-backend-wheel/o11y_reflect-0.9.8-py3-none-any.whl. Guarded rollout supervisor reviewed, including continuous writer exclusion, clone-only rehearsal, disk guards, cancellation cleanup and controlled handoff.

The initial execution was rejected by automatic approval review before process
creation. The operator later explicitly approved the guarded rollout; final results
are recorded below.

### Explore latency follow-up

The clone rehearsal exposed unused overview work in Explore Usage and wide-row
sorting in the graph's virtual tool projection. Usage now reads only its displayed
typed tabs with the same event-scoped counts and costs. Graph ranks the same recent
40 call IDs through an existing covering index before fetching their details.
No migration, evidence reduction, or live-store edits are required.

Frozen-time rehearsal comparison preserved all 50 Usage fields exactly; the complete
Graph payload also matched the baseline. Three fresh-process reads with warm OS
cache measured Usage 0.294/0.278/0.285s and Graph 0.605/0.585/0.687s. Physical disk-cold
latency remains a separate rollout gate. Targeted validation: 95 passed; repository
Ruff, whitespace, and generated dashboard parity passed. Full regression rerun is
in progress; evidence: `/tmp/reflect-explore-performance-report.md`.

Performance follow-up final frozen gate: **2111 passed, 22 skipped** in 176.46s;
all 417 source/test/artifact manifest hashes unchanged through test and wheel build.
Updated wheel: `/tmp/reflect-backend-wheel-perf/o11y_reflect-0.9.8-py3-none-any.whl`,
SHA-256 `8431ce011c7b97891021225c7c2f498318f6af4c7bd15fd4bfd9284a370388a5`.
Full log: `/tmp/reflect-performance-full.log`; manifest:
`/tmp/reflect-performance-frozen-manifest.json`. Installed cutover and physical
cold-cache performance remain parent rollout checks.

### Cold-query follow-up after retry3

Retry3 isolated the remaining time in session scope selection and semantic edge
selection. Explore now requests IDs only and uses the existing covering step-time
index. Scoped graph edges probe displayed sources and explicitly retain the prior
session/rowid tie ordering. Exact full Usage and Graph payload comparisons pass;
no schema change was added. First post-edit clone measurements: Usage 1.513s and
Graph traced work 2.357s; subsequent combined Graph call 0.381s with warm filesystem
cache. Supervisor cold-read acceptance remains a separate gate.

Final frozen suite: **2115 passed, 22 skipped**, 171.24s. Ruff, whitespace,
authored/generated dashboard parity, and all 417 frozen manifest hashes passed.
Wheel: `/tmp/reflect-backend-wheel-cold/o11y_reflect-0.9.8-py3-none-any.whl`, SHA-256
`295860d0a740b7a2d8e0a4727469106a728b29fddcb26c069ef94d431d2a7258`.
Manifest: `/tmp/reflect-retry3-frozen-manifest.json`; full log:
`/tmp/reflect-retry3-full.log`. No live changes were made by this follow-up.

### Retry4 graph ranking gate

Migration57 adds two compact graph metadata indexes (77.06MiB combined on the
clone,7.52s construction) and fetches node/bridge payloads only after existing
limits. Exact full Graph payload parity passes; first combined indexed-clone read
2.305s. No live changes. Final regression gate: **2116 passed,22 skipped** in
176.73s, Ruff and dashboard artifact parity pass. All418 hashes remained unchanged
during tests; wheel build updated only generated SOURCES.txt to include migration57.
Candidate wheel `/tmp/reflect-backend-wheel-rank/o11y_reflect-0.9.8-py3-none-any.whl`,
SHA256 `7adb281e847f6843748ef4a26f6ae6481715482f051271b946d9d523ba9f1a70`.
### Final installed rollout and job-path verification

The guarded APFS-clone rehearsal and live cutover both applied migration57. The
7-day Cursor reads measured Usage1.51s, Tools0.11s and Graph2.15s in the clone;
the live reads measured1.52s,0.11s and2.07s. Usage and Tools payload sizes matched
between rehearsal and live. The report server restarted with scheduled refresh
enabled at300seconds; capture remained accepting. Authenticated status and Impact
APIs returned200, and the dashboard data route returned200 after its background read.

After rollout, Codex skill discovery exposed that the backend inherited `/tmp` as its
working directory, so Codex rejected the untrusted directory. Discovery and loop
build now carry the invoking project root into the durable job and agent process.
The installed retry ran from the trusted repo and succeeded with no candidates to
stage. The original failed job remains inspectable; the reported `Job not found`
came from a misspelled database path ending in `.d` rather than `.db`.

Final source regression gate: **2116 passed,22 skipped** in184.69seconds, with one
existing Starlette/httpx deprecation warning. Focused agent-job regressions passed,
generated dashboard artifacts match, source compiles, and `git diff --check` passes.
