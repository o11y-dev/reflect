from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from reflect.dashboard_explore import (
    build_explore_payload,
    canonical_explore_view,
    parse_dashboard_scope,
)
from reflect.dashboard_improvements import DashboardImprovementAdapter
from reflect.dashboard_queries import build_dashboard_payload
from reflect.dashboard_read_executor import (
    DashboardReadBusyError,
    DashboardReadExecutor,
    read_revision,
)
from reflect.dashboard_sessions import build_session_payload, load_session_detail
from reflect.improvements.service import FindingWorkflowConflict
from reflect.store.sqlite import (
    SnapshotNotReadyError,
    is_sqlite_busy,
)
from reflect.utils import _json_dumps, _json_loads, logger


def _persist_dashboard_json(path: Path, document: dict[str, object]) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(_json_dumps(document))
        temporary.replace(path)
    except OSError:
        logger.warning("Could not persist the published dashboard", exc_info=True)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _impact_recorded_at(payload: dict | None) -> str:
    return max((str(item.get("measured_at") or "")
                for item in (payload or {}).get("impact_checks", [])), default="")


class DashboardResponseCache:
    """Retain bounded, exact-scope read responses while their next snapshot prepares."""

    _READ_PATH = re.compile(
        r"^/api/(?:data|rules|explore/(?:usage|tools|graph|context)"
        r"|findings(?:/[^/]+(?:/evidence)?)?|loops(?:/[^/]+)?"
        r"|workflows(?:/[^/]+/evidence)?|impact(?:/[^/]+/evidence)?|skills(?:/[^/]+(?:/usage-graph)?)?)$"
    )

    def __init__(
        self, cache_path: Path, *, cache_key: str, default_range: str,
        max_entries: int = 64, max_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self._path = cache_path
        self._key = cache_key
        self._default_range = default_range
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._read_orders: dict[str, int] = {}
        self._jobs: dict[str, Future] = {}
        self._sequence = 0
        self._invalidated_before: dict[str, int] = {}
        self._size = 0
        try:
            if cache_path.stat().st_size > max_bytes + 4096:
                return
            saved = _json_loads(cache_path.read_bytes())
            if saved.get("format") != 1 or saved.get("key") != cache_key:
                return
            for key, entry in saved.get("entries", {}).items():
                if (isinstance(key, str) and isinstance(entry, dict)
                        and isinstance(entry.get("payload"), dict)
                        and isinstance(entry.get("loaded_at"), str) and not entry.get("memory_only")):
                    self._insert(key, _json_dumps(entry))
        except (OSError, ValueError, AttributeError, TypeError):
            self._entries.clear()
            self._read_orders.clear()
            self._size = 0

    def start_read(self) -> int:
        """Order reads before their queries, not by when slower responses finish."""
        with self._lock:
            self._sequence += 1
            return self._sequence

    def invalidate(self) -> None:
        """Review edits supersede workflow/finding reads, not retained telemetry."""
        prefixes = ("/api/workflows", "/api/findings")

        def affected(key: str) -> bool:
            path = _json_loads(key.removeprefix("recorded:"))[0]
            return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)

        with self._lock:
            self._sequence += 1
            self._invalidated_before.update(dict.fromkeys(prefixes, self._sequence))
            for key in list(self._entries):
                if affected(key):
                    encoded = self._entries.pop(key)
                    self._size -= len(key.encode()) + len(encoded.encode())
                    self._read_orders.pop(key, None)
            for key in list(self._jobs):
                if affected(key):
                    self._jobs.pop(key)
            _persist_dashboard_json(self._path, {
                "format": 1, "key": self._key,
                "entries": {key: entry for key, value in self._entries.items()
                            if not (entry := _json_loads(value)).get("memory_only")},
            })

    def key(self, path: str, params, *, method: str = "GET") -> str | None:
        if method != "GET" or not self._READ_PATH.fullmatch(path):
            return None
        # Routes use the last value of repeated parameters. Preserve that
        # meaning while making query ordering irrelevant.
        query = dict(params)
        if path == "/api/data" or path.startswith("/api/explore/"):
            query["range"] = query.get("range") or (
                "all" if (query.get("session") or "").strip() else self._default_range
            )
        return _json_dumps([path, sorted(query.items())])

    def _insert(self, key: str, encoded: str) -> bool:
        size = len(key.encode()) + len(encoded.encode())
        if size > self._max_bytes:
            return False
        old = self._entries.pop(key, None)
        if old is not None:
            self._size -= len(key.encode()) + len(old.encode())
        self._entries[key] = encoded
        self._size += size
        while len(self._entries) > self._max_entries or self._size > self._max_bytes:
            old_key, old_value = self._entries.popitem(last=False)
            self._read_orders.pop(old_key, None)
            self._size -= len(old_key.encode()) + len(old_value.encode())
        return True

    def put(
        self, key: str | None, payload: dict[str, object], *, read_order: int | None = None,
        read_revision: tuple[str, ...] | None = None, recorded: bool = False,
    ) -> None:
        publication = payload.get("publication")
        if (key is None or payload.get("analysis_state") == "pending" and not recorded
                or isinstance(publication, dict) and publication.get("state") == "stale"):
            return
        read_order = self.start_read() if read_order is None else read_order
        with self._lock:
            if self._invalidated_before:
                path = _json_loads(key)[0]
                if any(read_order < order and (path == prefix or path.startswith(prefix + "/"))
                       for prefix, order in self._invalidated_before.items()):
                    return
            recorded_key = f"recorded:{key}"
            if read_order < max(self._read_orders.get(key, 0), self._read_orders.get(recorded_key, 0)):
                return
            key = recorded_key if recorded else key
            old = self._entries.get(key)
            if old is not None:
                self._read_orders[key] = read_order
            previous = _json_loads(old) if old is not None else None
            revision = list(read_revision) if read_revision is not None else None
            if previous is not None and previous["payload"] == payload:
                if previous.get("read_revision") == revision:
                    return
                entry = {**previous, "read_revision": revision}
            else:
                entry = {
                    "payload": payload,
                    "loaded_at": datetime.now(UTC).isoformat(),
                    "snapshot_completed_at": payload.get("snapshot_completed_at") or payload.get("generated_at"),
                    "read_revision": revision,
                    "memory_only": recorded,
                }
            if not self._insert(key, _json_dumps(entry)):
                return
            if key in self._entries:
                self._read_orders[key] = read_order
            if not recorded:
                _persist_dashboard_json(self._path, {
                    "format": 1, "key": self._key,
                    "entries": {
                        name: entry for name, value in self._entries.items()
                        if not (entry := _json_loads(value)).get("memory_only")
                    },
                })

    def current(self, key: str, read_revision: tuple[str, ...]) -> dict | None:
        with self._lock:
            entries = [self._entries.get(name) for name in (key, f"recorded:{key}")]
        for encoded in entries:
            entry = _json_loads(encoded) if encoded is not None else None
            if entry is not None and entry.get("read_revision") == list(read_revision):
                return entry["payload"]
        return None

    def prepare(
        self, key: str, read_revision: tuple[str, ...], *, read_order: int,
        submit: Callable[[], Future], current_revision: Callable[[], tuple[str, ...]],
        allow_recorded: bool = False,
    ) -> Future:
        """Coalesce a cold scope while the existing read executor computes it."""
        with self._lock:
            running = self._jobs.get(key)
            if running is not None:
                return running
            future = submit()
            self._jobs[key] = future

        def publish(completed: Future) -> None:
            try:
                payload = completed.result()
                recorded = (
                    allow_recorded and payload.get("analysis_state") == "pending"
                    and payload.get("evidence_state") == "recorded"
                    and payload.get("publication", {}).get("state") == "recorded"
                )
                valid = recorded or payload.get("analysis_state") in {"ready", "preparing", "historical_pending"}
                if valid and current_revision() == read_revision:
                    self.put(key, payload, read_order=read_order, read_revision=read_revision, recorded=recorded)
            except (CancelledError, SnapshotNotReadyError):
                pass
            except Exception:
                logger.warning("Could not prepare the report read", exc_info=True)
            finally:
                with self._lock:
                    if self._jobs.get(key) is completed:
                        self._jobs.pop(key)

        future.add_done_callback(publish)
        return future

    def stale(
        self, key: str | None, *, reason: str, fallback: dict[str, object] | None = None,
        allow_recorded: bool = False,
    ) -> dict[str, object] | None:
        with self._lock:
            encoded = self._entries.get(key) if key is not None else None
            recorded = self._entries.get(f"recorded:{key}") if allow_recorded and key is not None else None
        if (encoded is not None and recorded is not None
                and _impact_recorded_at(_json_loads(recorded)["payload"])
                > _impact_recorded_at(_json_loads(encoded)["payload"])):
            encoded = None
        if encoded is None and recorded is not None and fallback is None:
            # A cold visit can retain a schema-ready evidence read until the
            # current refresh publishes. Keep its pending/recorded semantics.
            return _json_loads(recorded)["payload"]
        if encoded is None and fallback is None:
            return None
        entry = _json_loads(encoded) if encoded is not None else None
        if fallback is not None and (entry is None or entry["payload"] != fallback):
            # The default dashboard may have published a newer generation
            # since this exact tab URL was last requested. Its read time is
            # unknown; retain its genuine publication time instead.
            entry = {
                "payload": fallback, "loaded_at": None,
                "snapshot_completed_at": fallback.get("snapshot_completed_at"),
            }
        return {
            **entry["payload"],
            "analysis_state": "stale",
            "publication": {
                "state": "stale", "reason": reason,
                "loaded_at": entry["loaded_at"],
                "snapshot_completed_at": entry.get("snapshot_completed_at"),
            },
        }


