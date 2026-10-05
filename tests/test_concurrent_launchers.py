"""Several terminals start at once (opening a workspace), so launcher processes share one data folder."""
import os
import threading

import pytest

from vibe_launcher import runtime
from vibe_launcher.store import Store


def test_private_path_check_tolerates_files_that_come_and_go(tmp_path):
    target = tmp_path / "state.sqlite3-wal"
    stop = threading.Event()
    errors = []

    def churn():
        while not stop.is_set():
            try:
                target.write_text("x", encoding="utf-8")
                target.unlink()
            except OSError:
                pass

    worker = threading.Thread(target=churn, daemon=True)
    worker.start()
    try:
        for _ in range(4000):
            try:
                runtime.assert_private_path(target)
            except Exception as error:  # a vanished file must never surface as an error
                errors.append(repr(error))
                break
    finally:
        stop.set()
        worker.join(2)
    assert errors == []


def test_the_safety_checks_still_reject_what_they_should(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    runtime.assert_private_path(real)
    runtime.assert_private_path(tmp_path / "missing.txt")
    link = tmp_path / "hard.txt"
    try:
        os.link(real, link)
    except OSError:
        pytest.skip("hard links unavailable")
    with pytest.raises(ValueError, match="hard links"):
        runtime.assert_private_path(real)


def test_many_launchers_can_use_one_store_at_once(tmp_path):
    path = tmp_path / "shared.sqlite3"
    Store(path).save("accounts", {"id": "a", "name": "A", "tool": "codex", "auth": "subscription"})
    errors = []

    def launcher(n):
        try:
            store = Store(path)
            for i in range(25):
                store.list("accounts")
                store.save("sessions", {"id": f"s{n}x{i}", "account_id": "a", "environment_id": "local", "name": "n",
                                        "path": "p", "status": "running"})
        except Exception as error:
            errors.append(repr(error))

    threads = [threading.Thread(target=launcher, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert len(Store(path).list("sessions")) == 6 * 25
