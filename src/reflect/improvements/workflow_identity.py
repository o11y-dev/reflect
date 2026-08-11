from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_CONTRACT_FIELDS = (
    "schema_version",
    "behavior_type",
    "description",
    "abstain_when",
    "verification",
)


def _hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def workflow_contract_signature(content: Mapping[str, Any]) -> str:
    """Return the stable identity of the procedure described by ``content``.

    Contract-aware detectors own their signature. Other workflow sources use the
    same normalized procedure fields as their canonical contract definition.
    Evidence provenance and review wording never participate in this identity.
    """

    contract = content.get("workflow_contract")
    if isinstance(contract, Mapping):
        signature = str(contract.get("signature_hash") or "").strip()
        if signature:
            return signature
    procedure = content.get("steps") or content.get("source_markdown") or ""
    return _hash(
        {
            **{field: content.get(field) for field in _CONTRACT_FIELDS},
            "procedure": procedure,
        }
    )


def workflow_revision_hash(title: str, content: Mapping[str, Any]) -> str:
    """Identify the exact artifact presented for review."""

    return _hash(
        {
            "title": str(title).strip(),
            "content": dict(content),
        }
    )


@dataclass(frozen=True, slots=True)
class WorkflowIdentity:
    contract_signature: str
    revision_hash: str

    @classmethod
    def from_content(
        cls,
        title: str,
        content: Mapping[str, Any],
    ) -> WorkflowIdentity:
        return cls(
            contract_signature=workflow_contract_signature(content),
            revision_hash=workflow_revision_hash(title, content),
        )


def workflow_identity_from_json(title: object, content_json: object) -> WorkflowIdentity:
    """Parse persisted candidate content for the one-way schema migration."""

    try:
        raw = json.loads(str(content_json or "{}"))
    except (TypeError, json.JSONDecodeError):
        raw = {}
    content = raw if isinstance(raw, Mapping) else {}
    return WorkflowIdentity.from_content(str(title or ""), content)
