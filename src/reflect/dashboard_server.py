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
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from reflect.dashboard_explore import (
    EXPLORE_VIEW_TABS,
    build_explore_payload,
    canonical_explore_view,
    parse_dashboard_scope,
)
from reflect.dashboard_improvements import DashboardImprovementAdapter
from reflect.dashboard_queries import build_dashboard_payload, finalize_dashboard_analysis
from reflect.dashboard_read_executor import (
    DashboardReadBusyError,
    DashboardReadExecutor,
    read_revision,
)
from reflect.dashboard_sessions import build_session_payload, load_session_detail
from reflect.preparation import (
    PreparationCoordinator,
    PreparationProfile,
    PreparationScheduler,
    PreparationSnapshot,
    PreparationState,
    SQLiteSnapshotInspector,
)
from reflect.store.sqlite import (
    SnapshotNotReadyError,
    connect_sqlite_read_only,
    consistent_snapshot,
    snapshot_incomplete,
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
            recorded = self._entries.get(f"recorded:{key}") if allow_recorded and key is not None and encoded is None else None
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


class DashboardDataCache:
    """Publish one immutable dashboard payload at a time."""

    def __init__(
        self,
        loader: Callable[[], dict[str, object]],
        *,
        refresh_loader: Callable[[], dict[str, object]] | None = None,
        analysis_loader: Callable[..., dict[str, object]] | None = None,
        preparation_running: Callable[[], bool] | None = None,
        cache_path: Path | None = None,
        cache_key: str = "",
    ) -> None:
        self._refresh_loader = refresh_loader or loader
        self._analysis_loader = analysis_loader
        self._usage_publication: tuple[str, str, dict[str, object]] | None = None
        self._preparation_running = preparation_running
        self._lock = threading.Lock()
        self._cache_path = cache_path
        self._cache_key = cache_key
        self._refresh_pending = False
        self._refreshing = False
        self._payload = self._read_published()
        if self._payload is not None:
            self._refresh_pending = True
            return
        try:
            self._payload = loader()
        except SnapshotNotReadyError:
            # The server can start during another process's refresh. Its routes
            # report preparing until a coherent generation becomes available.
            self._payload = None

    @property
    def has_payload(self) -> bool:
        with self._lock:
            return self._payload is not None

    def _read_published(self) -> dict[str, object] | None:
        if self._cache_path is None:
            return None
        try:
            saved = _json_loads(self._cache_path.read_bytes())
            if saved.get("format") == 1 and saved.get("key") == self._cache_key:
                payload = saved.get("payload")
                return payload if isinstance(payload, dict) else None
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _persist(self, payload: dict[str, object]) -> None:
        if self._cache_path is None:
            return
        # Disk persistence is best-effort; readers retain the in-memory report.
        _persist_dashboard_json(self._cache_path, {
            "format": 1, "key": self._cache_key, "payload": payload,
        })

    def get(self) -> dict[str, object]:
        with self._lock:
            payload = self._payload
            pending = self._refresh_pending
            refreshing = self._refreshing
        preparation_running = self._preparation_running is not None and self._preparation_running()
        if (payload is None or pending) and not refreshing and not preparation_running:
            refreshed = self.refresh()
            if refreshed is not None:
                return refreshed
        if payload is None:
            raise SnapshotNotReadyError("The report is preparing. Retry after the active refresh completes.")
        return payload

    def refresh(self) -> dict[str, object] | None:
        with self._lock:
            self._usage_publication = None
            if self._refreshing:
                # Preserve a newer completion request while the current loader
                # publishes. Readers themselves do not enqueue duplicate loads.
                self._refresh_pending = True
                return self._payload
            self._refreshing = True
            self._refresh_pending = False
        try:
            payload = self._refresh_loader()
        except SnapshotNotReadyError:
            # Another process may start refreshing after our pipeline releases
            # its lock. Retain the completed cache and retry on the next read.
            with self._lock:
                self._refresh_pending = True
            return None
        else:
            self._persist(payload)
            with self._lock:
                self._payload = payload
            return payload
        finally:
            with self._lock:
                self._refreshing = False

    def publish_usage(self, details: dict[str, object]) -> None:
        payload = self.refresh()
        completed_at = details.get("completed_at")
        run = (details.get("analysis_window") or {}).get("date_to")
        with self._lock:
            if (run and completed_at and payload is self._payload and payload is not None
                    and payload.get("snapshot_completed_at") == completed_at
                    and not self._refreshing and not self._refresh_pending):
                self._usage_publication = (run, completed_at, payload)

    def complete(self, result) -> dict[str, object] | None:
        """Reuse only this run's successful usage read; otherwise rebuild normally."""
        details = result.details
        run = (details.get("analysis_window") or {}).get("date_to")
        revision = tuple(tuple(row) for row in details.get("publication_revision") or ())
        with self._lock:
            publication, self._usage_publication = self._usage_publication, None
            reuse = (self._analysis_loader is not None and publication is not None
                     and publication[0] == run and bool(revision)
                     and publication[2] is self._payload
                     and not self._refreshing and not self._refresh_pending)
            if reuse:
                self._refreshing = True
        if not reuse:
            return self.refresh()
        try:
            payload = self._analysis_loader(
                publication[2], usage_completed_at=publication[1], publication_revision=revision,
            )
            with self._lock:
                if self._refresh_pending:
                    raise SnapshotNotReadyError("Another publication requested a complete refresh.")
            self._persist(payload)
            with self._lock:
                self._payload = payload
            return payload
        except SnapshotNotReadyError:
            pass  # A changed generation requires the ordinary full loader.
        except BaseException:
            with self._lock:
                self._refresh_pending = True
            raise
        finally:
            with self._lock:
                self._refreshing = False
        return self.refresh()


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
    preparation_scheduler: PreparationScheduler | None = None,
    raw_storage_status_loader: Callable[[], dict[str, object]] | None = None,
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
        preparation_scheduler=preparation_scheduler,
        raw_storage_status_loader=raw_storage_status_loader,
        open_browser=open_browser,
    )


