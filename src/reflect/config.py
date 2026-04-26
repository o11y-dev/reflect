from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from reflect.utils import _json_loads, logger


@dataclass(frozen=True)
class ReflectConfig:
    """Centralized filesystem configuration for reflect runtime settings."""

    reflect_home: Path
    config_dir: Path
    cache_dir: Path
    state_dir: Path
    model_aliases_path: Path
    litellm_config_path: Path


@dataclass(frozen=True)
class LiteLLMConfig:
    """Runtime LiteLLM configuration for pricing / model metadata sources."""

    base_url: str
    model_prices_url: str
    api_key_env: str
    timeout_seconds: float



def resolve_config() -> ReflectConfig:
    """Resolve canonical config/cache/state paths for the current runtime.

    Environment overrides:
    - REFLECT_HOME: base directory for reflect state/config/cache
    - REFLECT_CONFIG_DIR: explicit config directory (defaults to REFLECT_HOME/config)
    - REFLECT_CACHE_DIR: explicit cache directory (defaults to REFLECT_HOME/cache)
    """

    reflect_home = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect")).expanduser()
    config_dir = Path(os.environ.get("REFLECT_CONFIG_DIR", reflect_home / "config")).expanduser()
    cache_dir = Path(os.environ.get("REFLECT_CACHE_DIR", reflect_home / "cache")).expanduser()

    return ReflectConfig(
        reflect_home=reflect_home,
        config_dir=config_dir,
        cache_dir=cache_dir,
        state_dir=reflect_home / "state",
        model_aliases_path=config_dir / "model-aliases.json",
        litellm_config_path=config_dir / "litellm.json",
    )



def load_model_aliases(path: Path | None = None) -> dict[str, str]:
    """Load model alias map from JSON config.

    Expected file shape:
    {
      "aliases": {
        "provider/model-a": "canonical-model",
        "foo": "bar"
      }
    }

    For convenience, a flat object is also accepted:
    {
      "provider/model-a": "canonical-model"
    }
    """

    cfg = resolve_config()
    alias_path = path or cfg.model_aliases_path

    if not alias_path.exists():
        return {}

    try:
        payload = _json_loads(alias_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("Failed to read model aliases from %s: %s", alias_path, exc)
        return {}

    raw_aliases = payload.get("aliases") if isinstance(payload, dict) and isinstance(payload.get("aliases"), dict) else payload
    if not isinstance(raw_aliases, dict):
        return {}

    aliases: dict[str, str] = {}
    for key, value in raw_aliases.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        src = key.strip()
        dst = value.strip()
        if not src or not dst:
            continue
        aliases[src] = dst
    return aliases


def load_litellm_config(path: Path | None = None) -> LiteLLMConfig:
    """Load LiteLLM config with file defaults and env overrides.

    Config file path defaults to `~/.reflect/config/litellm.json`.
    Accepted keys:
      - base_url
      - model_prices_url
      - api_key_env
      - timeout_seconds

    Environment overrides:
      - REFLECT_LITELLM_BASE_URL
      - REFLECT_LITELLM_MODEL_PRICES_URL
      - REFLECT_LITELLM_API_KEY_ENV
      - REFLECT_LITELLM_TIMEOUT_SECONDS
    """

    cfg = resolve_config()
    config_path = path or cfg.litellm_config_path

    payload: dict = {}
    if config_path.exists():
        try:
            loaded = _json_loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.warning("Failed to read LiteLLM config from %s: %s", config_path, exc)

    base_url = str(payload.get("base_url") or "https://litellm.ai")
    model_prices_url = str(
        payload.get("model_prices_url")
        or "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
    )
    api_key_env = str(payload.get("api_key_env") or "LITELLM_API_KEY")
    timeout_raw = payload.get("timeout_seconds", 10.0)
    try:
        timeout_seconds = float(timeout_raw)
    except (TypeError, ValueError):
        timeout_seconds = 10.0
    if timeout_seconds <= 0:
        timeout_seconds = 10.0

    base_url = os.environ.get("REFLECT_LITELLM_BASE_URL", base_url).strip() or base_url
    model_prices_url = (
        os.environ.get("REFLECT_LITELLM_MODEL_PRICES_URL", model_prices_url).strip() or model_prices_url
    )
    api_key_env = os.environ.get("REFLECT_LITELLM_API_KEY_ENV", api_key_env).strip() or api_key_env
    env_timeout = os.environ.get("REFLECT_LITELLM_TIMEOUT_SECONDS")
    if env_timeout:
        try:
            timeout_seconds = float(env_timeout)
        except ValueError:
            logger.warning("Invalid REFLECT_LITELLM_TIMEOUT_SECONDS=%r; using %s", env_timeout, timeout_seconds)

    return LiteLLMConfig(
        base_url=base_url,
        model_prices_url=model_prices_url,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
    )
