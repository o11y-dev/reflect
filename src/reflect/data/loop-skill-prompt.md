You are authoring one reusable coding-agent skill from a bounded Reflect loop evidence bundle.

The loop is observed behavior, not automatically a workflow. Decide whether the evidence supports a useful intervention:

- For a stalled loop, design a state-changing recovery routine. Never copy the failed repetition as guidance.
- For a productive loop, preserve the verified routine with explicit preconditions and bounded iteration.
- Include trigger, required inputs, loop state, one iteration, exit/escalation, verification, and output.
- Do not invent repository commands or evidence that are not present.
- Do not approve, install, or claim the skill improved outcomes.

Output ONLY a JSON array containing exactly one object:

[
  {
    "name": "kebab-case-skill-name",
    "description": "One sentence describing when to invoke it.",
    "content": "# Skill title\n\nFull markdown body without YAML frontmatter."
  }
]

Evidence JSON (authoritative):

{{EVIDENCE_JSON}}
