"""Observed Git/provider operations, derived from paired canonical tool calls.

This is an owned projection in ``evidence``, not another capture store. Command
text identifies an attempted operation; only its own result can confirm it.
"""
from __future__ import annotations

import hashlib
import re
import shlex
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any
from urllib.parse import unquote, urlsplit

try:
    import orjson as _json
except ImportError:
    import json as _json


def _object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            pass
    return {}


def _dump(value: object) -> str:
    result = _json.dumps(value)
    return result.decode() if isinstance(result, bytes) else result


@dataclass(frozen=True)
class GitOperation:
    operation: str
    provider: str
    repository: str | None = None
    artifact_kind: str | None = None
    artifact_id: str | None = None
    artifact_url: str | None = None
    artifact_key: str | None = None
    state: str = "attempted"
    reason: str = "terminal_result_missing"
    source: str = "cli"


_READS = {"view", "list", "diff", "status", "checks", "get", "show", "log", "fetch"}
_WRITES = {"create", "merge", "close", "reopen", "edit", "update", "approve", "review", "comment", "note"}
# shlex builds quoted tokens one character at a time. Captured shell tools can
# contain megabytes of embedded code; these are not bounded CLI evidence.
_MAX_COMMAND_CHARS = 32_768
_MCP_ACTIONS = {
    "create_merge_request": ("gitlab", "create", "merge_request"),
    "merge_merge_request": ("gitlab", "merge", "merge_request"),
    "accept_merge_request": ("gitlab", "merge", "merge_request"),
    "update_merge_request": ("gitlab", "update", "merge_request"),
    "approve_merge_request": ("gitlab", "approve", "merge_request"),
    "create_merge_request_note": ("gitlab", "comment", "merge_request"),
    "create_merge_request_line_comment": ("gitlab", "comment", "merge_request"),
    "get_merge_request_details": ("gitlab", "read", "merge_request"),
    "get_merge_request_changes": ("gitlab", "read", "merge_request"),
    "get_merge_request_discussions": ("gitlab", "read", "merge_request"),
    "get_merge_requests": ("gitlab", "read", "merge_request"),
    "create_pull_request": ("github", "create", "pull_request"),
    "merge_pull_request": ("github", "merge", "pull_request"),
    "update_pull_request": ("github", "update", "pull_request"),
    "pull_request_read": ("github", "read", "pull_request"),
    "get_pull_request": ("github", "read", "pull_request"),
}


def _option(tokens: list[str], *names: str) -> str | None:
    for index, token in enumerate(tokens):
        for name in names:
            if token.startswith(name + "="):
                return token[len(name) + 1:]
            if token == name and index + 1 < len(tokens):
                return tokens[index + 1]
    return None


def _api_operation(tokens: list[str], provider: str, repository: str | None) -> GitOperation | None:
    # Form fields imply POST for both gh/glab; an explicit method wins.
    method = (_option(tokens, "--method", "-X") or (
        "POST" if any(t in {"-f", "-F", "--field", "--raw-field", "--input"} for t in tokens) else "GET"
    )).upper()
    endpoint = next((t for t in tokens if re.match(r"/?(?:projects|repos)/", t)), "")
    match = re.search(r"(?:projects/(.+?)/merge_requests|repos/([^/]+/[^/]+)/pulls)(?:/(\d+))?(?:/(\w+))?(?:\?|$)", endpoint)
    if not match:
        return None
    repository = unquote(match.group(1) or match.group(2)) or repository
    number, suffix = match.group(3), match.group(4)
    kind = "merge_request" if match.group(1) else "pull_request"
    operation = "read" if method in {"GET", "HEAD"} else "update"
    if method == "POST" and not number:
        operation = "create"
    elif method in {"PUT", "POST"} and suffix == "merge":
        operation = "merge"
    elif suffix in {"notes", "comments", "reviews"}:
        operation = "comment" if method == "POST" else "read"
    elif suffix == "approve" and method == "POST":
        operation = "approve"
    return GitOperation(operation, provider, repository, kind, number)


