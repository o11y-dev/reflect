"""Shared fixtures and synthetic span data for the reflect test suite."""
from __future__ import annotations

import json
from collections import defaultdict
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Time constants
# ---------------------------------------------------------------------------

DAY1 = 1_774_310_400_000_000_000  # 2026-03-24 00:00 UTC
DAY2 = DAY1 + 86_400_000_000_000  # 2026-03-25 00:00 UTC
DAY3 = DAY2 + 86_400_000_000_000  # 2026-03-26 00:00 UTC
HOUR = 3_600_000_000_000
MIN  = 60_000_000_000
SEC  = 1_000_000_000

# ---------------------------------------------------------------------------
# Agent identifiers
# ---------------------------------------------------------------------------

CLAUDE  = "claude"
COPILOT = "copilot"
GEMINI  = "gemini-code-assist"

# ---------------------------------------------------------------------------
# MCP server names
# ---------------------------------------------------------------------------

MCP_GITLAB     = "mcp-gitlab"
MCP_JIRA       = "mcp-atlassian"
MCP_POSTGRES   = "mcp-postgres"
MCP_CORALOGIX  = "mcp-coralogix"
MCP_WIZ        = "mcp-wiz"
MCP_CLOUDFLARE = "mcp-cloudflare"
MCP_PLAYWRIGHT = "mcp-playwright"

# ---------------------------------------------------------------------------
# Model identifiers
# ---------------------------------------------------------------------------

MODEL_CLAUDE  = "claude-sonnet-4-20250514"
MODEL_COPILOT = "gpt-4o-2024-11-20"
MODEL_GEMINI  = "gemini-2.5-pro"

# ---------------------------------------------------------------------------
# Span factory
# ---------------------------------------------------------------------------

def make_span(
    event: str,
    *,
    agent: str = CLAUDE,
    model: str = MODEL_CLAUDE,
    tool: str | None = None,
    session: str = "sess-default-001",
    start_ns: int = DAY1 + 10 * HOUR,
    duration_ms: float = 150.0,
    mcp_server: str | None = None,
    subagent_type: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_create_tokens: int = 0,
    cache_read_tokens: int = 0,
    command: str | None = None,
    tool_input: str | None = None,
) -> dict:
    """Build a flat span dict matching _process_span's expected input format."""
    end_ns = start_ns + int(duration_ms * 1_000_000)
    attrs: dict = {
        "gen_ai.client.hook.event": event,
        "gen_ai.client.name": agent,
        "gen_ai.request.model": model,
        "gen_ai.client.session_id": session,
    }
    if tool:
        attrs["gen_ai.client.tool_name"] = tool
    if mcp_server:
        attrs["gen_ai.client.mcp_server"] = mcp_server
    if subagent_type:
        attrs["gen_ai.client.subagent_type"] = subagent_type
    if command:
        attrs["gen_ai.client.command"] = command
    if tool_input:
        attrs["gen_ai.client.tool.input"] = tool_input
    if input_tokens:
        attrs["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens:
        attrs["gen_ai.usage.output_tokens"] = output_tokens
    if cache_create_tokens:
        attrs["gen_ai.usage.cache_creation.input_tokens"] = cache_create_tokens
    if cache_read_tokens:
        attrs["gen_ai.usage.cache_read.input_tokens"] = cache_read_tokens
    return {
        "name": f"gen_ai.client.hook.{event}",
        "traceId": "aabbccdd00112233aabbccdd00112233",
        "spanId": uuid4().hex[:16],
        "parentSpanId": "",
        "start_time_ns": start_ns,
        "end_time_ns": end_ns,
        "attributes": attrs,
    }


def wrap_otlp(spans: list[dict], agent: str = CLAUDE, service: str = "ide-agent") -> str:
    """Wrap flat span dicts into a single OTLP JSON collector line."""
    otlp_spans = []
    for s in spans:
        attrs_list = []
        for k, v in s["attributes"].items():
            if isinstance(v, str):
                attrs_list.append({"key": k, "value": {"stringValue": v}})
            elif isinstance(v, bool):
                attrs_list.append({"key": k, "value": {"boolValue": v}})
            elif isinstance(v, int):
                attrs_list.append({"key": k, "value": {"intValue": str(v)}})
            elif isinstance(v, float):
                attrs_list.append({"key": k, "value": {"doubleValue": v}})
        otlp_spans.append({
            "traceId": s["traceId"],
            "spanId": s["spanId"],
            "parentSpanId": s["parentSpanId"],
            "name": s["name"],
            "kind": 1,
            "startTimeUnixNano": str(s["start_time_ns"]),
            "endTimeUnixNano": str(s["end_time_ns"]),
            "attributes": attrs_list,
            "status": {},
        })
    return json.dumps({
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    {"key": "service.name",       "value": {"stringValue": service}},
                    {"key": "gen_ai.client.name", "value": {"stringValue": agent}},
                ]
            },
            "scopeSpans": [{"scope": {"name": "ide-hooks"}, "spans": otlp_spans}],
        }]
    })


