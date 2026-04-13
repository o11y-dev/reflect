---
name: skills
description: Use when the user wants to extract reusable skills from their AI session history and distribute them to their coding agents.
---

# skills

Run `reflect skills` to analyze your recent AI agent sessions and extract reusable skill patterns using an agent CLI.

## Usage

```bash
reflect skills                   # analyze last 7 days, use claude CLI
reflect skills --all             # analyze all available sessions
reflect skills --week            # analyze last 7 days (default)
reflect skills --agent gemini    # use Gemini CLI for extraction
reflect skills --yes             # skip confirmation prompt
reflect skills --demo            # run with bundled sample data
```

## What it does

1. Loads your local telemetry (same data as `reflect`)
2. Invokes the specified agent CLI with a predefined extraction prompt
3. Shows you the extracted skills for review
4. Asks for confirmation before writing
5. Distributes confirmed skills to all detected agent directories (`~/.claude/skills/`, `~/.cursor/skills/`, etc.)

Extracted skills are immediately available via `/skill-name` in Claude Code.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--agent` | `claude` | Agent CLI binary to use for extraction |
| `--yes` / `-y` | off | Skip confirmation prompt |
| `--all` / `--week` / `--month` / `--day` | `--week` | Time range for session analysis |
| `--demo` | off | Use bundled sample data |

## Example output

```
Running claude --print ...

Extracted 3 skills:

  debug-loop            Iterative debugging workflow with focused tool sequences
  context-reset         Pattern for clearing context and re-establishing scope
  test-first-fix        Test-driven bug fixing approach

Write these 3 skill(s) to 2 detected agent(s)? [Y/n]: y

  ✓ Claude Code: ~/.claude/skills/
  ✓ Cursor:      ~/.cursor/skills/

3 skills ready. Use /debug-loop, /context-reset, /test-first-fix in Claude Code.
```
