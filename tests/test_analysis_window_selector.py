from contextlib import closing
from datetime import UTC, datetime

from reflect.store.analysis_window import AnalysisWindow
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite

NOW = "2026-11-07T00:00:00+00:00"
OLD = "2026-01-01T00:00:00+00:00"


def _session(conn, session_id, started=OLD):
    conn.execute("INSERT INTO sessions(id,started_at,created_at,updated_at) VALUES (?,?,?,?)", (session_id, started, NOW, NOW))


def _step(conn, step_id, session_id, started, ended=None, attrs="{}"):
    sequence = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM steps WHERE session_id=?", (session_id,)).fetchone()[0]
    conn.execute(
        """INSERT INTO steps(id,session_id,seq,type,started_at,ended_at,raw_attrs_json,created_at,updated_at)
           VALUES (?,?,?,'observed',?,?,?,?,?)""",
        (step_id, session_id, sequence, started, ended, attrs, NOW, NOW),
    )


def test_selector_uses_observed_starts_and_ends_with_normalized_half_open_bounds(tmp_path):
    with closing(connect_sqlite(tmp_path / "selector.db")) as conn:
        migrate(conn)
        cases = {
            "lower_z": ("2026-10-31T00:00:00Z", None, "{}"),
            "lower_offset": ("2026-10-31T02:00:00+02:00", None, "{}"),
            "before_lower": ("2026-10-30T23:59:59.999Z", None, "{}"),
            "upper": ("2026-11-07T02:00:00+02:00", None, "{}"),
            "future": ("2099-01-01T00:00:00Z", None, "{}"),
            "unknown": ("unknown", None, "{}"),
            "relative_now": ("now", "now", "{}"),
            "relative_localtime": ("localtime", "localtime", "{}"),
            "notatime": ("notatime", "notatime", "{}"),
            "closed_recently": (OLD, "2026-11-06T00:00:00Z", "{}"),
            "inferred": ("2026-11-06T00:00:00Z", NOW, '{"reflect.time.provenance":"inferred"}'),
            "mixed": ("2026-11-06T00:00:00Z", None, "{}"),
        }
        for name, (started, ended, attrs) in cases.items():
            _session(conn, name)
            _step(conn, name, name, started, ended, attrs)
        _step(conn, "mixed_future", "mixed", "2099-01-01T00:00:00Z")
        _step(conn, "mixed_unknown", "mixed", "unknown")
        _session(conn, "session_start", "2026-11-06T00:00:00Z")
        for invalid in ("now", "localtime", "notatime"):
            _session(conn, "session_" + invalid, invalid)
        window = AnalysisWindow.capture(7, now=datetime.fromisoformat(NOW))
        assert window.session_ids(conn) == {"lower_z", "lower_offset", "closed_recently", "mixed", "session_start"}


def test_unchanged_window_lookup_does_not_scan_retained_cold_steps(tmp_path):
    with closing(connect_sqlite(tmp_path / "selector.db")) as conn:
        migrate(conn)
        _session(conn, "old")
        _session(conn, "hot")
        _step(conn, "hot", "hot", "2026-11-06T00:00:00Z")
        window = AnalysisWindow.capture(7, now=datetime(2026, 11, 7, tzinfo=UTC))

        def lookup_work():
            instructions = 0

            def count():
                nonlocal instructions
                instructions += 1

            conn.set_progress_handler(count, 1)
            try:
                assert window.session_ids(conn) == {"hot"}
            finally:
                conn.set_progress_handler(None, 0)
            return instructions

        baseline = lookup_work()
        conn.executemany(
            """INSERT INTO steps(id,session_id,seq,type,started_at,ended_at,raw_attrs_json,created_at,updated_at)
               VALUES (?,'old',?,'observed',?,?,'{}',?,?)""",
            ((f"cold-{i}", i, OLD, OLD, NOW, NOW) for i in range(50_000)),
        )
        assert lookup_work() <= baseline + 100


def test_historical_corrections_include_complete_current_and_previous_repo_cohorts(tmp_path):
    from reflect.store.analysis_window import historical_cohort_session_ids

    with closing(connect_sqlite(tmp_path / "cohorts.db")) as conn:
        migrate(conn)
        for repo in ("before", "after", "unrelated"):
            conn.execute("INSERT INTO repos(id,full_name,created_at,updated_at) VALUES (?,?,?,?)", (repo, repo, NOW, NOW))
        for session_id, repo in (("corrected", "after"), ("old-before", "before"),
                                 ("old-after", "after"), ("hot", "after"),
                                 ("unrelated", "unrelated"), ("local-a", None), ("local-b", None)):
            _session(conn, session_id)
            conn.execute("UPDATE sessions SET repo_id=? WHERE id=?", (repo, session_id))
        conn.execute(
            """INSERT INTO execution_units(id,session_id,repo_id,source,started_at,created_at,updated_at)
               VALUES ('previous-owner','corrected','before','inferred',?,?,?)""", (OLD, NOW, NOW),
        )
        assert historical_cohort_session_ids(
            conn, corrected_session_ids={"corrected"}, hot_session_ids={"hot"},
        ) == {"corrected", "old-before", "old-after", "hot"}
        assert historical_cohort_session_ids(
            conn, corrected_session_ids={"local-a"}, hot_session_ids={"hot"},
        ) == {"local-a", "local-b"}
        assert historical_cohort_session_ids(
            conn, corrected_session_ids={"hot"}, hot_session_ids={"hot"},
        ) == set()
        assert historical_cohort_session_ids(
            conn, corrected_session_ids={"vanished"}, hot_session_ids={"hot"},
        ) == {row[0] for row in conn.execute("SELECT id FROM sessions")}
