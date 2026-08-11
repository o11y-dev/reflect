from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from reflect.config import load_model_aliases
from reflect.cost_aliases import CostAliasResult, ensure_cost_aliases
from reflect.pricing import PricingTable, load_pricing_table, pricing_inputs_fingerprint


@dataclass(frozen=True)
class CostRefreshInputs:
    pricing_table: PricingTable
    aliases: dict[str, str]
    alias_result: CostAliasResult
    fingerprint: str
    previous_fingerprint: str
    reason: str

    @property
    def requires_full_refresh(self) -> bool:
        return self.fingerprint != self.previous_fingerprint


class CostRefreshState:
    """Track the external pricing inputs used by persisted cost estimates."""

    METADATA_KEY = "cost_pricing_inputs_v1"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def inspect(
        self,
        *,
        alias_path: Path | None = None,
        session_ids: set[str] | None = None,
    ) -> CostRefreshInputs:
        pricing_table = load_pricing_table()
        aliases = load_model_aliases(alias_path)
        previous_fingerprint = self._stored_fingerprint()
        initial_fingerprint = pricing_inputs_fingerprint(pricing_table, aliases)
        alias_result = ensure_cost_aliases(
            self.conn,
            alias_path=alias_path,
            pricing_table=pricing_table,
            session_ids=None if initial_fingerprint != previous_fingerprint else session_ids,
        )
        aliases = load_model_aliases(alias_result.alias_path)
        fingerprint = pricing_inputs_fingerprint(pricing_table, aliases)
        if fingerprint == previous_fingerprint:
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
        )

    def mark_current(self, inputs: CostRefreshInputs) -> None:
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
                inputs.fingerprint,
                datetime.now(tz=UTC).isoformat(),
            ),
        )
        self.conn.commit()

    def _stored_fingerprint(self) -> str:
        row = self.conn.execute(
            "SELECT value FROM store_metadata WHERE key = ?",
            (self.METADATA_KEY,),
        ).fetchone()
        return str(row[0]) if row else ""
