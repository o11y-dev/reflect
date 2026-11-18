from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from reflect.config import load_model_aliases
from reflect.cost_aliases import CostAliasResult, _observed_sql_models, ensure_cost_aliases
from reflect.pricing import (
    PricingTable,
    calculate_cost,
    load_pricing_table,
    pricing_inputs_fingerprint,
)
from reflect.utils import _json_dumps, _json_loads


@dataclass(frozen=True)
class CostRefreshInputs:
    pricing_table: PricingTable
    aliases: dict[str, str]
    alias_result: CostAliasResult
    fingerprint: str
    previous_fingerprint: str
    reason: str
    model_fingerprints: dict[str, str]
    previous_model_fingerprints: dict[str, str]

    @property
    def requires_full_refresh(self) -> bool:
        # A newly captured model is covered by its changed session scope. Only
        # changed rates/resolution for previously observed models need history.
        return not self.previous_fingerprint or any(
            self.model_fingerprints.get(model) != fingerprint
            for model, fingerprint in self.previous_model_fingerprints.items()
        )


class CostRefreshState:
    """Track the external pricing inputs used by persisted cost estimates."""

    METADATA_KEY = "cost_pricing_inputs_v2"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def inspect(
        self,
        *,
        alias_path: Path | None = None,
        session_ids: set[str] | None = None,
    ) -> CostRefreshInputs:
        pricing_table = load_pricing_table()
        previous_fingerprint, previous_models, catalogue_fingerprint = self._stored_inputs()
        # The initial inventory covers all retained history. Later runs only
        # discover names in changed sessions and re-resolve the small inventory.
        models = sorted(set(previous_models) | set(_observed_sql_models(
            self.conn, session_ids=session_ids if previous_fingerprint else None,
        )))
        alias_result = ensure_cost_aliases(
            self.conn,
            alias_path=alias_path,
            pricing_table=pricing_table,
            observed_models=models,
        )
        aliases = load_model_aliases(alias_result.alias_path)
        model_fingerprints = {
            model: _resolved_model_fingerprint(model, pricing_table, aliases)
            for model in models
        }
        if catalogue_fingerprint and catalogue_fingerprint == pricing_inputs_fingerprint(pricing_table, aliases):
            previous_fingerprint = f"catalogue:{catalogue_fingerprint}"
            previous_models = model_fingerprints.copy()
        fingerprint = _fingerprint(sorted(model_fingerprints.items()))
        changed_existing = any(model_fingerprints.get(model) != value for model, value in previous_models.items())
        if previous_fingerprint and not changed_existing:
            reason = "pricing inputs are current"
        elif previous_fingerprint:
            reason = "pricing rates or model aliases changed"
        else:
            reason = "pricing inputs were not recorded"
        return CostRefreshInputs(
            pricing_table=pricing_table,
            aliases=aliases,
            alias_result=alias_result,
            fingerprint=fingerprint,
            previous_fingerprint=previous_fingerprint,
            reason=reason,
            model_fingerprints=model_fingerprints,
            previous_model_fingerprints=previous_models,
        )

    def mark_current(self, inputs: CostRefreshInputs) -> None:
        if inputs.fingerprint == inputs.previous_fingerprint:
            return
        self.conn.execute(
            """
            INSERT INTO store_metadata(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (
                self.METADATA_KEY,
                _json_dumps({"fingerprint": inputs.fingerprint, "models": inputs.model_fingerprints}),
                datetime.now(tz=UTC).isoformat(),
            ),
        )
        self.conn.commit()

    def _stored_inputs(self) -> tuple[str, dict[str, str], str]:
        row = self.conn.execute(
            "SELECT value FROM store_metadata WHERE key = ?",
            (self.METADATA_KEY,),
        ).fetchone()
        if row:
            try:
                payload = _json_loads(row[0])
                if isinstance(payload, dict) and isinstance(payload.get("catalogue_fingerprint"), str):
                    return "", {}, payload["catalogue_fingerprint"]
                models = payload["models"]
                if (isinstance(models, dict)
                        and all(isinstance(key, str) and isinstance(value, str) for key, value in models.items())
                        and payload["fingerprint"] == _fingerprint(sorted(models.items()))):
                    return payload["fingerprint"], models, ""
            except (ValueError, TypeError, KeyError):
                pass
        return "", {}, ""


def _fingerprint(payload: object) -> str:
    return hashlib.sha256(_json_dumps(payload).encode()).hexdigest()


def _resolved_model_fingerprint(model: str, table: PricingTable, aliases: dict[str, str]) -> str:
    # Unit counts exercise the same four rate buckets and matching rules as
    # persisted costs, including absent rates and missing-to-priced transitions.
    cost = calculate_cost(
        {"input": 1, "output": 1, "cache_creation": 1, "cache_read": 1},
        model, table, aliases=aliases,
    )
    return _fingerprint([
        table.pricing_unit, cost.resolution.canonical_model, cost.resolution.matched_model_key,
        cost.input_cost_usd, cost.output_cost_usd, cost.cache_creation_cost_usd,
        cost.cache_read_cost_usd, cost.missing_price_buckets,
    ])