# ---------------------------------------------------------------------------
# Session 1: Claude — feature development (Day 1, morning)
# MCP: mcp-gitlab + mcp-atlassian + mcp-coralogix
# Events: 2 prompts, 4 edits (1 failure), 1 subagent pair, 1 shell pair
# ---------------------------------------------------------------------------

SESS_CLAUDE_1 = "sess-claude-feat-001"

SESSION_1_SPANS = [
    make_span("UserPromptSubmit", agent=CLAUDE, session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR, duration_ms=12,
              input_tokens=2400, output_tokens=800, cache_read_tokens=15000),
    make_span("PreToolUse", agent=CLAUDE, tool="Read", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 1*MIN, duration_ms=45),
    make_span("BeforeReadFile", agent=CLAUDE, tool="Read", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 1*MIN, duration_ms=45),
    make_span("BeforeMCPExecution", agent=CLAUDE, tool="get_issue", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 2*MIN, duration_ms=1200, mcp_server=MCP_JIRA),
    make_span("AfterMCPExecution", agent=CLAUDE, tool="get_issue", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 2*MIN + int(1.2*SEC), duration_ms=5, mcp_server=MCP_JIRA),
    make_span("BeforeMCPExecution", agent=CLAUDE, tool="list_merge_requests", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 4*MIN, duration_ms=800, mcp_server=MCP_GITLAB),
    make_span("AfterMCPExecution", agent=CLAUDE, tool="list_merge_requests", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 4*MIN + int(0.8*SEC), duration_ms=3, mcp_server=MCP_GITLAB),
    make_span("BeforeMCPExecution", agent=CLAUDE, tool="get_logs", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 4*MIN + 30*SEC, duration_ms=650, mcp_server=MCP_CORALOGIX),
    make_span("AfterMCPExecution", agent=CLAUDE, tool="get_logs", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 4*MIN + 30*SEC + int(0.65*SEC), duration_ms=4, mcp_server=MCP_CORALOGIX),
    make_span("SubagentStart", agent=CLAUDE, session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 5*MIN, duration_ms=50, subagent_type="explore"),
    make_span("SubagentStop", agent=CLAUDE, session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 6*MIN, duration_ms=10, subagent_type="explore"),
    make_span("PreToolUse", agent=CLAUDE, tool="Edit", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 7*MIN, duration_ms=200),
    make_span("AfterFileEdit", agent=CLAUDE, tool="Edit", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 7*MIN + int(0.2*SEC), duration_ms=5),
    make_span("PreToolUse", agent=CLAUDE, tool="Edit", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 8*MIN, duration_ms=180),
    make_span("AfterFileEdit", agent=CLAUDE, tool="Edit", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 8*MIN + int(0.18*SEC), duration_ms=4),
    make_span("BeforeShellExecution", agent=CLAUDE, session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 10*MIN, duration_ms=5500,
              command="pytest tests/ -v --tb=short"),
    make_span("AfterShellExecution", agent=CLAUDE, session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 10*MIN + int(5.5*SEC), duration_ms=3),
    make_span("UserPromptSubmit", agent=CLAUDE, session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 15*MIN, duration_ms=10,
              input_tokens=3200, output_tokens=1200, cache_read_tokens=18000),
    make_span("PreToolUse", agent=CLAUDE, tool="Edit", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 16*MIN, duration_ms=150),
    make_span("PostToolUseFailure", agent=CLAUDE, tool="Edit", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 16*MIN + int(0.15*SEC), duration_ms=2),
    make_span("PreToolUse", agent=CLAUDE, tool="Edit", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 17*MIN, duration_ms=160),
    make_span("PostToolUse", agent=CLAUDE, tool="Edit", session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 17*MIN + int(0.16*SEC), duration_ms=3),
    make_span("Stop", agent=CLAUDE, session=SESS_CLAUDE_1,
              start_ns=DAY1 + 9*HOUR + 20*MIN, duration_ms=5),
]

