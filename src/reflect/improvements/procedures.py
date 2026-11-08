"""Bounded procedure evidence shared by workflow discovery and skill drafting.

Signatures describe operations and artifact scope, not success or causality.
Check results are read from canonical chronological assessments, never inferred
from role names, graph edges, or an agent's completion report.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import sqlite3
from collections import Counter
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from reflect.execution_assessment import attrs_dict, check_command, command_from_tool
from reflect.improvements.analysis_scope import AnalysisScope
from reflect.improvements.measurement_validity import (
    EXECUTION_EVIDENCE_CURRENT_SQL,
    EXECUTION_INPUTS_CURRENT_SQL,
)
from reflect.improvements.workflow_roles import (
    WorkflowEvidenceRepository,
    WorkflowSignal,
    workflow_roles,
)

SIGNAL_LIMIT = 80
EXAMPLE_LIMIT = 10

if TYPE_CHECKING:
    from reflect.improvements.contracts import WorkflowContract


def assessment_fingerprint(assessment: dict) -> str:
    """Bind an extraction example to the canonical assessment it described."""
    return hashlib.sha256(json.dumps(assessment, sort_keys=True).encode()).hexdigest()


def procedure_contract(procedure: dict) -> WorkflowContract:
    """The same scoped contract for a discovered workflow and its skill draft."""
    from reflect.improvements.base import stable_fingerprint
    from reflect.improvements.contracts import (
        WorkflowContract,
        WorkflowContractApplicability,
        WorkflowContractValidation,
        WorkflowMilestoneContract,
    )

    return WorkflowContract(
        signature_hash=stable_fingerprint(procedure['repo_id'], procedure['workspace_id'],
            procedure['task_archetype_id'], procedure['signature']),
        procedure_signature=procedure['signature'],
        applicability=WorkflowContractApplicability(repo_id=procedure['repo_id'],
            workspace_id=procedure['workspace_id'], task_archetype_id=procedure['task_archetype_id']),
        milestones=[WorkflowMilestoneContract(id=f'{role}-{index + 1}', role=role)
                    for index, role in enumerate(procedure['milestone_roles'])],
        validation=WorkflowContractValidation(baseline_minimum=3, baseline_maximum=20,
                                               minimum_evidence_coverage=.8),
    )


def _relative_path(path: str, root: str) -> str:
    if not path:
        return "unknown"
    value = str(PurePosixPath(path))
    if root and (value == root.rstrip('/') or value.startswith(root.rstrip('/') + '/')):
        value = value[len(root.rstrip('/')):].lstrip('/') or '.'
    if value.startswith('/'):
        return "external:" + value
    parts = PurePosixPath(value).parts
    if '..' in parts:
        return "unknown"
    if not parts:
        return "."
    return value


def _artifact(path: str, root: str) -> str:
    value = _relative_path(path, root)
    if value in {'unknown', '.'} or value.startswith('external:'):
        return value
    # Files being edited are parameters. Check selectors and dependency scopes
    # use _relative_path directly: their exact filenames affect verification.
    path_obj = PurePosixPath(value)
    if path_obj.suffix:
        return str(path_obj.parent / ('*' + path_obj.suffix))
    return value


def procedure_operations(signals: list[WorkflowSignal], *, workspace_root: str = "") -> list[dict[str, str]]:
    operations: list[dict[str, str]] = []
    for signal in signals:
        if signal.kind != "tool":
            continue
        name = signal.name.lower().rsplit('__', 1)[-1]
        payload = attrs_dict(signal.preview)
        path = signal.path or str(payload.get('file_path') or payload.get('path') or '')
        command = signal.command or command_from_tool({'tool_name': name, 'input_preview_redacted': signal.preview})
        target = _artifact(path, workspace_root)
        operation = ""
        if check_command(command):
            tokens = shlex.split(command)
            if tokens[:2] in (['poetry', 'run'], ['uv', 'run']):
                tokens = tokens[2:]
            if tokens and PurePosixPath(tokens[0]).name.startswith('python') and tokens[1:2] == ['-m']:
                tokens = tokens[2:]
            # Flags/values remain in the signature: differing test selections
            # must not silently become one procedure.
            if tokens:
                tokens[0] = PurePosixPath(tokens[0]).name
            target = shlex.join(_relative_path(t, workspace_root) if '/' in t else t for t in tokens)
            operation = 'check'
        elif any(part in name for part in ('apply_patch', 'edit', 'write_file', 'writefile')) or name == 'write':
            operation = 'modify'
            if target == 'unknown':
                patch = str(payload.get('patch') or payload.get('input') or signal.preview)
                paths = re.findall(r'^\*\*\* (?:Update|Add|Delete) File: (.+)$', patch, re.MULTILINE)
                target = ', '.join(sorted({_artifact(p, workspace_root) for p in paths})) or 'unknown'
        elif any(part in name for part in ('read', 'view', 'open')):
            operation = 'inspect'
        elif any(part in name for part in ('search', 'grep', 'find', 'list')):
            operation = 'search'
        elif command and len(command) <= 32768 and not any(c in command for c in ';&|<>\n`') and '$(' not in command:
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = []
            if tokens[:1] == ['git'] and len(tokens) > 1:
                verb = tokens[1]
                operation = 'inspect' if verb in {'diff', 'show', 'status', 'log'} else 'git:' + verb
                target = 'git:' + verb
            elif tokens[:2] in (['gh', 'pr'], ['glab', 'mr']) and len(tokens) > 2:
                verb = tokens[2]
                operation = 'inspect' if verb in {'view', 'diff', 'list', 'checks'} else 'change_request:' + verb
                target = 'change_request:' + verb
        if operation:
            item = {'operation': operation, 'target': target}
            if not operations or operations[-1] != item:
                operations.append(item)
    return operations


def procedure_signature(signals: list[WorkflowSignal], assessment: dict, *, workspace_root: str = "") -> str:
    operations = procedure_operations(signals, workspace_root=workspace_root)
    scopes = sorted({str(scope) for check in assessment.get('checks', []) if isinstance(check, dict)
                     for scope in check.get('scope', [])})
    payload = {'version': 1, 'operations': operations,
               'milestone_roles': workflow_roles([signal for signal in signals if signal.kind == 'tool']),
               'check_scopes': [_relative_path(scope, workspace_root) for scope in scopes]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ProcedureEvidenceRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def signatures(self, execution_ids: list[str], signals: dict[str, list[WorkflowSignal]]) -> dict[str, str]:
        """Match contracts from the same canonical scope as discovery."""
        result = {}
        for offset in range(0, len(execution_ids), 400):
            batch = execution_ids[offset:offset + 400]
            rows = self.conn.execute(f"""SELECT eu.id,eu.boundary_json,COALESCE(w.root_path,'')
                FROM execution_units eu LEFT JOIN workspaces w ON w.id=eu.workspace_id
                WHERE eu.id IN ({','.join('?' for _ in batch)})""", batch)
            for unit_id, raw, root in rows:
                unit_signals = signals.get(str(unit_id), [])
                if not unit_signals or len(unit_signals) > SIGNAL_LIMIT:
                    continue
                assessment = attrs_dict(attrs_dict(raw).get('assessment'))
                result[str(unit_id)] = procedure_signature(unit_signals, assessment, workspace_root=root)
        return result

    def bundle(self, *, session_ids: set[str] | None = None,
               execution_table: str = 'execution_units', limit: int = 300,
               current_execution_ids: set[str] | frozenset[str] = frozenset()) -> dict:
        allowed = {'execution_units', AnalysisScope(session_ids=set()).execution_units}
        if execution_table not in allowed:
            raise ValueError('Unsupported procedure execution scope')
        limit = min(max(int(limit), 1), 300)
        selection = {'execution_limit': limit, 'signal_limit_per_execution': SIGNAL_LIMIT,
                     'selected_execution_count': 0, 'excluded_execution_count': 0,
                     'truncated': False, 'denominator': 'selected comparable executions'}
        result = {'schema_version': 1, 'selection': selection, 'procedures': []}
        if session_ids == set():
            return result
        params: list = []
        where = ''
        if session_ids is not None:
            where = f"WHERE eu.session_id IN ({','.join('?' for _ in session_ids)})"
            params.extend(sorted(session_ids))
        rows = self.conn.execute(f"""
            SELECT eu.id,eu.session_id,eu.repo_id,eu.workspace_id,eu.eligible,eu.status,
                   eu.boundary_json,eua.task_archetype_id,eua.mixed,eua.confidence,
                   COALESCE(w.root_path,''), ({EXECUTION_EVIDENCE_CURRENT_SQL}) AS evidence_current,
                   ({EXECUTION_INPUTS_CURRENT_SQL}) AS inputs_current
            FROM {execution_table} eu
            LEFT JOIN execution_unit_archetypes eua ON eua.execution_unit_id=eu.id
            LEFT JOIN workspaces w ON w.id=eu.workspace_id
            {where} ORDER BY eu.started_at DESC,eu.id DESC LIMIT ?
        """, [*params, limit + 1]).fetchall()
        selection['truncated'] = len(rows) > limit
        rows = rows[:limit]
        selection['selected_execution_count'] = len(rows)
        eligible = [row for row in rows if row[4] and not row[8] and (row[9] or 0) >= .65
                    and (row[2] or row[3])]
        selection['excluded_execution_count'] = len(rows) - len(eligible)
        signals = WorkflowEvidenceRepository(self.conn).signals_for_execution_units(
            [str(row[0]) for row in eligible], limit_per_execution=SIGNAL_LIMIT + 1)
        groups: dict[tuple, dict] = {}
        for row in eligible:
            unit_id, session_id, repo_id, workspace_id = row[:4]
            unit_signals = signals.get(str(unit_id), [])
            truncated = len(unit_signals) > SIGNAL_LIMIT
            unit_signals = unit_signals[:SIGNAL_LIMIT]
            assessment = attrs_dict(attrs_dict(row[6]).get('assessment'))
            operations = procedure_operations(unit_signals, workspace_root=row[10])
            if not operations:
                continue
            signature = procedure_signature(unit_signals, assessment, workspace_root=row[10])
            key = (repo_id, workspace_id, row[7], signature)
            group = groups.setdefault(key, {
                'signature': signature, 'repo_id': repo_id, 'workspace_id': workspace_id,
                'task_archetype_id': row[7], 'operations': operations, 'execution_ids': [],
                'milestone_roles': workflow_roles([signal for signal in unit_signals if signal.kind == 'tool']),
                'session_ids': [], 'verification_counts': Counter(), 'outcome_counts': Counter(),
                'supporting_examples': [], 'counterexamples': [], 'checked_session_ids': [],
                'signals_truncated': False,
                'match_explanation': 'Same task scope, ordered operations, artifact areas, and recorded check scope.',
            })
            verification = str(assessment.get('verification') or 'unknown')
            # Internal discovery can use units rebuilt in this refresh before
            # its improvements revision is acknowledged. Captured inputs must
            # already be current; external discovery has no such override.
            current = row[11] or (unit_id in current_execution_ids and row[12])
            if not current or assessment.get('boundary') != 'closed' or assessment.get('pending_check_step_ids'):
                verification = 'unknown'
            checks = [check for check in assessment.get('checks', []) if isinstance(check, dict)]
            # Malformed or incomplete assessments cannot create positive support.
            if verification == 'passed' and (not checks or any(
                check.get('exit_code') != 0 or check.get('invalidated') for check in checks
            )):
                verification = 'unknown'
            outcome = str(assessment.get('outcome') or 'unknown')
            example = {'execution_unit_id': unit_id, 'session_id': session_id,
                       'verification': verification, 'outcome': outcome, 'checks': checks[:10],
                       'assessment_fingerprint': assessment_fingerprint(assessment),
                       'signals_truncated': truncated}
            group['execution_ids'].append(unit_id)
            group['session_ids'].append(session_id)
            group['verification_counts'][verification] += 1
            group['outcome_counts'][outcome] += 1
            group['signals_truncated'] |= truncated
            if verification == 'passed' and not truncated:
                group['checked_session_ids'].append(session_id)
                bucket = 'supporting_examples'
            else:
                bucket = 'counterexamples'
            group[bucket].append(example)
        for group in groups.values():
            for bucket in ('supporting_examples', 'counterexamples'):
                # Preserve independent source sessions in the bounded examples.
                first, repeated, seen_sessions = [], [], set()
                for example in group[bucket]:
                    (repeated if example['session_id'] in seen_sessions else first).append(example)
                    seen_sessions.add(example['session_id'])
                group[bucket] = (first + repeated)[:EXAMPLE_LIMIT]
            group['execution_ids'].sort()
            group['session_ids'] = sorted(set(group['session_ids']))
            group['checked_session_ids'] = sorted(set(group['checked_session_ids']))
            group['execution_count'] = len(group['execution_ids'])
            group['session_count'] = len(group['session_ids'])
            group['checked_ratio'] = group['verification_counts']['passed'] / group['execution_count']
            group['verification_counts'] = dict(group['verification_counts'])
            group['outcome_counts'] = dict(group['outcome_counts'])
            group['candidate_ready'] = (len(group['checked_session_ids']) >= 3
                and group['checked_ratio'] >= .6 and not group['signals_truncated']
                and len(group['operations']) >= 2
                and any(item['operation'] == 'check' for item in group['operations'])
                and all(item['target'] != 'unknown' for item in group['operations']))
            result['procedures'].append(group)
        result['procedures'].sort(key=lambda group: (-group['session_count'], group['signature'], str(group['workspace_id'])))
        return result