def build_dashboard_app(
    *,
    docs_dir: Path,
    db_path: Path,
    preparation_worker: PreparationCoordinator | None = None,
    preparation_scheduler: PreparationScheduler | None = None,
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
        return await run_in_threadpool(builder, db_path, *args, **kwargs)

    @app.exception_handler(DashboardReadBusyError)
    async def read_busy(request, exc):
        key = response_cache.key(request.url.path, request.query_params, method=request.method)
        cached = response_cache.stale(key, reason="busy")
        if cached is not None and SQLiteSnapshotInspector(db_path, profile=None).inspect().schema_ready:
            return JSONResponse(cached, headers={"X-Reflect-Publication": "stale"})
        return JSONResponse(
            {"error": str(exc), "state": "busy", "reason": "reading"},
            status_code=503, headers={"Retry-After": "2"},
        )

    @app.exception_handler(SnapshotNotReadyError)
    async def snapshot_not_ready(request, exc):
        key = response_cache.key(request.url.path, request.query_params, method=request.method)
        cached = response_cache.stale(key, reason=exc.state)
        if cached is not None and SQLiteSnapshotInspector(db_path, profile=None).inspect().schema_ready:
            return JSONResponse(cached, headers={"X-Reflect-Publication": "stale"})
        return JSONResponse(
            {"error": str(exc), "state": exc.state},
            status_code=503 if exc.state == "preparing" else 409,
            headers={"Retry-After": "2"} if exc.state == "preparing" else {},
        )
    if preparation_worker is not None and preparation_scheduler is not None:
        raise ValueError("pass a preparation worker or scheduler, not both")
    active_preparation = (
        preparation_scheduler.coordinator
        if preparation_scheduler is not None
        else preparation_worker
    )
    initial_snapshot_pending = active_preparation is not None

    def require_initial_snapshot(*, canonical_only: bool = False, usage_only: bool = False) -> None:
        nonlocal initial_snapshot_pending
        if not initial_snapshot_pending and not canonical_only:
            return
        snapshot = (
            SQLiteSnapshotInspector(db_path, profile=None)
            if canonical_only else SQLiteSnapshotInspector(
                db_path, profile=PreparationProfile.USAGE if usage_only else PreparationProfile.SNAPSHOT,
            )
        ).inspect()
        if snapshot.error:
            raise sqlite3.OperationalError(snapshot.error)
        if not snapshot.schema_ready:
            raise SnapshotNotReadyError("The first report is preparing. Retry after preparation completes.")
        if not canonical_only:
            initial_snapshot_pending = False

    def load_dashboard(**kwargs):
        require_initial_snapshot(usage_only=True)
        return build_dashboard_payload(db_path, **kwargs)

    @app.middleware("http")
    async def await_initial_snapshot(request, call_next):
        request.state.dashboard_read_order = response_cache.start_read()
        if request.url.path.startswith("/api/explore/"):
            view_name = request.url.path.removeprefix("/api/explore/")
            if canonical_explore_view(view_name) not in EXPLORE_VIEW_TABS:
                return JSONResponse({"error": f"Unknown Explore view: {view_name}", "view": view_name}, status_code=404)
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/status", "/api/refresh"}:
            try:
                require_initial_snapshot(
                    canonical_only=(
                        request.url.path.startswith(("/api/session/", "/api/skills/"))
                        or request.url.path in {"/api/skills", "/api/impact", "/api/workflows"}
                        or request.url.path in {"/api/explore/tools", "/api/explore/graph", "/api/explore/context"}
                        or bool(re.fullmatch(r"/api/(?:workflows|impact)/[^/]+/evidence", request.url.path))
                    ),
                    usage_only=request.url.path in {"/api/data", "/api/explore/usage"},
                )
            except SnapshotNotReadyError as exc:
                return await snapshot_not_ready(request, exc)
            # Serve completed evidence before doing an expensive read while a
            # refresh is in progress. Exact path/query keys prevent scope leaks.
            if request.method == "GET" and re.fullmatch(
                r"/api/(?:(?:workflows|impact)(?:/[^/]+/evidence)?|skills/[^/]+/usage-graph)", request.url.path,
            ):
                with closing(connect_sqlite_read_only(db_path, profile=None)) as conn:
                    pending = snapshot_incomplete(conn)
                if pending:
                    key = response_cache.key(request.url.path, request.query_params)
                    cached = response_cache.stale(key, reason="preparing", allow_recorded=True)
                    if cached is not None:
                        return JSONResponse(cached, headers={"X-Reflect-Publication": cached.get("publication", {}).get("state", "stale")})
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

    @consistent_snapshot
    def completed_panel_responses(_db_path):
        return {
            "/api/findings": improvement_adapter.findings(status=None, include_resolved=False, limit=100),
            "/api/rules": improvement_adapter.rules(),
            "/api/workflows": improvement_adapter.workflows(behavior_type=None, status=None),
            "/api/loops": improvement_adapter.loops(kind=None, status=None, limit=100),
            "/api/impact": improvement_adapter.impact(),
            "/api/skills": improvement_adapter.skills(status=None, include_stale=False, limit=500),
        }

    def retain_completed_panels(_result):
        read_order = response_cache.start_read()
        try:
            payloads = completed_panel_responses(db_path)
        except SnapshotNotReadyError:
            return
        except (sqlite3.Error, ValueError):
            logger.warning("Could not retain completed dashboard panels", exc_info=True)
            return
        for path, payload in payloads.items():
            params = {"limit": "500"} if path == "/api/skills" else {}
            response_cache.put(response_cache.key(path, params), payload, read_order=read_order)

    if active_preparation is not None:
        dashboard_cache = DashboardDataCache(
            lambda: load_dashboard(
                limit=50,
                offset=0,
                lazy_all_tabs=True,
                range_name=default_range,
            ),
            refresh_loader=lambda: load_dashboard(
                lazy_heavy_tabs=True,
                base_tab_names=set(EXPLORE_VIEW_TABS["usage"]),
                range_name=default_range,
            ),
            analysis_loader=lambda payload, **kwargs: finalize_dashboard_analysis(db_path, payload, **kwargs),
            preparation_running=lambda: active_preparation.snapshot().state is PreparationState.RUNNING,
            cache_path=db_path.with_suffix(".dashboard.json"),
            cache_key=f"{db_path.resolve()}:{default_range}",
        )
    else:
        dashboard_cache = DashboardDataCache(
            lambda: build_dashboard_payload(
                db_path, lazy_heavy_tabs=True, base_tab_names=set(EXPLORE_VIEW_TABS["usage"]),
                range_name=default_range,
            )
        )
    if active_preparation is not None:
        active_preparation.add_usage_callback(dashboard_cache.publish_usage)
        active_preparation.add_completion_callback(dashboard_cache.complete)
        active_preparation.add_completion_callback(retain_completed_panels)
        # The server starts its worker after app construction. Retain an
        # existing completed generation before the first refresh invalidates it.
        retain_completed_panels(None)
    if dashboard_cache.has_payload:
        initial_snapshot_pending = False

    def requested_scope(params):
        scope = list(parse_dashboard_scope(params))
        if not params.get("range") and not scope[1]:
            scope[-1] = default_range
        return scope

    async def prepared_response(request, operation, *args, **scope):
        revision = await run_in_threadpool(read_revision, db_path, operation)
        key = response_cache.key(request.url.path, request.query_params)
        retained = response_cache.stale(key, reason="reading")
        if operation == "graph" and revision[2] != "ready" and retained is not None:
            retained["publication"]["reason"] = "preparing"
            return JSONResponse(retained, headers={"X-Reflect-Publication": "stale"})
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
        has_filter = any(
            [q, session_id, agents, model != "all", status != "all", range_name != default_range]
        )
        try:
            if not has_filter:
                perf_kind = "cached"
                payload = await run_in_threadpool(dashboard_cache.get)
                try:
                    with closing(connect_sqlite_read_only(db_path, profile="usage")) as conn:
                        if payload.get("analysis_state") == "ready" and snapshot_incomplete(conn):
                            raise SnapshotNotReadyError("Analysis is refreshing; retaining the completed report.")
                except SnapshotNotReadyError as exc:
                    if not SQLiteSnapshotInspector(db_path, profile=None).inspect().schema_ready:
                        raise
                    retained = response_cache.stale(
                        response_cache.key(request.url.path, params),
                        reason=exc.state, fallback=payload,
                    )
                    return JSONResponse(retained, headers={"X-Reflect-Publication": "stale"})
                return read_response(request, payload)
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
        return JSONResponse(detail, headers={"Access-Control-Allow-Origin": "*"})

    @app.get("/api/status")
    def api_status():
        snapshot = (
            active_preparation.snapshot()
            if active_preparation is not None
            else PreparationSnapshot(state=PreparationState.IDLE, generation=0)
        )
        schedule = (
            preparation_scheduler.snapshot().as_dict()
            if preparation_scheduler is not None
            else {"enabled": False, "interval_seconds": 0, "next_run_at": ""}
        )
        raw_storage = None
        if raw_storage_status_loader is not None:
            try:
                raw_storage = raw_storage_status_loader()
            except OSError as exc:
                raw_storage = {
                    "accepting_telemetry": None,
                    "error": str(exc),
                }
        return JSONResponse(
            {
                "preparation": snapshot.as_dict(),
                "automatic_refresh": schedule,
                "raw_storage": raw_storage,
                "refresh_available": active_preparation is not None,
            }
        )

    @app.post("/api/refresh")
    def api_refresh():
        if active_preparation is None:
            return JSONResponse(
                {
                    "error": "This report server is snapshot-only. Start Reflect normally or use `reflect server --refresh start`.",
                    "refresh_available": False,
                },
                status_code=409,
            )
        started = active_preparation.start()
        return JSONResponse(
            {
                "started": started,
                "refresh_available": True,
                "preparation": active_preparation.snapshot().as_dict(),
            },
            status_code=202 if started else 200,
        )

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
    preparation_worker: PreparationCoordinator | None = None,
    preparation_scheduler: PreparationScheduler | None = None,
    raw_storage_status_loader: Callable[[], dict[str, object]] | None = None,
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
        preparation = preparation_scheduler.coordinator if preparation_scheduler else preparation_worker
        if preparation is not None:
            preparation.start()
            preparation.wait()
            snapshot = preparation.snapshot()
            if snapshot.state is PreparationState.FAILED:
                raise RuntimeError(f"Report preparation failed: {snapshot.error}") from None
        artifact = docs_dir / "_reflect_data.json"
        artifact.write_text(_json_dumps(build_dashboard_payload(db_path)), encoding="utf-8")
        print(f"Wrote: {artifact}")
        return

    read_executor = DashboardReadExecutor()
    try:
        app = build_dashboard_app(
            docs_dir=docs_dir,
            db_path=db_path,
            preparation_worker=preparation_worker,
            preparation_scheduler=preparation_scheduler,
            raw_storage_status_loader=raw_storage_status_loader,
            read_executor=read_executor,
        )
        url = f"http://127.0.0.1:{port}/?report=api/data"
        if open_browser:
            threading.Timer(0.5, webbrowser.open, args=[url]).start()
        print(f"\n  Serving at: {url}")
        print("  Press Ctrl-C to stop\n")
        if preparation_scheduler is not None:
            preparation_scheduler.start()
        elif preparation_worker is not None:
            preparation_worker.start()
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        read_executor.close()
        if preparation_scheduler is not None:
            preparation_scheduler.close()
        elif preparation_worker is not None:
            preparation_worker.close()
