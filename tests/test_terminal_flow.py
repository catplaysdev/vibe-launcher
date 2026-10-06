"""Bounded, lossless transfer, including a deliberately stalled browser consumer."""
import threading
import time
from types import SimpleNamespace

from vibe_launcher.terminal import _OutputBuffer, TerminalWidget


def test_reader_waits_for_browser_ack_and_preserves_split_utf8_and_controls():
    buffer = _OutputBuffer(limit=257)
    payload = ("中文\x1b[31m測試\x1b[0m\r\n" * 1000).encode()
    notified = threading.Event()
    finished = threading.Event()

    def produce():
        for offset in range(0, len(payload), 13):
            buffer.write(payload[offset:offset + 13], notified.set)
        finished.set()

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    received = bytearray()
    try:
        assert notified.wait(2)
        deadline = time.monotonic() + 2
        while buffer.outstanding < buffer.limit and time.monotonic() < deadline:
            time.sleep(0.001)
        assert buffer.outstanding == buffer.limit
        chunk = buffer.take()
        assert chunk and not buffer.take()
        assert not finished.wait(0.05), "take must not free the browser's in-flight budget"
        received.extend(chunk)
        buffer.acknowledge()
        deadline = time.monotonic() + 5
        while not finished.is_set() or buffer.outstanding:
            assert time.monotonic() < deadline
            chunk = buffer.take()
            if chunk:
                received.extend(chunk)
                buffer.acknowledge()
            else:
                time.sleep(0.001)
        assert received == payload
        assert buffer.peak == buffer.limit
    finally:
        buffer.close()
        producer.join(2)
    assert not producer.is_alive()


def test_dispose_unblocks_reader_even_when_browser_never_acknowledges():
    buffer = _OutputBuffer(limit=8)
    notified = threading.Event()
    producer = threading.Thread(target=lambda: buffer.write(b"x" * 32, notified.set), daemon=True)
    producer.start()
    assert notified.wait(2)
    assert buffer.take() == b"x" * 8
    buffer.close()
    producer.join(2)
    assert not producer.is_alive()
    assert buffer.outstanding == 8  # closing does not silently discard already accepted bytes


def test_small_writes_do_not_flood_qt_notifications():
    buffer = _OutputBuffer(limit=1000)
    signals = []
    for _ in range(1000):
        buffer.write(b"a", lambda: signals.append(True))
    assert len(signals) == 1
    assert buffer.take() == b"a" * 1000
    buffer.acknowledge()
    buffer.write(b"b", lambda: signals.append(True))
    assert len(signals) == 2


def test_stale_or_duplicate_ack_does_not_release_next_batch():
    buffer = _OutputBuffer(limit=8)
    buffer.write(b"12345678", lambda: None)
    buffer.take()
    scheduled, presented = [], []
    widget = SimpleNamespace(_disposed=False, _awaiting_write=2, _buffer=buffer,
                             exit_code=None, _present_timer=SimpleNamespace(start=lambda: presented.append(True)),
                             _schedule_flush=lambda: scheduled.append(True))
    TerminalWidget._on_written(widget, 1)
    assert buffer.outstanding == 8
    TerminalWidget._on_written(widget, 2)
    assert buffer.outstanding == 0 and scheduled == [True] and presented == [True]
    TerminalWidget._on_written(widget, 2)
    assert scheduled == [True] and presented == [True]


def test_exit_output_is_drained_only_after_browser_ack():
    buffer = _OutputBuffer(limit=8)
    buffer.write(b"last", lambda: None)
    buffer.take()
    drained = []
    widget = SimpleNamespace(_disposed=False, _awaiting_write=1, _buffer=buffer, exit_code=0,
                             _schedule_flush=lambda: None, _present_timer=SimpleNamespace(start=lambda: None),
                             output_drained=SimpleNamespace(emit=lambda: drained.append(True)))
    assert not TerminalWidget.is_output_drained.fget(widget)
    TerminalWidget._on_written(widget, 1)
    assert TerminalWidget.is_output_drained.fget(widget) and drained == [True]
    TerminalWidget._on_written(widget, 1)
    assert drained == [True]