def _retain_skill_analysis(current: dict, retained: dict | None) -> dict:
    """Overlay recorded analysis only; registry and package state stay live."""
    if retained is None:
        return current
    counts = ("evidence_count", "usage_count", "measurement_count")
    if "skills" in current:
        previous = {skill["id"]: skill for skill in retained.get("skills", [])}
        skills = [
            {
                **skill,
                **{field: previous[skill["id"]][field] for field in counts},
                "analysis_state": "stale", "evidence_state": "published",
            } if skill["id"] in previous else {**skill, "analysis_state": "pending", "evidence_state": "recorded"}
            for skill in current["skills"]
        ]
        if not any(skill.get("analysis_state") == "stale" for skill in skills):
            return current
        current = {**current, "skills": skills}
    elif current.get("skill", {}).get("id") == retained.get("skill", {}).get("id"):
        current = {
            **current,
            "skill": {
                **current["skill"],
                **{field: retained["skill"][field] for field in counts},
                "analysis_state": "stale", "evidence_state": "published",
            },
            **{field: retained[field] for field in ("evidence", "usage_occurrences", "measurements")},
        }
    else:
        return current
    return {**current, "analysis_state": "stale", "evidence_state": "published", "publication": retained["publication"]}


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


def _bounded_query_limit(value: object, *, default: int, maximum: int) -> int:
    return min(maximum, max(1, int(value or default)))


