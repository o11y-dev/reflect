"""Validated job arguments and process-owned long-running product operations."""
from __future__ import annotations

import json
import subprocess
from contextlib import closing
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class JobArguments(BaseModel):
    model_config = ConfigDict(extra='forbid')


class RefreshArguments(JobArguments):
    otlp_traces: str | None = None
    include_native_sessions: bool = True
    spans_dir: str | None = None
    keep_processed_raw: bool = False
    analysis_days: int | None = Field(default=None, ge=1)
    analysis_session_ids: list[str] | None = None
    native_session_ids: list[str] = Field(default_factory=list)


class DiscoveryArguments(JobArguments):
    agent: str
    project_root: str
    period: Literal['day', 'week', 'month', 'all'] = 'week'
    refresh: bool = False
    otlp_traces: str | None = None
    sessions_dir: str | None = None
    spans_dir: str | None = None
    demo: bool = False


class BuildArguments(JobArguments):
    agent: str
    project_root: str


class SyncArguments(JobArguments):
    scan_paths: list[str] = Field(default_factory=list)
    lifecycle: str | None = None


class CostArguments(JobArguments):
    alias_path: str | None = None


class MemoryArguments(JobArguments):
    path: str | None = None
    memory_id: str | None = None
    candidate_id: str | None = None


def execute_agent_job(db_path: Path, kind: str, arguments: dict) -> dict:
    from reflect.agent_capabilities import skill_agent_capabilities
    from reflect.application import ReflectApplication
    from reflect.skill_extraction import (
        _build_skill_evidence_bundle_from_sql,
        _build_skills_extraction_prompt_from_bundle,
        _load_extracted_skills,
    )

    agent = arguments['agent']
    capability = next((item for item in skill_agent_capabilities() if item.skill_cli == agent), None)
    if capability is None:
        raise ValueError('Select a supported skill agent CLI')
    source_bundle = None
    source_ids = None
    if kind == 'skill_discovery' and any(arguments.get(key) for key in ('demo', 'sessions_dir', 'spans_dir', 'otlp_traces')):
        import os
        from datetime import UTC, datetime, timedelta

        from reflect.processing import analyze_telemetry
        from reflect.skill_extraction import _build_skill_evidence_bundle
        demo = arguments.get('demo', False)
        traces = Path(str(resources.files('reflect') / 'data/demo-traces.json')) if demo else Path(arguments['otlp_traces']) if arguments.get('otlp_traces') else None
        sessions = Path(arguments['sessions_dir']) if arguments.get('sessions_dir') else Path(os.devnull)
        spans = Path(arguments['spans_dir']) if arguments.get('spans_dir') else Path(os.devnull)
        days = {'day': 1, 'week': 7, 'month': 30, 'all': None}[arguments.get('period', 'week')]
        since = datetime.now(UTC) - timedelta(days=days) if days and not demo else None
        stats = analyze_telemetry(sessions, spans, traces, since=since)
        source_bundle = _build_skill_evidence_bundle(stats)
        source_ids = sorted(stats.sessions_seen)
    application = ReflectApplication(db_path)
    with application.read() as service:
        if kind == 'loop_build':
            loop_id = arguments['loop_id']
            detail = service.loops.show(loop_id, limit=50)
            bundle = service.loops.evidence_bundle(loop_id)
            session_ids = sorted({item.session_id for item in detail.occurrences})
            template = (resources.files('reflect') / 'data/loop-skill-prompt.md').read_text()
            prompt = template.replace('{{EVIDENCE_JSON}}', json.dumps(bundle, sort_keys=True))
        else:
            period = arguments.get('period', 'week')
            days = {'day': 1, 'week': 7, 'month': 30, 'all': None}[period]
            where = "WHERE julianday(started_at) >= julianday('now', ?)" if days else ''
            session_ids = [row[0] for row in service.conn.execute(
                f'SELECT id FROM sessions {where} ORDER BY started_at DESC LIMIT 500',
                (f'-{days} days',) if days else ())]
            if source_ids is not None:
                session_ids = source_ids
            bundle = _build_skill_evidence_bundle_from_sql(service.conn, session_ids=set(session_ids))
            if not bundle or not bundle.get('sessions'):
                bundle = source_bundle
            if not bundle or not bundle.get('sessions'):
                return {'candidate_ids': [], 'reason': 'No eligible evidence in the selected period'}
            template = (resources.files('reflect') / 'data/skills-extraction-prompt.md').read_text()
            prompt = _build_skills_extraction_prompt_from_bundle(template, bundle)
    project_root_value = arguments.get('project_root')
    if not project_root_value:
        raise ValueError('No project root was recorded; rerun the command from the project directory')
    project_root = Path(str(project_root_value)).expanduser()
    if not project_root.is_dir():
        raise ValueError('A valid project_root is required to run the selected agent CLI')
    project_root = project_root.resolve()
    # No DB transaction or generation lease is held while the external agent runs.
    try:
        result = subprocess.run([agent, *capability.skill_cli_flags, prompt], capture_output=True,
                                text=True, timeout=1800, check=False, cwd=project_root)
    except OSError as exc:
        raise RuntimeError(f"Could not run agent CLI '{agent}': {exc}") from exc
    if result.returncode:
        error = (result.stderr or result.stdout or '(no output)').strip()
        if len(error) > 1500:
            error = error[:1500] + f'\n... truncated {len(error) - 1500} characters ...'
        raise RuntimeError(f'Agent exited with code {result.returncode}: {error}')
    drafts = _load_extracted_skills(result.stdout)
    if kind == 'loop_build' and len(drafts) > 1:
        raise ValueError('Loop build must produce exactly one skill draft')
    if not drafts:
        return {'candidate_ids': [], 'reason': 'No reusable candidates supported by the evidence'}
    if kind == 'loop_build':
        drafts = [{**draft, 'behavior_type': 'loop', 'source_loop_id': arguments['loop_id']} for draft in drafts]
    with application.state_action() as service:
        ids = service.stage_extracted_skills(drafts, session_ids=session_ids, source_agent=agent)
        for candidate_id in ids:
            service.conn.execute('INSERT OR IGNORE INTO workflow_review_ownership(candidate_id,evidence_revision_hash) '
                                 'SELECT id,revision_hash FROM workflow_candidates WHERE id=?', (candidate_id,))
        skills = [service.skills.skill_for_candidate(candidate_id) for candidate_id in ids]
    return {'candidate_ids': ids, 'skill_ids': [skill.id for skill in skills],
            'installed': False, 'evidence_session_count': len(session_ids)}


