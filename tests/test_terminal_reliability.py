"""Failure paths must preserve the worker and keep the GUI responsive."""
import json
import threading
from types import SimpleNamespace

import pytest

from vibe_launcher import conpty, runtime
from vibe_launcher.terminal import TerminalWidget, _OutputBuffer


def test_input_queue_is_bounded_while_pipe_is_blocked():
    entered, release = threading.Event(), threading.Event()
    received, errors = [], []

    def write(data):
        entered.set()
        assert release.wait(3)
        count = min(2, len(data)) if len(data) <= 6 else len(data)
        received.append(data[:count])
        return count

    writer = conpty._InputWriter(write, errors.append)
    try:
        assert writer.put(b"abcdef")
        assert entered.wait(2)
        assert writer.put(b"x" * (conpty.INPUT_LIMIT - 6))
        assert not writer.put(b"overflow")
        release.set()
        with writer._condition:
            assert writer._condition.wait_for(lambda: writer._size == 0, timeout=3)
        assert b"".join(received).startswith(b"abcdef")
        assert not errors
    finally:
        release.set()
        writer.close()
        writer.thread.join(3)
    assert not writer.thread.is_alive()


@pytest.mark.parametrize("failure", [0, OSError("broken input")])
def test_failed_or_zero_length_input_write_is_reported(failure):
    reported = threading.Event()
    errors = []

    def write(data):
        if isinstance(failure, OSError):
            raise failure
        return failure

    writer = conpty._InputWriter(write, lambda error: (errors.append(error), reported.set()))
    assert writer.put(b"input")
    assert reported.wait(2)
    writer.thread.join(2)
    assert not writer.put(b"retry") and len(errors) == 1


def test_writer_cancellation_uses_a_retained_handle_not_a_reused_thread_id():
    calls = []
    pty = object.__new__(conpty.ConPty)
    pty._writer = SimpleNamespace(close=lambda: None)
    pty._writer_handle = 42
    pty._lock = threading.Lock()
    pty._k = SimpleNamespace(CancelSynchronousIo=lambda handle: calls.append(handle))
    pty._stop_writer()
    pty._stop_writer()
    pty._writer_handle = None  # teardown has released it; later disposal must not cancel any other thread
    pty._stop_writer()
    assert calls == [42, 42]


def test_display_failure_unblocks_reader_without_stopping_program(qtbot, monkeypatch, tmp_path):
    terminal = TerminalWidget()
    qtbot.addWidget(terminal)
    terminal.resize(800, 400)
    terminal.show()
    terminal._display_timer.stop()
    terminal._buffer = buffer = _OutputBuffer(limit=8)
    started = threading.Event()
    reader = threading.Thread(target=lambda: buffer.write(b"x" * 100, started.set), daemon=True)
    reader.start()
    assert started.wait(2)
    process = SimpleNamespace(close=lambda: pytest.fail("display failure must not stop the worker"))
    terminal._pty = process
    try:
        terminal._fail_display()
        reader.join(2)
        assert not reader.is_alive() and buffer.outstanding == 0
        buffer.write(b"later output", lambda: pytest.fail("failed renderer must not receive output"))
        assert terminal._display_failed and not terminal._warning.isHidden()
        assert terminal.exit_code is None
        assert terminal.grab().save(str(tmp_path / "display-failure.png"))
    finally:
        terminal._pty = None


def test_exit_then_display_failure_emits_completion_once(qtbot):
    terminal = TerminalWidget()
    qtbot.addWidget(terminal)
    terminal._display_timer.stop()
    terminal._buffer.write(b"final", lambda: None)
    terminal._buffer.take()
    terminal._awaiting_write = 1
    drained = []
    terminal.output_drained.connect(lambda: drained.append(True))
    terminal._on_exit(0)
    assert not drained
    terminal._fail_display()
    terminal._fail_display()
    assert drained == [True] and terminal.is_output_drained


def test_replaced_page_cannot_fail_or_type_into_live_program(qtbot):
    terminal = TerminalWidget()
    qtbot.addWidget(terminal)
    terminal._display_timer.stop()
    old_page, old_bridge = terminal._page, terminal._bridge
    writes = []
    terminal._pty = SimpleNamespace(write=lambda data: writes.append(data), resize=lambda *a: None)
    try:
        terminal._fail_display()
        terminal._restore_display()
        old_page.loadFinished.emit(False)
        old_bridge.ready_sig.emit(120, 30)
        old_bridge.input_text.emit("must not reach worker")
        terminal.send_bytes(b"not ready")
        assert not terminal._display_failed and not terminal._page_ready and not writes
        terminal._bridge.ready_sig.emit(120, 30)
        terminal._bridge.input_text.emit("new page")
        assert writes == [b"new page"]
    finally:
        terminal._pty = None


def test_detach_targets_one_attachment_and_verifies_worker(tmp_path, monkeypatch):
    request = {"action": "detach", "session_id": "s", "environment_id": "host", "attachment_nonce": "abc12345"}
    runtime.write_json(runtime.session_path(tmp_path, "s"), {"session_id": "s", "environment_id": "host"})
    calls = []
    monkeypatch.setattr(runtime, "tmux_windows", lambda: {"s": "@1"})
    monkeypatch.setattr(runtime, "tmux_session_exists", lambda sid: sid == "s")

    def tmux(*args, **kwargs):
        calls.append(args)
        output = b"@1\n" if args[0] == "list-windows" else b"other-attachment\n"
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(runtime, "tmux", tmux)
    reports = []
    monkeypatch.setattr(runtime, "operation_result", lambda _, root, result: reports.append(result))
    assert runtime.perform(request, tmp_path) == 0
    assert reports == [{"status": "detached"}]
    assert ("detach-client", "-s", "=vibe-s-abc12345") in calls
    assert not any(call[0] in {"kill-window", "kill-session"} for call in calls)


def test_detach_refuses_an_attachment_for_another_window(tmp_path, monkeypatch):
    runtime.write_json(runtime.session_path(tmp_path, "s"), {"session_id": "s", "environment_id": "host"})
    monkeypatch.setattr(runtime, "tmux_windows", lambda: {"s": "@1"})
    monkeypatch.setattr(runtime, "tmux", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"@2\n"))
    reports = []
    monkeypatch.setattr(runtime, "operation_result", lambda _, root, result: reports.append(result))
    assert runtime.perform({"action": "detach", "session_id": "s", "environment_id": "host",
                            "attachment_nonce": "other"}, tmp_path) == 1
    assert reports == [{"status": "unknown"}]
