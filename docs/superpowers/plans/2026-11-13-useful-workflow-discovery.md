# Useful Workflow Discovery Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` to implement task-by-task. If the user explicitly chooses delegated execution, use `superpowers:subagent-driven-development`. Steps use checkbox syntax for tracking.

**Goal:** Replace repetitive generic suggestions with a small set of task-specific, source-backed procedures whose benefit is honestly assessed.

**Architecture:** Extend the existing execution-unit → archetype → procedure → candidate → contract → measurement path. Keep discovery, human acceptance, and comparative impact separate. Use the existing ledger and analytical UI; do not build a parallel recommendation engine.

**Tech Stack:** Python, SQLite, existing Pydantic models, vanilla JavaScript, pytest and Node browser-contract tests.

**Spec:** [Useful workflow discovery](../specs/2026-11-13-useful-workflow-discovery.md).

## Global constraints

- Minimum three independent source sessions; existing checked-pattern gate remains three checked sessions and checked ratio at least 0.6.
- Maximum 300 selected executions, 80 signals per execution, ten displayed examples, and five default suggestions.
- Unknown is not failure; missing data is not zero. Activity frequency and passing checks do not establish task success or causal impact.
- Preserve all current uncommitted changes. No commits, package installation, data repair, or workflow application are part of this planning task.
- At implementation time, isolate the approved changes and inspect applicable AGENTS.md. Version durable schema changes with the next available migration; do not assume migration 057 remains free.
- Discovery stays outside interactive requests. No new automatic full-history rebuild or remote data upload.
- Author UI in `src/reflect/frontend/`; rebuild both generated artifacts and keep them byte-identical.

## Review focus

- Missing or mixed task intent must abstain, rather than borrow a neighboring session's intent — Task 2.
- Batch PR/MR reviews and child-agent calls must not combine targets or inflate independence — Tasks 1 and 3.
- Passing checks, clean exits, and agent completion text must not become human acceptance — Tasks 1 and 6.
- Parameterization must preserve meaningful check/revision scope while removing incidental IDs — Task 3.
- Dismissed variants, stale evidence, and installed procedures must retain their lifecycle across refresh — Task 4.

## Task 1: Establish an evaluation baseline

**Files:** create `tests/improvements/fixtures/workflow_discovery_cases.json`, `tests/improvements/test_workflow_discovery_quality.py`, and `scripts/evaluate_workflow_discovery.py`; inspect `tests/improvements/test_procedures.py` and `tests/improvements/test_review_workflows.py`.

**Interfaces:** evaluation cases carry `case_id`, `split`, `task_family`, `session_group`, `expected_target`, `expected_steps`, `expected_outcome`, and `expected_disposition` (`propose` or `abstain`). The evaluator consumes candidate exports and these labels, returning per-case failures and aggregate counts; it does not write the live store.

- [ ] Assemble the 30-case private evaluation set described in the spec; keep full private captures outside tracked fixtures. Record source snapshot date and sampling limits.
- [ ] Produce synthetic/redacted equivalents for regression tests, including duplicate children, mixed targets, no-change reviews, and unsupported success claims.
- [ ] Implement `evaluate_cases(cases: list[dict], candidates: list[dict]) -> dict` in the evaluation script. Report unsupported steps, mixed targets, duplicate suggestions, eligible-case coverage, and abstention reasons separately.
- [ ] Run existing discovery against the frozen cases and save the baseline locally. Do not invent a passing baseline; record current failures.
- [ ] Verify `poetry run pytest tests/improvements/test_workflow_discovery_quality.py -q` checks evaluator correctness, including rejection of a candidate with one unsupported observed step.

**Deliverable:** a reproducible definition of “more useful,” including a held-out split and a human scoring sheet. Review this before changing detector behavior.

## Task 2: Represent task intent with provenance

**Files:** modify `src/reflect/improvements/archetypes.py`, `src/reflect/improvements/models.py`; add `tests/improvements/test_task_intent.py`; extend `tests/test_execution_units.py`.

**Interfaces:** add `TaskIntent` with `family: str | None`, `source_refs: list[EvidenceRef]`, `target_refs: list[str]`, `state: Literal['identified','mixed','unknown']`, and `reason: str`. Add `TaskArchetypeService.intent_for_execution(execution_unit_id: str) -> TaskIntent`. Persist its serialized representation in the existing archetype feature payload; use a numbered migration only if durable structure actually changes.

