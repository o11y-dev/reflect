# Workflow and Impact Tables Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace overwhelming cards with two linked, expandable analytical tables and honest evidence summaries.
**Architecture:** Keep canonical workflow/measurement adapters; use the existing frontend renderer and delegated actions. Correct token coverage in the measurement service rather than inferring missing values in the browser.
**Tech Stack:** Python/SQLite, vanilla JavaScript, CSS, pytest/Node contracts.
**Spec:** `docs/superpowers/specs/2026-11-13-workflow-impact-tables-design.md`

## Global constraints

- Preserve existing working-tree changes; execute in the current feature branch because this work builds on its uncommitted fixes.
- Edit authored frontend files and regenerate both packaged artifacts.
- No new discovery algorithms or invented evidence. Preserve explicit installation review.
- Missing telemetry is not zero; observed movement is not causal impact.
- User approved implementation; proceed inline without another approval round. Leave changes reviewable and uncommitted with the existing work.

## Review focus

- Explicit zero usage versus absent usage: backend coverage regression.
- Historical measurements without current coverage: recorded comparison regression.
- Unknown cohort dates and missing workflows: frontend fallback regression.
- Filtered-out cross-view destinations and repeated refreshes: navigation/expansion regression.
- Empty results, keyboard expansion and narrow screens: render contract and live visual checks.

### Task 1: Evidence semantics

Files: `src/reflect/improvements/measurement.py`, `tests/improvements/test_service.py`, relevant migration/recorded evidence tests if required.

- [x] Add and run a failing regression distinguishing absent token attributes from explicit zero and real usage.
- [x] Reuse canonical usage facts for bounded token measurement. Ensure retained incorrect comparisons cannot appear as valid current zero-to-zero results.
- [x] Run focused measurement and recorded-evidence tests.

### Task 2: Two linked tables

Files: `src/reflect/frontend/js_impact.js`, `js_ledger.js`, `index.template.html`, `css_improvements.css`, `tests/test_dashboard_impact_rendering.py`.

- [x] Add behavioral contracts for table markup, evidence limits, missing metrics and expansion/navigation state.
- [x] Render compact workflow/impact rows with expandable detail rows, explicit dates, coverage and historical state labels.
- [x] Add delegated expansion and cross-view navigation preserving URL filters and selected row. Use shared impact results for workflow outcome summaries, with explicit freshness labels.
- [x] Remove prominent heuristic confidence; separate history and current suggestions; preserve existing review actions.
- [x] Run browser contracts and update intentional old layout/copy expectations.

### Task 3: Package and verify

Files: `CHANGELOG.md`, generated `src/reflect/data/index.html` and `docs/report.html`.

- [x] Update changelog; build dashboard and confirm byte identity.
- [x] Run lint and focused tests, then full suite for the measurement change.
- [ ] Review changes, install the local source package, restart dashboard and inspect real rows and navigation visually. Review/install/restart complete; live visual inspection remains blocked by native Chrome timeouts and an unavailable browser connector.
- [x] Record validation and any remaining evidence limitations below.

## Execution log

- Plan and design reviewed together; two-view scope approved by user.

- Validation: final full suite `1982 passed, 22 skipped`; `uvx ruff check .` passed; `git diff --check` passed; generated dashboard artifacts byte-identical.
- Independent review corrected partial-usage totals, exact workflow navigation for grouped-away variants, and intervention-specific comparison histories. No remaining material findings.
- Installed with `uv tool install --force --reinstall` and restarted local server (PID 73975). Exact requested URL serves the table renderer. Served JavaScript passes `node --check`.
- Live API: exploration observed 1.25 -> 0 with August 30–September 28 frozen cohorts; token comparison now null/not_measurable; retry baseline remains unavailable. Background improvements validation still running.
- Visual limitation: native Chrome showed the old cached UI, then a blank reload and timed out. Browser connector reports no browser available. Cannot claim final rendered appearance or native cross-tab interaction verified; Node behavior contracts for expansion/filter/navigation passed.


### Follow-up usability and evidence fixes

- Dismiss/Restore is installed, persists across refreshes, handles grouped variants, and keeps unrelated cached dashboard data. Its final validation passed 1990 tests, with 22 skipped.
- Explore graph rendering now disposes prior simulations, timers and listeners, avoids rebuilding identical payloads, and preserves containers through empty results. Review corrected empty graph sizing and fitting after deliberate selection changes.
- Lifetime usage exposes retained token buckets with provenance; event-window views explain excluded undated totals. Cursor pricing diagnostics could not resolve the four captured model names against the current pricing table.
- The two user-supplied sessions had matching canonical tool inventories between Execution and scoped Explore: Cursor 12 calls, Codex 180 at inspection. A raw Codex call carried namespace mcp__cua_repl but ingestion discarded it. The shared qualifier and decoder version 5 repair the source projection; regression tests prove the durable upgrade does not duplicate calls.
- Installed via uv and restarted the dashboard. Served HTML contains the graph, usage, same-session Explore link and Dismiss controls. Historical repair is running; live MCP count read-back is still pending. Chrome rejected further actions because the user was interacting with it, so final graph visual behavior remains unverified.

- Final follow-up validation: 1995 passed, 22 skipped; Ruff clean; generated browser artifacts identical.

- Live normalization completed: canonical store and `/api/session/{session_id}` expose `cua_repl/js` with count 1. Explore aggregate publication remains preparing.

- Live Explore read-back is now ready: one MCP call, server cua-repl. It agrees with session detail cua_repl/js count 1. Background improvements derivation continues independently.

### Captured evidence and loading follow-up

- Explore preparing states now show an accessible loading indicator and automatic polling; Retry remains only for actual errors.
- Literal tool wrappers are decoded without executing code. Individual results are required; unsupported code and incomplete output stay opaque. Git and skill evidence keep the canonical owner plus invocation index.
- Skill discovery follows captured local read paths, including plugin directories, with invocation workdir and home expansion. Redirected writes are not skill reads; current file versions are not assumed to be the captured version.
- Added a change-review procedure supported by 11 independent local sessions. Metadata/diff reads are observed; head checks, runtime evidence boundaries, and publication approval are proposed safeguards. Benefits remain unmeasured, and missing adherence contracts cannot fall back to tool-failure counts.
- Historical Git repair runs directly in migration 56 rather than queuing raw normalization. The skill-usage join explicitly uses the existing step index to avoid scanning every session tool per step.
- Local installation uses the uv tool environment. Live validation must include the completed snapshot, workflow API, supplied sessions, and browser loading behavior.
- Follow-up validation: full suite 2008 passed, 22 skipped; separate 1000-step skill projection regression passed; Ruff and diff whitespace checks clean; generated browser artifacts identical. Live HTML and workflow API contain the new controls and review candidate.
- Session Execution now reads canonical skill evidence shared with Explore, with attempted loads shown separately. Final focused checks: 77 session/API tests and 42 streaming/browser tests passed. Live session API returned brainstorming loaded once, writing-plans attempted once, one cua_repl/js MCP call and five unresolved Git operations in 0.16 seconds.
- Historical publication completed successfully in 266.63 seconds: 5032 execution units, 1623 sessions retained, no pending historical sessions. Execution derivation took 122.58 seconds and skill projection 62.53 seconds; the subsequent input-first optimization skips unrelated output decoding (17 skill tests passed). Large-batch execution reconstruction and coarse progress remain observed refresh limitations.
