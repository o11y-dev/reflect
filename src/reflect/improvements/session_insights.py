"""R5 session insights from the same measurements used by CLI and dashboard."""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from typing import Any

from reflect.improvements.analysis_scope import AnalysisScope
from reflect.improvements.base import BaseImprovementRule
from reflect.improvements.models import (
    EvidenceRef,
    ObservationDraft,
    ObservationSessionRef,
    RuleDefinition,
)
from reflect.utils import _json_loads
from reflect.views.measurements import MeasurementView, TurnMeasurement, build_measurements

# Configuration lives in the existing versioned rule registry, not a second
# policy/settings system. Order is the stable presentation tie breaker.
_DEFINITIONS = (
    ("very_long", "Long session", "completed_turns", "turns", {"minimum_turns_exclusive": 20}, "Split the next task at a clear deliverable and carry forward a short verified summary."),
    ("heavy_turns", "Large input per turn", "mean_input_tokens", "tokens", {"minimum_mean_input_tokens": 5_000_000}, "Narrow the files and evidence supplied to each turn; reuse a concise task summary."),
    ("tool_spiral", "High tool count in one turn", "max_tool_calls_per_turn", "calls", {"minimum_calls_exclusive": 25}, "Set a bounded check or exploration goal before the next group of tool calls."),
    ("low_cache_hit", "Low observed cache reuse", "cache_read_ratio", "ratio", {"maximum_cache_ratio_exclusive": 0.1, "minimum_input_tokens": 500_000}, "Keep stable instructions and context consistent across related requests; inspect cache eligibility."),
    ("premium_model", "Premium models dominate known cost", "premium_cost_share", "ratio", {"minimum_premium_share": 0.8, "maximum_auto_share_exclusive": 0.2, "premium_model_patterns": ["opus", "fable"]}, "Review which routine steps could use a lower-cost model while preserving verification."),
    ("model_switching", "Repeated model switching", "switch_rebuild_cost_usd", "usd", {"minimum_switches": 3, "minimum_scenario_usd": 1.0}, "Group related work on one suitable model and compare the labelled cache-rebuild scenario."),
    ("idle_rebuild", "Context rebuild after a pause", "idle_rebuild_cost_usd", "usd", {"minimum_idle_ms_exclusive": 300_000, "minimum_scenario_usd": 1.0, "minimum_session_cost_share": 0.2}, "Resume with a bounded context summary and inspect the labelled rebuild scenario before changing the workflow."),
)


def _inclusive(turn: TurnMeasurement) -> int | None:
    if not turn.complete_usage or any(v is None for v in (turn.input_tokens, turn.cache_read_tokens, turn.cache_write_tokens)):
        return None
    return turn.input_tokens + turn.cache_read_tokens + turn.cache_write_tokens


