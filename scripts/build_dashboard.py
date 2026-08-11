#!/usr/bin/env python3
"""Build Reflect's two shipped dashboard files from one authored client."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = REPO_ROOT / "src" / "reflect" / "frontend"
OUTPUTS = (
    REPO_ROOT / "src" / "reflect" / "data" / "index.html",
    REPO_ROOT / "docs" / "report.html",
)
CSS_SOURCES = (
    "css_foundation.css",
    "css_improvements.css",
    "css_explore.css",
    "css_sessions.css",
    "css_conversation.css",
    "css_graphs.css",
)
JS_SOURCES = (
    "js_bootstrap.js",
    "js_ledger.js",
    "js_navigation.js",
    "js_sessions.js",
    "js_impact.js",
    "js_explore.js",
    "js_graphs.js",
)


def _bundle(sources: tuple[str, ...]) -> str:
    return "".join(
        (FRONTEND_ROOT / source).read_text(encoding="utf-8")
        for source in sources
    ).rstrip("\n")


def render_dashboard() -> str:
    template = (FRONTEND_ROOT / "index.template.html").read_text(encoding="utf-8")
    replacements = {
        "{{ dashboard_css }}": _bundle(CSS_SOURCES),
        "{{ dashboard_js }}": _bundle(JS_SOURCES),
    }
    for marker, content in replacements.items():
        if template.count(marker) != 1:
            raise ValueError(f"Expected exactly one {marker!r} marker")
        template = template.replace(marker, content)
    return template


def build_dashboard() -> tuple[Path, ...]:
    rendered = render_dashboard()
    for output in OUTPUTS:
        output.write_text(rendered, encoding="utf-8")
    return OUTPUTS


if __name__ == "__main__":
    for path in build_dashboard():
        print(path.relative_to(REPO_ROOT))
