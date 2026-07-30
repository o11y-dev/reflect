# Inbox Finding Source Sessions

Inbox findings may group equivalent observations across repository or workspace
scopes. The representative `observation_id` remains the public identifier; no
separate persisted finding ID is introduced.

`GET /api/inbox/{observation_id}/sessions` resolves the full grouped observation
set in the service, then queries the lightweight `observation_sessions` ledger.
The response contains:

- the representative `observation_id`
- every grouped `observation_id`
- an optional workflow `candidate_id`
- the complete distinct source-session count
- bounded session rows with agent, workspace, title, start time, occurrence
  count, redacted evidence summaries, and a focus entity

Detailed `observation_evidence` remains capped for interactive output.
`observation_sessions` stores one compact row per finding observation and
supporting session so counts and path/session/time scoping do not depend on that
display cap.
