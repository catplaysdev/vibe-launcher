"""Minimal Windows ConPTY wrapper (ctypes, no third-party dependency).

A pseudo console hands a child process a real console while we exchange *raw bytes* with it. Keeping bytes raw
(instead of decoding here) lets the terminal emulator handle UTF-8, including characters split across reads.

Lifecycle notes learned the hard way:
* the output pipe does not reach EOF when the child exits; ``ClosePseudoConsole`` has to be called first, so a waiter
  thread watches the process handle and tears the console down after a short drain delay;
* the whole process tree runs in a Job Object, so closing a terminal (or the app dying) also ends ``ssh.exe`` and friends.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from collections import deque
from ctypes import wintypes
from typing import Callable

_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_PSEUDOCONSOLE_RESIZE_QUIRK = 0x2
_STILL_ACTIVE = 259
_STARTF_USESTDHANDLES = 0x00000100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_INFINITE = 0xFFFFFFFF
INPUT_LIMIT = 1024 * 1024


class _InputWriter:
    """Bounded, ordered input; a blocked pipe must never block the GUI."""

    def __init__(self, write, on_error):
        self._write, self._on_error = write, on_error
        self._condition = threading.Condition()
        self._pending = deque()
        self._size = 0
        self._closed = False
        self.thread = threading.Thread(target=self._run, name="conpty-writer", daemon=True)
        self.thread.start()

    def put(self, data: bytes) -> bool:
        with self._condition:
            if self._closed or self._size + len(data) > INPUT_LIMIT:
                return False
            self._pending.append(bytes(data))
            self._size += len(data)
            self._condition.notify()
            return True

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._condition.notify_all()

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._pending)
                    if self._closed:
                        return
                    data = self._pending.popleft()
                offset = 0
                while offset < len(data):
                    with self._condition:
                        if self._closed:
                            return
                    chunk = data[offset:offset + 4096]
                    count = self._write(chunk)
                    if not 0 < count <= len(chunk):
                        raise OSError("Terminal input made no progress")
                    offset += count
                with self._condition:
                    self._size -= len(data)
                    self._condition.notify_all()
        except OSError as error:
            with self._condition:
                report = not self._closed
            self.close()
            if report:
                self._on_error(error)


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def available() -> bool:
    """ConPTY exists on Windows 10 1809+."""
    if os.name != "nt":
        return False
    try:
        return hasattr(ctypes.WinDLL("kernel32"), "CreatePseudoConsole")
    except OSError:
        return False


def _api():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    H, P = wintypes.HANDLE, ctypes.POINTER
    spec = {
        "CreatePipe": ([P(H), P(H), ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "CreatePseudoConsole": ([COORD, H, H, wintypes.DWORD, P(H)], ctypes.c_long),
        "ResizePseudoConsole": ([H, COORD], ctypes.c_long),
        "ClosePseudoConsole": ([H], None),
        "InitializeProcThreadAttributeList": ([ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, P(ctypes.c_size_t)], wintypes.BOOL),
        "UpdateProcThreadAttribute": ([ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
        "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
        "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.LPCWSTR, P(STARTUPINFOEXW), P(PROCESS_INFORMATION)], wintypes.BOOL),
        "ReadFile": ([H, ctypes.c_void_p, wintypes.DWORD, P(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        "WriteFile": ([H, ctypes.c_void_p, wintypes.DWORD, P(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        "CloseHandle": ([H], wintypes.BOOL),
        "GetExitCodeProcess": ([H, P(wintypes.DWORD)], wintypes.BOOL),
        "WaitForSingleObject": ([H, wintypes.DWORD], wintypes.DWORD),
        "CancelIoEx": ([H, ctypes.c_void_p], wintypes.BOOL),
        "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], H),
        "CancelSynchronousIo": ([H], wintypes.BOOL),
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], H),
        "SetInformationJobObject": ([H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([H, H], wintypes.BOOL),
        "TerminateJobObject": ([H, wintypes.UINT], wintypes.BOOL),
        "TerminateProcess": ([H, wintypes.UINT], wintypes.BOOL),
    }
    for name, (argtypes, restype) in spec.items():
        function = getattr(k, name)
        function.argtypes, function.restype = argtypes, restype
    return k


def _environment_block(env: dict[str, str]) -> ctypes.Array:
    text = "".join(f"{key}={value}\0" for key, value in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"
    return ctypes.create_unicode_buffer(text, len(text))


class ConPty:
    """Run ``command`` (an argv list) inside a pseudo console.

    ``on_output`` receives raw bytes on the reader thread; ``on_exit`` receives the exit code once, after all output.
    """

    def __init__(self, command: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None,
                 cols: int = 120, rows: int = 30, on_output: Callable[[bytes], None] | None = None,
                 on_exit: Callable[[int], None] | None = None,
                 on_input_error: Callable[[OSError], None] | None = None):
        if not available():
            raise OSError("ConPTY is not available on this system")
        self._k = k = _api()
        self.on_output, self.on_exit = on_output, on_exit
        self.on_input_error = on_input_error
        self._lock = threading.Lock()
        self._console_closed = False
        self._closing = False
        self.exit_code: int | None = None
        self._h_process = None
        self._job = None
        self.pid = 0
        in_read, in_write, out_read, out_write = (wintypes.HANDLE() for _ in range(4))
        if not k.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0) or \
                not k.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        self._pc = wintypes.HANDLE()
        # RESIZE_QUIRK stops the console from repainting the whole buffer on every resize (as Windows Terminal does);
        # systems that do not know the flag reject it, so fall back to none.
        for self.flags in (_PSEUDOCONSOLE_RESIZE_QUIRK, 0):
            hr = k.CreatePseudoConsole(COORD(cols, rows), in_read, out_write, self.flags, ctypes.byref(self._pc))
            if hr == 0:
                break
        # The pseudo console owns its ends now; keeping ours open would stop EOF from ever arriving.
        k.CloseHandle(in_read)
        k.CloseHandle(out_write)
        if hr != 0:
            k.CloseHandle(in_write)
            k.CloseHandle(out_read)
            raise OSError(f"CreatePseudoConsole failed (HRESULT {hr & 0xFFFFFFFF:#x})")
        self._in_write, self._out_read = in_write, out_read
        self._attrs = None
        try:
            self._spawn(command, cwd, env)
        except Exception:
            self._release()
            raise
        self._writer = _InputWriter(self._write_bytes, lambda error: self.on_input_error and self.on_input_error(error))
        # Retain the actual thread object handle: a native thread ID can be reused after exit.
        self._writer_handle = k.OpenThread(0x0001, False, self._writer.thread.native_id)
        self._reader = threading.Thread(target=self._read_loop, name="conpty-reader", daemon=True)
        self._reader.start()
        threading.Thread(target=self._wait_loop, name="conpty-waiter", daemon=True).start()

    def _spawn(self, command: list[str], cwd: str | None, env: dict[str, str] | None) -> None:
        k = self._k
        size = ctypes.c_size_t()
        k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        self._attrs = ctypes.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(self._attrs, 1, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not k.UpdateProcThreadAttribute(self._attrs, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, self._pc,
                                           ctypes.sizeof(wintypes.HANDLE), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        # Explicit NULL standard handles: without this the child can inherit *our* redirected stdout/stderr
        # (e.g. when the app itself was started from a pipe) instead of using the pseudo console.
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.lpAttributeList = ctypes.cast(self._attrs, ctypes.c_void_p)
        info = PROCESS_INFORMATION()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        block = _environment_block(env) if env is not None else None
        ok = k.CreateProcessW(None, command_line, None, None, False,
                              _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
                              ctypes.cast(block, ctypes.c_void_p) if block is not None else None, cwd,
                              ctypes.byref(startup), ctypes.byref(info))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        k.CloseHandle(info.hThread)
        self._h_process = info.hProcess
        self.pid = int(info.dwProcessId)
        self._join_job()

    def _join_job(self) -> None:
        """Best effort: put the child (and everything it starts) in a kill-on-close job."""
        k = self._k
        job = k.CreateJobObjectW(None, None)
        if not job:
            return
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if k.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)) \
                and k.AssignProcessToJobObject(job, self._h_process):
            self._job = job
        else:
            k.CloseHandle(job)

    # -- I/O ----------------------------------------------------------------------------------
    def _read_loop(self) -> None:
        k = self._k
        buffer = ctypes.create_string_buffer(65536)
        count = wintypes.DWORD()
        while True:
            if not k.ReadFile(self._out_read, buffer, len(buffer), ctypes.byref(count), None) or count.value == 0:
                break
            if self.on_output is not None:
                self.on_output(buffer.raw[:count.value])

    def _wait_loop(self) -> None:
        """Wait for the child, let its last output drain, close the pseudo console (which gives the reader EOF)."""
        k = self._k
        k.WaitForSingleObject(self._h_process, _INFINITE)
        code = wintypes.DWORD()
        if k.GetExitCodeProcess(self._h_process, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
            self.exit_code = int(code.value)
        else:
            self.exit_code = -1
        time.sleep(0.15)
        self._stop_writer()
        while self._writer.thread.is_alive():
            self._writer.thread.join(0.05)
            self._stop_writer()  # cancellation may have raced the beginning of a synchronous WriteFile
        self._close_console()
        # A busy/hidden renderer may apply backpressure longer than three seconds. Never close the pipe before
        # its final output has drained. TerminalWidget releases its reader on explicit stop/disposal.
        self._reader.join()
        self._writer.thread.join()
        self._release()
        if self.on_exit is not None:
            self.on_exit(self.exit_code)

    def _close_console(self) -> None:
        with self._lock:
            if self._console_closed:
                return
            self._console_closed = True
        self._k.ClosePseudoConsole(self._pc)

    def write(self, data: bytes) -> bool:
        if self._closing or self._console_closed or not self._writer_handle or not data:
            return False
        return self._writer.put(data)

    def _write_bytes(self, data: bytes) -> int:
        written = wintypes.DWORD()
        view = ctypes.create_string_buffer(data, len(data))
        if not self._k.WriteFile(self._in_write, view, len(data), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return written.value

    def _stop_writer(self) -> None:
        self._writer.close()
        with self._lock:
            if self._writer_handle:
                self._k.CancelSynchronousIo(self._writer_handle)

    def resize(self, cols: int, rows: int) -> None:
        if not (self._closing or self._console_closed) and cols > 0 and rows > 0:
            self._k.ResizePseudoConsole(self._pc, COORD(int(cols), int(rows)))

    def is_alive(self) -> bool:
        if self._h_process is None or self.exit_code is not None:
            return False
        code = wintypes.DWORD()
        return bool(self._k.GetExitCodeProcess(self._h_process, ctypes.byref(code))) and code.value == _STILL_ACTIVE

    # -- teardown -----------------------------------------------------------------------------
    def terminate(self) -> None:
        """Kill the child and everything it started."""
        if self._job:
            self._k.TerminateJobObject(self._job, 1)
        elif self._h_process is not None and self.is_alive():
            self._k.TerminateProcess(self._h_process, 1)

    def close(self) -> None:
        """Stop the child tree. The waiter thread finishes the teardown once the process is gone."""
        self._closing = True
        self._stop_writer()
        self.terminate()

    def _release(self) -> None:
        k = self._k
        for name in ("_in_write", "_out_read", "_h_process", "_job", "_writer_handle"):
            if name == "_writer_handle":
                with self._lock:
                    handle, self._writer_handle = getattr(self, name, None), None
                    if handle:
                        k.CloseHandle(handle)
                continue
            handle = getattr(self, name, None)
            if handle:
                if name == "_out_read":
                    k.CancelIoEx(handle, None)
                k.CloseHandle(handle)
                setattr(self, name, None)
        if self._attrs is not None:
            k.DeleteProcThreadAttributeList(self._attrs)
            self._attrs = None
        if not self._console_closed:
            self._close_console()
