"""Own only the local SSH forwards; remote Harness services survive shutdown."""
from __future__ import annotations

from .i18n import tr

import re
import socket
import subprocess
import time
from pathlib import Path

from . import runtime, ssh_security


def listener_owned(port: int, pid: int) -> bool:
    if not runtime.WINDOWS:
        return runtime.harness_listener_owned(port, pid) is True
    import ctypes
    from ctypes import wintypes
    class Row(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in ("state", "address", "port", "remote_address", "remote_port", "pid")]
    api = ctypes.WinDLL("iphlpapi.dll").GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                    wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    api.restype = wintypes.DWORD
    size = wintypes.DWORD()
    code = api(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
    if code not in (0, 122):
        raise RuntimeError(tr('harness.c5b07358c8fc'))
    for _ in range(3):
        buffer = ctypes.create_string_buffer(max(size.value, 4))
        code = api(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if code == 122:
            continue  # another listener changed the table size
        if code:
            raise RuntimeError(tr('harness.c5b07358c8fc'))
        count = wintypes.DWORD.from_buffer(buffer).value
        offset = ctypes.sizeof(wintypes.DWORD)
        if offset + count * ctypes.sizeof(Row) > len(buffer):
            raise RuntimeError(tr('harness.a48478d87ba4'))
        rows = [Row.from_buffer(buffer, offset + index * ctypes.sizeof(Row)) for index in range(count)]
        listeners = [row for row in rows if socket.ntohs(row.port & 0xffff) == port]
        return bool(listeners) and all(row.pid == pid and row.address == int.from_bytes(socket.inet_aton("127.0.0.1"), "little") for row in listeners)
    raise RuntimeError(tr('harness.8abad6d4107a'))


class TunnelManager:
    def __init__(self, root: Path):
        self.root = root
        self._processes: dict[str, tuple[subprocess.Popen, int, int, str]] = {}

    def status(self, service_key: str) -> str:
        item = self._processes.get(service_key)
        if not item:
            return "disconnected"
        if item[0].poll() is not None:
            return "disconnected"
        try:
            return "connected" if listener_owned(item[2], item[0].pid) else "disconnected"
        except (OSError, RuntimeError):
            return "unknown"

    def open(self, service_key: str, environment: dict, remote_port: int, local_port: int) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", service_key) or environment.get("kind") != "ssh":
            raise ValueError(tr('harness.5917eb5bbbaf'))
        if environment.get("ssh_auth_mode") == "password":
            raise ValueError(tr('harness.dd345369ded8'))
        runtime.harness_port(remote_port)
        runtime.harness_port(local_port)
        target = environment.get("ssh_target", "")
        if not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*", target) or target.startswith("-"):
            raise ValueError("Invalid SSH target")
        item = self._processes.get(service_key)
        if item and item[0].poll() is None:
            if item[1:] != (remote_port, local_port, environment["id"]):
                raise ValueError(tr('harness.ab9955c68d5a'))
            if self.status(service_key) == "connected":
                return f"http://127.0.0.1:{local_port}"
            self.close(service_key)
        # OpenSSH is the final authority for the race between this check and bind.
        with socket.socket() as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("127.0.0.1", local_port))
            except OSError as exc:
                raise RuntimeError(tr('harness.30e270220d23')) from exc
        ssh_security.resolve(self.root, environment)
        resolved = subprocess.run(ssh_security.base_args(self.root, environment) +
                                  ["-o", "ClearAllForwardings=no", "-G", target],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        if resolved.returncode or any(line.startswith(("localforward ", "remoteforward ", "dynamicforward "))
                                      for line in resolved.stdout.splitlines()):
            raise ValueError(tr('harness.f627bf98fa05'))
        args = ssh_security.base_args(self.root, environment)
        args += ["-N", "-T", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=no",
                 "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                 "-o", "ServerAliveCountMax=2", "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}", target]
        # The managed policy's first ClearAllForwardings wins; explicitly place the
        # override before -F. Otherwise OpenSSH silently discards our own -L.
        args[1:1] = ["-o", "ClearAllForwardings=no"]
        creation = subprocess.CREATE_NO_WINDOW if runtime.WINDOWS else 0
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=creation)
        self._processes[service_key] = (process, remote_port, local_port, environment["id"])
        try:
            deadline = time.monotonic() + 3
            while True:
                code = process.poll()
                if code is not None:
                    raise RuntimeError(tr('harness.51a7f57b7fe6', v0=f'{code}'))
                with socket.socket() as probe:
                    probe.settimeout(0.05)
                    ready = probe.connect_ex(("127.0.0.1", local_port)) == 0
                if ready and listener_owned(local_port, process.pid):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(tr('harness.f20d634852be'))
                time.sleep(0.05)
        except Exception:
            self.close(service_key)
            raise
        return f"http://127.0.0.1:{local_port}"

    def close(self, service_key: str) -> None:
        item = self._processes.pop(service_key, None)
        if not item or item[0].poll() is not None:
            return
        process = item[0]
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def shutdown(self) -> None:
        for key in list(self._processes):
            self.close(key)
