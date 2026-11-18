# Useful workflow discovery

## Product goal

Help a developer recognize a recurring task and adopt a concrete, evidence-backed way to perform it. The default Workflows view should contain a few useful procedures, with a clear explanation of what is observed, what is proposed, and what remains unmeasured.

## Current baseline

The repository already has execution units, task archetypes, bounded procedure extraction, chronological check assessment, explicit task outcomes, workflow contracts, dismissal, and impact comparisons. This is an extension of that system.

The current generic procedure rule requires three independent sessions and a 0.6 checked ratio. It can correctly produce zero candidates. The separate change-review rule identifies metadata/diff reads for the same change within an execution, but its added safeguards are proposals and its benefit is unmeasured. Neither repeated activity nor passing checks proves usefulness.

## Product decisions

1. Keep generic failure observations in Inbox. Do not automatically turn every failure variation into a workflow. Existing installed workflows and their evidence remain accessible.
2. Start with change review as the first complete task-specific procedure. Incident investigation and rollout validation follow only after evaluation of the first task family.
3. Model a procedure as intent, applicability, ordered actions, decision points, evidence requirements, stop conditions, and expected output. Treat paths, issue IDs, and change IDs as parameters without merging different repositories or targets within an execution.
4. Distinguish discovery evidence from benefit evidence. A draft may be supported by repeated, identified procedures while its outcome remains unknown. Label it “Observed procedure — benefit unmeasured.” Passing checks and human acceptance remain separate facts.
5. Use three independent source sessions for automatically proposed repeated procedures. Preserve the existing checked-pattern gate: three independent checked sessions and checked ratio at least 0.6. Do not lower either gate merely to obtain suggestions.
6. Each suggested step has a source: captured action, user-authored instruction, or proposed safeguard. Proposed safeguards are never reported as historical behavior.
7. Require a concrete task, at least two ordered meaningful actions, bounded applicability, traceable source executions, and an explicit expected output. Otherwise abstain with a reason. Do not promise savings from activity counts.
8. Show at most five ranked suggestions by default, grouped by task and procedure, with access to the rest and History. Preserve Dismiss/Restore, source links, and approval before installation.
9. Rank by evidence completeness and task specificity before recurrence. Show the reason for ranking, not a fabricated benefit percentage. Keep counterexamples visible.
10. Measure adoption, adherence, observed outcomes, and comparative impact separately. Unknown is not failure, and missing data is not zero. A comparison remains unavailable without a valid contract and comparable cohorts.

## First complete example

**Review a change with head-specific evidence**

- Trigger: a user requests review of an identified PR/MR.
- Inputs: repository, change identity, current revision, and requested review scope.
- Procedure: resolve the target; inspect metadata and diff; inspect checks for the reviewed revision; trace material dependencies; separate readiness from runtime evidence; write findings and remaining gates.
- Decision: missing current-head checks means report the missing evidence, not approve the change.
- Output: findings tied to the reviewed revision, supporting evidence, and unresolved gates.
- Publication: approval and read-back are proposed safeguards unless recorded explicitly. No automatic external posting.

## Evidence and evaluation

Use a private, frozen evaluation set of 30 execution units: ten change reviews, ten other task families, and ten negative/ambiguous examples. Label task, target, meaningful action sequence, check evidence, acceptance, and whether a useful procedure can be extracted. Include successful no-change reviews, batch reviews, missing prompts, failed reads, child agents, repeated retries, and corrected outcomes. Allocate 20 examples for development and ten for held-out evaluation; keep sessions and task chains in one split. Checked-in fixtures must be synthetic/redacted.

A human evaluates specificity, actionability, source fidelity, and distinctness on a 0–2 scale. Suggestions must score at least 6/8, with source fidelity exactly 2, before default promotion. Scores are evaluation labels, not customer impact metrics. Require zero invented observed steps, target contamination, or false success claims. If the held-out set contains too little eligible evidence, report that limit rather than relax the gate.

## Boundaries and acceptance

- Extend canonical normalization and public SQL read models; never derive aggregates from dashboard cards or native conversation display rows.
- Reuse the existing limit of 300 executions, 80 signals per execution, and ten displayed examples; expose sampling and exclusion counts.
- No automatic raw-history replay, installation, remote publication, or external LLM upload.
- Preserve unknown/mixed tasks and unknown outcomes. Provider-specific evidence mapping stays behind narrow adapters.
- Discovery runs during preparation, not in interactive API requests. Target less than two seconds for warm filtered reads on the local reference store; measure cold reads separately.
- Release requires a useful first task family, honest abstention for weak evidence, correct dismissal, and a valid impact path that can remain unmeasured.
- Refresh batching/progress is a separate follow-up; this work must not worsen refresh scope or add another full-history pass.