# ---------------------------------------------------------------------------
# Session 2: Copilot — code review + security (Day 1, afternoon)
# MCP: mcp-gitlab + mcp-postgres + mcp-wiz + mcp-cloudflare
# ---------------------------------------------------------------------------

SESS_COPILOT_1 = "sess-copilot-review-001"

SESSION_2_SPANS = [
    make_span("UserPromptSubmit", agent=COPILOT, model=MODEL_COPILOT, session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR, duration_ms=8,
              input_tokens=1800, output_tokens=600),
    make_span("PreToolUse", agent=COPILOT, model=MODEL_COPILOT, tool="Read", session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 1*MIN, duration_ms=30),
    make_span("PreToolUse", agent=COPILOT, model=MODEL_COPILOT, tool="Read", session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 2*MIN, duration_ms=25),
    make_span("PreToolUse", agent=COPILOT, model=MODEL_COPILOT, tool="Grep", session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 3*MIN, duration_ms=80),
    make_span("BeforeMCPExecution", agent=COPILOT, model=MODEL_COPILOT, tool="get_merge_request_diff",
              session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 5*MIN, duration_ms=950, mcp_server=MCP_GITLAB),
    make_span("AfterMCPExecution", agent=COPILOT, model=MODEL_COPILOT, tool="get_merge_request_diff",
              session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 5*MIN + SEC, duration_ms=4, mcp_server=MCP_GITLAB),
    make_span("BeforeMCPExecution", agent=COPILOT, model=MODEL_COPILOT, tool="query",
              session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 7*MIN, duration_ms=320, mcp_server=MCP_POSTGRES),
    make_span("AfterMCPExecution", agent=COPILOT, model=MODEL_COPILOT, tool="query",
              session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 7*MIN + int(0.32*SEC), duration_ms=3, mcp_server=MCP_POSTGRES),
    make_span("BeforeMCPExecution", agent=COPILOT, model=MODEL_COPILOT, tool="get_vulnerabilities",
              session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 8*MIN, duration_ms=1800, mcp_server=MCP_WIZ),
    make_span("AfterMCPExecution", agent=COPILOT, model=MODEL_COPILOT, tool="get_vulnerabilities",
              session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 8*MIN + int(1.8*SEC), duration_ms=5, mcp_server=MCP_WIZ),
    make_span("BeforeMCPExecution", agent=COPILOT, model=MODEL_COPILOT, tool="list_dns_records",
              session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 9*MIN, duration_ms=420, mcp_server=MCP_CLOUDFLARE),
    make_span("AfterMCPExecution", agent=COPILOT, model=MODEL_COPILOT, tool="list_dns_records",
              session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 9*MIN + int(0.42*SEC), duration_ms=3, mcp_server=MCP_CLOUDFLARE),
    make_span("Stop", agent=COPILOT, model=MODEL_COPILOT, session=SESS_COPILOT_1,
              start_ns=DAY1 + 14*HOUR + 12*MIN, duration_ms=4),
]

# ---------------------------------------------------------------------------
# Session 3: Gemini — DB migration + observability (Day 2, morning)
# MCP: mcp-atlassian + mcp-postgres (x2, one missing after) + mcp-coralogix + mcp-playwright
# ---------------------------------------------------------------------------

SESS_GEMINI_1 = "sess-gemini-migration-001"

