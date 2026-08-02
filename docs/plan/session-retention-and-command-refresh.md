# Reflect Session Retention and Command Refresh Plan

## Purpose

This plan addresses real Reflect product issues. The `reflect` skill helped investigate and structure the work, but the skill itself does not change Reflect. The fixes require implementation in the Reflect repository.

The work has two connected goals:

1. Safely remove obsolete sessions whose start time is invalid or stuck at the Unix epoch, while retaining durable memories and their provenance.
2. Make command refresh behavior consistent so read-oriented commands do not unexpectedly rebuild a large local database and appear stalled.

No database cleanup is performed by this plan document.

## Current evidence

The local snapshot inspected on 2026-07-28 showed:

- The Reflect SQLite database was approximately 5.4 GB.
- It contained 717 sessions.
- 277 sessions had an invalid or epoch start time before the year 2000.
- 87 of those epoch sessions had no trusted activity within the previous 60 days and were initial prune candidates.
- 190 epoch sessions had activity within the previous 31-60 days and must remain.
- The database contained approximately 1.13 million raw events and 456,000 steps.

These counts are a dated baseline and may change before implementation.

## Confirmed problems

### Invalid start times are sticky

Session normalization currently preserves the earliest start value using logic equivalent to:

```python
started_at = MIN(sessions.started_at, excluded.started_at)
```

Once a session contains `1970-01-01` or another invalid early value, a later valid timestamp cannot repair it.

### Some commands perform hidden expensive preparation

In the inspected 0.9.3 behavior, `reflect ask` synchronously prepares the SQL report database with native sessions before answering. It has no progress output and no refresh switch, so a legitimate rebuild can look like a stall.

`reflect improve` has refresh controls, progress output, and incremental preparation. The scoped-improvements implementation also makes its default path snapshot-first; equivalent read-first handling still needs to be applied consistently to the other commands.

### Deleting sessions can obscure memory provenance

The schema preserves memory and reviewed-evidence rows through `ON DELETE SET NULL`, but simply clearing `session_id` makes their origin look absent. Pruning must preserve both the durable content and a useful record of the removed source session.

### Deleted sessions can be re-imported

If an old source file remains discoverable, later ingestion can recreate a pruned session unless Reflect records that it was intentionally removed.

## Design

### 1. Track trusted last activity

Add `sessions.last_observed_at`.

Backfill and maintain it from trusted source timestamps:

- `raw_events.observed_at`
- step start/end timestamps
- a valid session `ended_at` as a fallback

Do not use ingestion time as session activity, because re-reading an old file must not make an obsolete session appear recent.

Update normalization so:

- A valid timestamp replaces a missing, epoch, or otherwise invalid `started_at`.
- The earliest valid start remains stable after correction.
- Invalid timestamps never win over valid timestamps.

### 2. Apply a narrow retention policy

Version 1 targets only sessions whose `started_at` is missing or earlier than the year 2000. Valid historical sessions are out of scope.

A session is eligible when:

- Its trusted `last_observed_at` is older than 60 days.
- It is not active.
- It is not the parent of a retained child session.

Use an explicit policy/service boundary, such as:

- `SessionRetentionPolicy` to classify sessions.
- `SessionPruner` to preview and apply the operation transactionally.

### 3. Preserve memories and reviewed evidence

Before deleting a session, create a lightweight `pruned_sessions` tombstone containing:

- Original session ID
- Agent
- Repository/workspace
- Source kind and source reference
- Last trusted activity
- Prune time and reason
- Optional source hash and deleted row counts

Durable memories, evidence, and observation evidence must remain searchable. Their provenance should retain the original session ID or a reference to the tombstone and indicate that the source session was pruned.

Memory search, evidence retrieval, and provenance display must continue to work after pruning.

### 4. Prevent accidental resurrection

During ingestion:

- Ignore unchanged source material for a tombstoned session.
- Do not recreate the session merely because its old source file is still present.
- Permit recreation only when genuinely newer observed activity exists than the tombstone records.
- Keep source checkpoints aligned with the tombstone behavior.

### 5. Provide a safe pruning CLI

Proposed interface:

```text
reflect db prune-sessions --older-than-days 60
reflect db prune-sessions --older-than-days 60 --apply
reflect db prune-sessions --older-than-days 60 --apply --vacuum
```

Behavior:

- Dry run is the default.
- Preview exact candidate sessions, source kind, last activity, and dependent row counts.
- Require `--apply` for mutation.
- Execute the apply phase in a transaction.
- Offer a database backup before applying.
- Make `VACUUM` explicit; never run it implicitly.
- Run `PRAGMA foreign_key_check` after applying.
- Rebuild affected graph and rollup data.

Prune bulky session-scoped data, including raw events, steps, model/tool/MCP calls, transient loop results, graph data, and rollups. Preserve durable memories and reviewed evidence through the tombstone relationship.

