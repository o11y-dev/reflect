from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class PreparationState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


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
                raise RuntimeError("completion callbacks must be registered before preparation starts")
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