def _cli_operation(command: str, repository: str | None) -> tuple[GitOperation | None, bool]:
    if len(command) > _MAX_COMMAND_CHARS:
        # Never truncate: a later separator/flag could change the operation.
        return None, False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        first = next(lexer, None)
        if first is None:
            return None, False
        # Reject wrappers before parsing their arbitrary script/body arguments.
        cli = PurePath(first).name
        if cli not in {"git", "gh", "glab"}:
            return None, False
        tokens = [first, *lexer]
    except ValueError:
        return None, False
    single = "\n" not in command and not any(token in {"&&", "||", ";", "|", "&", "(", ")"} for token in tokens)
    if any(t in {"--help", "-h", "--dry-run"} for t in tokens):
        return GitOperation("read", {"gh": "github", "glab": "gitlab"}.get(cli, cli), repository, reason="help_or_dry_run"), single
    if cli == "git":
        index = 1
        while index < len(tokens) and tokens[index].startswith("-"):
            flag = tokens[index]
            if flag in {"-C", "--git-dir", "--work-tree"}:
                # Worktree override is not evidence of the session's repository.
                repository = None
            index += 2 if flag in {"-C", "-c", "--git-dir", "--work-tree"} else 1
        if index >= len(tokens) or not re.fullmatch(r"[a-z-]+", tokens[index]):
            return None, False
        operation = tokens[index]
        return GitOperation(operation, "git", repository, "commit" if operation == "commit" else None), single
    provider = "github" if cli == "gh" else "gitlab"
    repository = _option(tokens, "--repo", "-R") or repository
    if len(tokens) > 1 and tokens[1] == "api":
        return _api_operation(tokens[2:], provider, repository), single
    if len(tokens) < 3 or tokens[1] not in {"pr", "mr"}:
        return None, False
    verb = tokens[2]
    if verb not in _READS | _WRITES:
        return None, False
    number = next((t for t in tokens[3:] if t.isdigit()), None)
    op = "read" if verb in _READS else "comment" if verb == "note" else verb
    if verb == "merge" and any(t in {"--auto", "--when-pipeline-succeeds"} for t in tokens):
        op = "schedule_merge"
    return GitOperation(op, provider, repository, "pull_request" if tokens[1] == "pr" else "merge_request", number), single


