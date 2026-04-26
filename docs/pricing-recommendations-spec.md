# Spec: LiteLLM-based Pricing + Recommendation Engine Improvements

## Context and why now

`reflect` already computes rich token and behavior telemetry (`TelemetryStats`) and has a signal-based recommendations system.
However, it currently does **not** compute true currency cost from model pricing, and recommendation severity is mostly heuristic without explicit "$ impact" prioritization.

This spec adds:

1. **Pricing pipeline** powered by LiteLLM model prices (with robust local cache + fallback behavior).
2. **Cost-aware recommendation ranking** and explicit savings estimates.
3. **Trend-aware recommendation status** (new / improving / resolved) for actionable follow-through.

---

## Current-state snapshot (reflect)

- Canonical telemetry lives in `TelemetryStats` and already includes global + per-session token counts and model counters; this is the correct source of truth for cost derivation.
- Token economy currently returns token-level metrics (total tokens, cache reuse, heavy-model share, etc.) but no USD cost outputs.
- Recommendations are generated as signal functions and returned as plain text from insights renderers.
- Dashboard filtered-state architecture already rebuilds metrics from canonical per-session maps (good foundation for filtered cost metrics).

---

## Research-backed pricing assumptions (LiteLLM)

Based on LiteLLM spend tracking and custom pricing docs:

- LiteLLM cost tracking depends on a model cost map and supports provider-specific adjustments; pricing data should be refreshed regularly.
- Accurate cost estimation needs token-category-aware inputs (input, output, cache creation, cache read, and optional reasoning/audio/image categories when present).
- Model-name mismatches are common (for example, deployment aliases and dated model versions); `base_model`-style canonicalization is required for stable pricing lookup.
- Custom/override pricing should be supported for private deployments, internal rates, and zero-cost on-prem models.
- Cost diagnostics should preserve provenance so users can debug discrepancies between estimated cost and provider billing.

These constraints should be integrated into reflect's Python architecture and existing signal engine.

---

## Goals / non-goals

### Goals

- Add deterministic, testable **cost accounting** per session, model, and agent.
- Keep `TelemetryStats` as canonical source; no recompute from presentation-only rows.
- Add **cost-aware recommendation evidence** (e.g., "potential monthly savings").
- Preserve graceful fallback when token fields or model mapping are missing.

### Non-goals (first release)

- No live billing API reconciliation with providers.
- No guaranteed accounting/audit-grade invoices.
- No automatic mutation of user config/files.

---

## Design overview

### 0) Central config module

Create `src/reflect/config.py` for all runtime config paths and user overrides:

- resolve canonical paths for:
  - `REFLECT_HOME`
  - `config_dir` (default `~/.reflect/config`)
  - `cache_dir` (default `~/.reflect/cache`)
  - `state_dir` (default `~/.reflect/state`)
- load model alias config from `model-aliases.json`
- load LiteLLM runtime config from `litellm.json` so users can point reflect to their own LiteLLM deployment
- keep config loading independent from rendering/processing modules

### 1) New pricing module

Create `src/reflect/pricing.py` with:

- `load_pricing_table(cache_ttl_hours=24) -> PricingTable`
  - Primary source: LiteLLM `model_prices_and_context_window.json` (configurable via `litellm.json`).
  - Cache path: `~/.reflect/cache/litellm-pricing.json`.
  - On fetch failure, return packaged fallback pricing table.
- `canonicalize_model_name(model: str) -> str`
  - strip provider prefixes (`anthropic/...`), dated suffixes, pinned variants.
- alias mapping:
  - built-in aliases for common provider-specific names.
  - optional user alias file (`~/.reflect/config/model-aliases.json`) overriding built-ins.
- LiteLLM endpoint config:
  - user-configurable `base_url`, `model_prices_url`, `api_key_env`, `timeout_seconds`, and `pricing_unit` via `~/.reflect/config/litellm.json`
  - env-var overrides for CI / ephemeral runs (`REFLECT_LITELLM_BASE_URL`, `REFLECT_LITELLM_MODEL_PRICES_URL`, etc.)
- `calculate_cost(tokens, model, speed='standard') -> CostBreakdown`
  - input/output/cache-create/cache-read + optional web-search request cost.

Data classes:

- `ModelPricing`
- `CostBreakdown`
- `PricingResolution` (matched model key, source=live|cache|fallback, confidence)

### 2) Extend TelemetryStats with cost fields

Add fields to `TelemetryStats`:

- totals:
  - `total_cost_usd: float`
  - `input_cost_usd`, `output_cost_usd`, `cache_creation_cost_usd`, `cache_read_cost_usd`
