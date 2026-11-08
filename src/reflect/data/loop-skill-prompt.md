You are authoring a reusable coding-agent skill from a bounded Reflect loop evidence bundle.

The loop is observed behavior, not automatically a reusable procedure. Return [] when it does not support a useful intervention. Otherwise return one draft for review:

- For a stalled loop, design a state-changing recovery routine. Never copy failed repetition as guidance.
- For a productive loop, preserve observed checks separately from reported or unknown outcomes, with explicit preconditions and bounded iteration.
- For an agent-native loop or goal, preserve its documented objective and trigger. Add bounded checkpoints, progress evidence, a verifiable stop condition, and an operator handoff. Do not reproduce wake sentinels as instructions.
- Include trigger, required inputs, loop state, one iteration, exit/escalation, relevant verification, and abstention conditions.
- Do not invent commands or evidence, turn agent success claims into verification, or claim measured improvement. No mandatory Reflect lifecycle or self-report calls.
- Drafts remain pending review. Do not approve or install them.

Output ONLY a JSON array containing zero or one object:

[
  {
    "name": "kebab-case-skill-name",
    "description": "One sentence describing when to invoke it.",
    "content": "# Skill title\n\nFull markdown body without YAML frontmatter.",
    "preconditions": ["Required inputs and applicable scope."],
    "checkpoints": ["Bounded progress or state-change decision."],
    "verification": ["Relevant observable check, with unknowns retained."],
    "abstain_when": ["Missing inputs, different scope, or unsupported checks."]
  }
]

Evidence JSON (authoritative):

{{EVIDENCE_JSON}}
