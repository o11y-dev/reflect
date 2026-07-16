from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from typing import Any

from reflect.improvements.repository import utc_now


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


class TeamBundleService:
    """Export and import signed, aggregate-only team beta bundles."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def export(self, *, signer_id: str, signing_key: bytes) -> dict[str, Any]:
        if len(signing_key) < 32:
            raise ValueError("Team bundle signing keys must be at least 32 bytes")
        observations = [
            {
                "rule_id": row[0],
                "rule_version": row[1],
                "category": row[2],
                "severity": row[3],
                "status": row[4],
                "observation_count": row[5],
                "affected_sessions": row[6],
                "mean_impact": row[7],
                "mean_confidence": row[8],
            }
            for row in self.conn.execute(
                """
                SELECT rule_id, rule_version, category, severity, status,
                       COUNT(*), SUM(affected_session_count), AVG(impact_score), AVG(confidence)
                FROM observations
                GROUP BY rule_id, rule_version, category, severity, status
                ORDER BY AVG(impact_score) DESC
                """
            ).fetchall()
        ]
        workflows = [
            {
                "action_type": row[0],
                "status": row[1],
                "candidate_count": row[2],
                "support_count": row[3],
                "mean_confidence": row[4],
            }
            for row in self.conn.execute(
                """
                SELECT action_type, status, COUNT(*), SUM(support_count), AVG(confidence)
                FROM workflow_candidates GROUP BY action_type, status
                """
            ).fetchall()
        ]
        measurements = [
            {
                "metric_name": row[0],
                "verdict": row[1],
                "measurement_count": row[2],
                "mean_delta": row[3],
                "mean_confidence": row[4],
            }
            for row in self.conn.execute(
                """
                SELECT metric_name, verdict, COUNT(*), AVG(delta), AVG(confidence)
                FROM measurements GROUP BY metric_name, verdict
                """
            ).fetchall()
        ]
        payload = {
            "schema_version": 1,
            "generated_at": utc_now(),
            "signer_id": signer_id,
            "redaction_policy": {
                "aggregate_only": True,
                "excluded": [
                    "prompts",
                    "responses",
                    "paths",
                    "session_ids",
                    "entity_ids",
                    "raw_evidence",
                    "operator_reasons",
                ],
            },
            "summary": {
                "observations": observations,
                "workflows": workflows,
                "measurements": measurements,
            },
        }
        content_hash = hashlib.sha256(_canonical(payload)).hexdigest()
        signature = hmac.new(signing_key, _canonical(payload), hashlib.sha256).hexdigest()
        return {"payload": payload, "content_hash": content_hash, "signature": signature}

    def import_bundle(self, bundle: dict[str, Any], *, signing_key: bytes) -> dict[str, Any]:
        payload = bundle.get("payload")
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("Unsupported or malformed team bundle")
        if payload.get("redaction_policy", {}).get("aggregate_only") is not True:
            raise ValueError("Team bundles must declare aggregate-only redaction")
        canonical = _canonical(payload)
        expected_hash = hashlib.sha256(canonical).hexdigest()
        expected_signature = hmac.new(signing_key, canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(bundle.get("content_hash") or ""), expected_hash):
            raise ValueError("Team bundle content hash does not match")
        if not hmac.compare_digest(str(bundle.get("signature") or ""), expected_signature):
            raise ValueError("Team bundle signature is invalid")
        existing = self.conn.execute(
            "SELECT id FROM team_bundle_imports WHERE content_hash = ?",
            (expected_hash,),
        ).fetchone()
        if existing:
            return {"id": existing[0], "content_hash": expected_hash, "status": "already_imported"}
        import_id = f"team_bundle_{uuid.uuid4().hex}"
        self.conn.execute(
            """
            INSERT INTO team_bundle_imports(
              id, bundle_version, signer_id, signature, content_hash,
              redaction_policy_json, summary_json, imported_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                import_id,
                str(payload.get("signer_id") or "unknown"),
                expected_signature,
                expected_hash,
                json.dumps(payload["redaction_policy"], sort_keys=True),
                json.dumps(payload.get("summary") or {}, sort_keys=True),
                utc_now(),
            ),
        )
        self.conn.commit()
        return {"id": import_id, "content_hash": expected_hash, "status": "imported"}
