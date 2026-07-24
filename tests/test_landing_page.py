"""Regression coverage for the public Reflect landing page."""

import json
import re
import tomllib
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LANDING_PAGE = REPO_ROOT / "docs" / "index.html"
SHOWCASE_REPORT = REPO_ROOT / "docs" / "reports" / "showcase.json"
PYPROJECT = REPO_ROOT / "pyproject.toml"


class _LandingPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(str(attributes["id"]))
        if tag == "a" and attributes.get("href"):
            self.hrefs.append(str(attributes["href"]))


def _landing_text() -> str:
    return LANDING_PAGE.read_text(encoding="utf-8")


def test_landing_page_has_clear_product_and_conversion_path():
    text = _landing_text()

    assert "See How Agents Work." in text
    assert "Improve What Happens Next." in text
    assert text.count("Evidence, Not Vibes.") == 1
    assert "Behavioral Memory Graph" not in text
    assert 'class="eyebrow"' not in text
    assert "From Telemetry to Better Work" in text
    assert "Private by Default. Useful by Design." in text
    assert "From Install to Evidence in 60 Seconds." in text
    assert "Explore the Live Dashboard" in text
    assert "reflect skills discover --week" in text
    assert "Search the durable registry" in text
    assert "six read-only Reflect MCP inspection tools" in text
    assert "codex mcp add reflect -- reflect-mcp" in text
    assert "optional memory providers such as OMEGA" in text
    assert "Bring Your Memory Provider." in text
    assert "Use Reflect When the Work Needs an Answer." in text


def test_landing_page_has_task_oriented_scenario_tiles():
    text = _landing_text()

    assert text.count('class="scenario-card"') == 7
    for command in (
        "reflect usage --global --week",
        "reflect --week",
        "reflect improve",
        "reflect loops build LOOP_ID --agent codex",
        'reflect ask "How should I debug CI here?"',
        'reflect memory search "release gate" .',
        "reflect doctor",
    ):
        assert command in text
    assert 'href="#scenarios"' in text


def test_landing_page_lists_every_memory_provider_with_honest_support_levels():
    text = _landing_text()

    assert text.count('class="provider-card"') == 8
    assert text.count('class="provider-icon') == 8
    for provider_name in (
        "Local SQLite",
        "OMEGA",
        "Agent Memory",
        "LiteLLM",
        "Memory Palace",
        "Mem0",
        "Graphiti",
        "TencentDB Agent Memory",
    ):
        assert f"<strong>{provider_name}</strong>" in text
    assert "Connected Providers" in text
    assert "Discovery Adapters" in text
    assert text.count("Discovery only") == 3


def test_landing_page_keeps_showcase_metrics_current():
    text = _landing_text()
    report = json.loads(SHOWCASE_REPORT.read_text(encoding="utf-8"))

    assert f"{report['unique_sessions']:,} sessions · {len(report['agents'])} agents" in text
    assert f"{report['avg_quality_score']:.1f}" in text
    assert f"{report['total_spans']:,}" in text
    assert f"{report['tool_calls']:,}" in text
    assert f"{report['tool_failures']:,}" in text
    assert f"{report['mcp_calls']:,}" in text
    assert f"${sum(report['model_costs_usd'].values()):.2f}" in text
    assert f"{report['subagent_launches']:,}" in text
    assert "Cost covers sessions with recognized model-pricing evidence." in text


def test_landing_page_agent_rail_uses_accessible_logo_marks():
    text = _landing_text()

    assert text.count('class="agent-logo') == 7
    assert text.count('aria-hidden="true"><svg width="20" height="20"') == 6
    for agent_name in (
        "Claude Code",
        "Codex",
        "Cursor",
        "GitHub Copilot",
        "Gemini CLI",
        "OpenCode",
        "Antigravity",
    ):
        assert f"<span>{agent_name}</span>" in text


def test_landing_page_repo_proof_progressively_loads_github_stats():
    text = _landing_text()
    version_match = re.search(
        r'^version\s*=\s*"([^"]+)"',
        PYPROJECT.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert version_match is not None

    assert 'class="repo-proof nav-repo-proof" href="https://github.com/o11y-dev/reflect"' in text
    nav_start = text.index('<nav class="nav"')
    nav_end = text.index("</nav>", nav_start)
    repo_proof = text.index('class="repo-proof nav-repo-proof"')
    assert nav_start < repo_proof < nav_end
    assert 'id="repo-stats" aria-live="polite" aria-atomic="true" aria-busy="true"' in text
    assert f'id="repo-release">v{version_match.group(1)}</span>' in text
    assert 'id="repo-stars">Stars</span>' in text
    assert 'id="repo-forks">Forks</span>' in text
    assert "new Intl.NumberFormat" in text
    assert "Promise.allSettled" in text
    assert "https://api.github.com/repos/o11y-dev/reflect" in text
    assert "https://api.github.com/repos/o11y-dev/reflect/releases/latest" in text
    assert "localStorage.setItem(cacheKey" in text
    assert "stats.setAttribute('aria-busy', 'false')" in text
    assert ".textContent =" in text
    assert ".innerHTML =" not in text


def test_landing_page_meets_static_accessibility_contracts():
    text = _landing_text()
    parser = _LandingPageParser()
    parser.feed(text)
    parser.close()

    assert '<a class="skip-link" href="#main-content">' in text
    assert '<main id="main-content">' in text
    assert 'aria-label="Primary Navigation"' in text
    assert ":focus-visible" in text
    assert "@media (prefers-reduced-motion:reduce)" in text
    assert "touch-action:manipulation" in text
    assert "scroll-margin-top" in text
    assert "color-scheme:dark" in text
    assert "transition:all" not in text.replace(" ", "")
    assert "outline:none" not in text.replace(" ", "")
    assert "user-scalable=no" not in text
    assert "maximum-scale=1" not in text

    fragment_links = {href[1:] for href in parser.hrefs if href.startswith("#")}
    assert fragment_links <= parser.ids


def test_landing_page_has_complete_social_and_structured_metadata():
    text = _landing_text()

    assert "Local-First Observability for AI Coding Agents" in text
    assert '<meta property="og:image" content="https://reflect.o11y.dev/og-image-v2.png">' in text
    assert '<meta property="og:image:type" content="image/png">' in text
    assert '<meta property="og:image:width" content="1200">' in text
    assert '<meta property="og:image:height" content="630">' in text
    assert '<meta property="og:image:alt"' in text
    assert '<meta name="twitter:card" content="summary_large_image">' in text
    assert '<meta name="twitter:image:alt"' in text
    assert '"@type": "SoftwareApplication"' in text
    assert '"license": "https://www.apache.org/licenses/LICENSE-2.0"' in text


def test_landing_page_social_image_is_shipped_at_declared_dimensions():
    public_image = REPO_ROOT / "docs" / "og-image-v2.png"
    packaged_image = REPO_ROOT / "src" / "reflect" / "data" / "og-image-v2.png"

    public_bytes = public_image.read_bytes()
    assert public_bytes == packaged_image.read_bytes()
    assert public_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(public_bytes[16:20], "big") == 1200
    assert int.from_bytes(public_bytes[20:24], "big") == 630

    package_data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))[
        "tool"
    ]["setuptools"]["package-data"]["reflect"]
    assert "data/og-image-v2.png" in package_data