def session_inputs(view: MeasurementView, *, premium_patterns: tuple[str, ...] = ("opus", "fable")) -> dict[str, dict[str, Any]]:
    """Pure per-session inputs; cache scenarios never change observed cost."""
    grouped: dict[str, list[TurnMeasurement]] = defaultdict(list)
    for turn in view.turns:
        grouped[turn.session_id].append(turn)
    result: dict[str, dict[str, Any]] = {}
    for session_id, turns in grouped.items():
        eligible = [t for t in turns if _inclusive(t) is not None]
        inclusive = sum(_inclusive(t) for t in eligible)
        tools = [t.tool_calls for t in turns if t.tool_calls is not None]
        known_cost = view.sessions.get(session_id, {}).get("known_cost_usd")
        priced = [t for t in turns if t.cost_usd is not None]
        mode_complete = bool(priced) and known_cost is not None and abs(sum(t.cost_usd for t in priced) - known_cost) < 1e-8 and all(t.model and t.requested_mode and t.requested_mode.lower() not in {"unknown", "unavailable"} for t in priced)
        warm: dict[str, list[float]] = defaultdict(list)
        previous: TurnMeasurement | None = None
        switches = 0
        for turn in turns:
            amount = _inclusive(turn)
            if previous and previous.model and turn.model:
                switches += int(previous.model != turn.model)
                if previous.model == turn.model and turn.idle_before_ms is not None and 0 <= turn.idle_before_ms <= 300_000 and amount:
                    warm[turn.model].append(turn.cache_read_tokens / amount)
            previous = turn
        scenarios = {"switch_rebuild_cost_usd": 0.0, "idle_rebuild_cost_usd": 0.0}
        scenario_eligible = {"switch": 0, "idle": 0}
        scenario_observed = {"switch": 0, "idle": 0}
        assumed_warm = False
        previous = None
        for turn in turns:
            category = None
            if previous and previous.model and turn.model:
                if previous.model != turn.model:
                    category = "switch"
                elif turn.idle_before_ms is not None and turn.idle_before_ms > 300_000:
                    category = "idle"
            previous = turn
            if category is None:
                continue
            scenario_observed[category] += 1
            amount = _inclusive(turn)
            if amount is None or turn.input_cost_usd is None or turn.cache_write_cost_usd is None:
                continue
            scenario_eligible[category] += 1
            rebuild_tokens = turn.input_tokens + turn.cache_write_tokens
            if not rebuild_tokens:
                continue
            samples = warm.get(turn.model, [])
            share = statistics.median(samples) if samples else 0.85
            assumed_warm = assumed_warm or not samples
            excess = (turn.input_cost_usd + turn.cache_write_cost_usd) * max(0.0, 1 - amount * (1 - share) / rebuild_tokens)
            scenarios[category + "_rebuild_cost_usd"] += excess
        # If no eligible rebuild observations exist, the value is unavailable,
        # rather than a measured zero. Partial supported sums retain coverage.
        for category in ("switch", "idle"):
            if not scenario_eligible[category]:
                scenarios[category + "_rebuild_cost_usd"] = None
        result[session_id] = {
            "completed_turns": len(turns),
            "mean_input_tokens": inclusive / len(eligible) if eligible else None,
            "inclusive_input_tokens": inclusive,
            "max_tool_calls_per_turn": max(tools) if tools else None,
            "cache_read_ratio": sum(t.cache_read_tokens for t in eligible) / inclusive if inclusive else None,
            "known_cost_usd": known_cost,
            "premium_cost_share": sum(t.cost_usd for t in priced if any(p in (t.model or "").lower() for p in premium_patterns)) / known_cost if mode_complete and known_cost else None,
            "auto_cost_share": sum(t.cost_usd for t in priced if (t.requested_mode or "").lower() == "auto") / known_cost if mode_complete and known_cost else None,
            "model_switches": switches, **scenarios,
            "warm_share_assumption": 0.85 if assumed_warm else None,
            "scope": "session_lifetime", "watermark": view.scope.get("watermark"),
            "coverage": {"completed_turns": len(turns), "eligible_usage_turns": len(eligible), "eligible_tool_turns": len(tools), "priced_turns": len(priced), "requested_mode_complete": mode_complete, "scenario_eligible": scenario_eligible, "scenario_observed": scenario_observed},
        }
    return result


class SessionInsightRule(BaseImprovementRule):
    """One R5 rule; refresh shares its input projection across all seven rules."""

    workflow = None

    def __init__(self, name: str):
        spec = next(item for item in _DEFINITIONS if item[0] == name)
        self.name, self.title, self.metric, self.unit, config, self.guidance = spec
        self.order = _DEFINITIONS.index(spec)
        self.definition = RuleDefinition(id=f"session_{name}", version=1, category="session_efficiency", title=self.title, description=self.guidance, detector_config=config, required_signals=["canonical_observed_turns", "usage_coverage"])

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        return self.detect_inputs(session_inputs(build_measurements(
            conn, session_ids=scope.session_ids if scope else None, include_turns=True,
        )))

    def detect_inputs(self, inputs: dict[str, dict[str, Any]]) -> list[ObservationDraft]:
        findings = []
        cfg = self.definition.detector_config
        for session_id, values in inputs.items():
            value = values.get(self.metric)
            if value is None:
                continue
            matches = {
                "very_long": lambda value, values: value > cfg["minimum_turns_exclusive"],
                "heavy_turns": lambda value, values: value >= cfg["minimum_mean_input_tokens"],
                "tool_spiral": lambda value, values: value > cfg["minimum_calls_exclusive"],
                "low_cache_hit": lambda value, values: value < cfg["maximum_cache_ratio_exclusive"] and values["inclusive_input_tokens"] >= cfg["minimum_input_tokens"],
                "premium_model": lambda value, values: value >= cfg["minimum_premium_share"] and values.get("auto_cost_share") is not None and values["auto_cost_share"] < cfg["maximum_auto_share_exclusive"],
                "model_switching": lambda value, values: values["model_switches"] >= cfg["minimum_switches"] and value >= cfg["minimum_scenario_usd"],
                "idle_rebuild": lambda value, values: value >= cfg["minimum_scenario_usd"] and values.get("known_cost_usd") and value >= values["known_cost_usd"] * cfg["minimum_session_cost_share"],
            }[self.name](value, values)
            if not matches:
                continue
            coverage = values["coverage"]
            eligible = coverage["eligible_tool_turns"] if self.name == "tool_spiral" else coverage["completed_turns"] if self.name == "very_long" else coverage["eligible_usage_turns"]
            completeness = eligible / max(1, coverage["completed_turns"])
            if self.name == "premium_model":
                completeness = float(coverage["requested_mode_complete"])
            if self.name in {"model_switching", "idle_rebuild"}:
                category = "switch" if self.name == "model_switching" else "idle"
                completeness = coverage["scenario_eligible"][category] / max(1, coverage["scenario_observed"][category])
            scenario = " Cache-rebuild scenario, not measured savings." if self.unit == "usd" else ""
            assumption = " Warm-cache share assumes 85%." if self.unit == "usd" and values["warm_share_assumption"] else ""
            finding = self.make_observation(identity=(session_id, self.name), title=self.title, summary=f"Observed {value:,.4g} {self.unit} over session lifetime.{scenario}{assumption} {self.guidance}", metric_name=self.metric, metric_value=float(value), metric_unit=self.unit, metric_direction="higher_is_better" if self.name == "low_cache_hit" else "lower_is_better", impact_score=40.0, confidence=completeness, scope_type="session", scope_id=session_id, baseline_query={"scope": "session_lifetime", "coverage": coverage, "watermark": values["watermark"], "presentation_order": self.order, "warm_share_assumption": values["warm_share_assumption"]}, evidence=[EvidenceRef(entity_type="session", entity_id=session_id, session_id=session_id, summary_redacted=f"{self.metric}={value:,.4g}; eligible evidence coverage attached", attrs={"coverage": coverage, "scope": "session_lifetime"})])
            finding.source_sessions = [ObservationSessionRef(session_id=session_id, occurrence_count=1, summary_redacted=self.title)]
            findings.append(finding)
        return findings


