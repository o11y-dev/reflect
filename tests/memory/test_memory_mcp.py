from reflect.mcp import memory_remember, memory_search, memory_validate, service_context


def test_mcp_memory_handlers_round_trip(tmp_path):
    db_path = str(tmp_path / "reflect.db")
    remembered = memory_remember(
        {
            "db_path": db_path,
            "content": "MCP memory search works",
            "type": "note",
            "scope": "project",
            "manual_note": True,
        }
    )
    memory_id = remembered["memory"]["id"]

    search = memory_search({"db_path": db_path, "query": "MCP"})
    validation = memory_validate({"db_path": db_path, "memory_id": memory_id})
    context = service_context({"db_path": db_path, "all": True})

    assert search["results"]
    assert validation["validation"]["status"] == "validated"
    assert context["providers"]
