"""Opt-in native capture acceptance, independent of agent-reported completion."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from reflect.parsing import (
    _discover_rich_session_files,
    _infer_otlp_logs_file,
    _native_session_path_matches_id,
)
from reflect.raw_segments import readable_segment_paths
from reflect.report_server import default_otlp_traces
from reflect.store.ingest import (
    ingest_native_session_file,
    ingest_otlp_logs_file,
    ingest_otlp_traces_file,
)
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite

from .harness import AGENT_ADAPTERS, AgentTestContext, retain_session_otlp, run_agent

pytestmark = [pytest.mark.local_agent_e2e, pytest.mark.skipif(
    os.environ.get("REFLECT_RUN_LOCAL_AGENT_E2E") != "1" or bool(os.environ.get("CI")),
    reason="explicit local telemetry run required; never runs in CI",
)]


def _session_id(stdout: str) -> str:
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            for key in ("thread_id", "session_id", "sessionId", "chatId"):
                if value.get(key):
                    return str(value[key])
    return ""


def _source_files(agent: str, session_id: str, workspace: Path) -> list[Path]:
    selected = []
    for source_agent, path in _discover_rich_session_files():
        if source_agent != agent:
            continue
        if session_id and _native_session_path_matches_id(path, session_id):
            selected.append(path)
            continue
        # Some headless clients return no id. Bind only explicit source cwd,
        # never timestamps alone or unrelated files under the user's home.
        with path.open(encoding="utf-8") as handle:
            for _ in range(64):
                line = handle.readline()
                if not line:
                    break
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                payload = item.get("payload") or item
                if isinstance(payload, dict) and payload.get("cwd") == str(workspace):
                    selected.append(path)
                    break
    return selected


@pytest.mark.parametrize("repeat", range(3))
@pytest.mark.parametrize("adapter", [
    adapter for adapter in AGENT_ADAPTERS
    if adapter.name in {"codex", "cursor", "claude"}
    and adapter.name in os.environ.get("REFLECT_LOCAL_AGENT_E2E_AGENTS", "codex,cursor,claude").split(",")
], ids=lambda adapter: adapter.name)
def test_native_capture_without_completion_calls(adapter, repeat):
    executable = shutil.which(adapter.executable_name)
    assert executable, f"{adapter.executable_name} is not installed"
    artifact_root = Path(os.environ.get("REFLECT_TELEMETRY_ARTIFACT_DIR", tempfile.gettempdir()))
    artifact_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=f"reflect-telemetry-{adapter.name}-{repeat}-", dir=artifact_root))
    print(f"telemetry artifacts: {workspace}")
    (workspace / "value.txt").write_text("wrong")
    (workspace / "verify.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "ok = Path('value.txt').read_text().strip() == 'ok'\n"
        "with Path('checks.jsonl').open('a') as out: out.write(json.dumps({'passed': ok}) + '\\n')\n"
        "print('CHECK_PASS' if ok else 'CHECK_FAIL')\nraise SystemExit(0 if ok else 1)\n"
    )
    common = (
        "Work only in this temporary workspace. Do not use MCP, subagents, network, git remotes, "
        "or Reflect completion/start calls. Do not alter verify.py. Run each requested check "
        "as a separate shell tool invocation using exactly `python3 verify.py`. "
    )
    prompts = [
        common + "Run the check now (expected failure), change value.txt to exactly 'ok', then rerun the check. Finish with PHASE_ONE_DONE.",
        common + "Change value.txt to 'changed', run the check (expected failure), change value.txt back to 'ok', then rerun the check. Finish with PHASE_TWO_DONE.",
    ]
    session_id = ""
    timeout = int(os.environ.get("REFLECT_LOCAL_AGENT_E2E_TIMEOUT", "180"))
    for phase, prompt in enumerate(prompts, 1):
        context = AgentTestContext(adapter.name, executable, workspace, workspace / "reflect.db", prompt_override=prompt)
        result = run_agent(adapter.build_telemetry(context, resume_session_id=session_id), timeout_seconds=timeout)
        (workspace / f"phase-{phase}.jsonl").write_text(result.stdout)
        (workspace / f"phase-{phase}.stderr.txt").write_text(result.stderr)
        assert result.returncode == 0, result.diagnostic()
        session_id = session_id or _session_id(result.stdout)
        assert session_id, "client returned no explicit session identity; cannot safely resume"
    checks = [json.loads(line)["passed"] for line in (workspace / "checks.jsonl").read_text().splitlines()]
    expected = iter([False, True, False, True])
    next_expected = next(expected)
    for passed in checks:
        if passed == next_expected:
            next_expected = next(expected, None)
    assert next_expected is None and checks[-1] is True, checks
    sources = _source_files(adapter.name, session_id, workspace)
    assert sources, f"{adapter.name} {session_id}: no supported native capture for this headless mode"
    capture_dir = workspace / "captured"
    capture_dir.mkdir()
    retained = []
    for index, source in enumerate(sources):
        target_dir = capture_dir / str(index)
        target_dir.mkdir()
        target = target_dir / source.name
        shutil.copyfile(source, target)
        retained.append(target)
    trace_path = default_otlp_traces()
    log_path = _infer_otlp_logs_file(trace_path)
    otlp = retain_session_otlp({
        "traces": list(readable_segment_paths(trace_path)) if trace_path else [],
        "logs": list(readable_segment_paths(log_path)) if log_path else [],
    }, capture_dir, session_id)
    conn = connect_sqlite(workspace / "reflect.db")
    try:
        migrate(conn)
        snapshots = []
        for _ in range(2):
            for kind, path in otlp.items():
                ingest = ingest_otlp_traces_file if kind == "traces" else ingest_otlp_logs_file
                ingest(conn, file_path=path, skip_unchanged=True)
            for path in retained:
                ingest_native_session_file(conn, file_path=path, agent=adapter.name, skip_unchanged=True)
            assert normalize_pending_raw_events(conn)["failed"] == 0
            snapshots.append(tuple(conn.execute(sql).fetchone() for sql in (
                "SELECT COUNT(*) FROM sessions", "SELECT COUNT(*) FROM steps", "SELECT COUNT(*) FROM tool_calls",
                "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens) FROM llm_calls",
            )))
        assert snapshots[0] == snapshots[1]
        assert conn.execute("SELECT COUNT(*) FROM mcp_task_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_calls WHERE output_hash IS NOT NULL").fetchone()[0] >= 4
        assert conn.execute("SELECT COUNT(*) FROM tool_calls WHERE status = 'error'").fetchone()[0] >= 2
    finally:
        conn.close()
