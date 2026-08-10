# Contributing to Reflect

Thanks for helping improve Reflect. This guide describes the architecture and
review contracts that keep the project local-first, evidence-backed, and
maintainable.

## Development setup

Reflect supports Python 3.12 and 3.13 on macOS and Linux. Source development
uses Poetry:

```bash
poetry install --extras test
poetry run reflect --demo
poetry run reflect doctor
```

Run the narrowest relevant tests while developing, then the complete checks
before opening a pull request:

```bash
poetry run ruff check .
poetry run pytest -q --no-cov
```

CI also installs the package from source and verifies its command surface,
package build, and supported Python and operating-system matrix.

## Product and trust boundary

Reflect is a local-first telemetry and workflow-improvement tool for AI coding
agents. Its core loop is:

```text
capture evidence -> discover a repeated procedure -> review a bounded workflow
    -> install with approval -> measure comparable future tasks
```

Contributions must preserve these guarantees:

- local evidence remains useful without a hosted service or Reflect account
- configuration never masquerades as observed telemetry
- findings are evidence, not installed interventions
- discovery and measurement are read-only
- setup, retention, installation, rollback, and external writes are explicit
- missing telemetry is unavailable, never silently converted to numeric zero
- outcome movement and attributable workflow impact remain separate claims

Do not add developer or agent ranking, automatic workflow installation, or
hidden mutation to read-oriented commands.

## Architecture and ownership

The canonical dependency direction is:

```text
source adapters -> ingestion -> normalization -> canonical SQLite
    -> derived state -> application services -> presentation
```

SQLite is the analytical source of truth. Presentation objects such as cards,
truncated session rows, and chart series are never aggregation inputs.

| Area | Primary owner |
|---|---|
| CLI composition and operator flows | `src/reflect/core.py`, `src/reflect/cli/` |
| Source discovery | `src/reflect/parsing.py` |
| Ingestion and checkpoints | `src/reflect/store/ingest.py` |
| Canonical normalization | `src/reflect/store/normalize.py` |
| Snapshot lifecycle | `src/reflect/preparation.py` |
| Refresh pipeline | `src/reflect/preparation_pipeline.py` |
| Public SQL read models | `src/reflect/views/` |
| Dashboard adapters | `src/reflect/dashboard_*.py` |
| Dashboard process lifecycle | `src/reflect/report_server.py` |
| Authored browser client | `src/reflect/frontend/` |
| OTLP gateway and raw segments | `src/reflect/gateway.py`, `src/reflect/raw_segments.py` |
| Agent support metadata | `src/reflect/agent_capabilities.py` |
| Findings, workflows, skills, and impact | `src/reflect/improvements/` |

Sessions are provider conversation containers and navigation units. Bounded
execution units are the evidence samples for procedure discovery, adherence,
and impact. Do not introduce another task model when an execution unit or task
contract can own the concept.

`tool_calls` owns one logical invocation. `mcp_calls` is its one-to-one protocol
metadata extension, not another countable call ledger.

## Design rules

### Keep one owner per concept

Before adding a new product concept, identify:

1. its user-facing noun and stable identity
2. the canonical table and module that own writes
3. the evidence unit behind the claim
4. the lifecycle dimension that changes
5. how refresh remains idempotent
6. the exact evidence exposed to users

Extend existing execution units, workflow contracts, logical calls, capability
metadata, and evidence ledgers before creating a parallel abstraction.

### Keep code lean

Use small classes for meaningful state, lifecycle, configuration, strategy, or
swappable behavior. Keep stateless transformations as functions. A new helper,
class, interface, fixture, or module should remove demonstrated duplication or
isolate a real variant.

Prefer composition and narrow provider adapters over growing `if`/`elif`
ladders. Keep provider-specific behavior out of shared orchestration.

### Make database evolution one-way

Schema changes use a new numbered migration under
`src/reflect/store/migrations/`. Historical migrations are immutable. Move
durable data forward, then remove obsolete runtime readers, dual writes,
aliases, routes, and fallback branches. Add migration and idempotency coverage.

Rollback means restoring a pre-migration database backup with the matching
older binary; it is not a permanent compatibility mode in current code.

### Keep reads bounded and side-effect free

Read commands and dashboard queries use query-only SQLite connections. Ingest,
migrate, reconcile, rebuild, prune, vacuum, install, and roll back only through
explicit mutation paths.

Filter graph inputs before joins, scope work to selected session or execution
unit IDs, cap timeline payloads, and paginate evidence drill-downs. Interactive
dashboard endpoints must remain responsive on large local stores.

### Preserve provenance and privacy

Normalize shared facts without losing their source. Mark estimated tokens and
costs as estimates. Keep prompt and response capture opt-in. Never put secrets,
private paths, repository identities, or confidential transcripts into tests,
fixtures, screenshots, or public issue reports.

External writes require an exact preview, explicit approval, and read-back
verification. Apply and rollback are separate reviewed operations.

## Common change paths

### Browser changes

Edit only the authored files under `src/reflect/frontend/`, then regenerate the
two shipped single-file clients:

```bash
poetry run python scripts/build_dashboard.py
cmp -s src/reflect/data/index.html docs/report.html
```

Never edit `src/reflect/data/index.html` or `docs/report.html` independently.
Preserve URL filters, selected sessions, tabs, focus states, reduced-motion
behavior, and responsive layouts. Validate heavy filtered views as well as the
default dashboard.

### New metrics or evidence

Add canonical fields to the owning model or table, populate them during
normalization or derivation, expose them through the owning read model, and
then update only the renderers that need them. Do not reconstruct aggregates
from dashboard session cards.

### New or changed agent support

Declare product capabilities in `agent_capabilities.py`. Keep setup, doctor,
aliases, hooks, native telemetry, MCP, skill targets, and tests aligned with the
same registry. A working MCP task does not prove native tokens, cost, hooks, or
conversation capture.

### Retention and raw telemetry

Raw active segments are bounded capture files; closed segments are replay
buffers. Never delete unprocessed evidence to make room. A closed segment is
eligible for cleanup only after successful canonical normalization. Retention
commands should default to preview, make apply explicit, preserve durable
provenance, and make disk compaction a separate choice.

## Tests and release notes

Add regression coverage at the lowest owning boundary. Useful focused commands
include:

```bash
poetry run pytest tests/test_dashboard_json.py -q --no-cov
poetry run pytest tests/test_dashboard_sql_api.py -q --no-cov
poetry run python -m py_compile src/reflect/dashboard_*.py
```

For broad changes, run the complete suite. Local-agent tests that invoke
authenticated external clients are opt-in and marked `local_agent_e2e`.

Every user-visible feature, fix, dependency change, or behavior change belongs
under the top `## 0.x.x (unreleased)` heading in `CHANGELOG.md`. Use `Added`,
`Fixed`, `Changed`, or `Dependencies`. Do not use a generic `Unreleased`
heading.

## Pull request checklist

- The change has one clear owner and does not introduce a parallel source of truth.
- Read paths remain query-only and mutations remain explicit.
- Provider differences stay behind a narrow capability or adapter boundary.
- New schema has a numbered one-way migration and idempotency coverage.
- Browser artifacts were regenerated and remain byte-identical when applicable.
- Narrow tests, Ruff, and the full test suite pass.
- The changelog describes the user-visible effect.
- Fixtures and examples contain no private telemetry or identifiers.
- Dead compatibility paths, duplicate helpers, and superseded fixtures were removed.

For substantial schema, lifecycle, or product-noun changes, open an issue or
proposal first so ownership and evidence semantics can be reviewed before a
large implementation lands.
