from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

_IDENTITY_FIELDS = (
    "schema_version",
    "slug",
    "behavior_type",
    "suggested_artifact",
    "description",
    "steps",
    "abstain_when",
    "verification",
    "source_markdown",
    "workflow_contract",
)


def workflow_proposal_signature(title: str, content: Mapping[str, Any]) -> str:
    """Identify the exact reviewable artifact while ignoring evidence provenance."""

    payload = {
        "title": str(title).strip(),
        "content": {field: content.get(field) for field in _IDENTITY_FIELDS},
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
