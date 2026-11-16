"""Durable, per-task handoff to the canonical store; no background service."""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from reflect.improvements.contracts import WorkflowMilestoneEvidence
from reflect.improvements.milestones import WorkflowMilestoneService
from reflect.improvements.repository import utc_now
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only, is_sqlite_busy
from reflect.task_runs import (
    MCPTaskRunLinkState,
    MCPTaskRunService,
    MCPTaskRunStatus,
    TaskStart,
    validate_completion,
)
from reflect.utils import _json_dumps, _json_loads


class TaskEventInbox:
    """One private pending file per task, removed only after canonical commit."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.root = self.db_path.with_name(self.db_path.name + ".task-inbox")

    def _path(self, task_id: str) -> Path:
        if not re.fullmatch(r"mcp_task_[0-9a-f]{32}", task_id):
            raise ValueError("Invalid task run ID")
        return self.root / f"{task_id}.json"

    @contextmanager
    def _lock(self, task_id: str):
        path = self._path(task_id)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield path

    def _sync_directory(self):
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write(self, path: Path, data: dict):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.root, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(_json_dumps(data))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._sync_directory()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _load(self, path: Path) -> dict | None:
        try:
            return _json_loads(path.read_text())
        except FileNotFoundError:
            return None

    def _canonical_status(self, task_id: str) -> MCPTaskRunStatus:
        with closing(connect_sqlite_read_only(self.db_path, profile=None)) as conn:
            return MCPTaskRunService(conn).status(task_id)

    def status(self, task_id: str) -> MCPTaskRunStatus:
        # Atomic file publication makes this a read-only operation, even while
        # another process projects or accepts an event.
        pending = self._load(self._path(task_id))
        if pending is None:
            return self._canonical_status(task_id)
        status = MCPTaskRunStatus.model_validate(pending["status"])
        return status.model_copy(update={
            "tracking_state": "accepted_pending", "projection_error": pending.get("error"),
        })

    def start(self, start: TaskStart) -> MCPTaskRunStatus:
        with self._lock(start.task_run_id) as path:
            if path.exists():
                raise ValueError("Task start already accepted")
            self._write(path, {
                "status": start.status().model_dump(mode="json"),
                "events": [self._event("start", start.model_dump(mode="json"))],
            })
        return start.status()

    @staticmethod
    def _event(kind: str, payload: dict) -> dict:
        return {"id": uuid.uuid4().hex, "kind": kind, "at": utc_now(), "payload": payload}

    def accept(self, task_id: str, kind: str, payload: dict) -> dict:
        with self._lock(task_id) as path:
            pending = self._load(path)
            task = MCPTaskRunStatus.model_validate(pending["status"]) if pending else self._canonical_status(task_id)
            pending = pending or {"status": task.model_dump(mode="json"), "events": []}
            now = utc_now()
            if kind == "complete":
                outcome, summary, identity, idempotent = validate_completion(task, **payload)
                payload = {**payload, "outcome": outcome.value, "summary_redacted": summary}
                if idempotent and (not identity or identity == task.runtime_session_id):
                    return {**task.model_dump(mode="json"), "idempotent": True}
                task = task.model_copy(update={
                    "status": "completed", "outcome": outcome,
                    "summary_redacted": summary, "verification_passed": payload.get("verification_passed"),
                    "completed_at": task.completed_at or now,
                    "runtime_session_id": identity or task.runtime_session_id,
                    "runtime_agent": task.runtime_agent or payload.get("runtime_agent"),
                    "runtime_identity_source": "explicit_argument" if identity and not task.runtime_session_id else task.runtime_identity_source,
                })
                result = {"idempotent": idempotent}
            elif kind == "milestone":
                for event in pending["events"]:
                    if event["kind"] == kind and event["payload"]["idempotency_key"] == payload["idempotency_key"]:
                        if event["payload"] != payload:
                            raise ValueError("idempotency_key is already bound to a different milestone")
                        return {**event["result"], "created": False, "tracking_state": "accepted_pending"}
                with closing(connect_sqlite_read_only(self.db_path, profile=None)) as conn:
                    result = WorkflowMilestoneService(conn).record(
                        task_run_id=task_id, **{k: v for k, v in payload.items() if k != "evidence"},
                        evidence=WorkflowMilestoneEvidence.model_validate(payload.get("evidence") or {}),
                        task_status=task.model_dump(mode="json"), dry_run=True, recorded_at=now,
                    ).model_dump(mode="json")
                if not result["created"]:
                    return {**result, "tracking_state": task.tracking_state}
                task = task.model_copy(update={"milestone_count": task.milestone_count + 1})
            else:
                raise ValueError(f"Unknown task event: {kind}")
            task = task.model_copy(update={
                "updated_at": now, "tracking_state": "accepted_pending",
                "link_state": (MCPTaskRunLinkState.PENDING_INGESTION if task.runtime_session_id
                               else MCPTaskRunLinkState.NO_RUNTIME_SESSION),
            })
            event = self._event(kind, payload)
            event["at"], event["result"] = now, result
            pending["events"].append(event)
            pending["status"] = task.model_dump(mode="json")
            self._write(path, pending)
            return {**task.model_dump(mode="json"), **result, "tracking_state": "accepted_pending"}

    def drain(self, *, task_id: str | None = None, conn: sqlite3.Connection | None = None) -> None:
        paths = sorted(self.root.glob("*.json"))
        if task_id:
            preferred = self._path(task_id)
            paths = [preferred, *(path for path in paths if path != preferred)]
        remaining = 100
        for path in paths:
            if remaining == 0:
                break
            if not path.exists():
                continue
            # Do not hold a file lock while waiting for SQLite. Producers remain
            # able to accept events during a long-running refresh transaction.
            owned = conn is None
            db = conn
            try:
                if db is None:
                    db = connect_sqlite(self.db_path, timeout_ms=1000)
                    migrate(db)
                db.execute("BEGIN IMMEDIATE")
                with self._lock(path.stem):
                    pending = self._load(path)
                    if pending is None:
                        db.rollback()
                        continue
                    events = pending["events"][:remaining]
                    for event in events:
                        if db.execute("SELECT 1 FROM task_event_receipts WHERE event_id = ?", (event["id"],)).fetchone():
                            continue
                        self._apply(db, path.stem, event)
                        db.execute("INSERT INTO task_event_receipts VALUES (?, ?, ?)",
                                   (event["id"], path.stem, utc_now()))
                    db.commit()
                    remaining -= len(events)
                    pending.setdefault('projected_event_ids', []).extend(event['id'] for event in events)
                    pending["events"] = pending["events"][len(events):]
                    if pending["events"]:
                        pending.pop("error", None)
                        self._write(path, pending)
                    else:
                        pending.pop('error', None)
                        self._write(path, pending)
            except sqlite3.OperationalError as exc:
                if db is not None:
                    db.rollback()
                if is_sqlite_busy(exc):
                    return
                self._error(path, exc)
            except (ValueError, KeyError, TypeError, RuntimeError, sqlite3.IntegrityError) as exc:
                if db is not None:
                    db.rollback()
                self._error(path, exc)
            finally:
                if owned and db is not None:
                    db.close()

    def acknowledge_publication(self, publication_path: Path) -> None:
        """Forget pending status only after every accepted event is published."""
        with closing(connect_sqlite_read_only(publication_path, immutable=True)) as conn:
            for path in self.root.glob('*.json'):
                with self._lock(path.stem):
                    pending = self._load(path)
                    if not pending or pending.get('events'):
                        continue
                    projected = pending.get('projected_event_ids', [])
                    if projected and all(conn.execute('SELECT 1 FROM task_event_receipts WHERE event_id=?',
                                                       (event_id,)).fetchone() for event_id in projected):
                        path.unlink()
                        self._sync_directory()

    def _error(self, path: Path, exc: Exception):
        with self._lock(path.stem):
            try:
                pending = self._load(path)
            except ValueError:
                return  # Health reports malformed files; other tasks can drain.
            if isinstance(pending, dict):
                pending["error"] = str(exc)
                self._write(path, pending)

    @staticmethod
    def _apply(conn: sqlite3.Connection, task_id: str, event: dict):
        service = MCPTaskRunService(conn)
        if event["kind"] == "start":
            start = TaskStart.model_validate(event["payload"])
            if start.task_run_id != task_id:
                raise ValueError("Task event identity does not match its inbox file")
            service.apply_start(start, commit=False)
        elif event["kind"] == "complete":
            service.complete(task_id, **event["payload"], completed_at=event["at"], commit=False)
        elif event["kind"] == "milestone":
            payload = dict(event["payload"])
            payload["evidence"] = WorkflowMilestoneEvidence.model_validate(payload.get("evidence") or {})
            WorkflowMilestoneService(conn).record(task_run_id=task_id, **payload,
                                                 recorded_at=event["at"], commit=False)
        else:
            raise ValueError("Unknown task event")

    def health(self) -> dict:
        pending, oldest, errors = 0, None, []
        for path in self.root.glob("*.json"):
            try:
                data = self._load(path)
                if data is None:
                    continue
                pending += len(data["events"])
                for event in data["events"]:
                    at = datetime.fromisoformat(event["at"])
                    oldest = min(oldest, at) if oldest else at
                if data.get("error"):
                    errors.append({"task_run_id": path.stem, "error": data["error"]})
            except (ValueError, KeyError, TypeError) as exc:
                errors.append({"task_run_id": path.stem, "error": str(exc)})
        return {"pending_count": pending,
                "oldest_pending_age_seconds": max(0, (datetime.now(UTC) - oldest).total_seconds()) if oldest else None,
                "projection_errors": errors}