- [ ] Write tests for explicit review intent, conflicting tasks, absent prompt evidence, child-agent evidence, and quoted examples that are not the user's task.
- [ ] Run `poetry run pytest tests/improvements/test_task_intent.py -q`; confirm the new contract fails before implementation.
- [ ] Extend classification using captured user intent, identified artifacts, and corroborating operations. Preserve existing archetype IDs; do not add a second competing classifier.
- [ ] Keep incident/rollout subtypes in the feature payload initially. Do not infer task identity solely from tool names or the word “error.”
- [ ] Rerun the focused tests and the evaluation baseline. Confirm unknown/mixed cases remain explicit and unrelated sessions cannot supply missing intent.

**Deliverable:** task intent that can explain its evidence and decline to classify.

## Task 3: Extract meaningful procedure steps

**Files:** modify `src/reflect/improvements/procedures.py`, `src/reflect/improvements/workflow_roles.py`, `src/reflect/improvements/review_workflows.py`, `src/reflect/improvements/models.py`; extend `tests/improvements/test_procedures.py` and `tests/improvements/test_review_workflows.py`.

**Interfaces:** add `ProcedureStep` with `action`, `target_parameter`, `evidence_required`, `decision`, `source_kind` (`observed`, `user_instruction`, `proposed`), and `source_refs`. Add `ProcedureDefinition` with `task_intent: TaskIntent`, `applicability`, `inputs`, `steps: list[ProcedureStep]`, `stop_conditions`, and `expected_output`. Store it under candidate `content.procedure_definition`, retaining existing procedure signatures for compatibility with current contracts until any explicit migration replaces them.

- [ ] Write failing cases for same-target metadata/diff inspection, different revision/check scopes, multi-change executions, repeated retries, and unknown outcomes.
- [ ] Extend `ProcedureEvidenceRepository.bundle(...)` to supply the typed procedure definition alongside existing evidence and counterexamples. Reuse its bounded selection and exclusion accounting.
- [ ] Map meaningful actions through the existing workflow evidence layer. Keep provider-specific argument names out of shared ranking and presentation logic.
- [ ] Refactor `RepeatedChangeReviewRule.propose(...)` to consume the shared definition. Every step must retain its provenance; retain unknown head revision/check coverage explicitly.
- [ ] Do not generalize a successful command into “validated rollout” or a file read into “completed review.” Parameterize incidental target IDs without merging tasks or repositories.
- [ ] Run `poetry run pytest tests/improvements/test_procedures.py tests/improvements/test_review_workflows.py tests/test_workflow_contracts.py -q`.

**Deliverable:** one complete change-review procedure with explicit decision points and an expected output; shared structure ready for later task families.

## Task 4: Gate, consolidate, and rank suggestions

**Files:** modify `src/reflect/improvements/rules.py`, `src/reflect/improvements/workflows.py`, `src/reflect/improvements/repository.py`, `src/reflect/improvements/models.py`; extend `tests/improvements/test_service.py` and `tests/test_workflow_suggestion_actions.py`.

**Interfaces:** add `WorkflowService.suggestion_assessment(candidate: WorkflowCandidateRecord) -> dict` returning `eligible: bool`, `reasons: list[str]`, `group_key: str`, and `rank_reasons: list[str]`. `group_key` binds repository, task family, procedure signature, and relevant applicability. Serialize the assessment through the existing candidate adapter; do not introduce a new lifecycle status.

- [ ] Write failing tests for generic failure variants, insufficient independent sessions, two steps without a meaningful task/output, installed equivalents, and dismissed candidates resurfacing after refresh.
- [ ] Gate new proposals on the spec's concrete-procedure requirements. Keep generic failures as Inbox findings unless they support a distinct actionable recovery procedure.
- [ ] Consolidate equivalent suggestions without deleting source evidence. Preserve installed artifacts, dismissal/restore state, versions, and historical measurements.
- [ ] Rank eligible candidates by completeness, specificity, then independent recurrence, with a stable ID tie-breaker. Return explanations rather than a benefit score.
- [ ] Preserve the existing checked-pattern threshold. Repeated procedure drafts with unknown outcomes must use the exact “Observed procedure — benefit unmeasured” label.
- [ ] Run `poetry run pytest tests/improvements/test_service.py tests/test_workflow_suggestion_actions.py -q` and compare evaluation results with Task 1.

