#!/usr/bin/env python3
"""Build the public report exclusively from deterministic, fictional telemetry.

This is a fixture producer, not an export command: it never opens the user's
Reflect store or native transcripts. The temporary database and installed sample
skills are discarded after the existing read models have produced the bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from reflect.dashboard_explore import build_explore_payload
from reflect.dashboard_improvements import DashboardImprovementAdapter
from reflect.dashboard_queries import build_dashboard_payload
from reflect.dashboard_sessions import load_session_detail
from reflect.improvements.models import (
    EvidenceRef,
    ObservationDraft,
    RuleDefinition,
    Severity,
    WorkflowProposal,
)
from reflect.improvements.service import ImprovementService
from reflect.improvements.skills import _skill_id
from reflect.pricing import ModelPricing, PricingTable
from reflect.store.graph_normalize import rebuild_graph
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.rollups import rebuild_rollups
from reflect.store.sqlite import connect_sqlite, mark_snapshot

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/reports/showcase.json"
CLOCK = "2026-11-09T12:00:00+00:00"
ACTIVATED = "2026-11-04T00:00:00+00:00"
PROJECTS = ("sample-library", "sample-checkout", "sample-service", "sample-counterexamples")


class FixtureClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime.fromisoformat(CLOCK)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def synthetic_sessions() -> list[dict]:
    """Small recorded hook shapes; all names, content, usage, and dates are fictional."""
    sessions = []
    for index in range(1, 9):
        before = index <= 3
        label = f"before-{index}" if before else f"after-{index - 3}"
        sessions.append({"id": f"demo-retry-{label}", "agent": "codex", "project": PROJECTS[0],
                         "day": index, "retries": 3 if before else 0, "skill": "inspect-before-retry" if not before else None,
                         "title": "Fix the missing dependency", "mode": "retry"})
    for index in range(1, 4):
        sessions.append({"id": f"demo-routine-{index}", "agent": "claude", "project": PROJECTS[1],
                         "day": index, "retries": 0, "skill": None,
                         "title": "Implement a bounded checkout change", "mode": "routine"})
    sessions.extend([
        {"id": "demo-insufficient-1", "agent": "cursor", "project": PROJECTS[2], "day": 6,
         "retries": 0, "skill": None, "title": "Implement a bounded service change", "mode": "insufficient"},
        {"id": "demo-pass-then-edit", "agent": "codex", "project": PROJECTS[3], "day": 8,
         "retries": 0, "skill": None, "title": "Check coverage after the final edit", "mode": "invalidated"},
        {"id": "demo-wrapper-failure", "agent": "claude", "project": PROJECTS[3], "day": 9,
         "retries": 0, "skill": None, "title": "Inspect a wrapped verification result", "mode": "wrapper"},
    ])
    return sessions


def _events(session: dict, skill_versions: dict | None = None) -> list[dict]:
    start = datetime(2026, 11, session["day"], 10, tzinfo=UTC)
    sid = session["id"]
    spans = []
    base = {"gen_ai.client.name": session["agent"], "gen_ai.client.session_id": sid,
            "gen_ai.client.turn_id": sid + "-turn", "gen_ai.client.cwd": f"/workspace/{session['project']}",
            "reflect.telemetry.origin": "hook_jsonl", "reflect.time.provenance": "observed"}

    def emit(name: str, attrs: dict) -> None:
        seq = len(spans)
        at = start + timedelta(seconds=seq * 20)
        spans.append({"name": name, "traceId": hashlib.sha256(sid.encode()).hexdigest()[:32],
                      "spanId": hashlib.sha256(f"{sid}:{seq}".encode()).hexdigest()[:16],
                      "start_time_ns": int(at.timestamp()) * 10**9,
                      "end_time_ns": int((at + timedelta(seconds=2)).timestamp()) * 10**9,
                      "attributes": {**base, **attrs}})

    def tool(name: str, *, command: str = "", path: str = "", output: str = "", exit_code: int = 0) -> None:
        attrs = {"gen_ai.client.tool_name": name, "gen_ai.client.tool.call_id": f"{sid}-tool-{len(spans)}",
                 "gen_ai.client.tool.input": json.dumps({"command": command} if command else {"file_path": path}),
                 "gen_ai.client.tool.output": output, "process.exit.code": exit_code,
                 "gen_ai.client.hook.event": "PostToolUse"}
        if command:
            attrs["gen_ai.client.command"] = command
        if path:
            attrs["gen_ai.client.file_path"] = path
            if session["skill"] and path.endswith("/SKILL.md"):
                attrs["reflect.skill.id"] = _skill_id(f"workflow:{session['skill']}")
                if version := (skill_versions or {}).get(session["skill"]):
                    attrs["reflect.skill.version_id"] = version["id"]
                    attrs["reflect.skill.content_hash"] = version["content_hash"]
        if exit_code:
            attrs.update({"error.type": "ModuleNotFoundError", "error.message": "No module named 'sample_helper'"})
        emit(f"execute_tool {command}" if command else name, attrs)

    emit("UserPromptSubmit", {"gen_ai.client.prompt.text": session["title"],
                              "gen_ai.client.hook.event": "UserPromptSubmit"})
    if session["skill"]:
        tool("Read", path=f".agents/skills/{session['skill']}/SKILL.md", output="Inspect the evidence, make one scoped change, then verify.")
    for _ in range(session["retries"]):
        tool("exec_command", command="pytest -q", output="ModuleNotFoundError: No module named 'sample_helper'", exit_code=1)
    if session["mode"] == "invalidated":
        tool("exec_command", command="pytest -q tests/test_checkout.py", output="47 passed, 446 deselected")
        tool("Edit", path="tests/test_checkout.py", output="Updated the checkout assertion.")
    elif session["mode"] == "wrapper":
        tool("exec_command", command="pytest -q; echo done", output="1 failed, 11 passed\ndone", exit_code=0)
    else:
        tool("Read", path="pyproject.toml" if session["mode"] == "retry" else "src/checkout.py", output="Read the relevant dependency or bounded implementation.")
        tool("Edit", path="pyproject.toml" if session["mode"] == "retry" else "src/checkout.py", output="Added sample_helper." if session["mode"] == "retry" else "Applied one bounded change.")
        tool("exec_command", command="pytest -q", output="12 passed")
    usage = {"gen_ai.usage.input_tokens": 1200 + len(spans) * 100,
             "gen_ai.usage.output_tokens": 240 + len(spans) * 20,
             "gen_ai.usage.cache_read.input_tokens": 400,
             "gen_ai.usage.cache_creation.input_tokens": 0}
    if session["mode"] != "insufficient":
        usage["gen_ai.request.model"] = "sample-model"
    emit("Stop", {**usage, "gen_ai.operation.name": "chat", "reflect.turn.terminal": True,
                  "gen_ai.client.hook.event": "Stop", "gen_ai.client.status": "completed",
                  "gen_ai.client.output": "The recorded check result is available; task effectiveness requires comparison."})
    return spans


def _seed_identity(conn, sessions: list[dict]) -> None:
    # The fixture manifest explicitly supplies repository identity, independent
    # of this machine's directories or any Git discovery.
    for project in PROJECTS:
        conn.execute("INSERT OR IGNORE INTO repos(id,full_name,created_at,updated_at) VALUES(?,?,?,?)",
                     (project, f"example/{project}", CLOCK, CLOCK))
        conn.execute("""INSERT OR IGNORE INTO workspaces(id,root_path,path_hash,label,repo_id,source_key,confidence,
                       raw_json,created_at,updated_at) VALUES(?,?,?,?,?,'synthetic_manifest',1,'{}',?,?)""",
                     (f"workspace-{project}", f"/workspace/{project}", project, project, project, CLOCK, CLOCK))
    for session in sessions:
        conn.execute("UPDATE sessions SET title=?,repo_id=?,workspace_id=? WHERE id=?",
                     (session["title"], session["project"], f"workspace-{session['project']}", session["id"]))


def _preview(session: dict) -> list[dict]:
    rows = []
    for event in _events(session):
        attrs = event["attributes"]
        if not attrs.get("gen_ai.client.tool_name") or str(attrs.get("gen_ai.client.file_path", "")).endswith("/SKILL.md"):
            continue
        command = attrs.get("gen_ai.client.command")
        rows.append({"label": command or attrs["gen_ai.client.tool_name"],
                     "result": str(attrs["gen_ai.client.tool.output"]).split(":", 1)[0],
                     "status": "failed" if attrs["process.exit.code"] else "passed" if command else "info"})
    return rows[:3]


def _workflow(service, *, slug: str, project: str, sessions: list[str], title: str,
              summary: str, behavior: str, metric: str, clock: str, temp: Path) -> tuple[str, str, dict]:
    repo = service.repository
    rule = RuleDefinition(id=f"sample_{slug}", version=1, category=behavior, title=title,
                          description="Illustrative sample with explicit synthetic evidence.")
    repo.sync_rule_definitions([rule], now=clock)
    unit_rows = service.conn.execute(
        "SELECT id,session_id FROM execution_units WHERE session_id IN (" + ",".join("?" for _ in sessions) + ") AND eligible=1 ORDER BY session_id", sessions).fetchall()
    if behavior == "recovery":
        observed_metric, metric_unit = "failed_tool_calls", "calls"
        observed_value = service.conn.execute(
            "SELECT COUNT(*) FROM tool_calls tc JOIN execution_unit_steps eus ON eus.step_id=tc.step_id "
            "WHERE tc.status='error' AND eus.execution_unit_id IN (" + ",".join("?" for _ in unit_rows) + ")",
            [row[0] for row in unit_rows],
        ).fetchone()[0]
    elif behavior == "proven_pattern":
        observed_metric, metric_unit = "checked_execution_units", "tasks"
        observed_value = sum(json.loads(row[0])["assessment"]["verification"] == "passed" for row in service.conn.execute(
            "SELECT boundary_json FROM execution_units WHERE id IN (" + ",".join("?" for _ in unit_rows) + ")", [row[0] for row in unit_rows]))
    else:
        observed_metric, observed_value, metric_unit = "observed_execution_units", len(unit_rows), "tasks"
    observation = repo.upsert_observation(ObservationDraft(
        rule_id=rule.id, rule_version=1, scope_type="repository", scope_id=project, repo_id=project,
        fingerprint=slug, category=behavior, title=title, summary=summary,
        metric_name=observed_metric, metric_value=observed_value, metric_unit=metric_unit,
        metric_direction="lower_is_better", impact_score=60 if behavior == "recovery" else 30,
        severity=Severity.MEDIUM if behavior == "recovery" else Severity.LOW, confidence=1,
        occurrence_count=int(observed_value), affected_session_count=len(sessions),
        evidence=[EvidenceRef(entity_type="execution_unit", entity_id=row[0], session_id=row[1],
                              summary_redacted="Illustrative recorded sequence; inspect the session tool results.") for row in unit_rows],
    ), now=clock)
    content = {"schema_version": 1, "slug": slug, "behavior_type": behavior,
               "description": summary, "steps": ["Inspect the relevant error or file.", "Change the condition before retrying.", "Run the scoped check after the final edit."],
               "constraints": ["A passed check covers only its observed scope.", "Stop if the same failure remains after the changed condition."],
               "verification": ["Inspect terminal check output and exit status."],
               "source": {"rule_id": rule.id, "synthetic": True},
               "workflow_contract": {"signature_hash": slug,
                   "applicability": {"repo_id": project},
                   "milestones": [{"id": role, "role": role, "required": True, "evidence": "corroborated"} for role in ("observe", "act", "verify")],
                   "validation": {"baseline_minimum": 3, "baseline_maximum": 20}}}
    candidate = repo.ensure_candidate(observation, proposal=WorkflowProposal(
        title=f"Workflow: {title}", hypothesis="This illustrative routine aims to reduce repeated work; compare later tasks before claiming impact.",
        risk="low", content=content, target_metric=metric, target_value=0.5,
    ), now=clock)
    service.conn.execute("UPDATE workflow_candidates SET measurement_window=5 WHERE id=?", (candidate,))
    service.conn.commit()
    root = temp / project
    (root / ".git").mkdir(parents=True)
    preview = service.workflows.preview(candidate, project_root=root)
    with patch("reflect.improvements.workflows.utc_now", lambda: clock):
        service.workflows.approve(candidate, actor="synthetic_reviewer")
        service.workflows.apply(candidate, project_root=root, actor="synthetic_reviewer")
    service.conn.commit()
    return observation, candidate, preview


def _fixed_clocks(stack: ExitStack) -> None:
    # Fixture-only clock/ID control. Domain derivation is real, never mocked.
    counter = iter(range(1, 10000))
    stack.enter_context(patch("uuid.uuid4", side_effect=lambda: uuid.uuid5(uuid.NAMESPACE_URL, f"reflect-showcase:{next(counter)}")))
    for name, module in list(sys.modules.items()):
        if name.startswith("reflect.") and module:
            if getattr(module, "datetime", None) is datetime:
                stack.enter_context(patch.object(module, "datetime", FixtureClock))
            if callable(getattr(module, "utc_now", None)):
                stack.enter_context(patch.object(module, "utc_now", lambda: CLOCK))


def _clean(value, temporary_root: str, identifiers: dict[str, str]):
    if isinstance(value, dict):
        return {key: _clean(item, temporary_root, identifiers) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item, temporary_root, identifiers) for item in value]
    if isinstance(value, str):
        for source in sorted({temporary_root, str(Path(temporary_root).resolve())}, key=len, reverse=True):
            value = value.replace(source, "/workspace")
        for source, target in identifiers.items():
            value = value.replace(source, target)
        return value
    return value


def build_showcase() -> dict:
    from reflect.preparation_pipeline import reprice_sql_store

    sessions = synthetic_sessions()
    with tempfile.TemporaryDirectory(prefix="reflect-showcase-") as directory, ExitStack() as stack:
        temp = Path(directory)
        _fixed_clocks(stack)
        conn = connect_sqlite(temp / "sample.db")
        stack.callback(conn.close)
        migrate(conn)
        def ingest(selected: list[dict], batch: str, versions: dict | None = None) -> None:
            spans = [span for session in selected for span in _events(session, versions)]
            source = temp / f"{batch}.jsonl"
            source.write_text("\n".join(json.dumps(span) for span in spans) + "\n")
            ingest_local_spans_file(conn, file_path=source, source_id=f"synthetic:reflect-showcase-v1:{batch}")
            normalized = normalize_pending_raw_events(conn)
            if normalized["failed"]:
                raise RuntimeError(f"Synthetic normalization failed: {normalized}")
            _seed_identity(conn, selected)

        ingest([session for session in sessions if not session["skill"]], "baseline")
        # Explicit illustrative rates, independent of online pricing or user aliases.
        reprice_sql_store(conn, pricing_table=PricingTable({"sample-model": ModelPricing(
            "sample-model", 0.000002, 0.000008, 0.000002, 0.0000005)}, "synthetic-fixture", 0), aliases={})
        rebuild_rollups(conn)
        rebuild_graph(conn)
        service = ImprovementService(conn, rules=[])
        service.refresh(measure=False)
        definitions = [
            ("retry-recovery", "inspect-before-retry", PROJECTS[0], [f"demo-retry-before-{i}" for i in range(1, 4)],
             "Why did the agent retry three times?", "In each of three tasks, three unchanged pytest attempts hit the same missing dependency before a relevant edit and passing rerun.", "recovery", "tool_failure_rate", ACTIVATED),
            ("preserve-routine", "inspect-change-verify", PROJECTS[1], [f"demo-routine-{i}" for i in range(1, 4)],
             "Keep a routine that worked", "Three independent tasks inspected the relevant file, made a bounded change, and checked the result.", "proven_pattern", "read_only_exploration_calls", ACTIVATED),
            ("insufficient-evidence", "verify-after-change", PROJECTS[2], ["demo-insufficient-1"],
             "Installed is not the same as effective", "One observed task is available. Comparable baseline and later tasks are still missing.", "verification", "unverified_change_sessions", "2026-11-07T00:00:00+00:00"),
        ]
        scenarios, previews = [], {}
        for sid, slug, project, ids, title, description, behavior, metric, clock in definitions:
            observation, candidate, preview = _workflow(service, slug=slug, project=project, sessions=ids,
                title=title, summary=description, behavior=behavior, metric=metric, clock=clock, temp=temp)
            previews[candidate] = preview
            scenarios.append({"id": sid, "title": title, "description": description,
                              "session_id": ids[0], "finding_id": observation, "workflow_id": candidate,
                              "before_session_ids": ids, "after_session_ids": [f"demo-retry-after-{i}" for i in range(1, 6)] if sid == "retry-recovery" else [],
                              "review_summary": "Recorded synthetic review: inspect the exact procedure and proposed skill file.",
                              "preview": _preview(next(item for item in sessions if item["id"] == ids[0]))})
        service.skills.sync_workflow_candidates()
        versions = {row[0]: {"id": row[1], "content_hash": row[2]} for row in conn.execute(
            "SELECT s.slug,v.id,v.content_hash FROM skills s JOIN skill_versions v ON v.skill_id=s.id")}
        ingest([session for session in sessions if session["skill"]], "later", versions)
        reprice_sql_store(conn, pricing_table=PricingTable({"sample-model": ModelPricing(
            "sample-model", 0.000002, 0.000008, 0.000002, 0.0000005)}, "synthetic-fixture", 0), aliases={})
        rebuild_rollups(conn)
        rebuild_graph(conn)
        # Mark processed inputs before comparison, as the normal refresh coordinator does.
        conn.execute("DELETE FROM pending_refresh")
        conn.commit()
        service.refresh(measure=False)
        conn.execute("DELETE FROM pending_refresh")
        for scenario in scenarios:
            measurement = service.measurements.measure(scenario["workflow_id"])
            scenario["impact_id"] = measurement["id"]
            scenario["result_summary"] = ("Illustrative observed comparison; inspect its exact tasks and attribution limits."
                if measurement["verdict"] in {"improved", "no_effect", "regressed"} else "There is not enough comparable evidence to claim improvement.")
        service.skills.refresh()
        for scenario in scenarios:
            scenario["skill_id"] = service.skills.skill_for_candidate(scenario["workflow_id"]).id
        conn.execute("DELETE FROM pending_refresh")
        for key in ("last_successful_refresh", "last_successful_usage_refresh"):
            conn.execute("INSERT OR REPLACE INTO store_metadata(key,value,updated_at) VALUES(?,?,?)", (key, CLOCK, CLOCK))
        mark_snapshot(conn, ready=True)
        assessments = {row[0]: json.loads(row[1])["assessment"] for row in conn.execute(
            "SELECT session_id,boundary_json FROM execution_units WHERE eligible=1")}
        adapter = DashboardImprovementAdapter(temp / "sample.db")
        bundle = build_dashboard_payload(temp / "sample.db", limit=100)
        resources = {
            "/api/findings": adapter.findings(status=None, include_resolved=True, limit=100),
            "/api/workflows": adapter.workflows(behavior_type=None, status=None),
            "/api/impact": adapter.impact(), "/api/rules": adapter.rules(),
            "/api/loops": adapter.loops(kind=None, status=None, limit=100),
            "/api/skills": adapter.skills(status=None, include_stale=True, limit=100),
            "/api/explore/usage": build_dashboard_payload(temp / "sample.db", limit=100, lazy_heavy_tabs=True,
                include_comparison=False, base_tab_names={"activity", "models", "costs", "usage_tools", "mcp"}),
        }
        for view in ("tools", "graph", "context"):
            resources[f"/api/explore/{view}"] = build_explore_payload(temp / "sample.db", view)
        for session in sessions:
            resources[f"/api/session/{session['id']}"] = load_session_detail(temp / "sample.db", session["id"])
        for scenario in scenarios:
            finding, workflow, skill, impact = (scenario[key] for key in ("finding_id", "workflow_id", "skill_id", "impact_id"))
            resources[f"/api/findings/{finding}"] = adapter.finding(finding)
            resources[f"/api/findings/{finding}/evidence"] = adapter.finding_evidence(finding, limit=100)
            resources[f"/api/workflows/{workflow}/preview"] = previews[workflow]
            resources[f"/api/workflows/{workflow}/evidence"] = adapter.workflow_evidence(workflow, limit=100)
            resources[f"/api/skills/{skill}"] = adapter.skill(skill)
            resources[f"/api/skills/{skill}/usage-graph"] = adapter.skill_usage_graph(skill, days=None, now=datetime.fromisoformat(CLOCK))
            resources[f"/api/impact/{impact}/evidence"] = adapter.impact_evidence(impact)
        for loop in resources["/api/loops"]["loops"]:
            resources[f"/api/loops/{loop['id']}"] = adapter.loop(loop["id"])
        bundle["demo"] = {"schema_version": 1, "clock": CLOCK, "label": "Illustrative sample",
                          "provenance": "synthetic", "pricing_note": "Fictional model and illustrative rates; not a provider quote.",
                          "default_scenario": "retry-recovery", "scenarios": scenarios,
                          "assessments": assessments, "resources": resources}
        # Installation IDs use resolved filesystem paths in the application.
        # The export has fictional paths, so remap those identifiers consistently
        # rather than exposing a random temporary directory through their hashes.
        identifiers = {row[0]: "skill_installation_" + hashlib.sha256(
            f"{row[1]}:{row[2]}".encode()).hexdigest()[:24]
            for row in conn.execute("SELECT id,skill_id,target_kind FROM skill_installations")}
        return _clean(bundle, directory, identifiers)


def serialize(bundle: dict) -> str:
    return json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the committed synthetic bundle has drifted")
    args = parser.parse_args()
    rendered = serialize(build_showcase())
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != rendered:
            print("Synthetic showcase is stale; run scripts/build_showcase.py", file=sys.stderr)
            return 1
    else:
        OUTPUT.write_text(rendered)
        print(f"Wrote {OUTPUT.relative_to(ROOT)} ({len(rendered):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
