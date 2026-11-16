from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from reflect.core import main
from reflect.improvements.service import ImprovementService
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite

NOW = "2026-07-16T10:00:00+00:00"


def _seed_loop(db_path):
    conn = connect_sqlite(db_path)
    migrate(conn)
    conn.execute(
        "INSERT INTO agents(id, name, created_at, updated_at) VALUES ('agent-1', 'codex', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO repos(id, full_name, created_at, updated_at) VALUES ('repo-1', 'o11ydev/reflect', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO sessions(id, agent_id, repo_id, started_at, ended_at, status, created_at, updated_at)
        VALUES ('session-1', 'agent-1', 'repo-1', ?, ?, 'completed', ?, ?)
        """,
        (NOW, NOW, NOW, NOW),
    )
    for sequence in range(1, 4):
        step_id = f"step-{sequence}"
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, raw_attrs_json, created_at, updated_at
            ) VALUES (?, 'session-1', ?, 'tool_call', ?, 'failed', '{}', ?, ?)
            """,
            (step_id, sequence, NOW, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO tool_calls(
              id, step_id, session_id, tool_name, status, input_hash,
              input_preview_redacted, error_type, raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, 'session-1', 'exec', 'failed', 'same-command',
                      'same-command-preview', 'exit_nonzero', '{}', ?, ?)
            """,
            (f"tool-{sequence}", step_id, NOW, NOW),
        )
    service = ImprovementService(conn)
    service.loops.refresh()
    loop_id = service.loops.list()[0].id
    conn.close()
    return loop_id


def test_skills_sync_is_explicit_and_read_only_list_does_not_reconcile(tmp_path, monkeypatch):
    root = tmp_path / ".agents" / "skills" / "bounded-recovery"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: bounded-recovery\n"
        "description: Change one relevant condition before retrying.\n"
        "---\n\n"
        "# Bounded recovery\n\n1. Observe.\n2. Change state.\n3. Verify.\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "reflect.db"
    from tests.backend_helpers import bind_backend, execute_job
    backend = bind_backend(monkeypatch, db_path)

    synced = CliRunner().invoke(
        main,
        [
            "skills",
            "sync",
            "--json",
            "--path",
            str(root.parent),
            "--db-path",
            str(db_path),
        ],
    )

    assert synced.exit_code == 0, synced.output
    job_result = execute_job(backend, json.loads(synced.stdout)["job_id"])
    assert job_result["filesystem_skills"] == 1
    payload = json.loads(CliRunner().invoke(main, ["skills", "--json", "--db-path", str(db_path)]).output)
    assert payload["skills"][0]["slug"] == "bounded-recovery"
    assert payload["skills"][0]["version_count"] == 1
    skill_id = payload["skills"][0]["id"]

    skill_file = root / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8")
        + "\n4. Record the verified result.\n",
        encoding="utf-8",
    )
    listed = CliRunner().invoke(
        main,
        ["skills", "--json", "--db-path", str(db_path)],
    )
    assert listed.exit_code == 0, listed.output
    listed_payload = json.loads(listed.output)
    assert listed_payload.get("refresh") is None
    assert listed_payload["skills"][0]["version_count"] == 1

    shown = CliRunner().invoke(
        main,
        ["skills", "show", skill_id, "--json", "--db-path", str(db_path)],
    )
    assert shown.exit_code == 0, shown.output
    detail = json.loads(shown.output)
    assert len(detail["versions"]) == 1
    assert detail["installations"][0]["path"].endswith("bounded-recovery/SKILL.md")

    resynced = CliRunner().invoke(
        main,
        [
            "skills",
            "sync",
            "--json",
            "--path",
            str(root.parent),
            "--db-path",
            str(db_path),
        ],
    )
    assert resynced.exit_code == 0, resynced.output
    execute_job(backend, json.loads(resynced.stdout)["job_id"])
    assert json.loads(CliRunner().invoke(main, ["skills", "--json", "--db-path", str(db_path)]).output)["skills"][0]["version_count"] == 2


@pytest.mark.parametrize("initialized", [False, True])
def test_skills_sync_reports_external_writer_and_recovers(tmp_path, monkeypatch, initialized):
    from tests.backend_helpers import bind_backend, execute_job
    db_path = tmp_path / 'reflect.db'
    root = tmp_path / 'skills'
    root.mkdir()
    writer = connect_sqlite(db_path)
    if initialized:
        migrate(writer)
    backend = bind_backend(monkeypatch, db_path)
    args = ['skills', 'sync', '--json', '--path', str(root), '--db-path', str(db_path)]
    try:
        writer.execute('BEGIN IMMEDIATE')
        queued = CliRunner().invoke(main, args)
        assert queued.exit_code == 0, queued.output
        job = backend.jobs.get(json.loads(queued.stdout)['job_id'])
        assert job.state == 'queued'
        assert job.arguments['scan_paths'] == [str(root)]
        writer.rollback()
        result = execute_job(backend, job.job_id)
        assert result['filesystem_skills'] == 0
    finally:
        writer.close()


def test_loops_list_show_and_build_promote_selected_evidence_to_pending_skill(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    loop_id = _seed_loop(db_path)
    from tests.backend_helpers import bind_backend, execute_job, publish_actions
    publish_actions(db_path)
    backend = bind_backend(monkeypatch, db_path)
    runner = CliRunner()

    with patch("reflect.preparation_pipeline.prepare_sql_report_db"):
        listed = runner.invoke(main, ["loops", "--json", "--db-path", str(db_path)])
        shown = runner.invoke(
            main,
            ["loops", "show", loop_id, "--json", "--db-path", str(db_path)],
        )

    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)["loops"][0]["kind"] == "stalled"
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["occurrences"][0]["session_id"] == "session-1"

    agent_result = SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            [
                {
                    "name": "state-change-before-retry",
                    "description": "Change one relevant condition before retrying an unchanged failure.",
                    "content": "# State change before retry\n\n1. Observe.\n2. Change state.\n3. Verify or stop.",
                }
            ]
        ),
        stderr="",
    )
    with patch("reflect.preparation_pipeline.prepare_sql_report_db"), patch(
        "reflect.core.shutil.which", return_value="/usr/bin/codex"
    ), patch("subprocess.run", return_value=agent_result):
        built = runner.invoke(
            main,
            [
                "loops",
                "build",
                loop_id,
                "--agent",
                "codex",
                "--json",
                "--db-path",
                str(db_path),
            ],
        )

        assert built.exit_code == 0, built.output
        built_result = execute_job(backend, json.loads(built.output)['job_id'])
    execute_job(backend, built_result['projection_job_id'])
    built_payload = {'skill_id': built_result['skill_ids'][0]}

    conn = connect_sqlite(db_path)
    try:
        version = conn.execute(
            "SELECT source_loop_id, source_agent, status FROM skill_versions WHERE skill_id = ?",
            (built_payload["skill_id"],),
        ).fetchone()
        loop = conn.execute(
            "SELECT status, json_extract(evidence_json, '$.promoted_skill_id') FROM loop_patterns WHERE id = ?",
            (loop_id,),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(version) == (loop_id, "codex", "pending")
    assert tuple(loop) == ("promoted", built_payload["skill_id"])


def test_loop_build_empty_array_leaves_pattern_and_registry_unchanged(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    loop_id = _seed_loop(db_path)
    from tests.backend_helpers import bind_backend, execute_job, publish_actions
    publish_actions(db_path)
    backend = bind_backend(monkeypatch, db_path)
    conn = connect_sqlite(db_path)
    before = tuple(conn.execute("SELECT status,evidence_json FROM loop_patterns WHERE id=?", (loop_id,)).fetchone())
    conn.close()
    with patch("reflect.core.shutil.which", return_value="/usr/bin/codex"), patch(
        "subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="[]", stderr="")
    ):
        result = CliRunner().invoke(main, ["loops", "build", loop_id, "--agent", "codex", "--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        job_id = result.output.split()[0]
        outcome = execute_job(backend, job_id)
    assert outcome['candidate_ids'] == []
    conn = connect_sqlite(db_path)
    try:
        assert tuple(conn.execute("SELECT status,evidence_json FROM loop_patterns WHERE id=?", (loop_id,)).fetchone()) == before
        assert conn.execute("SELECT COUNT(*) FROM workflow_candidates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0
    finally:
        conn.close()


def test_loop_build_rejects_multiple_drafts(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    loop_id = _seed_loop(db_path)
    from tests.backend_helpers import bind_backend, publish_actions
    publish_actions(db_path)
    backend = bind_backend(monkeypatch, db_path)
    with patch("reflect.core.shutil.which", return_value="/usr/bin/codex"), patch(
        "subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="[{}, {}]", stderr="")
    ):
        result = CliRunner().invoke(main, ["loops", "build", loop_id, "--agent", "codex", "--db-path", str(db_path)])
        from reflect.backend_runtime import _execute_job
        job_id = result.output.split()[0]
        backend.jobs.claim(job_id, owner='fixture')
        _execute_job(db_path, backend.jobs.path, job_id, 'fixture')
    assert backend.jobs.get(job_id).state == 'failed'
    assert 'exactly one' in backend.jobs.get(job_id).error
