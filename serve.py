#!/usr/bin/env python3
"""
Local FastAPI server for the reflect web dashboard.

Usage:
    reflect --publish          # starts this automatically
    python serve.py [--otlp-traces PATH] [--port 8765]

Serves docs/index.html with dashboard data via /api/data and session
detail via /api/session/{id}.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent
DOCS_DIR = REPO_ROOT / "docs"
SRC_DIR = REPO_ROOT / "src"
TESTS_DIR = REPO_ROOT / "tests"

sys.path.insert(0, str(SRC_DIR))
from reflect.core import (
    analyze_telemetry,
    _build_dashboard_json,
    _discover_rich_session_files,
    _iter_claude_session_spans,
    _iter_copilot_session_spans,
    _iter_cursor_session_spans,
    _iter_gemini_session_spans,
    _extract_session_id,
    _load_otlp_traces,
    _flatten_text_content,
)


def _make_synthetic_stats():
    sys.path.insert(0, str(TESTS_DIR))
    from conftest import ALL_SPANS, wrap_otlp
    from collections import defaultdict

    tmp = Path(tempfile.mkdtemp())
    p = tmp / "traces.json"
    by_agent = defaultdict(list)
    for s in ALL_SPANS:
        by_agent[s["attributes"]["gen_ai.client.name"]].append(s)
    with p.open("w") as f:
        for agent, spans in by_agent.items():
            f.write(wrap_otlp(spans, agent=agent) + "\n")
    return analyze_telemetry(tmp / "s", tmp / "sp", p)


def _load_session_detail(session_id: str, stats) -> Optional[dict]:
    """Load full conversation detail for a session from its source file."""
    source_info = stats.session_source.get(session_id)
    if source_info:
        agent_name, file_path = source_info
        fp = Path(file_path)
        if fp.exists():
            return _load_detail_from_native(session_id, agent_name, fp)

    conv = stats.session_conversation.get(session_id)
    if conv:
        return {"session_id": session_id, "agent": "", "events": conv, "source": "spans"}
    return None


def _load_detail_from_native(session_id: str, agent: str, file_path: Path) -> dict:
    """Read a native session file and return full conversation events."""
    try:
        import orjson
        _loads = orjson.loads
    except ImportError:
        _loads = json.loads

    events = []

    if agent == "claude":
        for line in file_path.open("r", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = _loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            etype = entry.get("type")
            ts = entry.get("timestamp", "")
            if etype == "user":
                content = entry.get("message", {}).get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"
                    )
                events.append({"type": "prompt", "content": str(content), "timestamp": ts})
            elif etype == "assistant":
                msg = entry.get("message", {}) or {}
                usage = msg.get("usage", {}) or {}
                model = msg.get("model", "")
                content_items = msg.get("content") or []
                text_parts = []
                tool_uses = []
                for item in content_items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif item.get("type") == "tool_use":
                        tool_uses.append({
                            "type": "tool_call",
                            "tool_name": item.get("name", ""),
                            "input": json.dumps(item.get("input", {}), default=str)[:2000],
                            "timestamp": ts,
                        })
                events.append({
                    "type": "response",
                    "content": "\n".join(text_parts)[:5000],
                    "model": model,
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "timestamp": ts,
                })
                events.extend(tool_uses)

    elif agent == "copilot":
        for line in file_path.open("r", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = _loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            etype = entry.get("type")
            data = entry.get("data", {})
            ts = entry.get("timestamp", "")
            if etype == "user.message":
                events.append({"type": "prompt", "content": data.get("content", ""), "timestamp": ts})
            elif etype == "assistant.message":
                events.append({
                    "type": "response",
                    "content": data.get("content", "")[:5000],
                    "model": data.get("model", ""),
                    "output_tokens": data.get("outputTokens", 0),
                    "timestamp": ts,
                })
            elif etype == "tool.execution_start":
                events.append({
                    "type": "tool_call",
                    "tool_name": data.get("toolName", ""),
                    "input": json.dumps(data.get("arguments", {}), default=str)[:2000],
                    "timestamp": ts,
                })
            elif etype == "tool.execution_complete":
                events.append({
                    "type": "tool_result",
                    "tool_name": data.get("toolName", ""),
                    "success": bool(data.get("success", False)),
                    "timestamp": ts,
                })

    elif agent == "gemini":
        payload = _loads(file_path.read_text())
        for msg in payload.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            ts = msg.get("timestamp", "")
            if msg.get("type") == "user":
                events.append({"type": "prompt", "content": msg.get("content", ""), "timestamp": ts})
            elif msg.get("type") == "gemini":
                events.append({
                    "type": "response",
                    "content": msg.get("content", "")[:5000],
                    "model": msg.get("model", ""),
                    "input_tokens": (msg.get("tokens") or {}).get("input", 0),
                    "output_tokens": (msg.get("tokens") or {}).get("output", 0),
                    "timestamp": ts,
                })
                for call in msg.get("toolCalls") or []:
                    if not isinstance(call, dict):
                        continue
                    events.append({
                        "type": "tool_call",
                        "tool_name": call.get("displayName") or call.get("name", ""),
                        "input": json.dumps(call.get("args", {}), default=str)[:2000],
                        "timestamp": call.get("timestamp", ts),
                    })

    elif agent == "cursor":
        for line in file_path.open("r", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = _loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            role = entry.get("role")
            ts = entry.get("timestamp", "")
            content = entry.get("message", {}).get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    item.get("text", "") for item in content if isinstance(item, dict)
                )
            if role == "user":
                events.append({"type": "prompt", "content": str(content)[:5000], "timestamp": ts})
            elif role == "assistant":
                events.append({"type": "response", "content": str(content)[:5000], "timestamp": ts})

    return {"session_id": session_id, "agent": agent, "events": events, "source": "native"}


def create_app(stats=None, docs_dir: Path | None = None):
    """Create the FastAPI app. Can be called from core.py for --publish."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles

    _docs = docs_dir or DOCS_DIR
    app = FastAPI(title="reflect dashboard", docs_url=None, redoc_url=None)

    # Cache the dashboard JSON
    _dashboard_json: dict | None = None

    @app.get("/api/data")
    def api_data():
        nonlocal _dashboard_json
        if stats is None:
            return JSONResponse({"error": "no data loaded"}, status_code=500)
        if _dashboard_json is None:
            _dashboard_json = json.loads(_build_dashboard_json(stats))
        return JSONResponse(_dashboard_json)

    @app.get("/api/session/{session_id:path}")
    def api_session(session_id: str):
        if stats is None:
            return JSONResponse({"error": "no stats loaded"}, status_code=404)
        detail = _load_session_detail(session_id, stats)
        if detail is None:
            return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)
        return JSONResponse(detail, headers={"Access-Control-Allow-Origin": "*"})

    @app.get("/")
    def index():
        index_path = _docs / "index.html"
        if index_path.exists():
            return FileResponse(index_path, media_type="text/html")
        return HTMLResponse("<h1>reflect dashboard</h1><p>docs/index.html not found</p>", status_code=404)

    # Serve static files from docs/ (JS, CSS, etc.)
    if _docs.exists():
        app.mount("/", StaticFiles(directory=str(_docs)), name="static")

    return app