def start_publish_server(
    *,
    db_path: Path,
    raw_storage_status_loader: Callable[[], dict[str, object]] | None = None,
    open_browser: bool = True,
    backend_runner=None,
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
        raw_storage_status_loader=raw_storage_status_loader,
        open_browser=open_browser,
        backend_runner=backend_runner,
    )


def build_dashboard_app(
    *,
    docs_dir: Path,
    db_path: Path,
    raw_storage_status_loader: Callable[[], dict[str, object]] | None = None,
    project_root: Path | None = None,
    read_executor: DashboardReadExecutor | None = None,
):
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.concurrency import run_in_threadpool

    globals()["Request"] = Request

    # Agent OTLP environment belongs to capture. The report must not create
    # its own exporters (FastAPI may otherwise treat a gRPC endpoint as HTTP).
    app = FastAPI(
        title="reflect dashboard", docs_url=None, redoc_url=None,
        telemetry={"auto_configure": False},
    )

    async def read_query(operation, *args, **kwargs):
        if read_executor is not None:
            return await read_executor.read(operation, db_path, *args, **kwargs)
        builder = {
            "dashboard": build_dashboard_payload,
            "explore": build_explore_payload,
            "session": build_session_payload,
            "conversation": load_session_detail,
        }[operation]
        def read_pinned():
            with SnapshotReadContext(db_path):
                return builder(db_path, *args, **kwargs)
        return await run_in_threadpool(read_pinned)

    @app.exception_handler(DashboardReadBusyError)
    async def read_busy(request, exc):
        key = response_cache.key(request.url.path, request.query_params, method=request.method)
        cached = response_cache.stale(key, reason="busy")
        if cached is not None and PublishedSnapshotStore(db_path).current() is not None:
            return JSONResponse(cached, headers={"X-Reflect-Publication": "stale"})
        return JSONResponse(
            {"error": str(exc), "state": "busy", "reason": "reading"},
            status_code=503, headers={"Retry-After": "2"},
        )

    @app.exception_handler(SnapshotNotReadyError)
    async def snapshot_not_ready(request, exc):
        key = response_cache.key(request.url.path, request.query_params, method=request.method)
        cached = response_cache.stale(key, reason=exc.state)
        if cached is not None and PublishedSnapshotStore(db_path).current() is not None:
            return JSONResponse(cached, headers={"X-Reflect-Publication": "stale"})
        return JSONResponse(
            {"error": str(exc), "state": exc.state},
            status_code=503 if exc.state == "preparing" else 409,
            headers={"Retry-After": "2"} if exc.state == "preparing" else {},
        )
    from reflect.store.snapshots import PublishedSnapshotStore, SnapshotReadContext

    @app.middleware("http")
    async def await_initial_snapshot(request, call_next):
        request.state.dashboard_read_order = response_cache.start_read()
        operational = request.url.path in {"/api/status", "/api/refresh", "/api/backend", "/api/database/health", "/api/capture/status"} or request.url.path.startswith("/api/jobs")
        submission = request.method == 'POST' and bool(re.fullmatch(
            r'/api/(?:skills/(?:discover|sync)|maintenance(?:/[^/]+)+|database/[^/]+|memories/sync|loops/[^/]+/build)', request.url.path))
        if request.url.path.startswith("/api/") and not operational and not submission and PublishedSnapshotStore(db_path).current() is None:
            return JSONResponse({"error": "The first completed report is preparing", "state": "preparing", "reason": "initial_publication",
                                 "status_url": "/api/status"}, status_code=503, headers={"Retry-After": "2"})
        return await call_next(request)

    workflow_project_root = (project_root or Path.cwd()).expanduser().resolve()
    improvement_adapter = DashboardImprovementAdapter(db_path)

    def resolve_workflow_project_root(value: object = None) -> Path:
        requested = str(value or "").strip()
        return Path(requested).expanduser().resolve() if requested else workflow_project_root

    from reflect.config import analysis_window_days
    from reflect.store.migrate import load_migrations

    default_range = f"{analysis_window_days()}d"
    schema_version = max(migration.version for migration in load_migrations())
    response_cache = DashboardResponseCache(
        db_path.with_suffix(".dashboard-responses.json"),
        cache_key=f"{db_path.resolve()}:{default_range}:schema-{schema_version}",
        default_range=default_range,
    )

    def read_response(request, payload):
        key = response_cache.key(request.url.path, request.query_params, method=request.method)
        if payload.get("analysis_state") == "pending":
            recorded_inspection = bool(re.fullmatch(
                r"/api/(?:(?:workflows|impact)(?:/[^/]+/evidence)?|skills/[^/]+/usage-graph)", request.url.path,
            ))
            retained = response_cache.stale(key, reason="preparing", allow_recorded=recorded_inspection)
            if (request.url.path == "/api/impact"
                    and _impact_recorded_at(payload) > _impact_recorded_at(retained)):
                retained = None
            skill_registry = request.url.path.startswith("/api/skills") and not request.url.path.endswith("/usage-graph")
            payload = _retain_skill_analysis(payload, retained) if skill_registry else retained or payload
            if (retained is None and recorded_inspection
                    and payload.get("evidence_state") == "recorded"):
                response_cache.put(key, payload, read_order=request.state.dashboard_read_order, recorded=True)
        else:
            response_cache.put(key, payload, read_order=request.state.dashboard_read_order)
        publication_state = payload.get("publication", {}).get("state")
        headers = {"X-Reflect-Publication": publication_state} if publication_state else {}
        return JSONResponse(payload, headers=headers)

    def requested_scope(params):
        scope = list(parse_dashboard_scope(params))
        if not params.get("range") and not scope[1]:
            scope[-1] = default_range
        return scope

    async def prepared_response(request, operation, *args, **scope):
        revision = await run_in_threadpool(read_revision, db_path, operation)
        key = response_cache.key(request.url.path, request.query_params)
        retained = response_cache.stale(key, reason="reading")
        current = response_cache.current(key, revision)
        if current is not None:
            return JSONResponse(current)
        future = response_cache.prepare(
            key, revision, read_order=request.state.dashboard_read_order,
            submit=lambda: read_executor.submit(operation, db_path, *args, read_revision=revision, **scope),
            current_revision=lambda: read_revision(db_path, operation), allow_recorded=operation == "graph",
        )
        if retained is not None:
            return JSONResponse(retained, headers={"X-Reflect-Publication": "stale"})
        # Small reads can finish in this request. A large cold read keeps running
        # once in the bounded executor; polling shares its publication.
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        completed, _ = await asyncio.wait({wrapped}, timeout=1.0)
        if completed:
            wrapped.result()
        revision = await run_in_threadpool(read_revision, db_path, operation)
        current = response_cache.current(key, revision)
        if current is not None:
            return JSONResponse(current)
        return JSONResponse(
            {"state": "preparing", "reason": "reading",
             "error": "This report read is preparing in the background. Please retry."},
            status_code=503, headers={"Retry-After": "2"},
        )

    @app.get("/api/data")
    async def api_data(request: Request):
        perf_start = _perf_start()
        perf_kind = "unknown"
        params = request.query_params
        q, session_id, agents, model, status, range_name = requested_scope(params)
        active_tab = (params.get("tab") or "sessions").strip().lower()
        explore_view = canonical_explore_view(params.get("view") or "usage")
        usage_view = active_tab == "explore" and explore_view == "usage"
        filtered_base_tabs = {
            ("explore", "usage"): {"activity", "models", "costs", "usage_tools", "mcp"},
            ("explore", "tools"): {"tools", "mcp"},
        }.get((active_tab, explore_view), set())
        if session_id and not usage_view:
            filtered_base_tabs = {"activity", "models", "costs", "tools", "mcp", "agents"}
        try:
            if session_id and not usage_view and not any(
                [q, agents, model != "all", status != "all", range_name != "all"]
            ):
                perf_kind = "session"
                if read_executor is not None:
                    return await prepared_response(request, "session", session_id)
                return read_response(request, await read_query("session", session_id))
            perf_kind = "filtered"
            filters = {
                "limit": 50, "offset": 0, "q": q, "session_id": session_id, "agents": agents,
                "model": model, "status": status, "range_name": range_name, "lazy_heavy_tabs": True,
                "include_comparison": usage_view, "base_tab_names": filtered_base_tabs,
            }
            if read_executor is not None:
                return await prepared_response(request, "dashboard", **filters)
            return read_response(request, await read_query("dashboard", **filters))
        finally:
            _perf_finish(
                "api.data",
                perf_start,
                kind=perf_kind,
                session=bool(session_id),
                agents=",".join(sorted(agents)),
            )

    @app.get("/api/explore/{view_name}")
    async def api_explore(view_name: str, request: Request):
        perf_start = _perf_start()
        q, session_id, agents, model, status, range_name = requested_scope(
            request.query_params
        )
        try:
            if view_name in {"tools", "graph"} and read_executor is not None:
                return await prepared_response(
                    request, view_name, session_id=session_id, q=q, agents=agents,
                    model=model, status=status, range_name=range_name,
                )
            return read_response(request,
                await read_query(
                    "explore",
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
        except (SnapshotNotReadyError, DashboardReadBusyError):
            raise
        except Exception as exc:
            return JSONResponse({"error": str(exc), "db_path": str(db_path)}, status_code=500)
        finally:
            _perf_finish("api.explore", perf_start, view=view_name, session=bool(session_id))

    @app.get("/api/session/{session_id:path}")
    async def api_session(session_id: str):
        detail = await read_query("conversation", session_id)
        if detail is None:
            return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)
        return JSONResponse(detail)

    @app.get("/api/findings")
    def api_findings(request: Request):
        params = request.query_params
        try:
            return read_response(request,
                improvement_adapter.findings(
                    status=(params.get("status") or "").strip() or None,
                    include_resolved=(params.get("include_resolved") or "").lower()
                    in {"1", "true", "yes"},
                    limit=_bounded_query_limit(params.get("limit"), default=100, maximum=500),
                )
            )
        except SnapshotNotReadyError:
            raise
        except (ValueError, sqlite3.Error) as exc:
            return JSONResponse({"error": str(exc), "db_path": str(db_path)}, status_code=500)

    @app.get("/api/rules")
    def api_improvement_rules(request: Request):
        return read_response(request, improvement_adapter.rules())

    @app.get("/api/findings/{finding_id}")
    def api_finding_detail(finding_id: str, request: Request):
        observation = improvement_adapter.finding(finding_id)
        if observation is None:
            return JSONResponse({"error": f"Finding {finding_id} not found"}, status_code=404)
        return read_response(request, observation)

    @app.get("/api/findings/{observation_id}/evidence")
    def api_finding_evidence(observation_id: str, request: Request):
        try:
            return read_response(request,
                improvement_adapter.finding_evidence(
                    observation_id,
                    limit=_bounded_query_limit(
                        request.query_params.get("limit"), default=50, maximum=200
                    ),
                )
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    @app.get("/api/workflows")
    def api_workflows(request: Request):
        return read_response(request,
            improvement_adapter.workflows(
                behavior_type=request.query_params.get("type"),
                status=request.query_params.get("status"),
            )
        )

    @app.get("/api/loops")
    def api_loops(request: Request):
        try:
            return read_response(request,
                improvement_adapter.loops(
                    kind=request.query_params.get("kind"),
                    status=request.query_params.get("status"),
                    limit=_bounded_query_limit(
                        request.query_params.get("limit"), default=100, maximum=500
                    ),
                )
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @app.get("/api/loops/{loop_id}")
    def api_loop_detail(loop_id: str, request: Request):
        try:
            return read_response(request, improvement_adapter.loop(loop_id))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    @app.get("/api/skills")
    def api_skills(request: Request):
        try:
            return read_response(request,
                improvement_adapter.skills(
                    status=request.query_params.get("status"),
                    include_stale=(request.query_params.get("include_stale") or "").lower()
                    in {"1", "true", "yes"},
                    limit=_bounded_query_limit(
                        request.query_params.get("limit"), default=100, maximum=500
                    ),
                )
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @app.get("/api/skills/{skill_id}")
    def api_skill_detail(skill_id: str, request: Request):
        try:
            return read_response(request, improvement_adapter.skill(skill_id))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    @app.get("/api/skills/{skill_id}/usage-graph")
    def api_skill_usage_graph(skill_id: str, request: Request):
        params = request.query_params
        try:
            return read_response(request, improvement_adapter.skill_usage_graph(
                skill_id, days=None if params.get("days") == "all" else int(params.get("days", "7")), repo_id=params.get("repo"),
                agents={name.strip() for name in params.get("agents", "").split(",") if name.strip()} or None,
                version=params.get("version"), offset=int(params.get("offset", "0")),
                limit=int(params.get("limit", "20")), execution_unit_id=params.get("execution"),
                session_id=params.get("session"), evidence_offset=int(params.get("evidence_offset", "0")),
            ))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @app.get("/api/workflows/{candidate_id}/evidence")
    def api_workflow_evidence(candidate_id: str, request: Request):
        try:
            return read_response(request,
                improvement_adapter.workflow_evidence(
                    candidate_id,
                    limit=_bounded_query_limit(
                        request.query_params.get("limit"), default=50, maximum=200
                    ),
                )
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    @app.get("/api/workflows/{candidate_id}/preview")
    def api_workflow_preview(candidate_id: str, request: Request):
        try:
            return JSONResponse(
                improvement_adapter.workflow_preview(
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

    @app.get("/api/findings/{finding_id}/workflow-draft")
    def api_finding_workflow_draft(finding_id: str):
        try:
            return JSONResponse(improvement_adapter.finding_workflow_context(finding_id))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    @app.post("/api/workflows")
    def api_workflow_create(body: dict):
        content = body.get("content")
        if not isinstance(content, dict):
            return JSONResponse({"error": "Workflow instructions are required"}, status_code=422)
        finding_id = body.get("finding_id")
        if finding_id is not None and (not isinstance(finding_id, str) or not finding_id.strip()):
            return JSONResponse({"error": "finding_id must be a nonempty identifier"}, status_code=422)
        try:
            return JSONResponse(improvement_adapter.create_workflow(content=content, finding_id=finding_id), status_code=201)
        except FindingWorkflowConflict as exc:
            return JSONResponse({"error": str(exc), "candidate_id": exc.candidate_id}, status_code=409)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.put("/api/workflows/{candidate_id}")
    def api_workflow_edit(candidate_id: str, body: dict):
        content = body.get("content")
        if not isinstance(content, dict):
            return JSONResponse(
                {"error": "A structured workflow content object is required"},
                status_code=422,
            )
        try:
            return JSONResponse(improvement_adapter.edit_workflow(candidate_id, content=content))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/workflows/{candidate_id}/apply")
    def api_workflow_apply(candidate_id: str, body: dict | None = None):
        try:
            return JSONResponse(
                improvement_adapter.apply_workflow(
                    candidate_id,
                    project_root=resolve_workflow_project_root((body or {}).get("project_root")),
                    review=(body or {}).get('review'),
                )
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (OSError, RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/workflows/{candidate_id}/rollback")
    def api_workflow_rollback(candidate_id: str):
        try:
            return JSONResponse(improvement_adapter.rollback_workflow(candidate_id))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/workflows/{candidate_id}/reject")
    def api_workflow_reject(candidate_id: str, body: dict | None = None):
        try:
            return JSONResponse(
                improvement_adapter.reject_workflow(
                    candidate_id,
                    reason=str((body or {}).get("reason") or "operator_rejected"),
                )
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    def change_suggestion_disposition(candidate_id: str, *, restore: bool = False):
        try:
            result = improvement_adapter.dismiss_suggestion(candidate_id, restore=restore)
            response_cache.invalidate()
            return JSONResponse(result)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except sqlite3.OperationalError as exc:
            if not is_sqlite_busy(exc):
                raise
            return JSONResponse({"error": "The database is busy. Please retry shortly."},
                                status_code=503, headers={"Retry-After": "2"})

    @app.post("/api/workflows/{candidate_id}/dismiss")
    def api_workflow_dismiss(candidate_id: str):
        return change_suggestion_disposition(candidate_id)

    @app.post("/api/workflows/{candidate_id}/restore")
    def api_workflow_restore(candidate_id: str):
        return change_suggestion_disposition(candidate_id, restore=True)

    @app.post("/api/feedback/{session_id:path}")
    def api_session_feedback(session_id: str, body: dict):
        try:
            return JSONResponse(
                improvement_adapter.record_feedback(
                    session_id,
                    outcome=str(body.get("outcome") or ""),
                    reason=body.get("reason"),
                ),
                status_code=201,
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @app.get("/api/impact")
    def api_impact(request: Request):
        return read_response(request, improvement_adapter.impact())

    @app.get("/api/impact/{impact_id}/evidence")
    def api_impact_evidence(impact_id: str, request: Request):
        try:
            return read_response(request, improvement_adapter.impact_evidence(impact_id))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

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
    raw_storage_status_loader: Callable[[], dict[str, object]] | None = None,
    open_browser: bool = True,
    backend_runner=None,
) -> None:
    """Inline FastAPI server for the local `reflect` browser report."""
    import threading
    import webbrowser

    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("The Reflect backend requires FastAPI and uvicorn") from exc
    from reflect.backend_api import install_backend
    from reflect.backend_lifecycle import instance_token
    from reflect.backend_runtime import BackendJobRunner
    backend_runner = backend_runner or BackendJobRunner(db_path)
    backend_runner.raw_storage_status_loader = raw_storage_status_loader
    read_executor = DashboardReadExecutor()
    try:
        app = build_dashboard_app(
            docs_dir=docs_dir,
            db_path=db_path,
                raw_storage_status_loader=raw_storage_status_loader,
            read_executor=read_executor,
        )
        install_backend(app, backend_runner, token=instance_token(db_path), port=port)
        url = f"http://127.0.0.1:{port}/?report=api/data"
        if open_browser:
            threading.Timer(0.5, webbrowser.open, args=[url]).start()
        print(f"\n  Serving at: {url}")
        print("  Press Ctrl-C to stop\n")
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        read_executor.close()