SESSION_3_SPANS = [
    make_span("UserPromptSubmit", agent=GEMINI, model=MODEL_GEMINI, session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR, duration_ms=15,
              input_tokens=2000, output_tokens=900, cache_create_tokens=5000),
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="get_issue",
              session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 1*MIN, duration_ms=1100, mcp_server=MCP_JIRA),
    make_span("AfterMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="get_issue",
              session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 1*MIN + int(1.1*SEC), duration_ms=4, mcp_server=MCP_JIRA),
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="query",
              session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 3*MIN, duration_ms=250, mcp_server=MCP_POSTGRES),
    make_span("AfterMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="query",
              session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 3*MIN + int(0.25*SEC), duration_ms=3, mcp_server=MCP_POSTGRES),
    make_span("PreToolUse", agent=GEMINI, model=MODEL_GEMINI, tool="Write", session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 5*MIN, duration_ms=300),
    make_span("PostToolUse", agent=GEMINI, model=MODEL_GEMINI, tool="Write", session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 5*MIN + int(0.3*SEC), duration_ms=3),
    make_span("BeforeShellExecution", agent=GEMINI, model=MODEL_GEMINI, session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 7*MIN, duration_ms=8000,
              command="alembic upgrade head"),
    make_span("AfterShellExecution", agent=GEMINI, model=MODEL_GEMINI, session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 7*MIN + 8*SEC, duration_ms=3),
    # Missing AfterMCPExecution — simulates Postgres timeout
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="query",
              session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 9*MIN, duration_ms=400, mcp_server=MCP_POSTGRES),
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="get_logs",
              session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 10*MIN, duration_ms=550, mcp_server=MCP_CORALOGIX),
    make_span("AfterMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="get_logs",
              session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 10*MIN + int(0.55*SEC), duration_ms=4, mcp_server=MCP_CORALOGIX),
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="browser_navigate",
              session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 11*MIN, duration_ms=2200, mcp_server=MCP_PLAYWRIGHT),
    make_span("AfterMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="browser_navigate",
              session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 11*MIN + int(2.2*SEC), duration_ms=5, mcp_server=MCP_PLAYWRIGHT),
    make_span("Stop", agent=GEMINI, model=MODEL_GEMINI, session=SESS_GEMINI_1,
              start_ns=DAY2 + 10*HOUR + 13*MIN, duration_ms=5),
]

# ---------------------------------------------------------------------------
# Session 4: Claude — bug fix + security check (Day 2, afternoon)
# MCP: mcp-wiz
# Subagents: sprint-daily-review (paired), explore (unpaired = incomplete)
# ---------------------------------------------------------------------------

SESS_CLAUDE_2 = "sess-claude-bugfix-002"

SESSION_4_SPANS = [
    make_span("UserPromptSubmit", agent=CLAUDE, session=SESS_CLAUDE_2,
              start_ns=DAY2 + 15*HOUR, duration_ms=10,
              input_tokens=1500, output_tokens=500, cache_read_tokens=12000),
    make_span("PreToolUse", agent=CLAUDE, tool="Grep", session=SESS_CLAUDE_2,
              start_ns=DAY2 + 15*HOUR + 1*MIN, duration_ms=60),
    make_span("PreToolUse", agent=CLAUDE, tool="Read", session=SESS_CLAUDE_2,
              start_ns=DAY2 + 15*HOUR + 2*MIN, duration_ms=35),
    make_span("BeforeMCPExecution", agent=CLAUDE, tool="get_vulnerabilities", session=SESS_CLAUDE_2,
              start_ns=DAY2 + 15*HOUR + 2*MIN + 30*SEC, duration_ms=1400, mcp_server=MCP_WIZ),
    make_span("AfterMCPExecution", agent=CLAUDE, tool="get_vulnerabilities", session=SESS_CLAUDE_2,
              start_ns=DAY2 + 15*HOUR + 2*MIN + 30*SEC + int(1.4*SEC), duration_ms=4, mcp_server=MCP_WIZ),
    make_span("SubagentStart", agent=CLAUDE, session=SESS_CLAUDE_2,
              start_ns=DAY2 + 15*HOUR + 3*MIN, duration_ms=40, subagent_type="sprint-daily-review"),
    make_span("SubagentStop", agent=CLAUDE, session=SESS_CLAUDE_2,
              start_ns=DAY2 + 15*HOUR + 5*MIN, duration_ms=8, subagent_type="sprint-daily-review"),
    # explore subagent start only — no stop (incomplete)
    make_span("SubagentStart", agent=CLAUDE, session=SESS_CLAUDE_2,
              start_ns=DAY2 + 15*HOUR + 6*MIN, duration_ms=30, subagent_type="explore"),
    make_span("PreToolUse", agent=CLAUDE, tool="Edit", session=SESS_CLAUDE_2,
              start_ns=DAY2 + 15*HOUR + 8*MIN, duration_ms=170),
    make_span("PreToolUse", agent=CLAUDE, tool="Bash", session=SESS_CLAUDE_2,
              start_ns=DAY2 + 15*HOUR + 9*MIN, duration_ms=3200,
              tool_input="git diff --stat"),
    make_span("Stop", agent=CLAUDE, session=SESS_CLAUDE_2,
              start_ns=DAY2 + 15*HOUR + 12*MIN, duration_ms=4),
]

