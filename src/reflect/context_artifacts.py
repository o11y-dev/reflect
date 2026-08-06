from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContextArtifactExposure:
    """Privacy-safe evidence that one context artifact was exposed to a session."""

    artifact_id: str
    type: str
    scope: str
    source_path: Path
    content_hash: str
    preview: str

    def as_memory_attributes(self) -> dict[str, str | float]:
        return {
            "gen_ai.memory.id": self.artifact_id,
            "gen_ai.memory.scope": self.scope,
            "gen_ai.memory.type": self.type,
            "gen_ai.memory.source": "codex_session_context",
            "gen_ai.memory.source_path": str(self.source_path),
            "gen_ai.memory.content_hash": self.content_hash,
            "gen_ai.memory.content_preview": self.preview,
            "gen_ai.memory.confidence": 1.0,
            "gen_ai.memory.sensitivity": "private" if self.scope == "user" else "unknown",
        }


@dataclass(frozen=True)
class TaskContractArtifact:
    artifact_id: str
    content_hash: str
    title: str
    source_path: str


def context_artifact_id(path: Path) -> str:
    """Return the stable filesystem identity shared by discovery and exposure."""

    return f"instruction_{hashlib.sha1(str(path).encode('utf-8')).hexdigest()}"


def task_contract_id(path: Path) -> str:
    """Return a stable identity for an explicitly supplied task contract."""

    return f"task_contract_{hashlib.sha1(str(path).encode('utf-8')).hexdigest()}"


def task_contract_artifact(path: Path, *, workspace_root: Path) -> TaskContractArtifact:
    try:
        source_path = str(path.relative_to(workspace_root))
    except ValueError:
        source_path = path.name
    title = path.stem.replace("-", " ").replace("_", " ").strip() or path.name
    return TaskContractArtifact(
        artifact_id=task_contract_id(path),
        content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        title=title,
        source_path=source_path,
    )


def codex_context_exposures(
    text: str,
    *,
    workspace_root: Path | None,
    home_root: Path | None = None,
) -> tuple[ContextArtifactExposure, ...]:
    """Extract explicit Codex context envelopes without retaining their contents."""

    if not text:
        return ()
    home = (home_root or Path.home()).expanduser()
    exposures: list[ContextArtifactExposure] = []

    memory_match = re.search(
        r"=+\s*MEMORY_SUMMARY BEGINS\s*=+\s*(.*?)\s*=+\s*MEMORY_SUMMARY ENDS\s*=+",
        text,
        flags=re.DOTALL,
    )
    if memory_match:
        content = memory_match.group(1).strip()
        path = home / ".codex" / "memories" / "memory_summary.md"
        exposures.append(
            ContextArtifactExposure(
                artifact_id=context_artifact_id(path),
                type="codex_memory_summary",
                scope="user",
                source_path=path,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                preview="[Codex memory summary]",
            )
        )

    instruction_pattern = re.compile(
        r"# AGENTS\.md instructions for ([^\r\n]+)\s*\n+<INSTRUCTIONS>\s*\n?(.*?)\n?</INSTRUCTIONS>",
        flags=re.DOTALL,
    )
    for match in instruction_pattern.finditer(text):
        raw_path = match.group(1).strip()
        path = Path(raw_path).expanduser()
        if path.name != "AGENTS.md":
            continue
        content = match.group(2).strip()
        scope = "project"
        try:
            path.relative_to(home / ".codex")
            scope = "user"
        except ValueError:
            if workspace_root is not None:
                try:
                    path.relative_to(workspace_root)
                except ValueError:
                    continue
        exposures.append(
            ContextArtifactExposure(
                artifact_id=context_artifact_id(path),
                type="codex_instruction" if scope == "user" else "agent_instruction",
                scope=scope,
                source_path=path,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                preview=f"[{path.name} instructions]",
            )
        )

    unique = {exposure.artifact_id: exposure for exposure in exposures}
    return tuple(unique.values())
