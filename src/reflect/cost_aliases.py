from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from reflect.config import load_model_aliases, resolve_config
from reflect.pricing import (
    PricingTable,
    calculate_cost,
    canonicalize_model_name,
    load_pricing_table,
)
from reflect.utils import _json_loads


@dataclass(frozen=True)
class CostAliasResult:
    alias_path: Path
    observed_models: int
    resolved_models: int
    added_aliases: dict[str, str]
    unresolved_models: tuple[str, ...]


def ensure_cost_aliases(
    conn: sqlite3.Connection,
    *,
    alias_path: Path | None = None,
    pricing_table: PricingTable | None = None,
) -> CostAliasResult:
    """Append missing model aliases inferred from observed SQL model names."""

    cfg = resolve_config()
    target_path = alias_path or cfg.model_aliases_path
    table = pricing_table or load_pricing_table()
    existing_aliases = load_model_aliases(target_path)
    observed_models = _observed_sql_models(conn)

    additions: dict[str, str] = {}
    resolved_count = 0
    unresolved: list[str] = []

    for model in observed_models:
        key = model.strip().lower()
        if not key:
            continue
        aliases_for_resolution = {**existing_aliases, **additions}
        if _model_has_price(key, table, aliases_for_resolution):
            resolved_count += 1
            continue
        if key in existing_aliases:
            unresolved.append(key)
            continue
        inferred = infer_pricing_alias(key, table)
        if inferred:
            additions[key] = inferred
            resolved_count += 1
        else:
            unresolved.append(key)

    if additions:
        _append_aliases(target_path, additions)

    return CostAliasResult(
        alias_path=target_path,
        observed_models=len(observed_models),
        resolved_models=resolved_count,
        added_aliases=additions,
        unresolved_models=tuple(unresolved),
    )


def infer_pricing_alias(model: str, pricing_table: PricingTable) -> str:
    """Return a canonical pricing key only when the model match is unambiguous."""

    prices = pricing_table.prices
    candidates = _direct_model_candidates(model)
    for candidate in candidates:
        if candidate in prices:
            return candidate

    observed_signature = _model_signature(model)
    if not observed_signature:
        return ""
    signature_matches = [
        price_key
        for price_key in prices
        if _model_signature(price_key) == observed_signature
    ]
    if len(signature_matches) == 1:
        return signature_matches[0]
    return ""


def _observed_sql_models(conn: sqlite3.Connection) -> list[str]:
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT model, SUM(count) AS count
            FROM (
              SELECT
                COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) AS model,
                COUNT(*) AS count
              FROM llm_calls
              WHERE COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) IS NOT NULL
              GROUP BY model
              UNION ALL
              SELECT
                COALESCE(
                  NULLIF(json_extract(raw_attrs_json, '$."gen_ai.response.model"'), ''),
                  NULLIF(json_extract(raw_attrs_json, '$."gen_ai.request.model"'), '')
                ) AS model,
                COUNT(*) AS count
              FROM steps
              WHERE COALESCE(
                NULLIF(json_extract(raw_attrs_json, '$."gen_ai.response.model"'), ''),
                NULLIF(json_extract(raw_attrs_json, '$."gen_ai.request.model"'), '')
              ) IS NOT NULL
              GROUP BY model
            )
            WHERE model IS NOT NULL
            GROUP BY model
            ORDER BY count DESC, model ASC
            """
        ).fetchall()
    finally:
        conn.row_factory = previous_row_factory
    return [str(row["model"]).strip().lower() for row in rows if str(row["model"] or "").strip()]


def _model_has_price(model: str, pricing_table: PricingTable, aliases: dict[str, str]) -> bool:
    breakdown = calculate_cost(
        {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0},
        model,
        pricing_table,
        aliases=aliases,
    )
    return bool(breakdown.resolution.matched_model_key)


def _direct_model_candidates(model: str) -> list[str]:
    value = model.strip().lower()
    candidates = [
        value,
        canonicalize_model_name(value, aliases={}),
    ]
    if "/" in value:
        candidates.append(value.split("/", 1)[-1])
    if "@" in value:
        candidates.append(value.split("@", 1)[0])

    stripped_date = re.sub(r"[-_:]?\d{8}$", "", value)
    if stripped_date != value:
        candidates.append(canonicalize_model_name(stripped_date, aliases={}))

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip().lower()
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def _model_signature(model: str) -> tuple[str, ...]:
    canonical = canonicalize_model_name(model, aliases={})
    tokens = re.findall(r"[a-z]+|\d+(?:\.\d+)?", canonical.lower())
    if not tokens:
        return ()
    return tuple(sorted(tokens))


def _append_aliases(alias_path: Path, additions: dict[str, str]) -> None:
    payload: object
    if alias_path.exists():
        try:
            payload = _json_loads(alias_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    else:
        payload = {"aliases": {}}

    if isinstance(payload, dict) and isinstance(payload.get("aliases"), dict):
        alias_map = payload["aliases"]
    elif isinstance(payload, dict):
        alias_map = payload
    else:
        payload = {"aliases": {}}
        alias_map = payload["aliases"]

    assert isinstance(alias_map, dict)
    existing_keys = {
        str(key).strip().lower()
        for key in alias_map
        if isinstance(key, str) and key.strip()
    }
    changed = False
    for source, target in sorted(additions.items()):
        if source in existing_keys:
            continue
        alias_map[source] = target
        existing_keys.add(source)
        changed = True

    if changed:
        alias_path.parent.mkdir(parents=True, exist_ok=True)
        import json

        alias_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
