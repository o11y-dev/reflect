# Local agent MCP end-to-end tests

These tests launch the real Claude, Codex, and Cursor CLIs using the current
machine's authentication. They exercise the complete `reflect_context` →
`reflect_complete` lifecycle against an isolated temporary SQLite database and
verify the persisted task record.

They are opt-in, make real provider requests, and never run in CI.

```bash
poetry run python scripts/test_local_agents.py
poetry run python scripts/test_local_agents.py --agent codex
poetry run python scripts/test_local_agents.py --agent claude --agent cursor
```

The suite uses a read-only/ask execution mode and temporary MCP configuration.
It does not ask the agents to inspect or modify repository files. Authentication,
provider availability, MCP startup, tool invocation, and completion persistence
are intentionally treated as test failures so local machine problems stay
visible.
