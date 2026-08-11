"""Release claims must match the executable capability and CI contracts."""

import tomllib
from pathlib import Path

from reflect.agent_capabilities import AGENT_CAPABILITIES, AgentSupport

ROOT = Path(__file__).parents[1]


def test_readme_capability_table_matches_registry() -> None:
    section = (ROOT / "README.md").read_text(encoding="utf-8").split(
        "## Agent Capability Truth", 1
    )[1].split("\n## ", 1)[0]
    rows = [
        tuple(cell.strip() for cell in line.strip("|").split("|"))
        for line in section.splitlines()
        if line.startswith("| ") and not line.startswith(("| Agent ", "|---"))
    ]
    expected = [
        (capability.display_name, capability.support.value, capability.telemetry_path)
        for capability in AGENT_CAPABILITIES
        if capability.support is not AgentSupport.PLANNED
    ]

    assert rows == expected


def test_package_platform_claims_match_ci_matrix() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    operating_systems = {
        classifier
        for classifier in project["classifiers"]
        if classifier.startswith("Operating System ::")
    }

    assert project["description"] == (
        "Local-first telemetry and measurable workflow improvement for AI coding agents"
    )
    assert operating_systems == {
        "Operating System :: MacOS",
        "Operating System :: POSIX :: Linux",
    }
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    assert "os: [ubuntu-latest, macos-latest]" in workflow
