from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ReflectModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class RawAttributes(BaseModel):
    model_config = ConfigDict(extra="allow")
