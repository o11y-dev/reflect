import json
import os
import selectors
import subprocess
import sys

import pytest

from reflect.improvements.service import ImprovementService
from reflect.preparation import PreparationLock
from reflect.store.sqlite import connect_sqlite, mark_snapshot

WRITE_OPERATION = """
import json
import sys
from pathlib import Path
from reflect.core import main
from reflect import mcp
from reflect.dashboard_improvements import DashboardImprovementAdapter

operation, db, root = sys.argv[1:]
root = Path(root)
print('started', flush=True)
if operation == 'mcp_review':
    result = mcp.reflect_review_change('apply_workflow', 'safe-review', project_root=str(root))
    print(json.dumps({'state': result['state']}))
elif operation == 'dashboard_feedback':
    result = DashboardImprovementAdapter(Path(db)).record_feedback('session', outcome='good', reason=None)
    print(json.dumps(result))
else:
    commands = {
        'memory_sync': ['memory', 'sync', str(root), '--json'],
        'workflow_add': ['workflows', 'add', str(root / 'SKILL.md')],
        'feedback': ['feedback', 'session', '--outcome', 'good'],
    }
    main(args=[*commands[operation], '--db-path', db])
"""


@pytest.mark.parametrize("operation", [
    "memory_sync", "workflow_add", "feedback", "mcp_review", "dashboard_feedback",
])
def test_mutations_wait_for_refresh_and_commit_after_release(tmp_path, operation):
    db = tmp_path / "reflect.db"
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "AGENTS.md").write_text("# Instructions\nVerify changes before committing.\n")
    (root / "SKILL.md").write_text(
        "---\nname: reviewed-work\ndescription: Verify changes.\n---\n\n# Work\n\n1. Verify.\n"
    )
    writer = connect_sqlite(db)
    service = ImprovementService(writer)
    now = "2026-10-18T12:00:00+00:00"
    writer.execute("INSERT INTO agents(id, name, created_at, updated_at) VALUES ('agent', 'codex', ?, ?)", (now, now))
    writer.execute(
        "INSERT INTO sessions(id, agent_id, started_at, status, created_at, updated_at) "
        "VALUES ('session', 'agent', ?, 'completed', ?, ?)", (now, now, now),
    )
    service.stage_extracted_skills([
        {"name": "safe-review", "description": "Verify work.", "content": "# Review\n1. Verify.",
         "behavior_type": "verification"},
    ], session_ids=[])
    mark_snapshot(writer, ready=True)
    process = None
    try:
        with PreparationLock(db):
            writer.execute("BEGIN IMMEDIATE")
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", WRITE_OPERATION, operation, str(db), str(root)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "REFLECT_DB_PATH": str(db),
                     "REFLECT_HOME": str(tmp_path / "home"), "REFLECT_SQLITE_BUSY_TIMEOUT_MS": "50"},
            )
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(10), "writer process did not start"
            assert process.stdout.readline().strip() == b"started"
            try:
                stdout, stderr = process.communicate(timeout=0.3)
            except subprocess.TimeoutExpired:
                pass
            else:
                pytest.fail(f"Mutation bypassed the refresh lock: {stdout.decode()} {stderr.decode()}")
            writer.rollback()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr.decode()
        if operation == "memory_sync":
            assert json.loads(stdout)["inserted"] >= 1
        elif operation == "workflow_add":
            assert b"Staged" in stdout
        elif operation == "mcp_review":
            assert json.loads(stdout)["state"] == "pending"
            assert not (root / ".agents").exists()
        else:
            assert writer.execute("SELECT COUNT(*) FROM operator_feedback").fetchone()[0] == 1
    finally:
        writer.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
