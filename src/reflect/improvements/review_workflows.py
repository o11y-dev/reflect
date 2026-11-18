"""Review procedure suggestions grounded in repeated, identified change reviews."""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from reflect.execution_assessment import attrs_dict
from reflect.improvements.analysis_scope import AnalysisScope
from reflect.improvements.archetypes import TaskArchetypeService
from reflect.improvements.base import BaseImprovementRule, scope_for_repository, stable_fingerprint
from reflect.improvements.models import (
    EvidenceRef,
    ObservationDraft,
    ObservationSessionRef,
    RuleDefinition,
    WorkflowBehaviorType,
    WorkflowDefinition,
)
from reflect.improvements.workflow_roles import review_operation


class RepeatedChangeReviewRule(BaseImprovementRule):
    definition = RuleDefinition(
        id='repeated_change_review', version=2, category='workflow',
        title='Repeatable change-review procedure',
        description='Preserve repeated metadata and diff inspection for the same identified change within each execution.',
        detector_config={'minimum_source_sessions':3, 'maximum_execution_units':300},
        required_signals=['execution_unit_steps.step_id','tool_calls.input_preview_redacted'],
    )
    workflow = WorkflowDefinition(
        slug='review-change-evidence', behavior_type=WorkflowBehaviorType.VERIFICATION,
        description='Use when reviewing a pull or merge request in the linked repository.',
        steps=[
            'Resolve the requested PR/MR URL, repository, target branch, and current head revision.',
            'Read the change metadata and diff for that exact PR/MR; list the affected behavior and scope.',
            'Inspect relevant pipeline jobs and test results for that head revision. State which checks did not run or have no evidence.',
            'Trace material changes to their callers, configuration, and rollout dependencies; distinguish new defects from inherited limitations.',
            'Write findings with file or evidence references, severity, and a concrete next action. Keep passing CI separate from deployed or runtime proof.',
            'Before posting, obtain approval for the exact review text; read back the posted review if publication is requested.',
        ],
        abstain_when=['The target change or head revision cannot be identified.',
                      'Required diff or check evidence is inaccessible; report the missing evidence instead of approving the change.'],
        verification=['Confirm metadata and diff refer to the same requested change.',
                      'Confirm checks correspond to the reviewed head and label unverified rollout conditions.',
                      'Do not infer review quality or measured savings from the frequency of these operations.'],
    )

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        scope = scope or AnalysisScope()
        # Select review executions before joining their calls; unrelated recent
        # coding turns must not crowd review evidence out of the bounded sample.
        units = conn.execute(f"""WITH review_steps AS MATERIALIZED (
              SELECT DISTINCT step_id FROM tool_calls
              WHERE tool_name LIKE '%merge_request%' OR tool_name LIKE '%pull_request%'
            ), review_units AS MATERIALIZED (
              SELECT DISTINCT us.execution_unit_id FROM review_steps rs
              JOIN execution_unit_steps us ON us.step_id=rs.step_id
            )
            SELECT eu.id,eu.session_id,eu.repo_id FROM review_units ru
            JOIN {scope.execution_units} eu ON eu.id=ru.execution_unit_id
            WHERE eu.eligible=1 AND eu.repo_id IS NOT NULL
            ORDER BY eu.started_at DESC,eu.id DESC LIMIT 300""").fetchall()
        groups = defaultdict(list)
        intents = {}
        roots = {}
        archetypes = TaskArchetypeService(conn)
        for unit_id, session_id, repo_id in units:
            intent = archetypes.intent_for_execution(unit_id)
            if intent.state != 'identified' or intent.family != 'review':
                continue
            from reflect.improvements.procedures import independent_session
            root = independent_session(conn, session_id)
            if root is None:
                continue
            roots[session_id] = root
            intents[unit_id] = intent
            rows = conn.execute("""SELECT tc.id,tc.step_id,tc.tool_name,tc.status,
                COALESCE(json_extract(tc.raw_attrs_json,'$."gen_ai.client.tool.input"'),tc.input_preview_redacted)
                FROM execution_unit_steps us JOIN tool_calls tc ON tc.step_id=us.step_id
                JOIN steps st ON st.id=tc.step_id
                WHERE us.execution_unit_id=? AND
                  (tc.tool_name LIKE '%merge_request%' OR tc.tool_name LIKE '%pull_request%')
                ORDER BY st.started_at,st.seq,tc.id LIMIT 81""", (unit_id,)).fetchall()
            if len(rows) > 80:
                continue
            targets = defaultdict(dict)
            for tool_id, step_id, name, status, raw in rows:
                match = review_operation(name, attrs_dict(raw))
                if match is None:
                    continue
                target, role = match
                role = role.removeprefix('review_')
                # Failed calls still establish that multiple targets were mixed.
                targets[target]
                if status == 'ok':
                    targets[target].setdefault(role, {'tool_call_id':tool_id,'step_id':step_id})
            if len(targets) != 1:
                continue
            target, roles = next(iter(targets.items()))
            if list(roles) != ['metadata','diff']:
                continue
            groups[(repo_id,target.split(':',1)[0])].append((unit_id,session_id,roles))
            intents[unit_id] = intent.model_copy(update={'target_refs':[target]})
        findings = []
        for (repo_id,provider), examples in groups.items():
            sessions = sorted({item[1] for item in examples})
            independent_sessions = sorted({roots[sid] for sid in sessions})
            if len(independent_sessions) < 3:
                continue
            scope_type, scope_id = scope_for_repository(repo_id)
            # Include independent sessions before repeated executions in examples.
            selected, seen = [], set()
            for example in examples:
                if example[1] not in seen:
                    selected.append(example)
                    seen.add(example[1])
                if len(selected) == 10:
                    break
            findings.append(ObservationDraft(
                rule_id=self.definition.id,rule_version=2,scope_type=scope_type,scope_id=scope_id,repo_id=repo_id,
                fingerprint=stable_fingerprint(repo_id,provider,'identified-change-review'),category='workflow',
                title='Review change scope, diff, and validation evidence',
                summary=f'Metadata and diff were inspected for the same identified change in {len(examples)} executions across {len(sessions)} sessions. The proposed check and publication steps are safeguards; review quality and impact are not measured.',
                metric_name='review_execution_count',metric_value=len(examples),metric_unit='executions',
                metric_direction='higher_is_better',baseline_value=None,
                baseline_query={'repo_id':repo_id,'provider':provider,'execution_limit':300,
                                'independent_sessions':independent_sessions,
                                'task_intent':intents[examples[0][0]].model_dump(mode='json'),
                                'observed_steps':['inspect identified change metadata','inspect the same change diff'],
                                'proposed_steps':['verify current-head checks','separate deployment evidence','approve and read back publication']},
                impact_score=65,severity='medium',confidence=.8,occurrence_count=len(examples),affected_session_count=len(sessions),
                evidence=[EvidenceRef(entity_type='execution_unit',entity_id=unit,session_id=session,
                    tool_call_id=roles['diff']['tool_call_id'],step_id=roles['diff']['step_id'],
                    summary_redacted='Captured metadata and diff reads for one identified change; review outcome unknown.',attrs={'observed_calls':roles})
                    for unit,session,roles in selected],
                source_sessions=[ObservationSessionRef(session_id=sid,summary_redacted='Contains an identified change review.') for sid in sessions],
            ))
        return findings

    def propose(self, finding: ObservationDraft):
        proposal = super().propose(finding)
        if proposal is None:
            return None
        from reflect.improvements.contracts import (
            WorkflowContract,
            WorkflowContractApplicability,
            WorkflowMilestoneContract,
        )
        from reflect.improvements.procedures import review_procedure_definition
        definition = review_procedure_definition(finding)
        contract = WorkflowContract(signature_hash=finding.fingerprint,
            applicability=WorkflowContractApplicability(repo_id=finding.repo_id,task_archetype_id='review'),
            milestones=[WorkflowMilestoneContract(id=role,role=role) for role in ('review_metadata','review_diff')])
        return proposal.model_copy(update={
            'title':'Review a change with head-specific evidence',
            'hypothesis':'Preserve the observed metadata and diff inspection, then make missing checks and publication approval explicit. Benefits remain unmeasured.',
            'target_metric':'workflow_adherence', 'target_value':None,
            'content':{**proposal.content,'procedure_definition':definition.model_dump(mode='json'),
                       'workflow_contract':contract.model_dump(mode='json'),
                       'independent_session_count':len(finding.baseline_query['independent_sessions']),
                       'observed_steps':finding.baseline_query['observed_steps'],
                       'proposed_steps':finding.baseline_query['proposed_steps']},
        })