**Deliverable:** fewer, distinct, explainable suggestions; valid zero-suggestion results.

## Task 5: Make the workflow decision clear in the UI

**Files:** modify `src/reflect/dashboard_improvements.py`, `src/reflect/frontend/js_impact.js`, `src/reflect/frontend/js_ledger.js`, `src/reflect/frontend/css_improvements.css`; extend `tests/test_dashboard_procedure_rendering.py`, `tests/test_dashboard_impact_rendering.py`, `tests/test_dashboard_stale_modals.py`; regenerate `src/reflect/data/index.html` and `docs/report.html`.

**Interfaces:** consume Task 3's `content.procedure_definition` and Task 4's suggestion assessment. Existing API routes and lifecycle actions remain authoritative.

- [ ] Write rendering tests for the five-suggestion default, “Show all,” empty/insufficient evidence, observed versus proposed steps, unknown outcomes, and keyboard-accessible expansion.
- [ ] Use the existing compact table. Primary columns: task/use case, procedure, why suggested, evidence, and next action. Expand for decisions, source executions, counterexamples, and limitations.
- [ ] Keep Dismiss/Restore obvious; retain History and installed procedures. Put generic findings in Inbox and link them when relevant.
- [ ] Display expected benefit as a hypothesis until measured. Do not blend human acceptance, check completion, and usage counts into one success badge.
- [ ] Run `poetry run python scripts/build_dashboard.py` and the three rendering test files. Confirm generated artifact parity and test filter/navigation persistence.
- [ ] Inspect the change-review flow in the browser: understand the suggestion, inspect evidence, dismiss/restore, and review the installation preview. Do not install a workflow merely to complete UI validation.

**Deliverable:** a user can quickly decide whether a suggestion applies and why to trust it.

## Task 6: Connect outcomes and prove usefulness

**Files:** modify `src/reflect/improvements/contracts.py`, `src/reflect/improvements/archetypes.py`, `src/reflect/improvements/measurement.py` only where necessary; extend `tests/test_workflow_contracts.py`, `tests/test_measurements.py`, and `tests/improvements/test_workflow_discovery_quality.py`; update `CHANGELOG.md`.

**Interfaces:** reuse `WorkflowContract`, existing exposure/adherence records, execution assessments, and human outcome records. Proposal → preview → apply → exposure → measurement remains the existing lifecycle. No new generic success metric.

- [ ] Add end-to-end tests for observed reads with unknown task outcome, passing checks without acceptance, accepted no-change work, later corrections, missing contracts, and incomparable before/after tasks.
- [ ] Define the review contract only for milestones the evidence model can evaluate. Unsupported safeguards remain unmeasured; failure counts must never substitute for adherence.
- [ ] Run the held-out evaluation. Require zero fabricated observed steps, contaminated targets, and false success claims. Record human scores for specificity, actionability, fidelity, and distinctness; require at least 6/8 and fidelity exactly 2 for promotion.
- [ ] Demonstrate one valid review suggestion when eligible evidence exists, plus correct abstention on negative cases. If useful evidence is insufficient, ship honest abstention and an explicit capture gap rather than manufacture candidates.
- [ ] Benchmark bounded discovery and warm filtered API reads on the same frozen store; record cold times separately. Confirm no added full-history ingestion or analysis from API requests.
- [ ] Run `poetry run ruff check .`, `poetry run pytest -q --no-cov`, and artifact parity checks. Update the current versioned unreleased changelog.
- [ ] After approval to roll out, install the source build locally, refresh only the required evidence scope, and read back actual candidate/evidence/measurement APIs. Measure actual behavior before expanding to incident and rollout procedure families.

**Deliverable:** an evaluated first task family with honest benefit limits and a working measurement path.

## Delivery order and stopping points

Tasks 1–3 establish evidence and one useful procedure. Tasks 4–5 make it usable. Task 6 is the promotion gate. Do not broaden task-family coverage until the first family passes held-out evaluation and a human usefulness review.

This plan does not claim productivity improvement from the evaluation score. Actual benefit requires adoption and later comparable outcome evidence. Historical batching and detailed refresh progress remain a separate implementation effort.

## Execution handoff

Recommended approach: native sequential implementation, because the tasks share one canonical evidence contract and the first evaluation may change later ranking decisions. Review this plan before implementation; delegated execution is an alternative if explicitly selected. No application code is changed by creating this plan.
