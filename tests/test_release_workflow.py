from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import release_workflow


def test_parse_semantic_release_output_ignores_already_released_version():
    output = "\n".join([
        "[20:31:38] WARNING  Token value is missing!",
        "0.1.1",
        "No release will be made, 0.1.1 has already been released!",
    ])

    assert release_workflow.parse_semantic_release_output(output) is None


def test_determine_version_prefers_untagged_pyproject_version(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.2.0"\n',
        encoding="utf-8",
    )

    runner = Mock()
    version = release_workflow.determine_version(root=tmp_path, runner=runner)

    assert version == "0.2.0"
    runner.assert_not_called()


def test_determine_version_uses_semantic_release_when_tag_exists(tmp_path: Path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.1.1"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(release_workflow, "version_tag_exists", lambda version, root: True)
    runner = Mock(return_value=Mock(stdout="0.2.0\n", stderr="", returncode=0))

    version = release_workflow.determine_version(root=tmp_path, force="minor", runner=runner)

    assert version == "0.2.0"
    runner.assert_called_once()
    assert runner.call_args.args[0] == ["semantic-release", "version", "--print", "--minor"]


def test_extract_release_notes_uses_requested_section_only(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "\n".join([
            "# Changelog",
            "",
            "## 0.3.0 (unreleased)",
            "",
            "### Added",
            "- future work",
            "",
            "## 0.2.0 (2026-04-13)",
            "",
            "### Added",
            "- shipped change",
            "",
            "### Fixed",
            "- released fix",
            "",
        ]),
        encoding="utf-8",
    )

    notes = release_workflow.extract_release_notes("0.2.0", changelog)

    assert notes == "### Added\n- shipped change\n\n### Fixed\n- released fix\n"
    assert "future work" not in notes


def test_extract_release_notes_requires_existing_section(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no changelog section found for 0.2.0"):
        release_workflow.extract_release_notes("0.2.0", changelog)
