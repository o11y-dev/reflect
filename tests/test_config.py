from __future__ import annotations

import json
from pathlib import Path

from reflect.config import load_litellm_config, load_model_aliases, resolve_config


class TestResolveConfig:
    def test_defaults_under_reflect_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REFLECT_HOME", str(tmp_path / ".reflect"))
        monkeypatch.delenv("REFLECT_CONFIG_DIR", raising=False)
        monkeypatch.delenv("REFLECT_CACHE_DIR", raising=False)

        cfg = resolve_config()

        assert cfg.reflect_home == tmp_path / ".reflect"
        assert cfg.config_dir == cfg.reflect_home / "config"
        assert cfg.cache_dir == cfg.reflect_home / "cache"
        assert cfg.model_aliases_path == cfg.config_dir / "model-aliases.json"

    def test_explicit_overrides(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REFLECT_HOME", str(tmp_path / "base"))
        monkeypatch.setenv("REFLECT_CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setenv("REFLECT_CACHE_DIR", str(tmp_path / "cache"))

        cfg = resolve_config()

        assert cfg.reflect_home == tmp_path / "base"
        assert cfg.config_dir == tmp_path / "cfg"
        assert cfg.cache_dir == tmp_path / "cache"
        assert cfg.litellm_config_path == tmp_path / "cfg" / "litellm.json"


class TestLoadModelAliases:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_model_aliases(tmp_path / "does-not-exist.json") == {}

    def test_nested_aliases_shape(self, tmp_path):
        path = tmp_path / "model-aliases.json"
        path.write_text(json.dumps({"aliases": {"anthropic/claude-sonnet": "claude-sonnet-4"}}))

        aliases = load_model_aliases(path)

        assert aliases == {"anthropic/claude-sonnet": "claude-sonnet-4"}

    def test_flat_aliases_shape(self, tmp_path):
        path = tmp_path / "model-aliases.json"
        path.write_text(json.dumps({"gpt-4o-mini": "gpt-4o-mini-2024-07-18"}))

        aliases = load_model_aliases(path)

        assert aliases == {"gpt-4o-mini": "gpt-4o-mini-2024-07-18"}

    def test_invalid_entries_are_ignored(self, tmp_path):
        path = tmp_path / "model-aliases.json"
        path.write_text(json.dumps({"aliases": {"ok": "mapped", "": "x", "a": "", "nonstr": 1}}))

        aliases = load_model_aliases(path)

        assert aliases == {"ok": "mapped"}


class TestLoadLiteLLMConfig:
    def test_defaults_when_missing(self, tmp_path):
        cfg = load_litellm_config(tmp_path / "missing.json")

        assert cfg.base_url == "https://litellm.ai"
        assert cfg.model_prices_url.endswith("model_prices_and_context_window.json")
        assert cfg.api_key_env == "LITELLM_API_KEY"
        assert cfg.timeout_seconds == 10.0

    def test_file_values(self, tmp_path):
        path = tmp_path / "litellm.json"
        path.write_text(
            json.dumps(
                {
                    "base_url": "https://litellm.internal",
                    "model_prices_url": "https://litellm.internal/prices.json",
                    "api_key_env": "INTERNAL_LITELLM_KEY",
                    "timeout_seconds": 3.5,
                }
            )
        )
        cfg = load_litellm_config(path)

        assert cfg.base_url == "https://litellm.internal"
        assert cfg.model_prices_url == "https://litellm.internal/prices.json"
        assert cfg.api_key_env == "INTERNAL_LITELLM_KEY"
        assert cfg.timeout_seconds == 3.5

    def test_env_overrides(self, tmp_path, monkeypatch):
        path = tmp_path / "litellm.json"
        path.write_text(json.dumps({"base_url": "https://from-file"}))
        monkeypatch.setenv("REFLECT_LITELLM_BASE_URL", "https://from-env")
        monkeypatch.setenv("REFLECT_LITELLM_MODEL_PRICES_URL", "https://from-env/prices.json")
        monkeypatch.setenv("REFLECT_LITELLM_API_KEY_ENV", "ENV_KEY")
        monkeypatch.setenv("REFLECT_LITELLM_TIMEOUT_SECONDS", "22")

        cfg = load_litellm_config(path)

        assert cfg.base_url == "https://from-env"
        assert cfg.model_prices_url == "https://from-env/prices.json"
        assert cfg.api_key_env == "ENV_KEY"
        assert cfg.timeout_seconds == 22.0
