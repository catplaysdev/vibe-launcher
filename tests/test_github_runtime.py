import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from vibe_launcher import runtime


def request(path, operation, **updates):
    return {"github_account": {"id": "a" * 32, "environment_id": "local"},
            "environment_id": "local", "github_operation": operation, "path": str(path), **updates}


def test_github_env_isolates_accounts_and_discards_inherited_identity(tmp_path):
    parent = {"PATH": "bin", "GH_TOKEN": "secret", "github_token": "secret",
              "GH_CONFIG_DIR": "other", "GH_HOST": "evil.example", "GH_REPO": "other/repo",
              "GIT_CONFIG_COUNT": "1", "GIT_DIR": "elsewhere"}
    first = runtime.github_env({"id": "a" * 32}, tmp_path, parent)
    second = runtime.github_env({"id": "b" * 32}, tmp_path, parent)
    assert first["GH_CONFIG_DIR"] != second["GH_CONFIG_DIR"]
    assert first["GH_HOST"] == "github.com"
    assert not any(key.upper().endswith("TOKEN") for key in first)
    assert "GIT_CONFIG_COUNT" not in first and "GIT_DIR" not in first
    assert parent["GH_TOKEN"] == "secret"


@pytest.mark.parametrize("url", ["https://user:secret@github.com/a/b", "https://github.com/a/b?token=x",
                                  "git@github.com:a/b", "https://evil.example/a/b", "https://github.com/../b"])
def test_clone_url_rejects_credentials_or_unsupported_host(url):
    with pytest.raises(ValueError):
        runtime.github_url(url)


def test_account_cannot_cross_environment(tmp_path):
    with pytest.raises(ValueError, match="執行環境"):
        runtime.github_action(request(tmp_path, "status", environment_id="remote"), tmp_path)


def test_official_login_cancelled_does_not_query_or_read_credentials(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "trusted_executable", lambda value: value)
    monkeypatch.setattr(runtime.subprocess, "call", lambda args, **kw: calls.append((args, kw)) or 1)
    monkeypatch.setattr(runtime, "github_run", lambda *a, **kw: pytest.fail("cancelled login queried identity"))
    result = runtime.github_action(request(tmp_path, "login"), tmp_path)
    assert result == {"status": "failed", "credential_storage": "unverified"}
    assert calls[0][0] == ["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"]
    assert "GH_PROMPT_DISABLED" not in calls[0][1]["env"]
    assert calls[0][1]["env"]["GIT_CONFIG_GLOBAL"] == str(Path(calls[0][1]["env"]["GH_CONFIG_DIR"]) / "login.gitconfig")
    assert calls[0][1]["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert calls[0][1]["cwd"] == Path(calls[0][1]["env"]["GH_CONFIG_DIR"])


def test_status_never_claims_secure_storage_or_returns_raw_auth_output(tmp_path, monkeypatch):
    user = json.dumps({"login": "tester", "id": 7, "name": "Tester <x>\n", "email": "private@example.test"})
    monkeypatch.setattr(runtime, "github_run", lambda *a, **kw: subprocess.CompletedProcess([], 0, user, "secret"))
    result = runtime.github_action(request(tmp_path, "status"), tmp_path)
    assert result == {"status": "authenticated", "login": "tester", "user_id": "7", "display_name": "Tester x",
                      "noreply_email": "7+tester@users.noreply.github.com", "credential_storage": "unverified"}


def test_status_signed_out_and_malformed_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "github_run", lambda *a, **kw: subprocess.CompletedProcess([], 1, "", "not logged in"))
    assert runtime.github_action(request(tmp_path, "status"), tmp_path)["status"] == "unavailable"
    monkeypatch.setattr(runtime, "github_run", lambda *a, **kw: subprocess.CompletedProcess([], 0, '{"login": "bad name", "id": 1}', ""))
    with pytest.raises(RuntimeError):
        runtime.github_action(request(tmp_path, "status"), tmp_path)


def test_login_explains_which_browser_account_is_authorized(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runtime, "trusted_executable", lambda value: value)
    monkeypatch.setattr(runtime.subprocess, "call", lambda args, **kw: 1)
    runtime.github_action(request(tmp_path, "login"), tmp_path)
    assert "無痕" in capsys.readouterr().out


