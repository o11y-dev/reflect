# Analyst Desk Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize Reflect's whole browser dashboard into a table-led analyst workspace with persistent navigation, visible scope, and evidence details that preserve the user's place.

**Architecture:** Keep the current dashboard data contracts and surface-specific rendering. Replace the horizontal top-level tab row with a consistent left navigation frame, share scope and detail patterns across surfaces, and keep the URL as the navigable state source. Author all changes in the existing frontend modules and rebuild the packaged HTML artifacts.

**Tech Stack:** HTML template, CSS, vanilla JavaScript, existing dashboard APIs, and `scripts/build_dashboard.py`.

**Spec:** `docs/superpowers/specs/2026-11-18-analyst-desk-dashboard-design.md`

## Global Constraints

- Authored frontend files live in `src/reflect/frontend/`. Generated `src/reflect/data/index.html` and `docs/report.html` must be rebuilt and remain byte-identical.
- Preserve public JSON/API contracts and current data semantics. Frontend-only composition changes should not fork or reaggregate backend read models.
- Preserve the existing local-first behavior and URL filters.
- Keep the user’s pre-existing working-tree changes intact; the current checkout contains unrelated modifications in CLI, backend, tests, README, changelog, and packaged skill files.
- Follow repository changelog and dashboard build rules for user-visible implementation changes.

## Review Focus

- Deep links and browser history across all six surfaces and Explore subviews must restore the selected view.
- Filters that are inactive on a surface must remain visibly inactive and restore on a compatible surface.
- Missing or invalid selected-record IDs must not leave stale detail content visible.
- The navigation, tables, and full-width session detail must fit desktop and mobile without horizontal page overflow.
- Loading, stale, partial, empty, and failed states must remain distinct after the layout change.

---

### Task 1: Build the persistent Analyst desk shell

**Files:**
- Modify: `src/reflect/frontend/index.template.html`
- Modify: `src/reflect/frontend/css_foundation.css`
- Modify: `src/reflect/frontend/js_navigation.js`
- Modify: `src/reflect/frontend/js_bootstrap.js`

**Interfaces:**
- Consumes: current `data-tab` names (`sessions`, `inbox`, `workflows`, `skills`, `impact`, `explore`) and Explore `data-explore-view` values (`usage`, `tools`, `graph`, `context`).
- Produces: one persistent primary rail, labeled secondary navigation/scope pane, one active content workspace, and active URL-backed navigation using the existing `activateTab(tabName, options)` entry point.

- [ ] Replace the horizontal top-level navigation in `index.template.html` with the compact global rail and labeled section navigation shown in the approved Analyst desk direction. Keep the existing section IDs and tab names so data loaders and deep links retain their identities.
- [ ] Define shared shell geometry, active states, focus styles, and the responsive collapse/overlay behavior in `css_foundation.css`, retaining Reflect's dark surfaces and orange active signal.
- [ ] Update `js_navigation.js` and `js_bootstrap.js` so click, keyboard, browser back/forward, initial deep links, and existing `tab`/`view` URL parameters continue to select exactly one visible panel.
- [ ] Manually verify direct links to all six surfaces, each Explore subview, keyboard navigation, and back/forward navigation at desktop and narrow viewport widths.

### Task 2: Unify scope and table-led surface hierarchy

**Files:**
- Modify: `src/reflect/frontend/index.template.html`
- Modify: `src/reflect/frontend/css_sessions.css`
- Modify: `src/reflect/frontend/css_improvements.css`
- Modify: `src/reflect/frontend/css_explore.css`
- Modify: `src/reflect/frontend/js_navigation.js`
- Modify: `src/reflect/frontend/js_impact.js`
- Modify: `src/reflect/frontend/js_explore.js`

**Interfaces:**
- Consumes: existing `D` dashboard payloads, current filters, and each surface's existing read API.
- Produces: one surface-aware scope presentation, compact table/row hierarchy for analytic views, and explicit scope labels for filters that do not apply.

- [ ] Move section-specific filters into the shared scope pane and show only controls applicable to the active surface. Do not change filter values or backend query semantics.
- [ ] Keep inapplicable filters in the URL as saved inactive state; restore them when returning to a compatible view, and label the inactive state clearly.
- [ ] Rework Sessions, Inbox, Workflows, Skills, Impact, and Explore summaries into compact analytic rows/tables with concise headings and supporting counts. Keep existing evidence links, create/review actions, and impact qualifications attached to the same records.
- [ ] Keep Explore's Usage, Tools & MCP, Graph, and Context subviews in one secondary navigation list with the current per-view scope explanation. Keep Git & delivery as a clearly surfaced section inside Usage; do not introduce a new data view.
- [ ] Manually verify that existing skills remain listed independently of discovery candidates; findings still lead to workflow creation; and impact never presents insufficient evidence as no effect.

### Task 3: Preserve context in full-width session details and evidence inspection

**Files:**
- Modify: `src/reflect/frontend/index.template.html`
- Modify: `src/reflect/frontend/css_sessions.css`
- Modify: `src/reflect/frontend/css_improvements.css`
- Modify: `src/reflect/frontend/js_navigation.js`
- Modify: `src/reflect/frontend/js_sessions.js`
- Modify: `src/reflect/frontend/js_ledger.js`

**Interfaces:**
- Consumes: current selected session, finding, workflow, skill, and impact IDs already represented in URL parameters and existing detail loaders.
- Produces: full-width session detail with a return-to-list action that restores the same selected row and scroll position, plus on-demand evidence detail that does not discard filter or surface state.

- [ ] Add a full-width session detail state entered from the Sessions table. The return action must restore the original table, URL scope, selection, and scroll position.
- [ ] Adapt existing finding/workflow/skill/impact detail interactions into a consistent contextual inspector or drawer while retaining their current data loaders and mutation safeguards.
- [ ] Clear or replace detail content when the URL-selected record changes or no longer exists; do not leave a prior record's details visible during loading or failure.
- [ ] Manually verify keyboard operation, deep-linked detail, back/return behavior, missing IDs, and narrow-screen drawer behavior.

### Task 4: Rebuild generated dashboard files and visually verify

**Files:**
- Generate: `src/reflect/data/index.html`
- Generate: `docs/report.html`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: authored template, CSS, and JavaScript from Tasks 1–3.
- Produces: generated browser artifacts byte-identical to one another and a visible desktop/mobile dashboard matching the approved direction.

- [ ] Add a concise entry under the current unreleased changelog heading for the dashboard navigation and layout change.
- [ ] Run `poetry run python scripts/build_dashboard.py`.
- [ ] Confirm `src/reflect/data/index.html` and `docs/report.html` are byte-identical.
- [ ] Open the rebuilt local dashboard and visually inspect desktop and mobile views for hierarchy, overflow, active navigation, loading/empty states, tables, and session return behavior. Fix concrete visual issues before finishing.

## Self-review

- Spec coverage: all six surfaces, Explore subviews, preserved evidence semantics, global URL-backed state, inactive filters, distinct loading/empty/stale/error states, responsiveness, accessibility, and generated artifact parity are assigned above.
- Task boundaries: shell/navigation, shared scope and surface hierarchy, record details, and packaged visual verification can each be reviewed independently.
- Review focus: every listed failure condition has an explicit manual verification step in its owning task.
- No backend, telemetry, workflow threshold, evidence eligibility, impact method, or skill discovery changes are included.
