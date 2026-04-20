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

    assert re.search(r"body\{[^}]*font-size:16px;[^}]*line-height:1\.6;", text)
    assert re.search(r"\.header-meta\{[^}]*font-size:13px;", text)
    assert re.search(r"\.tab\{[^}]*font-size:14px;", text)