def _result(output: object) -> tuple[dict[str, Any], str, int | None, bool]:
    """Decode recognized outer result envelopes; arbitrary stdout is not status."""
    obj = _object(output)
    code = obj.get("exit_code")
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    failed = obj.get("isError") is True or obj.get("is_error") is True or (code is not None and code != 0)
    text = output if isinstance(output, str) else ""
    if isinstance(obj.get("output"), str):
        text = obj["output"]
        obj = _object(text) or obj
    elif isinstance(obj.get("structuredContent"), dict):
        obj = obj["structuredContent"]
    elif isinstance(obj.get("content"), list):
        texts = [b.get("text", "") for b in obj["content"] if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(texts)
        obj = _object(text) or obj
    return obj, text, code, failed


def _artifact_url(text: str) -> tuple[str, str, str, str] | None:
    match = re.search(r"https?://[^\s<>\"']+/(?:-/merge_requests|pull)/\d+", text)
    if not match:
        return None
    parsed = urlsplit(match.group(0))
    # Do not retain credentials/query strings from command or response URLs.
    if parsed.username or parsed.password:
        return None
    path = parsed.path.rstrip("/")
    repo, _, number = re.split(r"/(-/merge_requests|pull)/", path.lstrip("/"), maxsplit=1)
    url = f"{parsed.scheme}://{parsed.netloc}{path}"
    kind = "merge_request" if "/-/merge_requests/" in path else "pull_request"
    return url, f"{parsed.netloc.lower()}/{repo}", number, f"{parsed.netloc.lower()}/{repo}:{kind}:{number}"


def operation_for_tool(row: dict[str, Any]) -> GitOperation | None:
    attrs = _object(row.get("raw_attrs_json"))
    inputs = _object(row.get("input_preview_redacted")) or _object(attrs.get("gen_ai.client.tool.input"))
    repository = row.get("repository")
    command = next((attrs.get(k) for k in ("gen_ai.client.command", "ide.command", "command", "shell.command") if isinstance(attrs.get(k), str)), None)
    command = command or inputs.get("cmd") or inputs.get("command")
    single = True
    if isinstance(command, str):
        op, single = _cli_operation(command, repository)
    else:
        name = str(attrs.get("mcp.tool.name") or attrs.get("gen_ai.client.mcp_tool") or row.get("tool_name") or "")
        match = next((value for name_key, value in _MCP_ACTIONS.items() if name == name_key or name.endswith("__" + name_key) or name.endswith("_" + name_key) or name in {"mcp__gitlab_mcp" + name_key, "mcp__github_mcp" + name_key}), None)
        if not match:
            return None
        provider, action, kind = match
        repo = inputs.get("project_id") or inputs.get("repository") or inputs.get("repo") or repository
        if inputs.get("owner") and inputs.get("repo"):
            repo = f"{inputs['owner']}/{inputs['repo']}"
        number = inputs.get("merge_request_iid") or inputs.get("pull_number") or inputs.get("number")
        op = GitOperation(action, provider, str(repo) if repo else None, kind, str(number) if number else None, source="mcp")
    if op is None:
        return None
    output = row.get("output_preview_redacted") or attrs.get("gen_ai.client.tool.output") or attrs.get("tool.output")
    obj, text, code, failed = _result(output)
    raw_code = attrs.get("process.exit.code")
    if isinstance(raw_code, int) and not isinstance(raw_code, bool):
        code = raw_code
        failed = failed or code != 0
    failed = failed or str(row.get("status", "")).lower() in {"error", "failed", "failure"}
    state, reason = "attempted", "terminal_result_missing"
    if failed:
        state, reason = "failed", "operation_failed"
    elif output or code is not None:
        state, reason = "unresolved", "result_does_not_confirm_operation"
    artifact = _artifact_url(str(obj.get("web_url") or obj.get("html_url") or obj.get("url") or text))
    values = asdict(op)
    if artifact:
        values.update(artifact_url=artifact[0], repository=artifact[1], artifact_id=artifact[2], artifact_key=artifact[3])
    if not failed and single and op.reason != "help_or_dry_run":
        if op.provider == "git":
            if code == 0:
                state, reason = "confirmed", "terminal_exit_zero"
            if op.operation == "commit" and code == 0:
                commit = re.search(r"^\[[^\]\n]*?\b([0-9a-f]{7,64})\]", text, re.MULTILINE)
                if commit:
                    sha = commit.group(1)
                    values["artifact_id"] = sha
                    # Abbreviations are displayed but cannot establish global distinct identity.
                    if len(sha) in {40, 64} and values["repository"]:
                        values["artifact_key"] = f"{values['repository']}@{sha}"
                else:
                    state, reason = "unresolved", "commit_identity_missing"
        elif op.operation == "merge":
            if obj.get("merged") is True or artifact and obj.get("state") == "merged":
                state, reason = "confirmed", "provider_reports_merged"
        elif op.operation == "create":
            if artifact and (code == 0 or op.source == "mcp" or obj.get("id") is not None):
                state, reason = "confirmed", "created_artifact_returned"
        elif code == 0 or (op.source == "mcp" and output and (obj.get("id") is not None or artifact)):
            state, reason = "confirmed", "paired_terminal_result"
    if not single:
        state, reason = "unresolved", "compound_command_result_ambiguous"
    if op.operation == "merge" and (obj.get("already_merged") is True or "already merged" in str(obj.get("message") or "").lower()):
        state, reason = "unresolved", "merge_previously_completed"
    if op.reason == "help_or_dry_run":
        reason = op.reason
    values.update(state=state, reason=reason)
    return GitOperation(**values)


def refresh_git_activity(conn: sqlite3.Connection, *, session_ids: set[str] | None = None) -> dict[str, int]:
    """Reconcile only this projection's rows; caller owns the transaction."""
    if session_ids == set():
        return {"operations": 0}
    where = f"WHERE tc.session_id IN ({','.join('?' for _ in session_ids)})" if session_ids is not None else ""
    rows = conn.execute(f"""
        SELECT tc.*, st.started_at AS observed_at, s.repo_id, r.full_name AS repository
        FROM tool_calls tc JOIN steps st ON st.id=tc.step_id
        JOIN sessions s ON s.id=tc.session_id LEFT JOIN repos r ON r.id=s.repo_id
        {where}
    """, sorted(session_ids) if session_ids is not None else []).fetchall()
    columns = [c[0] for c in conn.execute("SELECT tc.*, '' AS observed_at, '' AS repo_id, '' AS repository FROM tool_calls tc LIMIT 0").description]
    now = datetime.now(UTC).isoformat()
    seen: set[str] = set()
    for raw in rows:
        row = dict(zip(columns, raw, strict=True))
        op = operation_for_tool(row)
        if op is None:
            continue
        identity = f"{row['session_id']}:{row.get('logical_call_id') or row['id']}"
        evidence_id = "vcs_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        seen.add(evidence_id)
        observed_at = row["observed_at"] if _object(row["raw_attrs_json"]).get("reflect.time.provenance") not in {"inferred", "synthetic"} else None
        payload = {"schema_version": 1, **asdict(op), "tool_call_id": row["id"], "logical_call_id": row.get("logical_call_id"), "observed_at": observed_at}
        conn.execute("""
            INSERT INTO evidence(id,session_id,step_id,repo_id,kind,summary,confidence,raw_json,created_at,updated_at)
            VALUES(?,?,?,?, 'vcs_operation', ?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            step_id=excluded.step_id, repo_id=excluded.repo_id, summary=excluded.summary,
            confidence=excluded.confidence, raw_json=excluded.raw_json, updated_at=excluded.updated_at
        """, (evidence_id, row["session_id"], row["step_id"], row["repo_id"], f"{op.provider} {op.operation}: {op.state}", 1.0 if op.state == "confirmed" else 0.0, _dump(payload), now, now))
    stale_where = f" AND session_id IN ({','.join('?' for _ in session_ids)})" if session_ids is not None else ""
    old_ids = [r[0] for r in conn.execute("SELECT id FROM evidence WHERE kind='vcs_operation'" + stale_where, sorted(session_ids) if session_ids is not None else [])]
    conn.executemany("DELETE FROM evidence WHERE id=? AND kind='vcs_operation'", [(i,) for i in old_ids if i not in seen])
    return {"operations": len(seen)}


def git_activity(conn: sqlite3.Connection, *, session_ids: set[str] | None = None, date_from: str | None = None, date_to: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Read derived facts only, including exact scoped totals before pagination."""
    clauses, params = ["e.kind='vcs_operation'"], []
    if session_ids is not None:
        clauses.append("e.session_id IN (%s)" % (",".join("?" for _ in session_ids) or "NULL"))
        params.extend(sorted(session_ids))
    base_where = " AND ".join(clauses)
    undated = conn.execute("SELECT COUNT(*) FROM evidence e JOIN sessions s ON s.id=e.session_id WHERE " + base_where + " AND julianday(json_extract(e.raw_json,'$.observed_at')) IS NULL", params).fetchone()[0]
    if date_from:
        clauses.append("julianday(json_extract(e.raw_json,'$.observed_at')) >= julianday(?)")
        params.append(date_from)
    if date_to:
        clauses.append("julianday(json_extract(e.raw_json,'$.observed_at')) < julianday(?)")
        params.append(date_to)
    source = "FROM evidence e JOIN sessions s ON s.id=e.session_id WHERE " + " AND ".join(clauses)
    state = "json_extract(e.raw_json,'$.state')"
    action = "json_extract(e.raw_json,'$.operation')"
    kind = "json_extract(e.raw_json,'$.artifact_kind')"
    key = "json_extract(e.raw_json,'$.artifact_key')"
    counts: dict[str, int] = {}
    for label, artifact_kind, operation in (
        ("distinct_commits", "commit", "commit"),
        ("created_pull_requests", "pull_request", "create"),
        ("created_merge_requests", "merge_request", "create"),
        ("merged_pull_requests", "pull_request", "merge"),
        ("merged_merge_requests", "merge_request", "merge"),
    ):
        counts[label] = conn.execute(f"SELECT COUNT(DISTINCT {key}) {source} AND {state}='confirmed' AND {kind}=? AND {action}=?", [*params, artifact_kind, operation]).fetchone()[0]
    states = dict(conn.execute(f"SELECT {state},COUNT(*) {source} GROUP BY {state}", params).fetchall())
    unidentified = conn.execute(f"SELECT COUNT(*) {source} AND {state}='confirmed' AND {action} IN ('create','commit','merge') AND {key} IS NULL", params).fetchone()[0]
    rows = conn.execute("SELECT e.id,e.session_id,e.step_id,e.raw_json " + source + " ORDER BY julianday(json_extract(e.raw_json,'$.observed_at')) DESC,e.id LIMIT ? OFFSET ?", [*params, max(0, min(limit, 500)), max(0, offset)]).fetchall()
    records = [{"evidence_id": r[0], "session_id": r[1], "step_id": r[2], **_object(r[3])} for r in rows]
    return {
        "scope": {"date_from": date_from, "date_to": date_to, "captured_sessions_only": True},
        "total_operations": sum(states.values()), "states": states, **counts,
        "undated_operations": undated,
        "unidentified_confirmed_artifacts": unidentified,
        "operations": records,
    }