def session_insight_rules() -> tuple[SessionInsightRule, ...]:
    return tuple(SessionInsightRule(spec[0]) for spec in _DEFINITIONS)


def top_session_insights(inputs: dict[str, dict[str, Any]], *, limit: int = 3) -> dict[str, list[dict[str, Any]]]:
    """Presentation-only ranking, using the same rule implementations as refresh."""
    grouped: dict[str, list[ObservationDraft]] = defaultdict(list)
    for rule in session_insight_rules():
        for finding in rule.detect_inputs(inputs):
            grouped[finding.scope_id].append(finding)
    return {sid: [f.model_dump() for f in sorted(findings, key=lambda f: (-f.confidence, f.baseline_query["presentation_order"]))[:max(0, min(limit, 3))]] for sid, findings in grouped.items()}


def read_session_insights(conn: sqlite3.Connection, *, session_ids: set[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Read persisted findings; dashboard reads do not run the detectors."""
    rule_ids = [f"session_{spec[0]}" for spec in _DEFINITIONS]
    params: list = list(rule_ids)
    clause = ""
    if session_ids is not None:
        clause = " AND o.scope_id IN (%s)" % (",".join("?" for _ in session_ids) or "NULL")
        params.extend(sorted(session_ids))
    cursor = conn.execute(f"""
        WITH ranked AS (
          SELECT o.id,o.rule_id,o.rule_version,o.scope_id AS session_id,o.title,o.summary,
                 o.metric_name,o.metric_value,o.metric_unit,o.confidence,o.baseline_query_json,
                 o.last_evaluated_at,
                 ROW_NUMBER() OVER (PARTITION BY o.scope_id ORDER BY o.confidence DESC,
                     json_extract(o.baseline_query_json,'$.presentation_order'),o.id) AS rank
          FROM observations o JOIN sessions s ON s.id=o.scope_id
          WHERE o.scope_type='session' AND o.rule_version=1
            AND o.rule_id IN ({','.join('?' for _ in rule_ids)})
            AND o.status NOT IN ('resolved','dismissed','rejected')
            AND (o.suppressed_until IS NULL OR julianday(o.suppressed_until)<=julianday('now'))
            {clause}
        ) SELECT * FROM ranked WHERE rank<=3 ORDER BY session_id,rank
    """, params)
    names = [c[0] for c in cursor.description]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for raw in cursor:
        row = dict(zip(names, raw, strict=True))
        row.pop("rank")
        row["baseline_query"] = _json_loads(row.pop("baseline_query_json"))
        grouped[row["session_id"]].append(row)
    return dict(grouped)
