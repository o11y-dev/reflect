"""Regression checks for static dashboard assets."""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_HTML_FILES = (
    REPO_ROOT / "src/reflect/data/index.html",
    REPO_ROOT / "docs/index.html",
)


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_uses_larger_font_baseline(path: Path):
    text = path.read_text(encoding="utf-8")

    assert re.search(r"body\s*\{[^}]*font-size\s*:\s*16px\s*;", text), (
        f"Expected body font-size: 16px in {path}"
    )
    assert re.search(r"body\s*\{[^}]*line-height\s*:\s*1\.6\s*;", text), (
        f"Expected body line-height: 1.6 in {path}"
    )
    assert re.search(r"\.header-meta\s*\{[^}]*font-size\s*:\s*13px\s*;", text), (
        f"Expected .header-meta font-size: 13px in {path}"
    )
    assert re.search(r"\.tab\s*\{[^}]*font-size\s*:\s*14px\s*;", text), (
        f"Expected .tab font-size: 14px in {path}"
    )


@pytest.mark.parametrize("path", DASHBOARD_HTML_FILES)
def test_dashboard_html_surfaces_cost_controls(path: Path):
    text = path.read_text(encoding="utf-8")

    assert '<option value="cost">Most cost</option>' in text
    assert 'id="cost-stats"' in text
    assert 'id="model-cost-share"' in text
    assert "fmtCost" in text


def test_development_docs_use_poetry_commands():
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "poetry install --extras test" in agents
    assert "poetry run pytest" in agents
    assert "poetry install --extras test" in readme
    assert "poetry run reflect doctor" in readme
    assert "pip install -e .[test]" not in agents
    assert "python3 -m pytest" not in agents
