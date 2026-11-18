"""Bounded backend completion queries over a pinned publication."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CompletionQuery:
    """A bounded, read-only completion query over the local Reflect store."""

    table: str
    value: str
    label: str
    order_by: str
    distinct: bool = False


class CompletionCatalog:
    """Return privacy-safe completion labels without creating or migrating a DB."""

    _queries = {
        "observation": CompletionQuery("observations", "id", "title", "updated_at DESC"),
        "workflow": CompletionQuery("workflow_candidates", "id", "title", "updated_at DESC"),
        "loop": CompletionQuery("loop_patterns", "id", "title", "updated_at DESC"),
        "session": CompletionQuery(
            "sessions",
            "id",
            "status || ' · ' || started_at",
            "started_at DESC",
        ),
        "skill": CompletionQuery("skills", "id", "slug", "updated_at DESC"),
        "memory": CompletionQuery("memories", "id", "type || ' · ' || scope", "updated_at DESC"),
        "memory_candidate": CompletionQuery(
            "memory_candidates",
            "id",
            "type || ' · ' || scope",
            "updated_at DESC",
        ),
        "memory_type": CompletionQuery("memories", "type", "type", "type", distinct=True),
        "memory_scope": CompletionQuery("memories", "scope", "scope", "scope", distinct=True),
        "memory_source": CompletionQuery("memories", "source", "source", "source", distinct=True),
        "memory_provider": CompletionQuery("memories", "provider", "provider", "provider", distinct=True),
    }

    def __init__(self, *, limit: int = 100) -> None:
        self.limit = limit

    def complete(self, entity: str, incomplete: str, conn) -> list[dict]:
        query = self._queries[entity]
        prefix = self._escape_like(incomplete) + "%"
        distinct = "DISTINCT " if query.distinct else ""
        table = query.table
        schemas = {row[1] for row in conn.execute("PRAGMA database_list")}
        if entity == "workflow" and "user_state" in schemas:
            table = "(SELECT id,title,updated_at FROM user_state.workflow_candidates WHERE id IN " \
                    "(SELECT candidate_id FROM user_state.workflow_review_ownership) UNION ALL " \
                    "SELECT id,title,updated_at FROM main.workflow_candidates WHERE id NOT IN " \
                    "(SELECT candidate_id FROM user_state.workflow_review_ownership))"
        sql = (
            f"SELECT {distinct}{query.value}, {query.label} "
            f"FROM {table} "
            f"WHERE {query.value} LIKE ? ESCAPE '\\' "
            f"ORDER BY {query.order_by} LIMIT ?"
        )
        rows = conn.execute(sql, (prefix, max(1, min(self.limit, 100)))).fetchall()
        return [
            {'value': str(value), 'help': str(label or '')}
            for value, label in rows
            if value is not None
        ]

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
