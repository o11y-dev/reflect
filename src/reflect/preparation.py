from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class PreparationState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class PreparationStage(StrEnum):
    OPENING_STORE = "opening_store"
    INGESTING_TRACES = "ingesting_traces"
    INGESTING_LOGS = "ingesting_logs"
    INGESTING_SESSIONS = "ingesting_sessions"
    NORMALIZING = "normalizing"
    UPDATING_CANONICAL_STATE = "updating_canonical_state"
    REFRESHING_GRAPH = "refreshing_graph"
    REFRESHING_ROLLUPS = "refreshing_rollups"
    REFRESHING_IMPROVEMENTS = "refreshing_improvements"
    COMPLETE = "complete"


class SnapshotAction(StrEnum):
    READ = "read"
    REFRESH = "refresh"
    ERROR = "error"


@dataclass(frozen=True)
class SnapshotStatus:
    exists: bool
    has_sessions: bool
    schema_current: bool = False
    pending_migrations: tuple[int, ...] = ()
    stale_reasons: tuple[str, ...] = ()
    error: str = ""

    @property
    def ready(self) -> bool:
        return (
            self.schema_ready
            and self.has_sessions
        )

    @property
    def schema_ready(self) -> bool:
        return (
            self.exists
            and self.schema_current
            and not self.stale_reasons
            and not self.error
        )

    @property
    def state(self) -> str:
        if not self.exists:
            return "missing"
        if self.error:
            return "unreadable"
        if not self.schema_current:
            if not self.pending_migrations and not self.has_sessions:
                return "empty"
            return "outdated"
        if self.stale_reasons:
            return "stale"
        if not self.has_sessions:
            return "empty"
        return "ready"


class SnapshotInspector(Protocol):
    def inspect(self) -> SnapshotStatus: ...


class SnapshotReadinessProbe(Protocol):
    """Inspect command-specific derived state without mutating the snapshot."""

    def inspect(self, conn: sqlite3.Connection) -> tuple[str, ...]: ...


class SQLiteSnapshotInspector:
    """Inspect a Reflect snapshot through a query-only SQLite connection."""

    def __init__(
        self,
        db_path: Path,
        *,
        readiness_probes: Iterable[SnapshotReadinessProbe] = (),
    ):
        self.db_path = db_path.expanduser()
        self.readiness_probes = tuple(readiness_probes)

    def inspect(self) -> SnapshotStatus:
        from reflect.store.migrate import load_migrations
        from reflect.store.sqlite import connect_sqlite_read_only

        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return SnapshotStatus(
                exists=self.db_path.exists(),
                has_sessions=False,
            )

        try:
            conn = connect_sqlite_read_only(self.db_path)
        except sqlite3.Error as exc:
            return SnapshotStatus(
                exists=True,
                has_sessions=False,
                error=str(exc),
            )
        try:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            expected = {migration.version for migration in load_migrations()}
            applied = (
                {
                    int(row[0])
                    for row in conn.execute("SELECT version FROM schema_migrations")
                }
                if "schema_migrations" in tables
                else set()
            )
            pending = tuple(sorted(expected - applied))
            has_sessions = (
                "sessions" in tables
                and conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone()
                is not None
            )
            stale_reasons = (
                tuple(
                    reason
                    for probe in self.readiness_probes
                    for reason in probe.inspect(conn)
                )
                if not pending
                else ()
            )
            return SnapshotStatus(
                exists=True,
                has_sessions=has_sessions,
                schema_current=not pending,
                pending_migrations=pending,
                stale_reasons=stale_reasons,
            )
        except sqlite3.Error as exc:
            return SnapshotStatus(
                exists=True,
                has_sessions=False,
                error=str(exc),
            )
        finally:
            conn.close()


class CommandPreparationPolicy:
    """Require explicit authorization before a command mutates its snapshot."""

    def __init__(self, *, require_sessions: bool = True):
        self.require_sessions = require_sessions

    def is_ready(self, snapshot: SnapshotStatus) -> bool:
        return snapshot.ready if self.require_sessions else snapshot.schema_ready

    def decide(
        self,
        *,
        requested_refresh: bool | None,
        snapshot: SnapshotStatus,
    ) -> SnapshotAction:
        if requested_refresh is True:
            return SnapshotAction.REFRESH
        if self.is_ready(snapshot):
            return SnapshotAction.READ
        return SnapshotAction.ERROR


class SnapshotRefresher(Protocol):
    def refresh(self) -> dict[str, Any]: ...


class CallableSnapshotRefresher:
    """Adapt an existing refresh function to the snapshot lifecycle contract."""

    def __init__(self, refresh: Callable[[], dict[str, Any]]):
        self._refresh = refresh

    def refresh(self) -> dict[str, Any]:
        return self._refresh()


