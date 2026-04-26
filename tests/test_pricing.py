from __future__ import annotations

import pytest

from reflect.pricing import (
    PricingTable,
    calculate_cost,
    canonicalize_model_name,
    load_pricing_table,
)


class TestCanonicalizeModelName:
    def test_alias_then_normalize(self):
        aliases = {"anthropic/claude-sonnet-latest": "claude-3-5-sonnet"}
        assert canonicalize_model_name("anthropic/claude-sonnet-latest", aliases) == "claude-3-5-sonnet"

    def test_strips_provider_and_revision(self):
        assert canonicalize_model_name("openai/gpt-4o-mini@2024-07-18") == "gpt-4o-mini"


class TestLoadPricingTable:
    def test_prefers_fresh_cache_before_network(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REFLECT_HOME", str(tmp_path / ".reflect"))
        cache_path = tmp_path / ".reflect" / "cache" / "litellm-pricing.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            '{"fetched_at_unix": 9999999999, "prices": {"gpt-4o-mini": {"input_cost_per_token": 0.1, "output_cost_per_token": 0.2}}}'
        )

        from reflect import pricing as pricing_mod

        def _raise(*_args, **_kwargs):
            raise AssertionError("network should not be called when cache is fresh")

        monkeypatch.setattr(pricing_mod, "_fetch_json_url", _raise)
        table = load_pricing_table()
        assert table.source == "cache"

    def test_uses_live_prices_when_available(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REFLECT_HOME", str(tmp_path / ".reflect"))
        monkeypatch.setenv("REFLECT_LITELLM_MODEL_PRICES_URL", "https://example.invalid/prices.json")

        from reflect import pricing as pricing_mod

        def _fake_fetch(_url: str, _timeout: float, api_key: str = ""):
            return {
                "gpt-4o-mini": {
                    "input_cost_per_token": 1.0,
                    "output_cost_per_token": 2.0,
                }
            }

        monkeypatch.setattr(pricing_mod, "_fetch_json_url", _fake_fetch)

        table = load_pricing_table()

        assert table.source == "live"
        assert "gpt-4o-mini" in table.prices

    def test_falls_back_when_live_fetch_fails(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REFLECT_HOME", str(tmp_path / ".reflect"))

        from reflect import pricing as pricing_mod

        def _raise(*_args, **_kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr(pricing_mod, "_fetch_json_url", _raise)

        table = load_pricing_table()

        assert table.source in {"fallback", "cache"}
        assert len(table.prices) > 0


class TestCalculateCost:
    def test_calculates_cost_using_resolution(self):
        table = PricingTable(
            prices={
                "gpt-4o-mini": pricing_row(0.1, 0.2, 0.05, 0.01),
            },
            source="live",
            fetched_at_unix=0,
        )

        breakdown = calculate_cost(
            tokens={"input": 10, "output": 20, "cache_creation": 4, "cache_read": 5},
            model="openai/gpt-4o-mini",
            pricing_table=table,
        )

        assert breakdown.total_cost_usd == (10 * 0.1) + (20 * 0.2) + (4 * 0.05) + (5 * 0.01)
        assert breakdown.resolution.matched_model_key == "gpt-4o-mini"


class TestFetchJsonUrl:
    def test_rejects_non_http_scheme(self):
        from reflect import pricing as pricing_mod

        with pytest.raises(ValueError):
            pricing_mod._fetch_json_url("file:///tmp/test.json", 1.0)

    def test_attaches_bearer_header(self, monkeypatch):
        from reflect import pricing as pricing_mod

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        captured = {}

        def _fake_urlopen(req, timeout=0):
            captured["auth"] = req.headers.get("Authorization")
            return _FakeResponse()

        monkeypatch.setattr(pricing_mod.urllib.request, "urlopen", _fake_urlopen)
        payload = pricing_mod._fetch_json_url("https://example.com/prices.json", 1.0, api_key="secret")
        assert payload["ok"] is True
        assert captured["auth"] == "Bearer secret"


# local helper to keep tests concise
from reflect.pricing import ModelPricing  # noqa: E402


def pricing_row(input_cost: float, output_cost: float, cache_create: float, cache_read: float) -> ModelPricing:
    return ModelPricing(
        model_key="gpt-4o-mini",
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
        cache_creation_cost_per_token=cache_create,
        cache_read_cost_per_token=cache_read,
    )
