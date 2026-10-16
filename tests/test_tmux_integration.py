"""Real tmux lifecycle proof on an isolated socket; never uses the user's tmux server."""
import os
import shutil
import subprocess
import time
import uuid

import pytest

from vibe_launcher import runtime


@pytest.mark.skipif(os.name == "nt" or not shutil.which("tmux"), reason="requires POSIX and a local tmux installation")
def test_detach_preserves_the_real_worker_and_other_attachment(tmp_path, monkeypatch):
    executable = shutil.which("tmux")
    socket_name = "vibe-test-" + uuid.uuid4().hex
    prefix = [executable, "-L", socket_name]
    def tmux(*args, capture=False):
        return subprocess.run(prefix + list(args), capture_output=capture, timeout=5)
    monkeypatch.setattr(runtime, "tmux", tmux)
    request = {"action": "detach", "session_id": "s", "environment_id": "host", "attachment_nonce": "abc12345"}
    runtime.write_json(runtime.session_path(tmp_path, "s"), {"session_id": "s", "environment_id": "host"})
    clients = []
    try:
        worker = tmux("new-session", "-d", "-s", "work", "-P", "-F", "#{window_id}", "sleep 60", capture=True)
        assert worker.returncode == 0
        window = worker.stdout.decode().strip()
        assert tmux("set-option", "-w", "-t", window, "@vibe-session", "s", capture=True).returncode == 0
        for view in ("vibe-s-abc12345", "other-view"):
            placeholder = tmux("new-session", "-d", "-s", view, "-P", "-F", "#{window_id}", "sleep 60", capture=True).stdout.decode().strip()
            assert tmux("link-window", "-s", window, "-t", "=" + view + ":", capture=True).returncode == 0
            assert tmux("kill-window", "-t", placeholder, capture=True).returncode == 0
            clients.append(subprocess.Popen(prefix + ["-C", "attach-session", "-t", "=" + view],
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        deadline = time.monotonic() + 5
        while len(tmux("list-clients", "-F", "#{session_name}", capture=True).stdout.splitlines()) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        reports = []
        monkeypatch.setattr(runtime, "operation_result", lambda _, root, result: reports.append(result))
        assert runtime.detach_session(request, tmp_path) == 0
        assert reports == [{"status": "detached"}]
        assert clients[0].wait(timeout=5) == 0
        assert clients[1].poll() is None
        panes = tmux("list-panes", "-t", window, "-F", "#{pane_pid}", capture=True)
        assert runtime.pid_alive(int(panes.stdout.strip())) is True
    finally:
        tmux("kill-server", capture=True)
        for client in clients:
            client.communicate(timeout=5)