class SnapshotUnavailableError(RuntimeError):
    def __init__(self, status: SnapshotStatus, *, refresh_hint: str):
        detail = f" ({status.error})" if status.error else ""
        migrations = (
            f"; pending migrations: {', '.join(map(str, status.pending_migrations))}"
            if status.pending_migrations
            else ""
        )
        stale = (
            f"; {'; '.join(status.stale_reasons)}"
            if status.stale_reasons
            else ""
        )
        super().__init__(
            f"The local snapshot is {status.state}{detail}{migrations}{stale}. "
            f"{refresh_hint}"
        )
        self.status = status


@dataclass(frozen=True)
class SnapshotPreparationResult:
    action: SnapshotAction
    status: SnapshotStatus
    refresh_result: dict[str, Any] | None = None

    @property
    def refreshed(self) -> bool:
        return self.action is SnapshotAction.REFRESH


class SnapshotLifecycleService:
    """Coordinate query-only inspection and explicitly authorized refreshes."""

    def __init__(
        self,
        inspector: SnapshotInspector,
        *,
        refresher: SnapshotRefresher | None = None,
        policy: CommandPreparationPolicy | None = None,
        refresh_hint: str = "Run `reflect refresh` to prepare it.",
    ):
        self.inspector = inspector
        self.refresher = refresher
        self.policy = policy or CommandPreparationPolicy()
        self.refresh_hint = refresh_hint

    def prepare(self, *, requested_refresh: bool | None) -> SnapshotPreparationResult:
        before = self.inspector.inspect()
        action = self.policy.decide(
            requested_refresh=requested_refresh,
            snapshot=before,
        )
        if action is SnapshotAction.READ:
            return SnapshotPreparationResult(action=action, status=before)
        if action is SnapshotAction.ERROR or self.refresher is None:
            raise SnapshotUnavailableError(before, refresh_hint=self.refresh_hint)

        refresh_result = self.refresher.refresh()
        after = self.inspector.inspect()
        if not self.policy.is_ready(after):
            raise SnapshotUnavailableError(after, refresh_hint=self.refresh_hint)
        return SnapshotPreparationResult(
            action=action,
            status=after,
            refresh_result=refresh_result,
        )


@dataclass(frozen=True)
class PreparationProgress:
    stage: PreparationStage
    message: str


class PreparationProgressReporter(Protocol):
    def __call__(self, progress: PreparationProgress) -> None: ...


def report_preparation_progress(
    reporter: PreparationProgressReporter | None,
    stage: PreparationStage,
    message: str,
) -> None:
    if reporter is not None:
        reporter(PreparationProgress(stage=stage, message=message))


@dataclass(frozen=True)
class PreparationSnapshot:
    state: PreparationState
    generation: int
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    result: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        return payload


class BackgroundPreparationWorker:
    """Own one background preparation lifecycle and its observable state."""

    def __init__(
        self,
        prepare: Callable[[], dict[str, Any]],
        *,
        name: str = "reflect-report-preparation",
    ) -> None:
        self._prepare = prepare
        self._name = name
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._thread: threading.Thread | None = None
        self._snapshot = PreparationSnapshot(state=PreparationState.IDLE, generation=0)

    def add_completion_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if self._snapshot.state is not PreparationState.IDLE:
                raise RuntimeError(
                    "completion callbacks must be registered before preparation starts"
                )
            self._callbacks.append(callback)

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            generation = self._snapshot.generation
            self._snapshot = PreparationSnapshot(
                state=PreparationState.RUNNING,
                generation=generation,
                started_at=_now(),
            )
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()
            return True

    def snapshot(self) -> PreparationSnapshot:
        with self._lock:
            return self._snapshot

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def close(self, timeout: float = 5.0) -> bool:
        return self.wait(timeout)

    def _run(self) -> None:
        try:
            result = self._prepare()
            with self._lock:
                callbacks = tuple(self._callbacks)
            for callback in callbacks:
                callback(result)
        except Exception as exc:
            with self._lock:
                current = self._snapshot
                self._snapshot = PreparationSnapshot(
                    state=PreparationState.FAILED,
                    generation=current.generation,
                    started_at=current.started_at,
                    finished_at=_now(),
                    error=str(exc),
                )
            return

        with self._lock:
            current = self._snapshot
            self._snapshot = PreparationSnapshot(
                state=PreparationState.COMPLETE,
                generation=current.generation + 1,
                started_at=current.started_at,
                finished_at=_now(),
                result=result,
            )


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
