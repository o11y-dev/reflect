import threading

from reflect.preparation import BackgroundPreparationWorker, PreparationState


def test_background_preparation_worker_completes_and_runs_callbacks():
    callback_results = []
    worker = BackgroundPreparationWorker(lambda: {"sessions": 3})
    worker.add_completion_callback(callback_results.append)

    assert worker.start() is True
    assert worker.wait(timeout=2) is True

    snapshot = worker.snapshot()
    assert snapshot.state is PreparationState.COMPLETE
    assert snapshot.generation == 1
    assert snapshot.result == {"sessions": 3}
    assert callback_results == [{"sessions": 3}]


def test_background_preparation_worker_rejects_duplicate_running_start():
    release = threading.Event()
    worker = BackgroundPreparationWorker(lambda: release.wait(timeout=2) or {})

    assert worker.start() is True
    assert worker.start() is False
    release.set()
    assert worker.wait(timeout=2) is True


def test_background_preparation_worker_exposes_failures():
    def fail():
        raise RuntimeError("preparation failed")

    worker = BackgroundPreparationWorker(fail)
    assert worker.start() is True
    assert worker.wait(timeout=2) is True

    snapshot = worker.snapshot()
    assert snapshot.state is PreparationState.FAILED
    assert snapshot.error == "preparation failed"
