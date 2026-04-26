from __future__ import annotations

import time
import urllib.request
from dataclasses import dataclass

from reflect.config import load_litellm_config, load_model_aliases, resolve_config
from reflect.utils import _json_dumps, _json_loads, logger

_DEFAULT_FALLBACK_PRICES: dict[str, dict[str, float]] = {
    # Conservative fallback values; source of truth should be live LiteLLM map.
    "gpt-4o-mini": {
        "input_cost_per_token": 0.00000015,
        "output_cost_per_token": 0.00000060,
    },
    "claude-3-5-sonnet": {
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
    },
}


@dataclass(frozen=True)
class ModelPricing:
    model_key: str
    input_cost_per_token: float = 0.0
    output_cost_per_token: float = 0.0
    cache_creation_cost_per_token: float = 0.0
    cache_read_cost_per_token: float = 0.0


@dataclass(frozen=True)
class PricingResolution:
    requested_model: str
    canonical_model: str
    matched_model_key: str
    source: str  # live | cache | fallback | missing
    confidence: float


@dataclass(frozen=True)
class CostBreakdown:
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    cache_creation_cost_usd: float
    cache_read_cost_usd: float
    total_cost_usd: float
    resolution: PricingResolution


@dataclass(frozen=True)
class PricingTable:
    prices: dict[str, ModelPricing]
    source: str
    fetched_at_unix: int



def canonicalize_model_name(model: str, aliases: dict[str, str] | None = None) -> str:
    value = (model or "").strip().lower()
    if not value:
        return ""

    alias_map = aliases or {}
    if value in alias_map:
        value = alias_map[value].strip().lower()

    if "/" in value:
        value = value.split("/", 1)[-1]

    if "@" in value:
        value = value.split("@", 1)[0]

    # normalize dated model variants e.g. gpt-4o-mini-2024-07-18 -> gpt-4o-mini
    parts = value.split("-")
    if len(parts) >= 4 and parts[-1].isdigit() and len(parts[-1]) == 2 and parts[-2].isdigit():
        value = "-".join(parts[:-3])

    return value



def _fetch_json_url(url: str, timeout_seconds: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310
        return _json_loads(response.read().decode("utf-8"))



def _coerce_model_pricing(model_key: str, payload: dict) -> ModelPricing:
    return ModelPricing(
        model_key=model_key,
        input_cost_per_token=float(payload.get("input_cost_per_token") or 0.0),
        output_cost_per_token=float(payload.get("output_cost_per_token") or 0.0),
        cache_creation_cost_per_token=float(
            payload.get("cache_creation_input_token_cost")
            or payload.get("cache_creation_cost_per_token")
            or 0.0
        ),
        cache_read_cost_per_token=float(
            payload.get("cache_read_input_token_cost")
            or payload.get("cache_read_cost_per_token")
            or 0.0
        ),
    )



def _parse_pricing_map(payload: dict) -> dict[str, ModelPricing]:
    if not isinstance(payload, dict):
        return {}
    prices: dict[str, ModelPricing] = {}
    for model_key, row in payload.items():
        if not isinstance(model_key, str) or not isinstance(row, dict):
            continue
        try:
            prices[model_key.lower()] = _coerce_model_pricing(model_key.lower(), row)
        except (TypeError, ValueError):
            continue
    return prices



def load_pricing_table(cache_ttl_hours: int = 24) -> PricingTable:
    cfg = resolve_config()
    lite = load_litellm_config()
    cache_path = cfg.cache_dir / "litellm-pricing.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    now = int(time.time())
    ttl_seconds = max(1, int(cache_ttl_hours * 3600))

    # 1) try live fetch
    try:
        live_payload = _fetch_json_url(lite.model_prices_url, lite.timeout_seconds)
        live_prices = _parse_pricing_map(live_payload)
        if live_prices:
            cache_path.write_text(_json_dumps({"fetched_at_unix": now, "prices": live_payload}), encoding="utf-8")
            return PricingTable(prices=live_prices, source="live", fetched_at_unix=now)
    except Exception as exc:  # pragma: no cover - network-dependent branch
        logger.debug("LiteLLM live pricing fetch failed: %s", exc)

    # 2) use fresh cache
    if cache_path.exists():
        try:
            cache_payload = _json_loads(cache_path.read_text(encoding="utf-8"))
            fetched = int(cache_payload.get("fetched_at_unix") or 0)
            prices_payload = cache_payload.get("prices") or {}
            prices = _parse_pricing_map(prices_payload)
            if prices and fetched > 0 and (now - fetched) <= ttl_seconds:
                return PricingTable(prices=prices, source="cache", fetched_at_unix=fetched)
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug("LiteLLM pricing cache read failed: %s", exc)

    # 3) static fallback
    fallback_prices = {
        key: _coerce_model_pricing(key, row)
        for key, row in _DEFAULT_FALLBACK_PRICES.items()
    }
    return PricingTable(prices=fallback_prices, source="fallback", fetched_at_unix=now)



def calculate_cost(
    tokens: dict[str, int],
    model: str,
    pricing_table: PricingTable,
    aliases: dict[str, str] | None = None,
) -> CostBreakdown:
    aliases = aliases or load_model_aliases()
    requested_model = model or ""
    canonical = canonicalize_model_name(requested_model, aliases)

    model_pricing = pricing_table.prices.get(canonical) or pricing_table.prices.get(requested_model.lower())
    if model_pricing is None:
        resolution = PricingResolution(
            requested_model=requested_model,
            canonical_model=canonical,
            matched_model_key="",
            source="missing",
            confidence=0.0,
        )
        model_pricing = ModelPricing(model_key="")
    else:
        confidence = 0.9 if model_pricing.model_key == canonical else 0.6
        resolution = PricingResolution(
            requested_model=requested_model,
            canonical_model=canonical,
            matched_model_key=model_pricing.model_key,
            source=pricing_table.source,
            confidence=confidence,
        )

    input_tokens = int(tokens.get("input", 0) or 0)
    output_tokens = int(tokens.get("output", 0) or 0)
    cache_creation_tokens = int(tokens.get("cache_creation", 0) or 0)
    cache_read_tokens = int(tokens.get("cache_read", 0) or 0)

    input_cost = input_tokens * model_pricing.input_cost_per_token
    output_cost = output_tokens * model_pricing.output_cost_per_token
    cache_creation_cost = cache_creation_tokens * model_pricing.cache_creation_cost_per_token
    cache_read_cost = cache_read_tokens * model_pricing.cache_read_cost_per_token
    total = input_cost + output_cost + cache_creation_cost + cache_read_cost

    return CostBreakdown(
        model=requested_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
        cache_creation_cost_usd=cache_creation_cost,
        cache_read_cost_usd=cache_read_cost,
        total_cost_usd=total,
        resolution=resolution,
    )
