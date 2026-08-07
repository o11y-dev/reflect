# Local agent MCP end-to-end tests

These tests launch the real Antigravity, Claude, Codex, Cursor, GitHub Copilot,
and OpenCode CLIs using the current machine's authentication. They exercise the
complete `reflect_context` → `reflect_complete` lifecycle against an isolated
temporary SQLite database and verify the persisted task record. Windsurf uses
editor configuration and is intentionally excluded because it has no supported
headless agent CLI to run here.

They are opt-in, make real provider requests, and never run in CI.

```bash
poetry run python scripts/test_local_agents.py
poetry run python scripts/test_local_agents.py --agent codex
poetry run python scripts/test_local_agents.py --agent claude --agent cursor
poetry run python scripts/test_local_agents.py --agent antigravity --agent copilot
poetry run python scripts/test_local_agents.py --agent opencode
poetry run python scripts/test_local_agents.py --suite effectiveness --agent codex
poetry run python scripts/test_local_agents.py --suite blog
```

The suite disables or denies non-Reflect tools where each CLI supports that
boundary and uses temporary MCP configuration. Antigravity uses its documented
workspace MCP file plus sandbox while retaining the developer's existing authentication. It does not ask the agents to
inspect or modify repository files. Authentication, provider availability, MCP
startup, tool invocation, and completion persistence are intentionally treated
as test failures so local machine problems stay visible.

The effectiveness suite performs paired trials. A baseline agent and a
Reflect-guided agent receive the same opaque routing task. The expected decision
exists only in an isolated approved skill. The test scores only the final agent
message and requires the guided result to improve on the baseline, while also
verifying the selected skill and completed task in SQLite. This measures
guidance delivery and adherence; it is not a statistically significant
productivity benchmark.

The blog suite builds a wheel, installs it into a fresh temporary virtual
environment, and keeps its Reflect state outside the developer's live store.
Each selected agent drafts from one validated project memory, then a different
agent revises that draft in a new process. The test verifies the hidden content
contract plus exactly two completed task runs and the same selected memory per
agent. The deterministic telemetry contract separately proves the expected
12 sessions, 12 memory exposures, 24 logical MCP calls, and idempotent refresh
for all six headless agents without making provider requests.
