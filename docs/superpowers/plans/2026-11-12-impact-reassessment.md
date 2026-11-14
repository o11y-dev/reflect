# Impact Reassessment Implementation Plan

> Implement inline using test-driven development.

**Goal:** Recover comparisons invalidated by assessment upgrades without replacing frozen members or rewriting history; improve classification and repository evidence inputs.

**Architecture:** A numbered migration explicitly remeasures affected frozen cohorts using current eligibility and metric rules. Ordinary reads continue to invalidate changed evidence. Classifier inputs prefer captured user prompts. Workspace resolution may use file paths only when they identify a real repository.

**Constraints:** Preserve thresholds, unknown outcomes, old measurements and exact cohort membership. Do not infer a repository from the current working directory. No live database edits outside normal migration/refresh.

## Tasks

- [x] Add failing regression tests for reassessment history/membership, current eligibility, prompt-based classification and verified file-path repository recovery.
- [x] Implement explicit reassessment and a one-way migration for version-1 frozen assessment fingerprints; preserve blocked results for missing/ineligible members.
- [x] Prefer bounded captured prompt text in archetype classification and recover repository identity from observed file paths; queue historical classification refresh.
- [x] Update changelog, run focused tests then full lint/test suite, inspect diff and verify on a disposable copy of local data if practical.

## Review focus

Missing snapshot evidence must not trigger reassessment; same-version digest changes remain invalid; missing members cannot be substituted; fallback-boundary confidence stays capped; files outside a repository cannot establish workspace identity.

## Validation

Focused regressions and migration rollback checks pass. A disposable backup of the local database recovered both frozen comparisons and passed SQLite quick_check; reclassification preserved their eligibility. The retry comparison remains unmeasurable where historical inputs are missing. Full suite: 1,973 passed, 22 skipped. Six reassessment safety cases and ruff pass. The source build was subsequently installed locally and the dashboard restarted. Live database migration was still running at the final installation status check; refreshed publication was not yet verified. The migration scan correction passed 1,977 tests with 22 skipped.
