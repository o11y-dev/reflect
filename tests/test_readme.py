from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _readme() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_has_current_quick_start_contract():
    readme = _readme()

    assert "Evidence, Not Vibes." in readme
    assert readme.count("## Quick Start") == 1
    assert "pipx install o11y-reflect" in readme
    assert "reflect setup" in readme
    assert "reflect doctor" in readme
    assert "reflect\n```" in readme
    assert "reflect --demo" in readme
    assert "### Ask through your agent" in readme
    assert "### Explore visually in the UI" in readme
    assert "Use Reflect to explain why this session was expensive" in readme
    assert "Use Reflect to prepare an internal AI budget increase request" in readme
    assert "Evidence-backed improvement for AI agent work across teams" in readme
    assert "From Personal Reflection to Organizational Improvement" in readme
    assert "human review" in readme


def test_readme_explains_current_product_surfaces():
    readme = _readme()

    for surface in ("Inbox", "Sessions", "Workflows", "Skills", "Impact", "Explore"):
        assert f"**{surface}**" in readme

    assert "A repeated loop can motivate a workflow" in readme
    assert "neither conversion happens automatically" in readme


def test_readme_does_not_restore_superseded_opening():
    readme = _readme()

    assert "## Quickstart" not in readme
    assert "Behavioral memory for developer-agent behavior and workflow." not in readme
    assert "switching prompt style can cut costs 30–50%" not in readme
    assert "portable skill for Claude Code" not in readme
    assert "░▒▓" not in readme
