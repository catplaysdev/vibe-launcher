"""The pre-push hook keeps work branches (feat/dev_*, fix_*) on this computer; only main, develop, hotfix and tags go out."""
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "pre-push"
ZERO, SHA = "0" * 40, "1" * 40
SH = shutil.which("sh")


def push(remote_ref: str, local_sha: str = SHA) -> int:
    line = f"refs/heads/x {local_sha} {remote_ref} {ZERO}\n"
    return subprocess.run([SH, str(HOOK), "origin", "url"], input=line, capture_output=True, text=True).returncode


@pytest.mark.skipif(SH is None, reason="needs a POSIX shell (Git for Windows ships one)")
@pytest.mark.parametrize("ref,allowed", [
    ("refs/heads/main", True), ("refs/heads/develop", True), ("refs/heads/hotfix", True), ("refs/tags/v1.2.3", True),
    ("refs/heads/feat/dev_workspace_restore", False), ("refs/heads/fix_runtime_sync1", False),
    ("refs/heads/feat/anything", False), ("refs/tags/nightly", False),
])
def test_only_shared_branches_and_version_tags_are_pushed(ref, allowed):
    assert (push(ref) == 0) is allowed


@pytest.mark.skipif(SH is None, reason="needs a POSIX shell (Git for Windows ships one)")
def test_removing_a_stray_remote_branch_is_allowed():
    assert push("refs/heads/feat/old", local_sha=ZERO) == 0


def test_the_hook_keeps_unix_line_endings():
    assert b"\r" not in HOOK.read_bytes()
