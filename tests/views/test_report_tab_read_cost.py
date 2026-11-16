from __future__ import annotations

import json
import sqlite3

import pytest

from reflect.views import report_tabs


def test_compact_sources_distinguish_usage_reports_mentions_and_actual_launches():
    from reflect.store.migrate import migrate

    conn = sqlite3.connect(":memory:")
    migrate(conn)
    now = "2026-11-08"
    conn.execute("INSERT INTO agents(id,name,created_at,updated_at) VALUES('agent','codex',?,?)", (now,now))
    for sid in ("selected", "other"):
        conn.execute("INSERT INTO sessions(id,agent_id,started_at,created_at,updated_at) VALUES(?,'agent',?,?,?)", (sid,now,now,now))
    for i, summary in enumerate(("BeforeReadFile", "AfterFileEdit", "SubagentStart", "SubagentStart", "SubagentStop", "UserPromptSubmit")):
        conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,summary,raw_attrs_json,created_at,updated_at)
            VALUES(?,'selected',?,'hook','2026-11-08',?,?,?,?)""", (str(i),i,summary,json.dumps({"prompt":"Use /review and the reader subagent; read edit " + "x" * 100_000}),now,now))
    for i, event in ((2,"SubagentStart"),(3,"SubagentStart"),(4,"SubagentStop")):
        conn.execute("""INSERT INTO agent_events(id,step_id,session_id,event_name,event_id,agent_type,created_at,updated_at)
            VALUES(?,?,'selected',?,?,'investigator',?,?)""", (str(i),str(i),event,event,now,now))
    conn.execute("""INSERT INTO skills(id,identity_key,slug,name,description,origin,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('skill','test:review','review','review','','test',?,?,?,?)""", (now,now,now,now))
    for i, state, relationship in ((0,"observed","loaded"),(1,"reported","reported_use"),(2,"inferred","mentioned"),(3,"observed","load_attempted")):
        conn.execute("""INSERT INTO skill_usage(id,skill_id,session_id,source_kind,source_id,state,evidence_json,observed_at,created_at,updated_at)
            VALUES(?,'skill','selected','test',?,?,?,?,?,?)""", (str(i),str(i),state,json.dumps({"relationship":relationship}),now,now,now))
    conn.executemany("INSERT INTO graph_nodes(id,kind,label,created_at,updated_at) VALUES(?,?,?,?,?)", [
        ("session","Session","selected",now,now),("mention","Subagent","reader",now,now)])
    conn.execute("""INSERT INTO graph_edges(id,source_node_id,target_node_id,kind,session_id,created_at,updated_at)
        VALUES('mention-edge','session','mention','spawned_subagent','selected',?,?)""", (now,now))
    conn.execute("""INSERT INTO graph_edge_sources(edge_id,source_kind,source_id,source_order,attrs_json)
        VALUES('mention-edge','step','5','5','{"evidence_class":"inferred"}')""")
    queries = []
    conn.set_trace_callback(queries.append)
    result = report_tabs._skill_subagent_counts(conn, ["selected"])
    assert report_tabs._skill_subagent_counts(conn, ["other"])["skills"] == {}
    conn.close()

    assert result["file_counts"] == {"reads": 1, "edits": 1}
    assert result["skills"] == {"review": 1}
    assert result["skill_reports"] == result["skill_mentions"] == result["skill_attempts"] == {"review": 1}
    assert result["skills_by_agent"] == {"codex": {"review": 1}}
    assert result["subagent_starts"] == result["subagent_stops"] == {"investigator": 1}
    assert result["subagent_mentions"] == {"reader": 1}
    assert result["subagents_by_agent"] == {"codex": {"investigator": 1}}
    assert all("raw_attrs_json" not in query for query in queries)


@pytest.mark.parametrize("scoped_ids", [None, ["selected"]])
def test_graph_selection_ranks_visible_metadata_and_preserves_kind_limits(monkeypatch, scoped_ids):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE graph_nodes(
            id TEXT PRIMARY KEY, kind TEXT, label TEXT, session_id TEXT,
            attrs_json TEXT, first_seen_at TEXT, last_seen_at TEXT
        );
        CREATE INDEX idx_graph_nodes_rank ON graph_nodes(session_id, kind, last_seen_at, first_seen_at, label, id);
    """)
    rows = [
        (f"{kind}-{idx}", kind, f"{kind}-{idx:03}", session, '{"retained":true}', None, "2026-01-01")
        for kind, count, session in [
            ("Tool", 55, "selected"), ("Execution", 55, "selected"),
            ("ToolCall", 100, "selected"), ("Agent", 2, None), ("Path", 1, "other"),
        ]
        for idx in range(count)
    ]
    conn.executemany("INSERT INTO graph_nodes VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    monkeypatch.setattr(report_tabs, "_workspace_peer_session_ids", lambda *_: scoped_ids)
    captured = []

    class SelectionCaptured(Exception):
        pass

    class FirstQuery:
        def execute(self, sql, params):
            captured.extend(conn.execute(sql, params).fetchall())
            self.plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
            raise SelectionCaptured

    query = FirstQuery()
    with pytest.raises(SelectionCaptured):
        report_tabs._semantic_graph(query, scoped_ids)
    conn.close()

    by_kind = {}
    for row in captured:
        by_kind.setdefault(row[1], []).append(row)
        assert json.loads(row[4]) == {"retained": True}
    assert {kind: len(items) for kind, items in by_kind.items()} == (
        {"Agent": 2, "Tool": 50, "Execution": 40, "ToolCall": 40, "Path": 1}
        if scoped_ids is None else {"Agent": 2, "Tool": 50, "Execution": 40, "ToolCall": 40}
    )
    assert [row[2] for row in by_kind["Tool"]] == [f"Tool-{idx:03}" for idx in range(50)]
    assert [row[2] for row in by_kind["ToolCall"]] == [f"ToolCall-{idx:03}" for idx in range(40)]
    assert any("COVERING INDEX idx_graph_nodes_rank" in line for line in query.plan)
