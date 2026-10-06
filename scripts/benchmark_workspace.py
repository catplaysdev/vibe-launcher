"""Synthetic local ConPTY/WebEngine benchmark. Never opens account profiles or repositories."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def process_metrics() -> tuple[float, int]:
    """CPU seconds and resident bytes for this process and its children (stdlib, Windows only)."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("name", wintypes.WCHAR * 260)]

    class Memory(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("peak", "working", "quota_peak_paged", "quota_paged",
                                              "quota_peak_nonpaged", "quota_nonpaged", "page", "peak_page")]

    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    parents = {}
    entry = Entry()
    entry.size = ctypes.sizeof(entry)
    try:
        valid = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while valid:
            parents[entry.pid] = entry.parent
            valid = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    pids = {os.getpid()}
    while True:
        children = {pid for pid, parent in parents.items() if parent in pids}
        if children <= pids:
            break
        pids |= children
    cpu, ram = 0.0, 0
    for pid in pids:
        handle = kernel.OpenProcess(0x410, False, pid)
        if not handle:
            continue
        try:
            created, exited, user, system = (wintypes.FILETIME() for _ in range(4))
            if kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(system), ctypes.byref(user)):
                cpu += sum((t.dwHighDateTime << 32) | t.dwLowDateTime for t in (user, system)) / 1e7
            memory = Memory()
            memory.size = ctypes.sizeof(memory)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.size):
                ram += memory.working
        finally:
            kernel.CloseHandle(handle)
    return cpu, ram


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terminals", type=int, choices=[0, 4, 8], default=8)
    parser.add_argument("--duration", type=float, default=1800, help="Continuous-output seconds")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offscreen", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--software", action="store_true", help="Disable Chromium GPU for headless measurements")
    args = parser.parse_args()
    if args.duration <= 0 or os.name != "nt":
        parser.error("Requires Windows and a positive duration")
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if args.software:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") + " --disable-gpu"
    if args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG)
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QTabWidget
    from vibe_launcher.terminal import TerminalWidget, OUTPUT_LIMIT, available
    from vibe_launcher import theme

    args.output.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    if not available():
        args.output.write_text(json.dumps({"status": "unavailable", "reason": "ConPTY/WebEngine"}), encoding="utf-8")
        return 2
    theme.apply_theme()
    tabs = QTabWidget()
    tabs.resize(1200, 800)
    terminals = []
    start = time.monotonic()
    report = {"status": "running", "terminals": args.terminals, "duration_seconds": args.duration,
              "output_limit_bytes_per_terminal": OUTPUT_LIMIT, "offscreen": args.offscreen,
              "software_rendering": args.software,
              "samples": [], "switch_ms": [], "synthetic_only": True}
    for index in range(args.terminals):
        terminal = TerminalWidget()
        terminals.append(terminal)
        tabs.addTab(terminal, f"Synthetic {index + 1}")
        # Constant 16 KiB/10ms producer; pipe backpressure throttles it rather than losing output.
        program = ("import os,time\nend=time.monotonic()+" + repr(args.duration) +
                   "\npayload=('中文壓測 ABC 0123456789\\r\\n'*512).encode('utf-8')\n"
                   "while time.monotonic()<end:\n os.write(1,payload);time.sleep(.01)\n")
        terminal.start([sys.executable, "-I", "-c", program], cwd=str(args.output.parent))
    tabs.show()
    # Exercise a minimal window even for the zero-terminal control.
    previous = [start, process_metrics()[0]]
    stopping = [None]
    finished = [False]

    def save():
        temporary = args.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(args.output)

    def finish(status):
        if finished[0]:
            return
        finished[0] = True
        timer.stop()
        report["status"] = status
        report["elapsed_seconds"] = time.monotonic() - start
        report["peak_backlog_bytes"] = [terminal._buffer.peak for terminal in terminals]
        report["final_backlog_bytes"] = [terminal._buffer.outstanding for terminal in terminals]
        report["exit_codes"] = [terminal.exit_code for terminal in terminals]
        report["page_ready"] = [terminal._page_ready for terminal in terminals]
        report["process_ids"] = [terminal.pid for terminal in terminals]
        report["switch_median_ms"] = statistics.median(report["switch_ms"]) if report["switch_ms"] else None
        tabs.grab().save(str(args.output.with_suffix(".png")))
        save()
        for terminal in terminals:
            terminal.dispose()
        QTimer.singleShot(500, app.quit)

    def tick():
        now = time.monotonic()
        cpu, ram = process_metrics()
        dt = now - previous[0]
        report["samples"].append({"elapsed": now - start, "cpu_percent_one_core": max(0, (cpu - previous[1]) / dt * 100),
                                   "ram_bytes_process_tree": ram,
                                   "backlog_bytes": [terminal._buffer.outstanding for terminal in terminals]})
        previous[:] = [now, cpu]
        if terminals and now - start > 30 and not all(t._page_ready and t.pid for t in terminals):
            report["reason"] = "Not all WebEngine pages/ConPTY processes started within 30 seconds"
            finish("startup_unavailable")
            return
        if terminals:
            switched = time.perf_counter()
            tabs.setCurrentIndex((tabs.currentIndex() + 1) % len(terminals))
            terminals[tabs.currentIndex()]._page.runJavaScript("1", lambda _: report["switch_ms"].append(
                (time.perf_counter() - switched) * 1000))
        if now - start >= args.duration:
            if stopping[0] is None:
                stopping[0] = now
                report["output_stopped_at_seconds"] = now - start
            if all(t.exit_code is not None and t._buffer.outstanding == 0 for t in terminals):
                report["drain_seconds"] = now - stopping[0]
                finish("passed")
                return
            if now - stopping[0] > 120:
                finish("drain_timeout")
                return
        save()
        print(f"{args.terminals} terminals: {now-start:.0f}s, RAM {ram/1048576:.1f} MiB, backlog "
              f"{sum(t._buffer.outstanding for t in terminals)}", flush=True)

    timer = QTimer(tabs)
    timer.setInterval(5000)
    timer.timeout.connect(tick)
    timer.start()
    app.aboutToQuit.connect(lambda: finish("interrupted") if not finished[0] else None)
    save()
    app.exec()
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
