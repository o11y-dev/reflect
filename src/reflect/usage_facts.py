"""Pure usage normalization shared by persisted pricing and read models."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UsageFacts:
    buckets: dict[str, int | None]
    tokens: dict[str, int]
    identity: tuple[Any, ...]
    present: bool


_BUCKETS = {
    "input_tokens": ("input_tokens", ("gen_ai.usage.input_tokens",)),
    "output_tokens": ("output_tokens", ("gen_ai.usage.output_tokens",)),
    "cache_read_tokens": ("cache_read_input_tokens", ("gen_ai.usage.cache_read.input_tokens", "hook.cache_read_tokens")),
    "cache_write_tokens": ("cache_creation_input_tokens", ("gen_ai.usage.cache_creation.input_tokens", "hook.cache_write_tokens")),
}


def usage_facts(row: Mapping[str, Any], attrs: Mapping[str, Any]) -> UsageFacts:
    """Preserve optional buckets while converting inclusive input only once.

    Legacy nonzero columns remain observed subtotals. An inclusive input whose
    cache buckets are missing cannot be represented as uncached input.
    """
    buckets: dict[str, int | None] = {}
    for field, (_, keys) in _BUCKETS.items():
        value = next((attrs[k] for k in keys if attrs.get(k) is not None), None)
        buckets[field] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None
    inclusive = attrs.get("reflect.usage.input_semantics") == "inclusive"
    if inclusive:
        u, r, w = (buckets[k] for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
        buckets["input_tokens"] = u-r-w if None not in (u, r, w) and u >= r+w else None
    tokens = {
        column: int(buckets[field] if buckets[field] is not None else row.get(column) or 0)
        for field, (column, _) in _BUCKETS.items()
    }
    if inclusive and buckets["input_tokens"] is None:
        tokens["input_tokens"] = 0
    tokens["reasoning_output_tokens"] = int(row.get("reasoning_output_tokens") or 0)
    present = any(value is not None for value in buckets.values()) or any(row.get(column) for column in tokens)
    request = attrs.get("gen_ai.request.id") or attrs.get("reflect.usage.identity")
    identity = (row["session_id"], row.get("provider"), request) if request else (row["id"],)
    return UsageFacts(buckets, tokens, identity, present)