def execute_maintenance(db_path: Path, kind: str, arguments: dict) -> dict:
    from reflect.preparation import PreparationLock
    from reflect.store.sqlite import connect_sqlite

    if kind.startswith('database_'):
        return execute_database_job(db_path, kind.removeprefix('database_'), arguments)
    with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
        if kind == 'loop_promotion':
            from reflect.improvements.service import ImprovementService
            ImprovementService(conn, initialize_schema=False).loops.mark_promoted(arguments['loop_id'], arguments['skill_id'])
            return {'loop_id': arguments['loop_id'], 'skill_id': arguments['skill_id']}
        if kind == 'skills_sync':
            from reflect.improvements.service import ImprovementService
            return ImprovementService(conn, initialize_schema=False).skills.refresh(scan_paths=[Path(value) for value in arguments['scan_paths']])
        if kind == 'cost_repair':
            from reflect.preparation_pipeline import ensure_sql_costs
            from reflect.store.cost_refresh import CostRefreshState
            from reflect.store.rollups import rebuild_rollups
            alias = Path(arguments['alias_path']) if arguments.get('alias_path') else None
            state = CostRefreshState(conn)
            inputs = state.inspect(alias_path=alias)
            result = ensure_sql_costs(conn, alias_path=alias, refresh_inputs=inputs)
            rebuild_rollups(conn)
            state.mark_current(inputs)
            conn.commit()
            return {'added_aliases': list(result.added_aliases), 'unresolved_models': list(result.unresolved_models)}
        if kind.startswith('memory_'):
            from reflect.memory import MemoryService
            service = MemoryService(conn)
            if kind == 'memory_sync':
                result = service.sync_path(Path(arguments['path']), home_root=Path.home())
                result['candidate_count'] = len(service.candidates(path=Path(arguments['path'])))
                return result
            if kind == 'memory_forget':
                return {'removed': service.forget(arguments['memory_id'])}
            if kind == 'memory_validate':
                memory_id = arguments.get('memory_id')
                if arguments.get('candidate_id'):
                    memory_id = service.promote_candidate(arguments['candidate_id'])['id']
                return service.validate(memory_id)
        raise ValueError(f'Unsupported maintenance operation: {kind}')