def test_session_env_pushes_and_commits_as_the_bound_account(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "trusted_executable", lambda value: "C:/gh/gh.exe")
    account = {"id": "a" * 32, "author_name": "Bound User", "author_email": "1+bound@users.noreply.github.com"}
    env = runtime.github_session_env(account, tmp_path, {"PATH": "bin", "GIT_CONFIG_COUNT": "5", "GIT_AUTHOR_NAME": "Other"})
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == env["GIT_CONFIG_KEY_1"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_0"] == "" and "auth git-credential" in env["GIT_CONFIG_VALUE_1"]
    assert "GH_CONFIG_DIR=" in env["GIT_CONFIG_VALUE_1"]
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "Bound User"
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == "1+bound@users.noreply.github.com"
    plain = runtime.github_session_env({"id": "a" * 32}, tmp_path, {"PATH": "bin", "GIT_AUTHOR_NAME": "Other"})
    assert plain["GIT_AUTHOR_NAME"] == "Other" and "GIT_COMMITTER_EMAIL" not in plain    # no identity saved: Git decides


@pytest.mark.parametrize("author", [{"author_name": "a\nb", "author_email": "x@y"}, {"author_name": "a", "author_email": "nope"},
                                    {"author_name": "<a>", "author_email": "x@y"}])
def test_invalid_commit_identity_is_rejected(author):
    with pytest.raises(ValueError):
        runtime.github_author(author)


def test_init_creates_repository_once(tmp_path, monkeypatch):
    git = shutil.which("git")
    if not git:
        pytest.skip("Git unavailable")
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: git)
    folder = tmp_path / "plain"
    folder.mkdir()
    result = runtime.github_action(request(folder, "init"), tmp_path / "data")
    assert result["status"] == "initialized" and result["git"] is True
    with pytest.raises(ValueError, match="已經是"):
        runtime.github_action(request(folder, "init"), tmp_path / "data")
    with pytest.raises(ValueError, match="不存在"):
        runtime.github_action(request(tmp_path / "missing", "init"), tmp_path / "data")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git = shutil.which("git")
    if not git:
        pytest.skip("Git unavailable")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: git if name == "git" else "C:/Program Files/GitHub CLI/gh.exe")
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run([git, "init", str(path)], check=True, capture_output=True)

    def config(*args):
        return subprocess.run([git, "-C", str(path), "config", "--local", *args],
                              check=True, capture_output=True, text=True).stdout

    subprocess.run([git, "-C", str(path), "remote", "add", "upstream", "https://github.com/example/project.git"], check=True)
    subprocess.run([git, "-C", str(path), "remote", "add", "second", "git@github.com:example/second.git"], check=True)
    config("user.name", "Original")
    config("user.email", "original@example.test")
    return path, config


def test_inspect_keeps_all_remotes_and_author_origin(repo, tmp_path):
    path, _ = repo
    result = runtime.github_action(request(path, "inspect"), tmp_path / "data")
    assert {item["name"] for item in result["remotes"]} == {"upstream", "second"}
    assert result["access"] == "unchecked"
    assert result["author"]["user.name"]["value"] == "Original"
    assert result["author"]["user.name"]["source"].startswith("file:")


def binding(path, **updates):
    return request(path, "bind", github_remote="upstream",
                   github_expected_remote_url="https://github.com/example/project.git",
                   github_author_name="Managed", github_author_email="managed@example.test", **updates)


def test_binding_preview_apply_and_unbind_preserve_external_edits(repo, tmp_path):
    path, config = repo
    root = tmp_path / "data"
    preview = runtime.github_action(binding(path), root)
    assert preview["status"] == "preview"
    assert config("--get", "user.name").strip() == "Original"
    assert "gh.exe" not in json.dumps(preview)
    assert runtime.github_action(binding(path, github_apply=True, github_revision=preview["revision"]), root)["status"] == "bound"
    helpers = runtime.github_config(runtime.github_env({"id": "a" * 32}, root), path, "credential.https://github.com.helper")
    assert helpers[0] == "" and " auth git-credential" in helpers[1]
    assert "GH_CONFIG_DIR=" in helpers[1] and "-u GH_TOKEN" in helpers[1]
    config("user.name", "Personal edit")
    result = runtime.github_action(request(path, "unbind"), root)
    assert result["preserved"] == ["user.name"]
    assert config("--get", "user.name").strip() == "Personal edit"
    assert config("--get", "user.email").strip() == "original@example.test"
    assert not runtime.github_config(runtime.github_env({"id": "a" * 32}, root), path, "credential.https://github.com.helper")


def test_changed_remote_cannot_apply_stale_confirmation(repo, tmp_path):
    path, config = repo
    config("remote.upstream.url", "https://github.com/example/changed.git")
    with pytest.raises(ValueError, match="Remote 已變更"):
        runtime.github_action(binding(path, github_apply=True), tmp_path / "data")
    assert config("--get", "user.name").strip() == "Original"


