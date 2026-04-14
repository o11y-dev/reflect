import re
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock

import pytest


def _load_release_workflow():
    repo_root = Path(__file__).resolve().parent.parent
    module_path = repo_root / "scripts" / "release_workflow.py"
    spec = spec_from_file_location("test_release_workflow_module", module_path)
    module = module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


release_workflow = _load_release_workflow()


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

    original_version_tag_exists = release_workflow.version_tag_exists
    release_workflow.version_tag_exists = lambda version, root: False
    runner = Mock()
    try:
        version = release_workflow.determine_version(root=tmp_path, runner=runner)
    finally:
        release_workflow.version_tag_exists = original_version_tag_exists

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


def test_version_tag_exists_checks_tag_namespace_only(monkeypatch):
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(release_workflow.subprocess, "run", run)

    assert release_workflow.version_tag_exists("0.2.0") is True
    assert run.call_args.args[0] == [
        "git",
        "show-ref",
        "--tags",
        "--verify",
        "--quiet",
        "refs/tags/v0.2.0",
    ]


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (None, "pyproject.toml not found"),
        ("not = [valid", "pyproject.toml is not valid TOML"),
        ("[project]\nname = 'o11y-reflect'\n", "pyproject.toml missing [project].version"),
    ],
)
def test_read_project_version_reports_common_configuration_errors(
    tmp_path: Path, contents: str | None, message: str
):
    pyproject = tmp_path / "pyproject.toml"
    if contents is not None:
        pyproject.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(message)):
        release_workflow.read_project_version(pyproject)


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
