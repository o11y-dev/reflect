import asyncio
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from reflect.context import ReflectContextService
from reflect.improvements.service import ImprovementService
from reflect.memory import MemoryItem, MemoryService, MemorySourceMetadata
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def _seed_memory(db_path, workspace):
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        remembered = MemoryService(conn).remember(
            MemoryItem(
                content="Run the release gate before publishing.",
                type="repo_convention",
                scope="project",
                source_metadata=MemorySourceMetadata(
                    source_kind="manual",
                    source_ref="manual",
                    path=str(workspace / "AGENTS.md"),
                    workspace_root=str(workspace),
                    manual_note=True,
                ),
                confidence=0.9,
            )
        )
        return remembered["id"]
    finally:
        conn.close()


def test_context_service_combines_memory_with_guidance(tmp_path):
    db_path = tmp_path / "reflect.db"
    memory_id = _seed_memory(db_path, tmp_path)
    conn = connect_sqlite(db_path)
    try:
        answer = ReflectContextService(conn).ask("release gate", path=tmp_path)
    finally:
        conn.close()

    assert answer.memories[0].id == memory_id
    assert answer.memories[0].provenance == "local_memory"
    assert answer.evidence == []
    assert "no matching approved workflow" in answer.answer


def test_context_service_records_and_completes_an_agent_task(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_memory(db_path, tmp_path)
    conn = connect_sqlite(db_path)
    try:
        service = ReflectContextService(conn)
        answer = service.begin_task("release gate with private detail", path=tmp_path)

        assert answer.task_run_id
        assert answer.next_action
        assert answer.next_action.tool == "reflect_complete"
        assert answer.next_action.arguments == {"task_run_id": answer.task_run_id}
        row = conn.execute(
            "SELECT question_hash, status FROM mcp_task_runs WHERE id = ?",
            (answer.task_run_id,),
        ).fetchone()
        assert row[0] != "release gate with private detail"
        assert row[1] == "started"

        completed = service.complete_task(
            answer.task_run_id,
            outcome="success",
            verification_passed=True,
            summary_redacted="Focused tests passed.",
        )
        repeated = service.complete_task(
            answer.task_run_id,
            outcome="success",
            verification_passed=True,
            summary_redacted="Focused tests passed.",
        )

        assert completed.status == "completed"
        assert completed.verification_passed is True
        assert completed.idempotent is False
        assert repeated.idempotent is True
        with pytest.raises(RuntimeError, match="already completed"):
            service.complete_task(answer.task_run_id, outcome="failure")
    finally:
        conn.close()


def test_context_service_returns_and_measures_the_selected_versioned_skill(
    tmp_path,
    monkeypatch,
):
    conn = connect_sqlite(tmp_path / "reflect.db")
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        service = ImprovementService(conn)
        candidate_id = service.stage_extracted_skills(
            [
                {
                    "name": "safe-release",
                    "description": "Publish a release with a focused validation gate.",
                    "content": (
                        "# Safe release\n\n"
                        "1. Run the focused release validation.\n"
                        "2. Publish only after it passes."
                    ),
                    "behavior_type": "verification",
                }
            ],
            session_ids=[],
            source_agent="codex",
        )[0]
        service.workflows.apply(candidate_id, project_root=project_root)
        service.skills.sync_workflow_candidates([candidate_id])
        now = "2026-07-24T10:00:00+00:00"
        conn.execute(
            "INSERT INTO agents(id, name, created_at, updated_at) VALUES ('agent-1', 'codex', ?, ?)",
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, started_at, status, created_at, updated_at)
            VALUES ('session-1', 'agent-1', ?, 'completed', ?, ?)
            """,
            (now, now, now),
        )
        conn.commit()
        monkeypatch.setenv("REFLECT_SESSION_ID", "session-1")

        context = ReflectContextService(conn)
        answer = context.begin_task(
            "Publish the release with validation",
            path=project_root,
        )

        assert answer.workflow_id == candidate_id
        assert len(answer.selected_skills) == 1
        selected = answer.selected_skills[0]
        assert selected.slug == "safe-release"
        assert selected.workflow_status == "active"
        assert selected.registry_lifecycle_state == "active"
        assert selected.execution_state == "follow_allowed"
        assert selected.instructions_truncated is False
        assert selected.installation_state == "installed"
        assert selected.installation_requires_operator_approval is True
        assert "Run the focused release validation" in selected.instructions
        completed = context.complete_task(
            answer.task_run_id,
            outcome="success",
            verification_passed=True,
        )
        assert completed.linked_to_session is True
        assert tuple(
            conn.execute(
                "SELECT state, outcome FROM skill_usage WHERE skill_id = ?",
                (selected.skill_id,),
            ).fetchone()
        ) == ("reported", "success")
        assert conn.execute(
            """
            SELECT outcome FROM session_outcomes
            WHERE session_id = 'session-1' AND source = 'agent_completion'
            """
        ).fetchone()[0] == "success"
    finally:
        conn.close()


def test_context_service_makes_approved_pending_skill_execution_unambiguous(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        service = ImprovementService(conn)
        candidate_id = service.stage_extracted_skills(
            [
                {
                    "name": "safe-release",
                    "description": "Publish a release with a focused validation gate.",
                    "content": "# Safe release\n\n1. Run the focused release validation.",
                    "behavior_type": "verification",
                }
            ],
            session_ids=[],
            source_agent="codex",
        )[0]
        conn.execute(
            "UPDATE workflow_candidates SET status = 'approved' WHERE id = ?",
            (candidate_id,),
        )
        conn.commit()

        answer = ReflectContextService(conn).begin_task(
            "Publish the safe release with validation",
            path=tmp_path,
        )

        selected = answer.selected_skills[0]
        assert selected.workflow_status == "approved"
        assert selected.registry_lifecycle_state == "pending"
        assert selected.execution_state == "follow_allowed"
        assert selected.installation_state == "not_installed"
        assert selected.installation_requires_operator_approval is True
    finally:
        conn.close()


def test_context_service_requires_full_skill_retrieval_when_inline_content_is_truncated(
    tmp_path,
):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        service = ImprovementService(conn)
        final_instruction = "FINAL VERIFICATION MARKER"
        candidate_id = service.stage_extracted_skills(
            [
                {
                    "name": "large-release",
                    "description": "Publish a large release with complete validation.",
                    "content": f"# Large release\n\n{'x' * 21_000}\n\n{final_instruction}",
                    "behavior_type": "verification",
                }
            ],
            session_ids=[],
            source_agent="codex",
        )[0]
        conn.execute(
            "UPDATE workflow_candidates SET status = 'approved' WHERE id = ?",
            (candidate_id,),
        )
        conn.commit()

        context = ReflectContextService(conn)
        answer = context.begin_task(
            "Publish the large release with validation",
            path=tmp_path,
        )

        selected = answer.selected_skills[0]
        assert selected.execution_state == "retrieve_full_instructions"
        assert selected.instructions_truncated is True
        assert final_instruction not in selected.instructions
        assert selected.full_instructions_action is not None
        assert selected.full_instructions_action.tool == "reflect_explain"
        assert selected.full_instructions_action.arguments == {
            "entity_id": selected.version_id
        }

        explanation = context.explain(selected.version_id)
        assert explanation["kind"] == "skill_version"
        assert final_instruction in explanation["entity"]["version"]["content_markdown"]
        assert explanation["entity"]["instructions_truncated"] is False
    finally:
        conn.close()


def test_reflect_mcp_supports_initialize_list_and_call(tmp_path):
    db_path = tmp_path / "reflect.db"
    memory_id = _seed_memory(db_path, tmp_path)

    async def exercise_server():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "reflect.mcp"],
            env={**os.environ, "REFLECT_DB_PATH": str(db_path)},
        )
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            tools = await session.list_tools()
            discovered = {tool.name: tool for tool in tools.tools}
            result = await session.call_tool(
                "reflect_context",
                {"question": "release gate", "path": str(tmp_path)},
            )
            completed = await session.call_tool(
                "reflect_complete",
                {
                    "task_run_id": result.structuredContent["task_run_id"],
                    "outcome": "success",
                    "verification_passed": True,
                    "summary": "Validated the release gate.",
                },
            )
            return initialized, discovered, result, completed

    initialized, discovered, result, completed = asyncio.run(exercise_server())
    names = set(discovered)

    assert initialized.serverInfo.name == "Reflect"
    assert "At the start of every non-trivial repository task" in initialized.instructions
    assert "execution_state is follow_allowed" in initialized.instructions
    assert "call reflect_complete exactly once" in initialized.instructions
    assert names == {
        "reflect_complete",
        "reflect_context",
        "reflect_explain",
        "reflect_improvements",
        "reflect_usage",
    }
    assert not result.isError
    assert result.structuredContent is not None
    assert result.structuredContent["memories"][0]["id"] == memory_id
    assert result.structuredContent["task_run_id"].startswith("mcp_task_")
    assert result.structuredContent["next_action"]["tool"] == "reflect_complete"
    assert not completed.isError
    assert completed.structuredContent["status"] == "completed"
    assert not {"memory_search", "memory_remember", "memory_validate"} & names
    assert discovered["reflect_context"].annotations.readOnlyHint is False
    assert discovered["reflect_complete"].annotations.readOnlyHint is False
    assert all(
        discovered[name].annotations and discovered[name].annotations.readOnlyHint
        for name in names - {"reflect_context", "reflect_complete"}
    )
    assert all(tool.annotations and not tool.annotations.destructiveHint for tool in discovered.values())

    conn = connect_sqlite(db_path)
    try:
        assert tuple(
            conn.execute("SELECT status, outcome FROM mcp_task_runs").fetchone()
        ) == ("completed", "success")
    finally:
        conn.close()