# ---------------------------------------------------------------------------
# Session 5: Copilot — QA with Playwright + Cloudflare (Day 3, morning)
# MCP: mcp-playwright (paired) + mcp-cloudflare (missing after = timeout)
# ---------------------------------------------------------------------------

SESS_COPILOT_2 = "sess-copilot-qa-002"

SESSION_5_SPANS = [
    make_span("UserPromptSubmit", agent=COPILOT, model=MODEL_COPILOT, session=SESS_COPILOT_2,
              start_ns=DAY3 + 8*HOUR, duration_ms=6,
              input_tokens=800, output_tokens=200),
    make_span("PreToolUse", agent=COPILOT, model=MODEL_COPILOT, tool="Grep", session=SESS_COPILOT_2,
              start_ns=DAY3 + 8*HOUR + 30*SEC, duration_ms=55),
    make_span("PreToolUse", agent=COPILOT, model=MODEL_COPILOT, tool="Read", session=SESS_COPILOT_2,
              start_ns=DAY3 + 8*HOUR + 1*MIN, duration_ms=20),
    make_span("BeforeMCPExecution", agent=COPILOT, model=MODEL_COPILOT, tool="browser_navigate",
              session=SESS_COPILOT_2,
              start_ns=DAY3 + 8*HOUR + 2*MIN, duration_ms=3100, mcp_server=MCP_PLAYWRIGHT),
    make_span("AfterMCPExecution", agent=COPILOT, model=MODEL_COPILOT, tool="browser_navigate",
              session=SESS_COPILOT_2,
              start_ns=DAY3 + 8*HOUR + 2*MIN + int(3.1*SEC), duration_ms=5, mcp_server=MCP_PLAYWRIGHT),
    # Missing AfterMCPExecution — simulates Cloudflare timeout
    make_span("BeforeMCPExecution", agent=COPILOT, model=MODEL_COPILOT, tool="purge_cache",
              session=SESS_COPILOT_2,
              start_ns=DAY3 + 8*HOUR + 3*MIN, duration_ms=5000, mcp_server=MCP_CLOUDFLARE),
    make_span("Stop", agent=COPILOT, model=MODEL_COPILOT, session=SESS_COPILOT_2,
              start_ns=DAY3 + 8*HOUR + 5*MIN, duration_ms=3),
]

# ---------------------------------------------------------------------------
# Session 6: Gemini — all 7 MCP servers (Day 3, afternoon)
# ---------------------------------------------------------------------------

SESS_GEMINI_2 = "sess-gemini-infra-002"

