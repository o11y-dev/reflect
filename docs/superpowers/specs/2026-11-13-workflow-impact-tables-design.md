# Workflows and Impact: two linked analytical tables

## Intent

Help users scan current workflow opportunities and measured outcomes quickly, then expand only the evidence needed for a decision. Reduce card overload without hiding uncertainty or presenting missing telemetry as a measured result.

## Approaches and decision

- **Two linked tables (selected by the user):** Workflows supports review and management; Impact supports outcome assessment. Shared workflow identity and links keep navigation continuous.
- **One combined table:** fewer surfaces, but mixes suggestions with installed workflows and requires too many columns. Not selected.

## Workflows

One compact row per workflow: name, observed problem, supporting task count and last seen date when available, lifecycle status, and next action. Use plain-language names rather than raw tool identifiers as the primary label.

Default to current suggestions and active workflows, with clear filters for each. Retired suggestions belong in History with their retirement reason. Distinguish an obsolete suggestion from a changed installation and from an outdated analysis snapshot.

Expand a row inline to inspect proposed steps, supporting task links, scope, installation/version details, and history. Keep long descriptions and CLI commands inside the expansion. Do not collapse distinct workflow contracts merely because their titles resemble each other.

## Impact

One compact row per measured workflow/version: workflow, outcome, before/after metric with units, baseline/post task counts, cohort dates, and evidence limitations. Keep missing-data and attribution limitations visible in the collapsed row.

Expand for the comparison chart, exact task cohorts, exclusions, observed workflow adherence, and measurement history. Show historical cohort dates explicitly; the global activity date range must not imply that it bounds a frozen comparison.

Missing token telemetry renders as **Not measurable**, never zero usage or no change. An observed metric decrease is an **Observed decrease**, not proof that the workflow caused it. Remove sample-size-derived confidence percentages from the decision summary. Preserve real measured zeroes. If coverage cannot be established, withhold the affected claim and state why.

## Navigation and freshness

Link Workflows to its matching Impact row and Impact back to its workflow. Preserve filters and selected tab in navigation, and expand/focus the destination row. If no comparison exists, explain the missing prerequisite rather than opening an unrelated result.

Use a consistent evidence generation or explicitly disclose differing freshness; do not silently present contradictory current measurement statuses across the two views. Retain pending-refresh indicators without duplicating banners on every row.

## Discovery scope

Present supported workflow types beyond tool failures, including exploration, verification, context management, and repeated procedures when backed by existing evidence. This change does not invent additional detectors, supporting evidence, or measured benefits. Improving discovery algorithms is separate work; rendering should not imply every suggestion is a tool-retry recipe.

## Implementation constraints

Reuse canonical workflow and measurement read models. Modify authored frontend files and regenerate both packaged dashboard artifacts. Preserve the current visual language. Use accessible table headers and keyboard-operable expansion buttons with expanded state; on narrow screens retain essential outcome and limitation information with horizontal scrolling where necessary.

Existing unrelated working-tree changes must be preserved. Keep the changelog current. Validate the installed local build after source tests and artifact generation, because source-only changes do not update the running dashboard.

## Acceptance

- Users can identify workflow status, outcome, dates, and evidence gaps without expanding a row.
- Details expand inline, support keyboard use, and retain state across ordinary refreshes where the record still exists.
- Cross-view links select the correct workflow/version and preserve navigation context.
- Historical suggestions are separated from current decisions with explicit reasons.
- Missing usage cannot produce a zero-to-zero result; observed changes do not claim causal impact.
- Focused backend/frontend regression checks pass; generated dashboard files are byte-identical; the locally installed UI is visually checked with actual records.

The builder may choose exact column widths, responsive breakpoints, and small implementation helpers consistent with existing conventions.
