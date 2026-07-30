"""Tests for skill integration: SKILL.md validation and CLI invocability."""

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from conftest import make_span, wrap_otlp

from reflect.core import main

SKILL_MD = Path(__file__).parent.parent / "src" / "reflect" / "data" / "skills" / "reflect" / "SKILL.md"
REFLECT_SKILLS_MD = (
    Path(__file__).parent.parent
    / "src"
    / "reflect"
    / "data"
    / "skills"
    / "reflect-skills"
    / "SKILL.md"
)
REFLECT_USAGE_MD = (
    Path(__file__).parent.parent
    / "src"
    / "reflect"
    / "data"
    / "skills"
    / "reflect-usage"
    / "SKILL.md"
)
REFLECT_USAGE_OPENAI_YAML = REFLECT_USAGE_MD.parent / "agents" / "openai.yaml"


class TestSkillMd:
    def test_skill_md_exists(self):
        assert SKILL_MD.exists(), f"SKILL.md not found at {SKILL_MD}"

    def test_reflect_skills_helper_exists(self):
        assert REFLECT_SKILLS_MD.exists(), f"SKILL.md not found at {REFLECT_SKILLS_MD}"

    def test_reflect_usage_helper_exists(self):
        assert REFLECT_USAGE_MD.exists(), f"SKILL.md not found at {REFLECT_USAGE_MD}"

    def test_package_data_references_current_skill_paths(self):
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")
        assert "data/skills/reflect/SKILL.md" in content
        assert "data/skills/reflect-skills/SKILL.md" in content
        assert "data/skills/reflect-usage/SKILL.md" in content
        assert "data/skills/reflect-usage/agents/openai.yaml" in content
        assert "data/loop-skill-prompt.md" in content
        assert "data/nudges/contract.json" in content
        assert "data/skills/skills/SKILL.md" not in content

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

    def test_skill_queries_approved_guidance_without_implicit_setup(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        assert 'reflect ask "<task question>" --json' in content
        assert "`reflect_skills`" in content
        assert "`reflect_patterns`" in content
        assert "`reflect_task_status`" in content
        assert "`reflect_review_change`" in content
        assert "`reflect_apply_change`" in content
        assert "ambiguous responses are not approval" in content
        assert "reflect loops build <loop-id>" in content
        assert "Do not run `reflect setup`" in content
        assert "only as an agent-operated fallback after explicit operator approval" in content

    def test_reflect_skill_packages_session_evidence_guidance(self):
        required_fragments = (
            "Making `reflect improve` actionable",
            "reflect improve --session current|SESSION_ID",
            "reflect improve --global --period day|week|month|all",
            "Repeated identical-input behavior lives in `reflect loops`",
            "cross-join inflation warning",
            "parent_session_id",
            "Do not use `sessions.agent` or `llm_calls.model`",
            "current schema does have `sessions.title`",
            "Missing token telemetry — cross-agent estimation fallback",
            "Cursor token counts are unavailable",
            "`token_provenance`",
            "Codex, OpenCode, or another normalized agent/session source",
        )

        content = SKILL_MD.read_text(encoding="utf-8")
        for fragment in required_fragments:
            assert fragment in content, f"{fragment!r} missing from {SKILL_MD}"
        assert "artifact file mtimes" not in content
        assert "SELECT session_id, tool_name, input_hash" not in content

    def test_reflect_usage_skill_uses_exact_cli_contract(self):
        content = REFLECT_USAGE_MD.read_text(encoding="utf-8")
        assert "reflect usage --json" in content
        assert "reflect usage --global --period week --json" in content
        assert "complete matching SQLite cohort" in content
        assert "Do not run `reflect setup`" in content
        assert '"where did my tokens go?"' in content
        assert (
            "Treat tool, MCP, and subagent counts as workflow context, not token attribution"
            in content
        )
        assert "`productive`, `mixed`, `inefficient`, or `indeterminate`" in content
        assert "Label behavioral explanations and value judgments `Inference`" in content

    def test_reflect_usage_openai_metadata_explains_token_analysis(self):
        content = REFLECT_USAGE_OPENAI_YAML.read_text(encoding="utf-8")
        assert 'display_name: "Reflect Token Usage"' in content
        assert "where this session’s tokens went" in content


class TestCliInvocable:
    def test_main_invocable_with_otlp(self, tmp_path):
        spans = [make_span("UserPromptSubmit", input_tokens=100)]
        p = tmp_path / "traces.json"
        p.write_text(wrap_otlp(spans) + "\n")
        runner = CliRunner()
        with patch("reflect.core._start_publish_server"):
            result = runner.invoke(main, [
                "--foreground",
                "--otlp-traces", str(p),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--db-path", str(tmp_path / "reflect.db"),
            ])
        assert result.exit_code == 0