class DatabaseArguments(JobArguments):
    otlp_traces: str | None = None
    spans_file: str | None = None
    limit: int | None = Field(default=None, ge=1)
    older_than_days: int = Field(default=60, ge=1)
    all_inactive_sessions: bool = False
    apply_changes: bool = False
    backup: bool = True
    vacuum: bool = False


def execute_database_job(db_path: Path, operation: str, arguments: dict):
    from dataclasses import asdict
    from datetime import UTC, datetime

    from reflect.preparation import PreparationLock
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite
    if operation == 'prune' and not arguments.get('apply_changes'):
        from reflect.store.retention import SessionPruner, SessionRetentionPolicy
        from reflect.store.sqlite import connect_sqlite_read_only
        if arguments.get('vacuum'):
            raise ValueError('Vacuum requires apply_changes')
        with closing(connect_sqlite_read_only(db_path, profile=None)) as conn:
            from reflect.store.doctor import inspect_database
            pending = inspect_database(conn)['pending_migrations']
            if pending:
                raise ValueError('pending migrations: ' + ', '.join(map(str, pending)))
            result = SessionPruner(conn, SessionRetentionPolicy(
                older_than_days=arguments.get('older_than_days', 60),
                include_valid_starts=arguments.get('all_inactive_sessions', False))).run(apply=False)
        return {**asdict(result), 'backup_path': None, 'candidate_count': len(result.candidates), 'vacuumed': False}
    with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
        backup = None
        if operation == 'prune' and arguments.get('backup', True):
            import shutil

            from reflect.store.sqlite import backup_sqlite
            if shutil.disk_usage(db_path.parent).free < db_path.stat().st_size + 2 * 1024**3:
                raise RuntimeError('Insufficient disk space for retention backup and 2 GiB reserve')
            conn.execute('BEGIN IMMEDIATE')
            backup = db_path.with_name(db_path.name + '.backup-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ'))
            backup_sqlite(db_path, backup)
        applied = migrate(conn)
        if operation == 'migrate':
            return {'applied_migrations': applied}
        if operation == 'ingest':
            from reflect.store.ingest import ingest_local_spans_file, ingest_otlp_traces_file
            if bool(arguments.get('otlp_traces')) == bool(arguments.get('spans_file')):
                raise ValueError('Pass exactly one of otlp_traces or spans_file')
            return (ingest_otlp_traces_file(conn, file_path=Path(arguments['otlp_traces']))
                    if arguments.get('otlp_traces') else ingest_local_spans_file(conn, file_path=Path(arguments['spans_file'])))
        if operation == 'normalize':
            from reflect.store.normalize import backfill_mcp_calls, normalize_pending_raw_events
            changed = set()
            result = normalize_pending_raw_events(conn, limit=arguments.get('limit'), changed_session_ids=changed)
            backfill_mcp_calls(conn, session_ids=changed)
            return result
        if operation == 'graph':
            from reflect.store.graph_normalize import rebuild_graph
            return rebuild_graph(conn)
        if operation == 'rollups':
            from reflect.store.rollups import rebuild_rollups
            return rebuild_rollups(conn)
        if operation == 'prune':
            from reflect.store.retention import SessionPruner, SessionRetentionPolicy
            result = SessionPruner(conn, SessionRetentionPolicy(
                older_than_days=arguments.get('older_than_days', 60),
                include_valid_starts=arguments.get('all_inactive_sessions', False))).run(apply=arguments.get('apply_changes', False))
            conn.commit()
            if arguments.get('vacuum') and result.pruned_session_ids:
                conn.execute('VACUUM')
            return {**asdict(result), 'backup_path': str(backup) if backup else None,
                    'candidate_count': len(result.candidates), 'vacuumed': bool(arguments.get('vacuum') and result.pruned_session_ids)}
        raise ValueError('Unsupported database maintenance operation')
