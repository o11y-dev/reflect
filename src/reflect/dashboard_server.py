from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from reflect.dashboard_queries import (
    EXPLORE_VIEW_TABS,
    build_dashboard_payload,
    build_explore_payload,
    build_session_payload,
    canonical_explore_view,
    load_session_detail,
    parse_dashboard_scope,
)
from reflect.preparation import (
    PreparationCoordinator,
    PreparationSnapshot,
    PreparationState,
)
from reflect.utils import logger


class DashboardDataCache:
    """Publish one immutable dashboard payload at a time."""

    def __init__(
        self,
        loader: Callable[[], dict[str, object]],
        *,
        refresh_loader: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self._refresh_loader = refresh_loader or loader
        self._lock = threading.Lock()
        self._payload = loader()

    def get(self) -> dict[str, object]:
        with self._lock:
            return self._payload

    def refresh(self) -> dict[str, object]:
        payload = self._refresh_loader()
        with self._lock:
            self._payload = payload
        return payload


def _perf_start() -> float:
    return time.perf_counter() if os.environ.get("REFLECT_DEBUG_PERF") else 0.0


def _perf_finish(name: str, start: float, **fields: object) -> None:
    if not start:
        return
    duration_ms = (time.perf_counter() - start) * 1000
    field_text = " ".join(
        f"{key}={value}" for key, value in fields.items() if value not in (None, "")
    )
    logger.info(
        "reflect.dashboard.perf %s duration_ms=%.1f%s",
        name,
        duration_ms,
        f" {field_text}" if field_text else "",
    )


def _dashboard_docs_dir() -> Path:
    repo_docs = Path(__file__).resolve().parents[2] / "docs"
    if (repo_docs / "report.html").exists():
        return repo_docs
    package_data = Path(__file__).resolve().parent / "data"
    if (package_data / "index.html").exists():
        return package_data
    return repo_docs


def start_publish_server(
    *,
    db_path: Path,
    preparation_worker: PreparationCoordinator | None = None,
    open_browser: bool = True,
) -> None:
    """Start a local FastAPI server and open the dashboard in a browser.

    Blocks until Ctrl-C. Uses ``?report=api/data`` so the dashboard
    fetches JSON from the API — no URL encoding at all.
    """
    port = int(os.environ.get("REFLECT_PORT", "8765"))
    docs_dir = _dashboard_docs_dir()
    _serve_dashboard(
        port,
        docs_dir,
        db_path=db_path,
        preparation_worker=preparation_worker,
        open_browser=open_browser,
    )


def build_dashboard_app(
    *,
    docs_dir: Path,
    db_path: Path,
    preparation_worker: PreparationCoordinator | None = None,
    project_root: Path | None = None,
):
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    globals()["Request"] = Request

    app = FastAPI(title="reflect dashboard", docs_url=None, redoc_url=None)
    workflow_project_root = (project_root or Path.cwd()).expanduser().resolve()

    def resolve_workflow_project_root(value: object = None) -> Path:
        requested = str(value or "").strip()
        return Path(requested).expanduser().resolve() if requested else workflow_project_root

    if preparation_worker is not None:
        dashboard_cache = DashboardDataCache(
            lambda: build_dashboard_payload(
                db_path,
                limit=50,
                offset=0,
                lazy_all_tabs=True,
            ),
            refresh_loader=lambda: build_dashboard_payload(
                db_path,
                lazy_heavy_tabs=True,
                base_tab_names=set(EXPLORE_VIEW_TABS["usage"]),
            ),
        )
    else:
        dashboard_cache = DashboardDataCache(
            lambda: build_dashboard_payload(db_path, lazy_heavy_tabs=True, base_tab_names=set(EXPLORE_VIEW_TABS["usage"]))
        )
    if preparation_worker is not None:
        preparation_worker.add_completion_callback(lambda _result: dashboard_cache.refresh())

    @app.get("/api/data")
    def api_data(request: Request):
        perf_start = _perf_start()
        perf_kind = "unknown"
        params = request.query_params
        q, session_id, agents, model, status, range_name = parse_dashboard_scope(params)
        active_tab = (params.get("tab") or "sessions").strip().lower()
        explore_view = canonical_explore_view(params.get("view") or "usage")
        filtered_base_tabs = {
            ("explore", "usage"): {"activity", "models", "costs", "usage_tools", "mcp"},
            ("explore", "tools"): {"tools", "mcp"},
        }.get((active_tab, explore_view), set())
        if session_id:
            filtered_base_tabs = {"activity", "models", "costs", "tools", "mcp", "agents"}
        has_filter = any([q, session_id, agents, model != "all", status != "all", range_name != "all"])
        try:
            if not has_filter:
                perf_kind = "cached"
                return JSONResponse(dashboard_cache.get())
            if session_id and not any([q, agents, model != "all", status != "all", range_name != "all"]):
                perf_kind = "session"
                return JSONResponse(build_session_payload(db_path, session_id))
            perf_kind = "filtered"
            return JSONResponse(build_dashboard_payload(
                db_path,
                limit=50,
                offset=0,
                q=q,
                session_id=session_id,
                agents=agents,
                model=model,
                status=status,
                range_name=range_name,
                lazy_heavy_tabs=True,
                include_comparison=active_tab == "explore" and explore_view == "usage",
                base_tab_names=filtered_base_tabs,
            ))
        finally:
            _perf_finish(
                "api.data",
                perf_start,
                kind=perf_kind,
                session=bool(session_id),
                agents=",".join(sorted(agents)),
            )

    @app.get("/api/explore/{view_name}")
    def api_explore(view_name: str, request: Request):
        perf_start = _perf_start()
        q, session_id, agents, model, status, range_name = parse_dashboard_scope(request.query_params)
        try:
            return JSONResponse(
                build_explore_payload(
                    db_path,
                    view_name,
                    session_id=session_id,
                    q=q,
                    agents=agents,
                    model=model,
                    status=status,
                    range_name=range_name,
                )
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc), "view": view_name}, status_code=404)
        except Exception as exc:
            return JSONResponse({"error": str(exc), "db_path": str(db_path)}, status_code=500)
        finally:
            _perf_finish("api.explore", perf_start, view=view_name, session=bool(session_id))

    @app.get("/api/session/{session_id:path}")
    def api_session(session_id: str):
        detail = load_session_detail(db_path, session_id)
        if detail is None:
            return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)
        return JSONResponse(detail, headers={"Access-Control-Allow-Origin": "*"})

    @app.get("/api/status")
    def api_status():
        snapshot = (
            preparation_worker.snapshot()
            if preparation_worker is not None
            else PreparationSnapshot(state=PreparationState.IDLE, generation=0)
        )
        return JSONResponse({
            "preparation": snapshot.as_dict(),
            "refresh_available": preparation_worker is not None,
        })

    @app.post("/api/refresh")
    def api_refresh():
        if preparation_worker is None:
            return JSONResponse(
                {
                    "error": "This report server is snapshot-only. Start Reflect normally or use `reflect server --refresh start`.",
                    "refresh_available": False,
                },
                status_code=409,
            )
        started = preparation_worker.start()
        return JSONResponse(
            {
                "started": started,
                "refresh_available": True,
                "preparation": preparation_worker.snapshot().as_dict(),
            },
            status_code=202 if started else 200,
        )

    @app.get("/api/findings")
    def api_findings(request: Request):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        params = request.query_params
        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            status = (params.get("status") or "").strip() or None
            include_resolved = (params.get("include_resolved") or "").lower() in {
                "1",
                "true",
                "yes",
            }
            limit = min(500, max(1, int(params.get("limit") or 100)))
            findings = service.list_findings(
                limit=500,
                status=status,
                include_resolved=include_resolved,
            )
            summary = service.repository.summary(limit=0)
            if status:
                observation_record_count = summary.counts_by_status.get(status, 0)
            elif include_resolved:
                observation_record_count = sum(summary.counts_by_status.values())
            else:
                observation_record_count = sum(
                    summary.counts_by_status.get(item, 0)
                    for item in (
                        "new",
                        "acknowledged",
                        "proposal_ready",
                        "approved",
                        "active",
                        "regressed",
                    )
                )
            return JSONResponse(
                {
                    "generated_at": summary.generated_at,
                    "findings": [
                        item.model_dump(mode="json") for item in findings[:limit]
                    ],
                    "finding_total_count": len(findings),
                    "observation_record_count": observation_record_count,
                    "counts_by_status": summary.counts_by_status,
                    "pending_workflows": summary.pending_workflows,
                    "active_interventions": summary.active_interventions,
                    "verified_improvement_rate": summary.verified_improvement_rate,
                }
            )
        except (ValueError, sqlite3.Error) as exc:
            return JSONResponse({"error": str(exc), "db_path": str(db_path)}, status_code=500)
        finally:
            conn.close()

    @app.get("/api/rules")
    def api_improvement_rules():
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            rules = ImprovementService(conn, initialize_schema=False).repository.list_rule_summaries()
            return JSONResponse(
                {
                    "rules": [rule.model_dump(mode="json") for rule in rules],
                    "extension": {
                        "kind": "code_backed",
                        "module": "reflect.improvements",
                        "base_class": "BaseImprovementRule",
                        "registry": "DEFAULT_RULE_REGISTRY",
                        "registration": "RuleRegistry.register",
                    },
                }
            )
        finally:
            conn.close()

    @app.get("/api/findings/{finding_id}")
    def api_finding_detail(finding_id: str):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            observation = ImprovementService(conn, initialize_schema=False).repository.get_observation(finding_id)
            if observation is None:
                return JSONResponse({"error": f"Finding {finding_id} not found"}, status_code=404)
            return JSONResponse(observation.model_dump(mode="json"))
        finally:
            conn.close()

    @app.get("/api/findings/{observation_id}/evidence")
    def api_finding_evidence(observation_id: str, request: Request):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            ledger = ImprovementService(conn, initialize_schema=False).finding_evidence_ledger(
                observation_id,
                limit=min(200, max(1, int(request.query_params.get("limit") or 50))),
            )
            return JSONResponse(ledger.model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()

    @app.get("/api/workflows")
    def api_workflows(request: Request):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            behavior_type = request.query_params.get("type")
            status = request.query_params.get("status")
            service = ImprovementService(conn, initialize_schema=False)
            candidates = service.workflows.list(
                behavior_types={behavior_type} if behavior_type else None,
                statuses={status} if status else None,
            )
            serialized = []
            for candidate in candidates:
                item = candidate.model_dump(mode="json")
                try:
                    item["skill_id"] = service.skills.skill_for_candidate(candidate.id).id
                except KeyError:
                    item["skill_id"] = None
                serialized.append(item)
            return JSONResponse(
                {"workflows": serialized}
            )
        finally:
            conn.close()

    @app.get("/api/loops")
    def api_loops(request: Request):
        from reflect.improvements.models import LoopKind, LoopStatus
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            kind = request.query_params.get("kind")
            status = request.query_params.get("status")
            records = service.loops.list(
                kind=LoopKind(kind) if kind else None,
                status=LoopStatus(status) if status else None,
                limit=min(500, max(1, int(request.query_params.get("limit") or 100))),
            )
            return JSONResponse(
                {
                    "loops": [record.model_dump(mode="json") for record in records],
                }
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        finally:
            conn.close()

    @app.get("/api/loops/{loop_id}")
    def api_loop_detail(loop_id: str):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            return JSONResponse(service.loops.show(loop_id).model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()

    @app.get("/api/skills")
    def api_skills(request: Request):
        from reflect.improvements.models import SkillLifecycleState
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            status = request.query_params.get("status")
            include_stale = (
                request.query_params.get("include_stale") or ""
            ).lower() in {"1", "true", "yes"}
            lifecycle = SkillLifecycleState(status) if status else None
            counts_by_lifecycle = service.skills.counts_by_lifecycle()
            records = service.skills.list(
                lifecycle=lifecycle,
                include_stale=include_stale,
                limit=min(500, max(1, int(request.query_params.get("limit") or 100))),
            )
            if lifecycle:
                total_count = counts_by_lifecycle.get(lifecycle.value, 0)
            elif include_stale:
                total_count = sum(counts_by_lifecycle.values())
            else:
                total_count = sum(
                    counts_by_lifecycle.get(item.value, 0)
                    for item in (SkillLifecycleState.ACTIVE, SkillLifecycleState.PENDING)
                )
            return JSONResponse(
                {
                    "skills": [record.model_dump(mode="json") for record in records],
                    "total_count": total_count,
                    "archived_count": counts_by_lifecycle.get(SkillLifecycleState.STALE.value, 0),
                    "counts_by_lifecycle": counts_by_lifecycle,
                }
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        finally:
            conn.close()

    @app.get("/api/skills/{skill_id}")
    def api_skill_detail(skill_id: str):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            return JSONResponse(service.skills.show(skill_id).model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()

    @app.get("/api/workflows/{candidate_id}/evidence")
    def api_workflow_evidence(candidate_id: str, request: Request):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            ledger = ImprovementService(conn, initialize_schema=False).repository.workflow_evidence_ledger(
                candidate_id,
                limit=min(200, max(1, int(request.query_params.get("limit") or 50))),
            )
            return JSONResponse(ledger.model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()

    @app.get("/api/workflows/{candidate_id}/preview")
    def api_workflow_preview(candidate_id: str, request: Request):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            return JSONResponse(
                ImprovementService(conn, initialize_schema=False).workflows.preview(
                    candidate_id,
                    project_root=resolve_workflow_project_root(
                        request.query_params.get("project_root")
                    ),
                )
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (OSError, RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            conn.close()

    @app.put("/api/workflows/{candidate_id}")
    def api_workflow_edit(candidate_id: str, body: dict):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite

        content = body.get("content")
        if not isinstance(content, dict):
            return JSONResponse({"error": "A structured workflow content object is required"}, status_code=422)
        conn = connect_sqlite(db_path)
        try:
            candidate = ImprovementService(conn, initialize_schema=False).workflows.edit(
                candidate_id, content=content
            )
            return JSONResponse(candidate.model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            conn.close()

    @app.post("/api/workflows/{candidate_id}/apply")
    def api_workflow_apply(candidate_id: str, body: dict | None = None):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite

        conn = connect_sqlite(db_path)
        try:
            return JSONResponse(
                ImprovementService(conn, initialize_schema=False).workflows.apply(
                    candidate_id,
                    project_root=resolve_workflow_project_root((body or {}).get("project_root")),
                )
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (OSError, RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            conn.close()

    @app.post("/api/workflows/{candidate_id}/rollback")
    def api_workflow_rollback(candidate_id: str):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite

        conn = connect_sqlite(db_path)
        try:
            return JSONResponse(
                ImprovementService(conn, initialize_schema=False).workflows.rollback(candidate_id)
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            conn.close()

    @app.post("/api/workflows/{candidate_id}/reject")
    def api_workflow_reject(candidate_id: str, body: dict | None = None):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite

        conn = connect_sqlite(db_path)
        try:
            candidate = ImprovementService(conn, initialize_schema=False).workflows.reject(
                candidate_id,
                reason=str((body or {}).get("reason") or "operator_rejected")[:200],
            )
            return JSONResponse(candidate.model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            conn.close()

    @app.post("/api/feedback/{session_id:path}")
    def api_session_feedback(session_id: str, body: dict):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite

        outcome = str(body.get("outcome") or "")
        reason = body.get("reason")
        conn = connect_sqlite(db_path)
        try:
            feedback_id = ImprovementService(conn, initialize_schema=False).repository.record_feedback(
                session_id,
                outcome,
                reason_redacted=str(reason) if reason is not None else None,
            )
            return JSONResponse(
                {"id": feedback_id, "session_id": session_id, "outcome": outcome},
                status_code=201,
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        finally:
            conn.close()

    @app.get("/api/impact")
    def api_impact():
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            return JSONResponse({"impact_checks": service.measurements.list()})
        finally:
            conn.close()

    @app.get("/api/impact/{impact_id}/evidence")
    def api_impact_evidence(impact_id: str):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            return JSONResponse(service.measurements.sessions(impact_id))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()

    @app.get("/")
    def index():
        html_file = docs_dir / "report.html"
        if not html_file.exists():
            html_file = docs_dir / "index.html"
        return FileResponse(html_file, media_type="text/html")

    if docs_dir.exists():
        app.mount("/", StaticFiles(directory=str(docs_dir)), name="static")

    return app


def _serve_dashboard(
    port: int,
    docs_dir: Path,
    *,
    db_path: Path,
    preparation_worker: PreparationCoordinator | None = None,
    open_browser: bool = True,
) -> None:
    """Inline FastAPI server for the local `reflect` browser report."""
    import threading
    import webbrowser

    try:
        import uvicorn
        __import__("fastapi")
    except ImportError:
        logger.warning("FastAPI/uvicorn not installed. Install with: pip install fastapi uvicorn")
        logger.warning("Falling back to writing artifact file...")
        artifact = docs_dir / "_reflect_data.json"
        artifact.write_text(json.dumps(build_dashboard_payload(db_path)), encoding="utf-8")
        print(f"Wrote: {artifact}")
        return

    app = build_dashboard_app(
        docs_dir=docs_dir,
        db_path=db_path,
        preparation_worker=preparation_worker,
    )
    url = f"http://127.0.0.1:{port}/?report=api/data"
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()
    print(f"\n  Serving at: {url}")
    print("  Press Ctrl-C to stop\n")
    if preparation_worker is not None:
        preparation_worker.start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        if preparation_worker is not None:
            preparation_worker.close()