SQLite will reuse freed pages after deletion, but the file will shrink on disk only after an explicit vacuum.

### 6. Standardize command preparation

Add a shared preparation contract for commands:

- Read the existing prepared SQLite snapshot immediately by default.
- Open read paths through query-only SQLite connections.
- Never ingest, migrate, reconcile, or rebuild from a read command.
- Use `reflect refresh` for a complete explicit rebuild.
- Use `--refresh` to request a command-owned ingestion, normalization, and derived-data refresh.
- Support `--no-refresh` explicitly.
- Print refresh stages to stderr immediately.
- If the snapshot is missing, outdated, empty, or maintenance-stale, return an actionable refresh error without changing it.

Implement this as reusable layers:

- `SQLiteSnapshotInspector` owns query-only schema and data readiness checks.
- `CommandPreparationPolicy` decides read, explicit refresh, or error.
- `SnapshotLifecycleService` coordinates inspection and a swappable
  `SnapshotRefresher`.
- Read service factories disable constructor migrations and search-index
  maintenance; mutation factories retain schema initialization.

Apply it as follows:

| Command | Desired behavior |
|---|---|
| `usage` | Query-only by default; stale rollups require explicit refresh |
| `improve` | Read the current-project snapshot by default; refresh only when requested |
| `ask` | Read first; add refresh/no-refresh and progress |
| `workflows list` | Read first; refresh only when requested |
| `feedback` | Never perform a full implicit refresh for a single write; return guidance if the session is absent |
| `skills` | List only; `skills sync` explicitly reconciles registry revisions |
| `skills discover` | Reuse graph evidence; make preparation explicit and visible |
| `loops` | Allow reading the existing loop registry without rerunning detectors; offer explicit refresh |
| Dashboard/server | Keep snapshot-first startup and background refresh |
| Memory/db/gateway/setup/update | Keep current behavior where no unnecessary preparation occurs |
| MCP read-only tools | Match their annotations with query-only connections |

## Expected implementation areas

- `src/reflect/store/migrations/022_session_retention.sql`
- `src/reflect/store/normalize.py`
- `src/reflect/store/retention.py`
- `src/reflect/store/refresh_plan.py`, if a shared derived-data refresh plan is useful
- `src/reflect/core.py`
- `tests/store/test_retention.py`
- `tests/store/test_normalize.py`
- `tests/store/test_migrate.py`
- `tests/improvements/test_service.py`
- `tests/test_cli.py`
- `CHANGELOG.md`

The current Reflect checkout had unrelated modifications when this plan was prepared. Implementation must preserve them and avoid broad rewrites, especially in `src/reflect/core.py` and `tests/test_cli.py`.

## Test coverage

Add tests for:

- A valid event correcting an epoch session start.
- The exact 60-day boundary.
- Retaining recent epoch sessions.
- Retaining active sessions and parents of retained children.
- Pruning old epoch sessions.
- Dry-run mode making no mutations.
- Transactional apply, cascades, and absence of orphans.
- A tombstone preventing unchanged old input from resurrecting a session.
- Genuinely newer observed activity permitting a session to return.
- Memories and reviewed evidence surviving pruning.
- Memory search and provenance identifying a pruned origin.
- Whether preparation is invoked for `ask`, `workflows list`, `feedback`, `skills discover`, and `loops`.
- Immediate progress output during an explicit refresh.
- Behavior with a large store.

## Validation

```text
poetry run pytest tests/store/test_retention.py tests/store/test_normalize.py tests/store/test_migrate.py tests/improvements/test_service.py tests/test_cli.py -q
poetry run ruff check .
poetry run pytest -q --no-cov
poetry run python scripts/release_workflow.py release-notes <version>
```

Also run focused syntax/import validation for any new retention or refresh modules.

## Definition of done

- Dry run against a copy of the inspected database reports approximately 87 candidates, adjusted for activity since the 2026-07-28 snapshot.
- Applying the policy leaves no invalid/epoch session older than 60 days by trusted `last_observed_at`.
- Durable memories and reviewed evidence remain searchable and keep meaningful provenance.
- Unchanged historical source files cannot resurrect pruned sessions.
- `reflect ask ... --json` reads the existing 5.4 GB store without a hidden refresh and completes in under two seconds under normal local conditions.
- `reflect ask --refresh` emits stage progress within one second.
- `workflows list` and `feedback` perform no hidden full preparation.
- Focused and full tests pass.
- The changelog documents retention semantics and command refresh behavior.

## Rollout

1. Land timestamp correction and backfill.
2. Land tombstones and ingestion resurrection protection.
3. Land dry-run pruning and validate candidate classification on a database copy.
4. Land transactional apply and preservation checks.
5. Land shared command preparation behavior.
6. Release with the prune operation opt-in.
7. After validation and backup, run apply separately; run vacuum only when disk reclamation is desired.
