from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

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


def test_skills_default_reconciles_and_lists_versioned_registry(tmp_path):
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

    listed = CliRunner().invoke(
        main,
        ["skills", "--json", "--path", str(root.parent), "--db-path", str(db_path)],
    )

    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["refresh"]["filesystem_skills"] == 1
    assert payload["skills"][0]["slug"] == "bounded-recovery"
    assert payload["skills"][0]["version_count"] == 1

    shown = CliRunner().invoke(
        main,
        ["skills", "show", payload["skills"][0]["id"], "--json", "--db-path", str(db_path)],
    )
    assert shown.exit_code == 0, shown.output
    detail = json.loads(shown.output)
    assert detail["installations"][0]["path"].endswith("bounded-recovery/SKILL.md")


def test_loops_list_show_and_build_promote_selected_evidence_to_pending_skill(tmp_path):
    db_path = tmp_path / "reflect.db"
    loop_id = _seed_loop(db_path)
    runner = CliRunner()

    with patch("reflect.core._prepare_sql_report_db"):
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
    with patch("reflect.core._prepare_sql_report_db"), patch(
        "subprocess.run", return_value=agent_result
    ):
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
    built_payload = json.loads(built.output)
    assert built_payload["loop_id"] == loop_id
    assert built_payload["status"] == "pending"

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
