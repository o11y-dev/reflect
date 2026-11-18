# Reflect dashboard: Analyst desk redesign

## Intent

Reorganize the whole Reflect dashboard into a quieter, table-led analyst workspace. The user chose the “Analyst desk” mockup, inspired by the compact navigation and focused canvas in the supplied Codex screenshot. The dashboard should make evidence easy to scan, keep its current product capabilities, and let people inspect details without losing their place.

## Why now

The current dashboard exposes six product surfaces as horizontal tabs, while Sessions and Explore add their own rails and filters. That creates multiple navigation patterns and makes it difficult to understand where findings, workflows, skills, impact, and telemetry fit together. The user also reports that the current UI feels overloaded and that skills are hard to find.

## Goals

- Establish one persistent navigation model for Sessions, Inbox, Workflows, Skills, Impact, and Explore.
- Make tables and compact analytic summaries the default for evidence-heavy views.
- Keep scope visible: date range, agent, project, and relevant selection state should be obvious and URL-backed.
- Open source evidence in a contextual inspector or detail panel without replacing the table or losing filters.
- Make the lifecycle legible: Inbox findings can lead to a reviewed workflow; Skills shows registered or generated skills; Impact reports only supported comparisons.
- Use consistent loading, empty, stale, unavailable, and error states that explain what is happening and provide a useful next action.
- Preserve the Reflect visual identity: near-black surfaces, warm off-white text, restrained graphite borders, and orange for active navigation and meaningful signals.
- Keep the interface responsive and keyboard accessible.

## Non-goals

- Changing telemetry, evidence eligibility, workflow generation thresholds, impact methodology, or skill discovery logic.
- Adding auto-installation, autonomous workflow activation, or new write actions.
- Inventing cost, impact, or skill evidence where the store has none.
- Copying chat-specific UI such as a prompt composer, conversation history, or chat controls.
- Replacing the CLI or MCP experience.

## Anti-goals

- Do not put every widget on one scrolling page.
- Do not hide important section names behind icon-only navigation on desktop.
- Do not make the evidence inspector permanently consume the main content width when nothing is selected.
- Do not silently reuse stale data as current evidence.
- Do not imply a skill was followed or effective because it was installed or referenced.

## Approaches considered

1. **Reflect sidebar** — compact navigation rail plus labeled section navigation and a roomy content canvas. Familiar and light, but less suitable for sustained cross-table analysis.
2. **Evidence workbench** — keep Inbox findings and filters beside their evidence. Strong for investigation and workflow creation, but makes the Inbox the organizing model for the rest of the product.
3. **Analyst desk (selected)** — compact global rail, a contextual navigation/scope pane, a central table-led workspace, and an on-demand evidence inspector. Best matches the user's analytic goal and keeps source evidence close to summaries. It needs responsive collapse rules to preserve table width.

## Design direction

### Shared application frame

- Replace horizontal top-level tabs with a persistent left navigation rail and a labeled secondary navigation pane.
- Group the six surfaces in product language, not implementation terms. The chosen names remain Sessions, Inbox, Workflows, Skills, Impact, and Explore.
- Keep global status and data freshness in a restrained top bar. Keep brand and a direct route to the public Reflect site visible without dominating the workspace.
- Use one shared scope control area. Show only filters that apply to the current surface, and label when a date or agent filter does not apply.
- Preserve deep links and browser back/forward behavior for active surface, Explore subview, filters, selected session, finding, workflow, skill, and evidence row.

### Workspace layout

- Default to a central analytic workspace with a clear title, short explanation, scope summary, and one primary data table or focused summary.
- Use compact rows with stable columns and meaningful status labels. Avoid repeated explanatory cards where a table row can carry the same information.
- Selecting a row opens a right-side inspector on wide screens. The inspector shows evidence provenance, freshness, limitations, and links to source sessions. On narrow screens it becomes an accessible drawer.
- On Sessions, the main table is the session list; selecting a session opens a full-width conversation/evidence detail state with a clear back path to the same table selection and scroll position.
- On Inbox, a finding's evidence stays inspectable and the “Create workflow” action remains explicit and directly connected to the selected finding.
- On Workflows, distinguish proposed, reviewed, installed, and monitored states and make each next action clear.
- On Skills, show the registry and make installed, generated, referenced, and measured states distinct. Empty discovery results must not hide registered skills.
- On Impact, keep comparisons tied to eligible execution units and make insufficient evidence visibly different from no effect.
- On Explore, use one secondary list for Usage, Tools & MCP, Graph, and Context. Keep Git & delivery as a clearly surfaced section within Usage, where the current dashboard data already lives. Preserve per-view filter and scope explanations.

### Interaction and states

- Keep query parameters as the source of navigable view state. Filters that do not apply to the current surface remain saved but inactive, and restore when the user returns to a compatible surface. Make the active scope clear so an inactive filter is never mistaken for an applied one.
- Show loading as an in-place skeleton or progress state, not as a retry prompt. Retry appears only after a request actually fails.
- Explain stale and partial data with its last completed time and what remains pending.
- Empty states say whether the dataset has no records, the filter returned none, or the registry/discovery has no entries; include the next useful action when one exists.
- All table rows, navigation items, inspector controls, and drawers support keyboard use, visible focus, and appropriate screen-reader labels.

## Constraints

- Authored frontend files live in `src/reflect/frontend/`. Generated `src/reflect/data/index.html` and `docs/report.html` must be rebuilt and remain byte-identical.
- Preserve public JSON/API contracts and current data semantics. Frontend-only composition changes should not fork or reaggregate backend read models.
- Preserve the existing local-first behavior and URL filters.
- Keep the user’s pre-existing working-tree changes intact; the current checkout contains unrelated modifications in CLI, backend, tests, README, changelog, and packaged skill files.
- Follow repository changelog and dashboard build rules for user-visible implementation changes.

## Acceptance signals

- The six product surfaces and all current Explore subviews remain reachable in one consistent navigation model.
- A user can move from an Inbox finding to its evidence and create a workflow without losing the finding context.
- A user can browse registered skills independently of whether the latest discovery run produced new candidates.
- Tables remain readable at desktop and mobile widths; selected-row detail does not create horizontal page overflow.
- URL state survives surface navigation and reload for supported filters and selections.
- Loading, stale, partial, empty, and failed requests have distinct and accurate presentation.
- No UI copy suggests evidence, cost, or impact that the data does not support.
- The local and packaged dashboard render the same authored design after the build step.

## Implementation remains open until this brief is approved

The builder can choose exact CSS grid values, typography scale, component boundaries, icons, animation timing, and collapse breakpoints as long as the selected Analyst desk direction and acceptance signals remain intact. Any change to evidence meaning, lifecycle actions, or product scope requires a new decision.
