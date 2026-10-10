import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibe_launcher import runtime


def fake_stat(uid, mode):
    return SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | mode)


def test_user_dir_is_trusted_only_when_owned_and_not_writable_by_others():
    assert runtime._safe_user_dir(Path("x"), 1000, fake_stat(1000, 0o755))
    assert runtime._safe_user_dir(Path("x"), 1000, fake_stat(1000, 0o700))
    assert not runtime._safe_user_dir(Path("x"), 1000, fake_stat(1000, 0o775)), "group-writable"
    assert not runtime._safe_user_dir(Path("x"), 1000, fake_stat(1000, 0o757)), "world-writable"
    assert not runtime._safe_user_dir(Path("x"), 1000, fake_stat(0, 0o755)), "owned by someone else"
    assert not runtime._safe_user_dir(Path("x"), 1000, SimpleNamespace(st_uid=1000, st_mode=stat.S_IFREG | 0o755)), "not a directory"
    assert not runtime._safe_user_dir(Path("definitely-missing-dir"), 1000)


def test_user_tool_dirs_list_common_locations_newest_node_first(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setattr(runtime, "_safe_user_dir", lambda path, uid, info=None: path.is_dir())
    for rel in (".local/bin", ".npm-global/bin", ".nvm/versions/node/v18.20.0/bin", ".nvm/versions/node/v22.1.0/bin",
                ".nvm/versions/node/v9.0.0/bin"):
        (tmp_path / rel).mkdir(parents=True)
    dirs = runtime.user_tool_dirs(tmp_path, uid=1)
    assert dirs[:2] == [str(tmp_path / ".local/bin"), str(tmp_path / ".npm-global/bin")]
    assert [Path(d).parent.name for d in dirs[2:]] == ["v22.1.0", "v18.20.0", "v9.0.0"]
    assert not any(".cargo" in d for d in dirs), "directories that do not exist are skipped"


def test_windows_native_install_dirs_are_found_without_path(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", True)
    local = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    (tmp_path / ".local/bin").mkdir(parents=True)
    codex_bin = local / "Programs" / "OpenAI" / "Codex" / "bin"
    codex_bin.mkdir(parents=True)
    assert runtime.user_tool_dirs(tmp_path) == [str(tmp_path / ".local/bin"), str(codex_bin)]
    assert runtime.user_tool_dirs(tmp_path / "missing") == [str(codex_bin)]


@pytest.mark.skipif(os.name != "nt", reason="Windows native executable discovery")
def test_windows_perform_discovers_native_clis_with_stale_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    for directory, name in [(home / ".local/bin", "claude"),
                            (local / "Programs/OpenAI/Codex/bin", "codex")]:
        directory.mkdir(parents=True)
        (directory / (name + ".exe")).write_bytes(b"native placeholder")
    found = []
    monkeypatch.setattr(runtime, "doctor", lambda *a: found.extend(runtime.trusted_executable(t) for t in ("codex", "claude")))
    request = {"action": "doctor", "environment_id": "local", "nonce": "n"}
    runtime.perform(request, tmp_path / "data")
    assert [Path(p).name for p in found] == ["codex.exe", "claude.exe"]


def test_windows_discovery_ignores_relative_install_root(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", "relative")
    (tmp_path / "relative/Programs/OpenAI/Codex/bin").mkdir(parents=True)
    assert runtime.user_tool_dirs(tmp_path / "home") == []


def test_augmented_path_keeps_system_directories_first_without_duplicates():
    system = os.pathsep.join(["/usr/local/bin", "/usr/bin"])
    result = runtime.augmented_path(system, ["/home/me/.local/bin", "/usr/bin"]).split(os.pathsep)
    assert result == ["/usr/local/bin", "/usr/bin", "/home/me/.local/bin"]


def test_cli_in_a_user_directory_is_found_when_system_path_lacks_it(tmp_path, monkeypatch):
    user_bin = tmp_path / "home" / ".local" / "bin"
    user_bin.mkdir(parents=True)
    tool = user_bin / ("codex.exe" if os.name == "nt" else "codex")
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError):
        runtime.trusted_executable("codex")
    monkeypatch.setenv("PATH", runtime.augmented_path(str(tmp_path / "empty"), [str(user_bin)]))
    assert Path(runtime.trusted_executable("codex")) == tool


def run_doctor(monkeypatch, request, present, capsys):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setattr(runtime, "emit", lambda *a, **k: None)

    def find(name):
        if name in present:
            return "/usr/local/bin/" + name
        raise RuntimeError("missing")
    monkeypatch.setattr(runtime, "trusted_executable", find)
    try:
        runtime.doctor(request, Path("."))
        return None, capsys.readouterr().out
    except RuntimeError as error:
        return str(error), capsys.readouterr().out


ACCOUNT = {"id": "a", "name": "A", "tool": "codex", "auth": "subscription"}


def test_environment_check_needs_only_one_cli_and_tmux(monkeypatch, capsys):
    error, out = run_doctor(monkeypatch, {"action": "doctor"}, {"codex", "tmux"}, capsys)
    assert error is None
    assert "claude: not installed (optional)" in out and "MISSING" not in out


def test_environment_check_fails_without_any_cli_but_not_without_tmux(monkeypatch, capsys):
    error, out = run_doctor(monkeypatch, {"action": "doctor"}, {"tmux"}, capsys)
    assert "codex or claude" in error and "developers.openai.com" in out
    # tmux is only reported by a plain environment check; it is not a failure
    error, out = run_doctor(monkeypatch, {"action": "doctor"}, {"codex"}, capsys)
    assert error is None and "only needed to keep a project running" in out


def test_launch_requires_the_selected_accounts_cli(monkeypatch, capsys):
    request = {"action": "launch", "account": ACCOUNT}
    error, out = run_doctor(monkeypatch, request, {"claude", "tmux"}, capsys)
    assert error == "Missing required tools: codex" and "codex: MISSING" in out
    error, out = run_doctor(monkeypatch, request, {"codex", "tmux"}, capsys)
    assert error is None


def test_a_keep_alive_launch_needs_tmux_but_a_direct_launch_does_not(monkeypatch, capsys):
    keep = {"action": "launch", "account": ACCOUNT, "persistent": True}
    error, out = run_doctor(monkeypatch, keep, {"codex"}, capsys)
    assert error == "Missing required tools: tmux" and "not installed automatically" in out
    direct = {"action": "launch", "account": ACCOUNT, "persistent": False}
    error, out = run_doctor(monkeypatch, direct, {"codex"}, capsys)
    assert error is None
    # an account login never needs tmux
    error, out = run_doctor(monkeypatch, {"action": "login", "account": ACCOUNT}, {"codex"}, capsys)
    assert error is None