def start_server(stats, port: int = 8765, open_browser: bool = True, docs_dir: Path | None = None):
    """Start the FastAPI server. Blocks until Ctrl-C."""
    import uvicorn
    import threading
    import webbrowser

    app = create_app(stats, docs_dir=docs_dir)
    url = f"http://127.0.0.1:{port}/?report=api/data"

    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()

    print(f"\n  Serving at: {url}")
    print("  Session detail API: /api/session/<session_id>")
    print("  Press Ctrl-C to stop\n")

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main():
    parser = argparse.ArgumentParser(description="Serve reflect dashboard locally")
    parser.add_argument("--otlp-traces", type=Path, default=None,
                        help="Path to OTLP traces file (default: synthetic test data)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.otlp_traces:
        if not args.otlp_traces.exists():
            sys.exit(f"File not found: {args.otlp_traces}")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stats = analyze_telemetry(tmp / "s", tmp / "sp", args.otlp_traces)
        source = str(args.otlp_traces)
    else:
        print("No --otlp-traces given, using synthetic test data.")
        stats = _make_synthetic_stats()
        source = "synthetic (conftest fixtures)"

    print(f"Source : {source}")
    print(f"Events : {stats.total_events}")
    print(f"Sessions: {len(stats.sessions_seen)}")

    start_server(stats, port=args.port)


if __name__ == "__main__":
    main()
