"""Lightweight local OTLP gateway for Reflect.

Accepts traces and logs over gRPC (port 4317) and HTTP (port 4318),
then appends them as JSON lines to the local files that Reflect already
reads:

    ~/.reflect/state/otlp/otel-traces.json
    ~/.reflect/state/otlp/otel-logs.json

Usage (foreground):
    reflect gateway --foreground

Daemon management:
    reflect gateway start
    reflect gateway stop
    reflect gateway status
"""
from __future__ import annotations

import base64
import fcntl
import logging
import os
import signal
import sys
import threading
from concurrent import futures
from pathlib import Path

import grpc
import orjson
import uvicorn
from fastapi import FastAPI, Request, Response
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.logs.v1 import (
    logs_service_pb2,
    logs_service_pb2_grpc,
)
from opentelemetry.proto.collector.trace.v1 import (
    trace_service_pb2,
    trace_service_pb2_grpc,
)

logger = logging.getLogger("reflect.gateway")

_REFLECT_HOME = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect"))
_DEFAULT_TRACES_PATH = _REFLECT_HOME / "state" / "otlp" / "otel-traces.json"
_DEFAULT_LOGS_PATH = _REFLECT_HOME / "state" / "otlp" / "otel-logs.json"
_PID_FILE = _REFLECT_HOME / "state" / "gateway.pid"
_LOG_FILE = _REFLECT_HOME / "state" / "gateway.log"


# ---------------------------------------------------------------------------
# File writer
# ---------------------------------------------------------------------------


def _append_jsonl(path: Path, payload: dict) -> None:
    """Append a single JSON object as one line, using file locking."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = orjson.dumps(payload) + b"\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, raw)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def append_traces(payload: dict, traces_path: Path | None = None) -> None:
    _append_jsonl(traces_path or _DEFAULT_TRACES_PATH, payload)


def append_logs(payload: dict, logs_path: Path | None = None) -> None:
    _append_jsonl(logs_path or _DEFAULT_LOGS_PATH, payload)


# ---------------------------------------------------------------------------
# Protobuf → OTLP JSON conversion
# ---------------------------------------------------------------------------


def _maybe_b64_to_hex(val: str) -> str:
    """Convert base64-encoded ID to hex, passing through already-hex strings."""
    if not val:
        return val
    # Already a hex string (32 hex chars for trace ID, 16 for span ID)
    if all(c in "0123456789abcdefABCDEF" for c in val):
        return val.lower()
    try:
        return base64.b64decode(val).hex()
    except Exception:
        return val


def _fix_trace_ids(payload: dict) -> dict:
    """Convert base64 trace/span IDs to hex in an OTLP traces payload."""
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                if "traceId" in span:
                    span["traceId"] = _maybe_b64_to_hex(span["traceId"])
                if "spanId" in span:
                    span["spanId"] = _maybe_b64_to_hex(span["spanId"])
                if "parentSpanId" in span:
                    span["parentSpanId"] = _maybe_b64_to_hex(span["parentSpanId"])
    return payload


def _fix_log_ids(payload: dict) -> dict:
    """Convert base64 IDs and enum severity numbers in an OTLP logs payload."""
    for rl in payload.get("resourceLogs", []):
        for sl in rl.get("scopeLogs", []):
            for record in sl.get("logRecords", []):
                if "traceId" in record:
                    record["traceId"] = _maybe_b64_to_hex(record["traceId"])
                if "spanId" in record:
                    record["spanId"] = _maybe_b64_to_hex(record["spanId"])
                # MessageToDict renders severityNumber as enum name string;
                # Reflect expects an integer.
                sn = record.get("severityNumber")
                if isinstance(sn, str):
                    # SEVERITY_NUMBER_INFO -> 9, etc.  Fall back to 0.
                    record["severityNumber"] = _SEVERITY_NAME_TO_INT.get(sn, 0)
    return payload


_SEVERITY_NAME_TO_INT: dict[str, int] = {
    "SEVERITY_NUMBER_UNSPECIFIED": 0,
    "SEVERITY_NUMBER_TRACE": 1, "SEVERITY_NUMBER_TRACE2": 2,
    "SEVERITY_NUMBER_TRACE3": 3, "SEVERITY_NUMBER_TRACE4": 4,
    "SEVERITY_NUMBER_DEBUG": 5, "SEVERITY_NUMBER_DEBUG2": 6,
    "SEVERITY_NUMBER_DEBUG3": 7, "SEVERITY_NUMBER_DEBUG4": 8,
    "SEVERITY_NUMBER_INFO": 9, "SEVERITY_NUMBER_INFO2": 10,
    "SEVERITY_NUMBER_INFO3": 11, "SEVERITY_NUMBER_INFO4": 12,
    "SEVERITY_NUMBER_WARN": 13, "SEVERITY_NUMBER_WARN2": 14,
    "SEVERITY_NUMBER_WARN3": 15, "SEVERITY_NUMBER_WARN4": 16,
    "SEVERITY_NUMBER_ERROR": 17, "SEVERITY_NUMBER_ERROR2": 18,
    "SEVERITY_NUMBER_ERROR3": 19, "SEVERITY_NUMBER_ERROR4": 20,
    "SEVERITY_NUMBER_FATAL": 21, "SEVERITY_NUMBER_FATAL2": 22,
    "SEVERITY_NUMBER_FATAL3": 23, "SEVERITY_NUMBER_FATAL4": 24,
}


def _proto_to_traces_dict(request) -> dict:
    """Convert a protobuf ExportTraceServiceRequest to OTLP JSON dict."""
    return _fix_trace_ids(MessageToDict(request))


def _proto_to_logs_dict(request) -> dict:
    """Convert a protobuf ExportLogsServiceRequest to OTLP JSON dict."""
    return _fix_log_ids(MessageToDict(request))


# ---------------------------------------------------------------------------
# gRPC servicers
# ---------------------------------------------------------------------------


class TraceServiceServicer(trace_service_pb2_grpc.TraceServiceServicer):
    def __init__(self, traces_path: Path | None = None) -> None:
        self._traces_path = traces_path

    def Export(self, request, context):  # noqa: N802
        payload = _proto_to_traces_dict(request)
        append_traces(payload, self._traces_path)
        return trace_service_pb2.ExportTraceServiceResponse()


class LogsServiceServicer(logs_service_pb2_grpc.LogsServiceServicer):
    def __init__(self, logs_path: Path | None = None) -> None:
        self._logs_path = logs_path

    def Export(self, request, context):  # noqa: N802
        payload = _proto_to_logs_dict(request)
        append_logs(payload, self._logs_path)
        return logs_service_pb2.ExportLogsServiceResponse()


# ---------------------------------------------------------------------------
# HTTP (OTLP/HTTP JSON) via FastAPI
# ---------------------------------------------------------------------------


def _build_http_app(
    traces_path: Path | None = None,
    logs_path: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="reflect-gateway", docs_url=None, redoc_url=None)

    @app.post("/v1/traces")
    async def ingest_traces(request: Request) -> Response:
        body = await request.body()
        payload = _fix_trace_ids(orjson.loads(body))
        append_traces(payload, traces_path)
        return Response(content=b"{}", media_type="application/json")

    @app.post("/v1/logs")
    async def ingest_logs(request: Request) -> Response:
        body = await request.body()
        payload = _fix_log_ids(orjson.loads(body))
        append_logs(payload, logs_path)
        return Response(content=b"{}", media_type="application/json")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def start_gateway(
    grpc_port: int = 4317,
    http_port: int = 4318,
    traces_path: Path | None = None,
    logs_path: Path | None = None,
) -> None:
    """Run both gRPC and HTTP servers (blocking)."""
    tp = traces_path or _DEFAULT_TRACES_PATH
    lp = logs_path or _DEFAULT_LOGS_PATH
    tp.parent.mkdir(parents=True, exist_ok=True)
    lp.parent.mkdir(parents=True, exist_ok=True)

    # gRPC server
    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(
        TraceServiceServicer(tp), grpc_server,
    )
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(
        LogsServiceServicer(lp), grpc_server,
    )
    grpc_server.add_insecure_port(f"127.0.0.1:{grpc_port}")
    grpc_server.start()
    logger.info("gRPC listening on 127.0.0.1:%d", grpc_port)

    # HTTP server (runs in a daemon thread so we can join on gRPC)
    http_app = _build_http_app(tp, lp)
    http_config = uvicorn.Config(
        http_app,
        host="127.0.0.1",
        port=http_port,
        log_level="warning",
    )
    http_server = uvicorn.Server(http_config)
    http_thread = threading.Thread(target=http_server.run, daemon=True)
    http_thread.start()
    logger.info("HTTP listening on 127.0.0.1:%d", http_port)

    # Graceful shutdown on SIGTERM / SIGINT
    shutdown_event = threading.Event()

    def _handle_signal(signum, frame):
        logger.info("Received signal %s, shutting down", signum)
        shutdown_event.set()
        http_server.should_exit = True
        grpc_server.stop(grace=2)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    print(
        f"reflect gateway: gRPC :{grpc_port} | HTTP :{http_port}"
        f"\n  traces → {tp}"
        f"\n  logs   → {lp}",
        flush=True,
    )

    shutdown_event.wait()


# ---------------------------------------------------------------------------
# Daemon helpers
# ---------------------------------------------------------------------------


def _is_running() -> int | None:
    """Return the gateway PID if it's alive, else None (and clean stale PID file)."""
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        _PID_FILE.unlink(missing_ok=True)
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        _PID_FILE.unlink(missing_ok=True)
        return None
    return pid


