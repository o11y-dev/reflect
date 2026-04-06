"""Tests for skill integration: SKILL.md validation and CLI invocability."""

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from conftest import make_span, wrap_otlp

from reflect.core import main

SKILL_MD = Path(__file__).parent.parent / "src" / "reflect" / "data" / "skills" / "reflect" / "SKILL.md"


class TestSkillMd:
    def test_skill_md_exists(self):
        assert SKILL_MD.exists(), f"SKILL.md not found at {SKILL_MD}"

    def test_skill_md_has_reflect_name(self):
        content = SKILL_MD.read_text()
        assert "reflect" in content.lower()

    def test_skill_md_references_cli(self):
        content = SKILL_MD.read_text()
        assert "reflect" in content and ("--otlp" in content or "otlp" in content.lower())

    def test_skill_md_has_workflow_steps(self):
        content = SKILL_MD.read_text()
        # Should describe a multi-step workflow
        assert any(
            marker in content
            for marker in ["1.", "Step 1", "##", "workflow", "Workflow"]
        )


class TestCliInvocable:
    def test_main_invocable_with_otlp(self, tmp_path):
        spans = [make_span("UserPromptSubmit", input_tokens=100)]
        p = tmp_path / "traces.json"
        p.write_text(wrap_otlp(spans) + "\n")
        runner = CliRunner()
        with patch("reflect.core._render_terminal"):
            result = runner.invoke(main, [
                "--otlp-traces", str(p),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code == 0