SESSION_6_SPANS = [
    make_span("UserPromptSubmit", agent=GEMINI, model=MODEL_GEMINI, session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR, duration_ms=11,
              input_tokens=2200, output_tokens=1100, cache_create_tokens=6000),
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="create_merge_request",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 1*MIN, duration_ms=1500, mcp_server=MCP_GITLAB),
    make_span("AfterMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="create_merge_request",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 1*MIN + int(1.5*SEC), duration_ms=5, mcp_server=MCP_GITLAB),
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="transition_issue",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 2*MIN, duration_ms=700, mcp_server=MCP_JIRA),
    make_span("AfterMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="transition_issue",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 2*MIN + int(0.7*SEC), duration_ms=3, mcp_server=MCP_JIRA),
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="query",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 3*MIN, duration_ms=180, mcp_server=MCP_POSTGRES),
    make_span("AfterMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="query",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 3*MIN + int(0.18*SEC), duration_ms=2, mcp_server=MCP_POSTGRES),
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="get_traces",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 4*MIN, duration_ms=480, mcp_server=MCP_CORALOGIX),
    make_span("AfterMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="get_traces",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 4*MIN + int(0.48*SEC), duration_ms=3, mcp_server=MCP_CORALOGIX),
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="list_issues",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 5*MIN, duration_ms=920, mcp_server=MCP_WIZ),
    make_span("AfterMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="list_issues",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 5*MIN + int(0.92*SEC), duration_ms=4, mcp_server=MCP_WIZ),
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="get_zone_settings",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 6*MIN, duration_ms=350, mcp_server=MCP_CLOUDFLARE),
    make_span("AfterMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="get_zone_settings",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 6*MIN + int(0.35*SEC), duration_ms=3, mcp_server=MCP_CLOUDFLARE),
    make_span("BeforeMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="browser_screenshot",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 7*MIN, duration_ms=1800, mcp_server=MCP_PLAYWRIGHT),
    make_span("AfterMCPExecution", agent=GEMINI, model=MODEL_GEMINI, tool="browser_screenshot",
              session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 7*MIN + int(1.8*SEC), duration_ms=4, mcp_server=MCP_PLAYWRIGHT),
    make_span("Stop", agent=GEMINI, model=MODEL_GEMINI, session=SESS_GEMINI_2,
              start_ns=DAY3 + 13*HOUR + 9*MIN, duration_ms=4),
]

# ---------------------------------------------------------------------------
# Combined span set
# ---------------------------------------------------------------------------

ALL_SPANS = (
    SESSION_1_SPANS + SESSION_2_SPANS + SESSION_3_SPANS +
    SESSION_4_SPANS + SESSION_5_SPANS + SESSION_6_SPANS
)

# ---------------------------------------------------------------------------
# Expected aggregate values (for assertions)
# ---------------------------------------------------------------------------

EXPECTED = {
    "total_spans": len(ALL_SPANS),
    "sessions": 6,
    "days_active": 3,
    "agents": {CLAUDE, COPILOT, GEMINI},
    "models": {MODEL_CLAUDE, MODEL_COPILOT, MODEL_GEMINI},
    "UserPromptSubmit": 6,
    "PreToolUse": 12,
    "PostToolUseFailure": 1,
    "BeforeMCPExecution": 22,
    "AfterMCPExecution": 20,  # 2 missing (sess3 postgres, sess5 cloudflare)
    "SubagentStart": 3,
    "SubagentStop": 2,  # 1 missing (sess4 explore)
    "mcp_servers": {
        MCP_GITLAB, MCP_JIRA, MCP_POSTGRES,
        MCP_CORALOGIX, MCP_WIZ, MCP_CLOUDFLARE, MCP_PLAYWRIGHT,
    },
    # Availability gaps
    "mcp_postgres_missing_after": 1,    # sess3
    "mcp_cloudflare_missing_after": 1,  # sess5
}

# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def otlp_traces_file(tmp_path):
    """Write all sessions to a tmp OTLP traces file, one JSON line per agent."""
    p = tmp_path / "otel-traces.json"
    by_agent: dict[str, list] = defaultdict(list)
    for s in ALL_SPANS:
        by_agent[s["attributes"]["gen_ai.client.name"]].append(s)
    with p.open("w") as f:
        for agent, spans in by_agent.items():
            f.write(wrap_otlp(spans, agent=agent) + "\n")
    return p


@pytest.fixture
def empty_otlp_file(tmp_path):
    """Empty OTLP traces file."""
    p = tmp_path / "empty.json"
    p.write_text("")
    return p


@pytest.fixture
def single_span_file(tmp_path):
    """OTLP file with a single UserPromptSubmit span."""
    spans = [make_span("UserPromptSubmit", session="sess-single-001",
                       input_tokens=100, output_tokens=50)]
    p = tmp_path / "single.json"
    p.write_text(wrap_otlp(spans) + "\n")
    return p