def daemon_start(grpc_port: int = 4317, http_port: int = 4318) -> int:
    """Spawn the gateway as a detached background process. Returns PID."""
    import subprocess

    existing = _is_running()
    if existing:
        raise RuntimeError(f"Gateway already running (PID {existing})")

    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOG_FILE, "a") as log_fd:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "reflect.gateway",
                "--grpc-port", str(grpc_port),
                "--http-port", str(http_port),
            ],
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
        )
    _PID_FILE.write_text(str(proc.pid))
    return proc.pid


def daemon_stop() -> bool:
    """Stop the gateway daemon. Returns True if a process was stopped."""
    pid = _is_running()
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    # Wait up to 3 seconds for clean exit
    import time
    for _ in range(30):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    _PID_FILE.unlink(missing_ok=True)
    return True


def daemon_status() -> dict:
    """Return gateway status dict."""
    pid = _is_running()
    traces_path = _DEFAULT_TRACES_PATH
    logs_path = _DEFAULT_LOGS_PATH
    return {
        "running": pid is not None,
        "pid": pid,
        "traces_path": str(traces_path),
        "logs_path": str(logs_path),
        "traces_size": traces_path.stat().st_size if traces_path.exists() else 0,
        "logs_size": logs_path.stat().st_size if logs_path.exists() else 0,
        "log_file": str(_LOG_FILE),
    }


# ---------------------------------------------------------------------------
# __main__ entry point (used by daemon_start via `python -m reflect.gateway`)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="reflect OTLP gateway")
    parser.add_argument("--grpc-port", type=int, default=4317)
    parser.add_argument("--http-port", type=int, default=4318)
    args = parser.parse_args()
    start_gateway(grpc_port=args.grpc_port, http_port=args.http_port)
