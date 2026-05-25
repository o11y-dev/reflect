from __future__ import annotations

from datetime import datetime

from pydantic import Field

from reflect.schema.base import RawAttributes, ReflectModel


class RawEvent(ReflectModel):
    id: str
    source_id: str
    source_type: str
    event_type: str
    observed_at: datetime
    received_at: datetime
    attrs: RawAttributes = Field(default_factory=RawAttributes)
    body: dict[str, object] = Field(default_factory=dict)
    content_hash: str
    normalized_status: str = "pending"
    normalization_error: str | None = None
