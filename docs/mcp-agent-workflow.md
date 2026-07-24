# MCP-first agent workflow

## Product decision

Ordinary users should not need to know or run Reflect CLI commands. They should give an agent a normal repository task. The agent should retrieve Reflect guidance, follow the selected skill, report the outcome, and present any evidence-backed improvement for conversational approval.

The CLI and browser remain useful for debugging, automation, audit, and deep review. The MCP server is the primary runtime interface for agents.

## Responsibility boundaries

| Layer | Responsibility |
|---|---|
| Skills | Reusable execution steps, preconditions, recovery, verification, and exit conditions |
| MCP | Task-time skill selection, evidence delivery, outcome capture, and reviewed change execution |
| Telemetry and SQLite | Evidence, provenance, adherence, outcomes, and before/after measurement |
| CLI and browser | Optional audit, administration, automation, and recovery surfaces |

MCP must not become a second workflow renderer. `WorkflowDefinition` remains the structured behavior contract and Skills v2 remains the durable versioned package registry. MCP selects and measures those records.

## Agent lifecycle

1. After identifying a non-trivial repository task and its path, the agent calls `reflect_context`.
2. Reflect returns approved guidance, selected skill versions, one explicit execution state, constraints, verification, and a `task_run_id`.
3. The agent follows a complete selected skill when `execution_state` is `follow_allowed` and its preconditions match. If the state is `retrieve_full_instructions`, it retrieves the complete version first.
4. After validation, the agent calls `reflect_complete` with the task outcome.
5. Reflect links the task run to the runtime session when ingestion is available and records selected-skill usage.
6. Existing detectors and measurements use the completed session evidence to identify improvements.
7. Future MCP review tools present an exact proposed change to the user.
8. Only an explicitly approved, immutable change may be applied.

## Delivery plan

### Phase 1. Task guidance and completion

Implemented by the initial MCP-first change:

- operational MCP server instructions with explicit call timing
- task-scoped `reflect_context` runs with privacy-safe question hashes
- selected Skills v2 metadata and bounded skill instructions
- explicit execution state separated from registry and installation lifecycle
- signaled instruction truncation with exact full-version retrieval through `reflect_explain`
- `reflect_complete` outcomes and verification results
- late-ingestion-safe runtime session identity
- selected-skill outcome linkage when the session already exists
- MCP integration tests and documentation

### Phase 2. Agent-native inspection

Expose typed read-only access without mirroring arbitrary CLI strings:

- list and search skills by lifecycle, availability, and evidence
- explain a skill version, its source sessions, and measurements
- inspect loops and workflow candidates
- extend `reflect_explain` to cover skill and task-run identifiers
- reconcile task runs whose telemetry arrived after `reflect_complete`

### Phase 3. Conversational review and application

Add a two-step mutation contract:

1. `reflect_review_change` returns the exact target, diff, evidence, risks, rollback plan, and an immutable approval token.
2. After explicit user approval, `reflect_apply_change` accepts only that token.

Pending proposals may be staged without changing agent configuration. Applying, replacing, or rolling back an installed skill always requires explicit operator approval. Do not expose a generic `reflect_cli(command)` escape hatch.

### Phase 4. Guidance reliability

- install the small Reflect bootstrap skill with every supported agent
- add host-specific task-start injection where a safe native hook exists
- include exact machine-readable `next_action` contracts in MCP responses
- measure eligible repository sessions that did not call `reflect_context`
- report task-start and task-completion coverage through `reflect doctor`

### Phase 5. Evidence-backed self-improvement

- compare outcomes across skill versions and task archetypes
- generate pending versions only from bounded supporting evidence
- include the expected metric improvement and required validation
- dogfood the same lifecycle on the Reflect repository
- preserve prior versions and rollback state

Reflect may propose improvements to itself. It must never silently apply them.

## Success measures

- guidance coverage for eligible repository sessions
- completion callback coverage
- selected-skill use and rejection rate
- verification pass rate
- task success, recovery, and repeated-failure rate
- time, tool-call, and token change after skill activation
- accepted, rejected, and rolled-back improvement rate

The product loop is:

> Observe. Guide. Execute. Verify. Learn. Reuse.
