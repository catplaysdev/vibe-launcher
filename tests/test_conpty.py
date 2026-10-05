"""The ConPTY layer against real processes (Windows only)."""
import os
import sys
import threading
import time

import pytest

from vibe_launcher import conpty

pytestmark = pytest.mark.skipif(not conpty.available(), reason="needs Windows ConPTY")


class Run:
    """Start a command in a ConPTY and collect its raw output and exit code."""

    def __init__(self, command, env=None, cwd=None):
        self.chunks, self.exits, self.pty = [], [], None
        self.pty = conpty.ConPty(command, cwd=cwd, env=env or dict(os.environ, PYTHONUTF8="1"), cols=100, rows=24,
                                 on_output=self._output, on_exit=self.exits.append)

    def _output(self, data):
        self.chunks.append(data)
        if b"\x1b[c" in data and self.pty:        # terminals answer this query; conhost waits for it
            self.pty.write(b"\x1b[?1;2c")

    def wait(self, seconds=15):
        end = time.time() + seconds
        while not self.exits and time.time() < end:
            time.sleep(0.05)
        return self

    def until(self, needle: bytes, seconds=10):
        end = time.time() + seconds
        while needle not in self.output and time.time() < end:
            time.sleep(0.05)
        return needle in self.output

    @property
    def output(self) -> bytes:
        return b"".join(self.chunks)


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_output_reaches_us_as_raw_bytes_and_the_exit_code_propagates():
    run = Run(py("import sys; sys.stdout.buffer.write('你好，世界 ok'.encode('utf-8')); sys.stdout.flush(); sys.exit(7)")).wait()
    assert run.exits == [7]
    assert "你好，世界 ok".encode("utf-8") in run.output, "UTF-8 must pass through untouched (we never decode it here)"


def test_the_child_really_has_a_terminal():
    run = Run(py("import os,sys; sys.stdout.write('T=%s,%s' % (os.isatty(0), os.isatty(1)))")).wait()
    assert b"T=True,True" in run.output


def test_the_child_does_not_inherit_our_redirected_stdout(capfd):
    Run(py("print('only-on-the-pty')")).wait()
    assert "only-on-the-pty" not in capfd.readouterr().out


def test_typing_reaches_an_interactive_program():
    run = Run(py("name = input('name> '); print('hello', name)"))
    assert run.until(b"name>")  # the trailing space may be sent as a cursor move
    run.pty.write("世界\r".encode("utf-8"))
    run.wait()
    assert "hello 世界".encode("utf-8") in run.output and run.exits == [0]


def test_resizing_reaches_the_child():
    code = ("import os,time,sys\n"
            "print('A', os.get_terminal_size().columns, flush=True)\n"
            "time.sleep(1.2)\n"
            "print('B', os.get_terminal_size().columns, flush=True)\n")
    run = Run(py(code))
    assert run.until(b"A 100")
    run.pty.resize(137, 40)
    run.wait()
    assert b"B 137" in run.output


def test_exit_is_reported_once_after_all_output():
    run = Run(py("import sys; print('last line'); sys.exit(3)")).wait()
    time.sleep(0.5)
    assert run.exits == [3] and b"last line" in run.output


def test_terminate_kills_the_whole_process_tree():
    parent = ("import subprocess,sys,time\n"
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              "print('child', child.pid, flush=True)\n"
              "time.sleep(60)\n")
    run = Run(py(parent))
    assert run.until(b"child ")
    text = run.output.decode("utf-8", "replace")
    child_pid = int(text.split("child ")[1].split()[0])
    run.pty.terminate()
    run.wait(10)
    assert run.exits, "the exit callback must fire after terminate()"
    time.sleep(0.5)
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x1000, False, child_pid)
    alive = False
    if handle:
        code = wintypes.DWORD()
        alive = bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        kernel.CloseHandle(handle)
    assert not alive, "the grandchild (think ssh.exe) must die with its terminal"


def test_unknown_program_raises_instead_of_hanging():
    with pytest.raises(OSError):
        conpty.ConPty(["definitely-not-a-real-program-xyz.exe"], env=dict(os.environ), cols=80, rows=24)


def test_writing_after_exit_is_harmless():
    run = Run(py("pass")).wait()
    run.pty.write(b"ignored")
    run.pty.resize(80, 24)