- per-session:
  - `session_costs: dict[str, dict]` (`input/output/cache.../total/model/source`)
- per-model:
  - `model_costs_usd: Counter[str]`
- per-agent:
  - in `AgentStats`, `total_cost_usd: float`

Keep provenance keys (`pricing_source`, `pricing_model_key`, `pricing_confidence`) so UI can disclose estimate quality.

### 3) Processing pipeline integration

In telemetry analysis stage (processing/core):

1. load pricing table once.
2. for each session, derive primary model mix from existing session/model counters.
3. compute cost from authoritative session token totals when present.
4. when session token provenance is estimated, tag cost as estimated and lower confidence.
5. aggregate to model/agent/global totals.

Important: avoid using dashboard-shaped rows as inputs.

### 4) Dashboard/report/terminal output additions

- Terminal cards:
  - total cost, average cost/session, top expensive sessions.
- Dashboard JSON:
  - global cost block + per-session cost fields + per-model cost breakdown.
  - include `pricing_metadata` (`source`, `fetched_at`, `cache_age_s`).
- Markdown report:
  - "Cost & pricing" section with top 5 expensive sessions and model cost share.

### 5) Recommendation engine upgrades

Add cost-aware signals in `insights/signals/recommendations.py`:

- `signal_rec_high_cost_concentration`
- `signal_rec_cache_miss_cost`
- `signal_rec_model_mix_cost`
- `signal_rec_read_churn_cost`

Each signal should include evidence:

- `estimated_tokens_saved`
- `estimated_cost_saved_usd`
- `confidence`
- `window_days`

Add ranking helper module (e.g., `insights/ranking.py`):

- `urgency_score = impact_weight * severity + normalized_cost_savings + recurrence_factor`
- stable sorting for deterministic output.

Add trend classifier:

- compare recent window (e.g., 48h) vs baseline (previous 14d) per recommendation fingerprint.
- status: `new | worsening | improving | resolved | stable`.

### 6) API contract for recommendations (structured)

Currently recommendations are rendered as strings for backward compatibility.
Add structured response path (without breaking current output):

- keep `build_recommendations(stats) -> list[str]`
- add `build_recommendation_insights(stats) -> list[Insight]` with enriched evidence fields.
- dashboard should consume structured insights and render savings + trend badges.

---

## Suggested implementation phases

### Phase 0: Scaffolding (low risk)

- Add pricing module + tests for loading/caching/canonicalization.
- Add fallback pricing fixture and alias config fixture.

### Phase 1: Cost computation core

- Extend models dataclasses.
- Thread cost aggregation through processing and agent rollups.
- Add unit tests for deterministic token->cost transformation.

### Phase 2: Rendering

- Terminal/report/dashboard JSON surfaces.
- Backward-compatible JSON additions only.

### Phase 3: Recommendation intelligence

- Add cost-aware signals.
- Add urgency score + trend status.
- Add tests for ranking and trend transitions.

### Phase 4: Polish and docs

- Document estimate disclaimers and pricing provenance.
- Add CLI flag `--pricing-source {auto,cache,fallback}` (optional).

---

## Testing plan

### Unit

- Pricing source parsing, TTL behavior, fallback branch.
- Model canonicalization/alias resolution.
- Cost calculation by token type and speed multiplier.
- Recommendation urgency ranking determinism.
- Trend classification window logic.

### Integration

- End-to-end `analyze_telemetry()` with synthetic multi-model sessions.
- Filtered dashboard rebuild verifies cost numbers remain consistent with selected sessions.
- Regression: recommendation output remains non-empty and sorted.

### Suggested commands

- `python3 -m pytest tests/test_calculations.py -q`
- `python3 -m pytest tests/test_dashboard_json.py -q`
- `python3 -m pytest tests/test_insights_signals.py -q`
- `python3 -m pytest -q`

---

## Risks and mitigations

- **Model-name drift / unknown variants**
  - Mitigation: canonicalization + alias table + provenance confidence.
- **Price table fetch failure / network constraints**
  - Mitigation: cache-first + packaged fallback.
- **False precision concerns**
  - Mitigation: label estimated costs and show pricing source clearly.
- **Recommendation noise**
  - Mitigation: min-support thresholds and trend-aware suppression.

---

## Definition of done

- Cost fields available in `TelemetryStats`, dashboard JSON, terminal, and markdown outputs.
- LiteLLM pricing fetch/caching/fallback fully covered by tests.
- At least 3 cost-aware recommendation signals with evidence + urgency score.
- Structured recommendation API available and consumed by dashboard.
- Full test suite passes.
