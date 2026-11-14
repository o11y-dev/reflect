"""One-way repair of comparisons pinned to the version-1 assessment format."""
from __future__ import annotations

import json
import sqlite3

from reflect.improvements.measurement import MeasurementService
from reflect.improvements.measurement_validity import COMPARISON_POLICY_VERSION


def reassess_upgraded_measurements(conn: sqlite3.Connection) -> int:
    """Append current assessments of frozen members, keeping the old ledger intact.

    Only a retained snapshot matching the pinned digest can prove the v1 -> v2
    transition. Missing snapshots, missing members and same-version changes do
    not authorize renewal. Current eligibility and metrics are evaluated again.
    The caller owns the transaction.
    """
    repaired = 0
    active = conn.execute("""
        SELECT i.id,wv.candidate_id FROM interventions i
        JOIN workflow_versions wv ON wv.id=i.workflow_version_id WHERE i.status='active'
    """).fetchall()
    for intervention_id, candidate_id in active:
        latest = conn.execute("""SELECT cohort_json FROM measurements
            WHERE intervention_id=? ORDER BY measured_at DESC LIMIT 1""", (intervention_id,)).fetchone()
        if latest is None:
            continue
        cohort = json.loads(latest[0])
        pinned = cohort.get('evidence_digests') or {}
        if (cohort.get('policy_version') != COMPARISON_POLICY_VERSION
                or not cohort.get('baseline_frozen') or not pinned
                or len(cohort.get('after_execution_unit_ids', []))
                < cohort.get('minimum_after_execution_units', 1)):
            continue
        changed = {}
        for unit_id, digest in pinned.items():
            row = conn.execute('SELECT boundary_json FROM execution_units WHERE id=?', (unit_id,)).fetchone()
            assessment = json.loads(row[0]).get('assessment', {}) if row else {}
            if assessment.get('input_digest') != digest:
                changed[unit_id] = assessment
        if not changed or any(a.get('version') != 2 for a in changed.values()):
            continue
        proven = set()
        for (raw,) in conn.execute("""SELECT cohort_json FROM measurements
                WHERE intervention_id=? ORDER BY measured_at""", (intervention_id,)):
            historical = json.loads(raw)
            for snapshots in historical.get('execution_unit_snapshots', {}).values():
                for snapshot in snapshots:
                    unit_id = snapshot.get('execution_unit_id')
                    assessment = snapshot.get('assessment', {})
                    if (unit_id in changed and assessment.get('version') == 1
                            and assessment.get('input_digest') == pinned[unit_id]):
                        proven.add(unit_id)
            if proven == set(changed):
                break
        if proven != set(changed):
            continue
        MeasurementService(conn).measure(candidate_id, reassess=True, commit=False)
        repaired += 1
    return repaired
