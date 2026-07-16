from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from reflect.parsing import REFLECT_HOME

_FORBIDDEN_KEYS = {
    "session_id",
    "prompt",
    "response",
    "content",
    "file_path",
    "cwd",
    "tool_input",
    "tool_output",
    "credentials",
    "token",
}


@dataclass(frozen=True)
class NudgeExchangePaths:
    root: Path
    contract: Path
    outbox: Path
    acknowledged: Path
    rejected: Path


class NudgeFileExchange:
    """Prepare a disabled local handoff for a future opentelemetry-hooks reader."""

    def __init__(self, root: Path | None = None):
        self.root = (root or REFLECT_HOME / "state" / "nudges").expanduser().resolve()

    @staticmethod
    def session_key(session_id: str) -> str:
        if not session_id:
            raise ValueError("A non-empty session id is required")
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]

    def prepare(self) -> NudgeExchangePaths:
        """Create private directories and the disabled contract without enabling delivery."""
        contract = self._contract()
        directories = contract["directories"]
        paths = NudgeExchangePaths(
            root=self.root,
            contract=self.root / "contract.json",
            outbox=self.root / str(directories["outbox"]),
            acknowledged=self.root / str(directories["acknowledged"]),
            rejected=self.root / str(directories["rejected"]),
        )
        for directory in (paths.root, paths.outbox, paths.acknowledged, paths.rejected):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
        self._atomic_json(paths.contract, contract)
        return paths

    def stage(self, session_id: str, nudge: dict[str, Any]) -> Path:
        """Stage one metadata-only nudge; this is not called by current hook setup."""
        self._reject_sensitive_keys(nudge)
        nudge_id = str(nudge.get("id") or "").strip()
        observation_id = str(nudge.get("observation_id") or "").strip()
        message = str(nudge.get("message") or nudge.get("message_redacted") or "").strip()
        created_at = str(nudge.get("created_at") or "").strip()
        if not nudge_id or not observation_id or not message or not created_at:
            raise ValueError("A nudge requires id, observation_id, redacted message, and created_at")
        if len(message) > 1000:
            raise ValueError("A redacted nudge message must not exceed 1000 characters")

        paths = self.prepare()
        session_key = self.session_key(session_id)
        session_outbox = paths.outbox / session_key
        session_outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(session_outbox, 0o700)
        filename = f"{hashlib.sha256(nudge_id.encode('utf-8')).hexdigest()[:24]}.json"
        target = session_outbox / filename
        envelope = {
            "schema_version": 1,
            "session_key": session_key,
            "nudge": {
                "id": nudge_id,
                "observation_id": observation_id,
                "message_redacted": message,
                "created_at": created_at,
            },
        }
        self._atomic_json(target, envelope)
        return target

    @staticmethod
    def _contract() -> dict[str, Any]:
        contract_file = resources.files("reflect").joinpath("data/nudges/contract.json")
        return json.loads(contract_file.read_text(encoding="utf-8"))

    @staticmethod
    def _reject_sensitive_keys(value: Any) -> None:
        if isinstance(value, dict):
            keys = {str(key).lower() for key in value}
            blocked = keys & _FORBIDDEN_KEYS
            if blocked:
                raise ValueError(f"Nudge payload contains forbidden field(s): {', '.join(sorted(blocked))}")
            for nested in value.values():
                NudgeFileExchange._reject_sensitive_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                NudgeFileExchange._reject_sensitive_keys(nested)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".reflect-nudge-",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