def test_changed_author_cannot_apply_stale_preview(repo, tmp_path):
    path, config = repo
    root = tmp_path / "data"
    preview = runtime.github_action(binding(path), root)
    config("user.name", "External edit after preview")
    with pytest.raises(ValueError, match="重新預覽"):
        runtime.github_action(binding(path, github_apply=True, github_revision=preview["revision"]), root)
    assert config("--get", "user.name").strip() == "External edit after preview"


def test_ssh_conversion_requires_explicit_https_target(repo, tmp_path):
    path, config = repo
    args = request(path, "bind", github_remote="second", github_expected_remote_url="git@github.com:example/second.git")
    with pytest.raises(ValueError):
        runtime.github_action(args, tmp_path / "data")
    args["github_url"] = "https://github.com/example/second.git"
    preview = runtime.github_action(args, tmp_path / "data")
    assert preview["changes"]["remote.second.url"]["before"] == ["git@github.com:example/second.git"]
    assert config("--get", "remote.second.url").strip().startswith("git@")


def test_clone_nonempty_destination_does_not_start_git(tmp_path, monkeypatch):
    (tmp_path / "existing.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(runtime, "github_run", lambda *a, **kw: pytest.fail("clone must not run"))
    with pytest.raises(ValueError, match="目的地"):
        runtime.github_action(request(tmp_path, "clone", github_url="https://github.com/example/project"), tmp_path / "data")


def test_failed_clone_keeps_partial_destination(tmp_path, monkeypatch):
    destination = tmp_path / "partial"
    monkeypatch.setattr(runtime, "github_helper", lambda env: "selected-helper")

    def fail(args, env, *a, **kw):
        destination.mkdir()
        (destination / "partial.txt").write_text("keep", encoding="utf-8")
        assert "credential.https://github.com.helper=" in args
        raise RuntimeError("clone interrupted")

    monkeypatch.setattr(runtime, "github_run", fail)
    with pytest.raises(RuntimeError):
        runtime.github_action(request(destination, "clone", github_url="https://github.com/example/project"), tmp_path / "data")
    assert (destination / "partial.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("existing", [False, True])
def test_clone_accepts_only_empty_or_absent_destination_and_inspects_success(tmp_path, monkeypatch, existing):
    destination = tmp_path / "destination"
    if existing:
        destination.mkdir()
    calls = []
    monkeypatch.setattr(runtime, "github_helper", lambda env: "selected-helper")
    monkeypatch.setattr(runtime, "github_run", lambda args, env, **kw: calls.append((args, env)))
    monkeypatch.setattr(runtime, "github_inspect", lambda path, env: {"root": str(path), "remotes": []})
    result = runtime.github_action(request(destination, "clone", github_url="https://github.com/example/project.git"), tmp_path / "data")
    assert result["status"] == "cloned"
    assert calls[0][0][-3:] == ["--", "https://github.com/example/project.git", str(destination)]
    assert calls[0][1]["GH_CONFIG_DIR"].endswith("a" * 32)


def test_unbind_validates_entire_manifest_before_any_mutation(repo, tmp_path):
    path, config = repo
    root = tmp_path / "data"
    preview = runtime.github_action(binding(path), root)
    runtime.github_action(binding(path, github_apply=True, github_revision=preview["revision"]), root)
    manifest_path = next((root / "github-bindings").glob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["changes"]["core.sshCommand"] = {"before": [], "after": ["malicious"]}
    runtime.write_json(manifest_path, manifest)
    with pytest.raises(ValueError, match="manifest"):
        runtime.github_action(request(path, "unbind"), root)
    assert config("--get", "user.name").strip() == "Managed"


def test_partial_apply_can_restore_only_completed_managed_values(repo, tmp_path, monkeypatch):
    path, config = repo
    root = tmp_path / "data"
    config("credential.https://github.com.helper", "original-helper")
    preview = runtime.github_action(binding(path), root)
    run = runtime.github_run

    def interrupt(args, *a, **kw):
        if "--add" in args and args[-2] == "credential.https://github.com.helper":
            raise RuntimeError("interrupted after unset")
        return run(args, *a, **kw)

    monkeypatch.setattr(runtime, "github_run", interrupt)
    with pytest.raises(RuntimeError):
        runtime.github_action(binding(path, github_apply=True, github_revision=preview["revision"]), root)
    monkeypatch.setattr(runtime, "github_run", run)
    assert runtime.github_action(request(path, "unbind"), root)["preserved"] == []
    assert config("--get", "credential.https://github.com.helper").strip() == "original-helper"
    assert config("--get", "user.name").strip() == "Original"


def test_two_accounts_in_same_environment_have_independent_repository_helpers(repo, tmp_path):
    path, _ = repo
    root = tmp_path / "data"
    first = binding(path)
    first["github_revision"] = runtime.github_action(first, root)["revision"]
    first["github_apply"] = True
    runtime.github_action(first, root)
    second_path = tmp_path / "second-repo"
    git = shutil.which("git")
    subprocess.run([git, "init", str(second_path)], check=True, capture_output=True)
    subprocess.run([git, "-C", str(second_path), "remote", "add", "upstream", "https://github.com/example/project.git"], check=True)
    second = binding(second_path)
    second["github_account"] = {"id": "b" * 32, "environment_id": "local"}
    second["github_revision"] = runtime.github_action(second, root)["revision"]
    second["github_apply"] = True
    runtime.github_action(second, root)
    env = runtime.github_env({"id": "a" * 32}, root)
    first_helper = runtime.github_config(env, path, "credential.https://github.com.helper")[-1]
    second_helper = runtime.github_config(env, second_path, "credential.https://github.com.helper")[-1]
    assert "a" * 32 in first_helper and "b" * 32 not in first_helper
    assert "b" * 32 in second_helper and "a" * 32 not in second_helper


def test_ssh_scheme_is_read_only_repository_identity():
    assert runtime.github_repository_url("ssh://git@github.com/example/repo.git") == "https://github.com/example/repo.git"
    with pytest.raises(ValueError):
        runtime.github_repository_url("ssh://git:secret@github.com/example/repo")


def test_account_binds_without_remote_and_origin_is_added_later(repo, tmp_path):
    path, config = repo
    root = tmp_path / "data"
    first = request(path, "bind", github_author_name="Managed")
    first.update(github_apply=True, github_revision=runtime.github_action(first, root)["revision"])
    assert runtime.github_action(first, root) == {"status": "bound", "root": str(path), "url": "", "remote": ""}
    assert config("--get", "user.name").strip() == "Managed"
    later = request(path, "bind", github_remote="origin", github_new_remote=True,
                    github_url="https://github.com/example/new.git")
    preview = runtime.github_action(later, root)
    assert preview["changes"]["remote.origin.url"] == {"before": [], "after": ["https://github.com/example/new.git"]}
    later.update(github_apply=True, github_revision=preview["revision"])
    assert runtime.github_action(later, root)["remote"] == "origin"   # same account: no unbind needed first
    assert config("--get", "remote.origin.url").strip() == "https://github.com/example/new.git"
    assert config("--get", "remote.origin.fetch").strip() == "+refs/heads/*:refs/remotes/origin/*"
    assert runtime.github_action(request(path, "unbind"), root)["preserved"] == []
    assert config("--get", "user.name").strip() == "Original"           # the very first value, not the managed one
    assert not runtime.github_config(runtime.github_env({"id": "a" * 32}, root), path, "remote.origin.url")


def test_new_remote_must_not_exist_and_other_account_cannot_extend_binding(repo, tmp_path):
    path, _ = repo
    root = tmp_path / "data"
    with pytest.raises(ValueError, match="已存在"):
        runtime.github_action(request(path, "bind", github_remote="upstream", github_new_remote=True,
                                      github_url="https://github.com/example/x.git"), root)
    with pytest.raises(ValueError, match="remote"):
        runtime.github_action(request(path, "bind", github_url="https://github.com/example/x.git"), root)
    first = request(path, "bind")
    first.update(github_apply=True, github_revision=runtime.github_action(first, root)["revision"])
    runtime.github_action(first, root)
    other = request(path, "bind")
    other["github_account"] = {"id": "b" * 32, "environment_id": "local"}
    other.update(github_apply=True, github_revision=runtime.github_action(other, root)["revision"])
    with pytest.raises(ValueError, match="其他 GitHub 帳號"):
        runtime.github_action(other, root)


def test_folder_without_git_can_be_inspected_but_not_configured(tmp_path, monkeypatch):
    git = shutil.which("git")
    if not git:
        pytest.skip("Git unavailable")
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: git)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    folder = tmp_path / "plain"
    folder.mkdir()
    info = runtime.github_action(request(folder, "inspect"), tmp_path / "data")
    assert info["git"] is False and info["remotes"] == [] and info["root"] == str(folder)
    with pytest.raises(ValueError, match="git init"):
        runtime.github_action(request(folder, "bind"), tmp_path / "data")
    assert runtime.github_action(request(folder, "unbind"), tmp_path / "data")["status"] == "unbound"
