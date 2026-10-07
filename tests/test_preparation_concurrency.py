import os
import selectors
import subprocess
import sys

import pytest

# Run the real shared report/usage pipeline entry points in separate processes.
# Stop after entering the store: no native telemetry or pricing network is needed.
PREPARE = """
import sys
from pathlib import Path
from reflect.preparation import PreparationStage
from reflect.preparation_pipeline import prepare_sql_report_db, prepare_usage_db

def progress(update):
    if update.stage == PreparationStage.WAITING_FOR_STORE:
        print('waiting', flush=True)
    if update.stage == PreparationStage.OPENING_STORE:
        print('opened', flush=True)
        sys.stdin.readline()
        raise RuntimeError('controlled preparation failure')

prepare = prepare_sql_report_db if sys.argv[2] == 'report' else prepare_usage_db
try:
    prepare(Path(sys.argv[1]), otlp_traces=None, include_native_sessions=False, progress=progress)
except RuntimeError:
    pass
"""


def _readline(process, timeout=5):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout), "preparation process did not report progress"
    return process.stdout.readline().strip()


@pytest.mark.parametrize("first_mode,second_mode", [("report", "usage"), ("usage", "report")])
def test_refresh_pipelines_share_process_lock_and_release_after_failure(
    tmp_path, first_mode, second_mode,
):
    db = tmp_path / "reflect.db"
    processes = []

    def start(path, mode):
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", PREPARE, str(path), mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=os.environ.copy(), bufsize=0,
        )
        processes.append(process)
        return process

    try:
        first = start(db, first_mode)
        assert _readline(first) == b"waiting"
        assert _readline(first) == b"opened"
        # Resolve aliases to the same lock key.
        alias = tmp_path / "alias.db"
        alias.symlink_to(db)
        second = start(alias, second_mode)
        assert _readline(second) == b"waiting"
        with selectors.DefaultSelector() as selector:
            selector.register(second.stdout, selectors.EVENT_READ)
            assert not selector.select(0.2)
        # A different store is independent of the blocked refresh.
        other = start(tmp_path / "other.db", second_mode)
        assert _readline(other) == b"waiting"
        assert _readline(other) == b"opened"
        other.communicate(b"continue\n", timeout=5)
        assert other.returncode == 0
        first.communicate(b"continue\n", timeout=5)
        assert first.returncode == 0
        assert _readline(second) == b"opened"
        second.communicate(b"continue\n", timeout=5)
        assert second.returncode == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
