#!/usr/bin/env python3
"""Run the opt-in real-agent MCP integration suite on a developer machine."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

AGENTS = ("claude", "codex", "cursor")
SUITES = ("smoke", "effectiveness", "all")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Reflect MCP lifecycle tests through real local agent CLIs."
    )
    parser.add_argument(
        "--agent",
        action="append",
        choices=AGENTS,
        dest="agents",
        help="Agent to test; repeat to select more than one. Defaults to all.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Per-agent timeout in seconds (default: 180).",
    )
    parser.add_argument(
        "--suite",
        choices=SUITES,
        default="all",
        help="Test suite to run (default: all).",
    )
    args = parser.parse_args()

    if os.environ.get("CI"):
        parser.error("local agent tests are disabled when CI is set")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["REFLECT_RUN_LOCAL_AGENT_E2E"] = "1"
    env["REFLECT_LOCAL_AGENT_E2E_AGENTS"] = ",".join(args.agents or AGENTS)
    env["REFLECT_LOCAL_AGENT_E2E_TIMEOUT"] = str(args.timeout)

    suite_targets = {
        "smoke": [repo_root / "tests" / "local_agents" / "test_mcp_e2e.py"],
        "effectiveness": [
            repo_root / "tests" / "local_agents" / "test_effectiveness.py"
        ],
        "all": [repo_root / "tests" / "local_agents"],
    }
    command = [
        sys.executable,
        "-m",
        "pytest",
        *(str(path) for path in suite_targets[args.suite]),
        "-m",
        "local_agent_e2e",
        "-q",
        "-s",
        "--no-cov",
    ]
    return subprocess.call(command, cwd=repo_root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
