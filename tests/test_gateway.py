"""Tests for the local OTLP gateway (reflect.gateway)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from conftest import DAY1, HOUR, make_span, wrap_otlp

from reflect.core import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def gateway_output_dir(tmp_path):
    """Provide a tmp dir for gateway output files."""
    otlp_dir = tmp_path / "otlp"
    otlp_dir.mkdir()
    return otlp_dir


@pytest.fixture
def traces_path(gateway_output_dir):
    return gateway_output_dir / "otel-traces.json"


@pytest.fixture
def logs_path(gateway_output_dir):
    return gateway_output_dir / "otel-logs.json"


# ---------------------------------------------------------------------------
# File writer tests
# ---------------------------------------------------------------------------


class TestAppendJsonl:
    def test_append_traces_creates_file(self, traces_path):
        from reflect.gateway import append_traces

        payload = {"resourceSpans": [{"resource": {}, "scopeSpans": []}]}
        append_traces(payload, traces_path)

        assert traces_path.exists()
        line = traces_path.read_text().strip()
        parsed = json.loads(line)
        assert "resourceSpans" in parsed

    def test_append_logs_creates_file(self, logs_path):
        from reflect.gateway import append_logs

        payload = {"resourceLogs": [{"resource": {}, "scopeLogs": []}]}
        append_logs(payload, logs_path)

        assert logs_path.exists()
        line = logs_path.read_text().strip()
        parsed = json.loads(line)
        assert "resourceLogs" in parsed

    def test_append_multiple_lines(self, traces_path):
        from reflect.gateway import append_traces

        for i in range(3):
            append_traces({"resourceSpans": [], "seq": i}, traces_path)

        lines = [line for line in traces_path.read_text().strip().split("\n") if line]
        assert len(lines) == 3
        assert json.loads(lines[2])["seq"] == 2

    def test_output_is_parseable_by_reflect(self, traces_path):
        """Verify the gateway output is compatible with _load_otlp_traces."""
        from reflect.gateway import append_traces
        from reflect.parsing import _load_otlp_traces

        payload = json.loads(wrap_otlp(
            [make_span("UserPromptSubmit", input_tokens=100, output_tokens=50)],
        ))
        append_traces(payload, traces_path)

        spans = list(_load_otlp_traces(traces_path))
        assert len(spans) == 1
        assert spans[0]["attributes"]["gen_ai.client.hook.event"] == "UserPromptSubmit"


# ---------------------------------------------------------------------------
# Protobuf conversion tests
# ---------------------------------------------------------------------------


class TestProtoConversion:
    def test_trace_ids_converted_to_hex(self):
        from reflect.gateway import _fix_trace_ids

        payload = {
            "resourceSpans": [{
                "resource": {},
                "scopeSpans": [{
                    "spans": [{
                        "traceId": "qrvM3aq7zN2qu8zdqrvM3Q==",
                        "spanId": "ASNFZ4mrze8=",
                        "parentSpanId": "",
                    }]
                }]
            }]
        }
        fixed = _fix_trace_ids(payload)
        span = fixed["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["traceId"] == "aabbccddaabbccddaabbccddaabbccdd"
        assert span["spanId"] == "0123456789abcdef"

    def test_log_ids_and_severity_converted(self):
        from reflect.gateway import _fix_log_ids

        payload = {
            "resourceLogs": [{
                "resource": {},
                "scopeLogs": [{
                    "logRecords": [{
                        "traceId": "qrvM3aq7zN2qu8zdqrvM3Q==",
                        "spanId": "ASNFZ4mrze8=",
                        "severityNumber": "SEVERITY_NUMBER_INFO",
                    }]
                }]
            }]
        }
        fixed = _fix_log_ids(payload)
        record = fixed["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        assert record["traceId"] == "aabbccddaabbccddaabbccddaabbccdd"
        assert record["severityNumber"] == 9

    def test_proto_to_traces_dict(self):
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

        from reflect.gateway import _proto_to_traces_dict

        req = trace_service_pb2.ExportTraceServiceRequest()
        rs = req.resource_spans.add()
        rs.resource.attributes.add(key="service.name").value.string_value = "test"
        ss = rs.scope_spans.add()
        span = ss.spans.add()
        span.trace_id = b"\xaa\xbb\xcc\xdd" * 4
        span.span_id = b"\x01\x23\x45\x67\x89\xab\xcd\xef"
        span.name = "test-span"
        span.start_time_unix_nano = DAY1
        span.end_time_unix_nano = DAY1 + HOUR

        result = _proto_to_traces_dict(req)
        assert "resourceSpans" in result
        s = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        # IDs should be hex, not base64
        assert s["traceId"] == "aabbccddaabbccddaabbccddaabbccdd"
        assert s["spanId"] == "0123456789abcdef"
        assert s["name"] == "test-span"


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


class TestHttpEndpoints:
    def test_health_endpoint(self):
        from fastapi.testclient import TestClient

        from reflect.gateway import _build_http_app

        app = _build_http_app()
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_post_traces(self, traces_path):
        from fastapi.testclient import TestClient

        from reflect.gateway import _build_http_app

        app = _build_http_app(traces_path=traces_path)
        client = TestClient(app)

        payload = json.loads(wrap_otlp(
            [make_span("UserPromptSubmit", input_tokens=100, output_tokens=50)],
        ))
        resp = client.post("/v1/traces", json=payload)
        assert resp.status_code == 200

        assert traces_path.exists()
        spans_on_disk = json.loads(traces_path.read_text().strip())
        assert "resourceSpans" in spans_on_disk

    def test_post_logs(self, logs_path):
        from fastapi.testclient import TestClient

        from reflect.gateway import _build_http_app

        app = _build_http_app(logs_path=logs_path)
        client = TestClient(app)

        payload = {
            "resourceLogs": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": "test"}},
                ]},
                "scopeLogs": [{
                    "logRecords": [{
                        "timeUnixNano": str(DAY1),
                        "severityText": "INFO",
                        "severityNumber": 9,
                        "body": {"stringValue": "test message"},
                        "attributes": [],
                    }]
                }]
            }]
        }
        resp = client.post("/v1/logs", json=payload)
        assert resp.status_code == 200

        assert logs_path.exists()
        logs_on_disk = json.loads(logs_path.read_text().strip())
        assert "resourceLogs" in logs_on_disk


# ---------------------------------------------------------------------------
# Daemon helpers tests
# ---------------------------------------------------------------------------


class TestDaemonHelpers:
    def test_is_running_no_pid_file(self, tmp_path):
        with patch("reflect.gateway._PID_FILE", tmp_path / "nonexistent.pid"):
            from reflect.gateway import _is_running
            assert _is_running() is None

    def test_is_running_stale_pid(self, tmp_path):
        pid_file = tmp_path / "gateway.pid"
        pid_file.write_text("999999999")  # unlikely to be a real PID
        with patch("reflect.gateway._PID_FILE", pid_file):
            from reflect.gateway import _is_running
            assert _is_running() is None
            assert not pid_file.exists()  # cleaned up

    def test_daemon_status_when_stopped(self, tmp_path):
        with (
            patch("reflect.gateway._PID_FILE", tmp_path / "gateway.pid"),
            patch("reflect.gateway._DEFAULT_TRACES_PATH", tmp_path / "traces.json"),
            patch("reflect.gateway._DEFAULT_LOGS_PATH", tmp_path / "logs.json"),
            patch("reflect.gateway._LOG_FILE", tmp_path / "gateway.log"),
        ):
            from reflect.gateway import daemon_status
            status = daemon_status()
            assert status["running"] is False
            assert status["pid"] is None


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestGatewayCLI:
    def test_gateway_help(self, runner):
        result = runner.invoke(main, ["gateway", "--help"])
        assert result.exit_code == 0
        assert "gateway" in result.output.lower()

    def test_gateway_start_help(self, runner):
        result = runner.invoke(main, ["gateway", "start", "--help"])
        assert result.exit_code == 0

    def test_gateway_stop_help(self, runner):
        result = runner.invoke(main, ["gateway", "stop", "--help"])
        assert result.exit_code == 0

    def test_gateway_status_help(self, runner):
        result = runner.invoke(main, ["gateway", "status", "--help"])
        assert result.exit_code == 0
