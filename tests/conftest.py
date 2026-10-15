import os

import pytest

# Existing UI assertions cover Traditional Chinese. English is checked explicitly
# in test_localization_settings.py; the installed app defaults to English.
os.environ.setdefault("VIBE_LAUNCHER_LANGUAGE", "zh-TW")


@pytest.fixture(autouse=True)
def no_real_cli_user_data(tmp_path, monkeypatch):
    """Automatic user-data discovery must never inspect the developer's skills or conversations in tests."""
    from vibe_launcher import runtime
    original = runtime.user_config_dir
    monkeypatch.setattr(runtime, "user_config_dir", lambda tool, home=None: original(tool, home or tmp_path / "native-home"))


@pytest.fixture(autouse=True)
def no_real_modal_dialogs(monkeypatch):
    """A confirmation dialog that really opens would block the whole test run. Turn that into a clear failure instead:
    tests that exercise a confirmation patch ``view._confirm`` / ``page._confirm`` explicitly."""
    from vibe_launcher import kit

    def refuse(parent, title, message, **kwargs):
        raise AssertionError(f"ConfirmDialog opened during a test ({title!r}); patch the _confirm seam")
    monkeypatch.setattr(kit.ConfirmDialog, "ask", staticmethod(refuse))
    monkeypatch.setattr(kit.ConfirmDialog, "ask_skippable", staticmethod(refuse))


@pytest.fixture(autouse=True)
def no_real_launch_dialogs(monkeypatch):
    """Same idea for the account chooser and the create-folders prompt: tests patch the view's seams instead."""
    from vibe_launcher import launch_dialogs

    def refuse(self, *args, **kwargs):
        raise AssertionError(f"{type(self).__name__} opened during a test; patch the view seam (_ask_account / _confirm_create_folders)")
    monkeypatch.setattr(launch_dialogs.AccountChoiceDialog, "exec", refuse)
    monkeypatch.setattr(launch_dialogs.CreateFoldersDialog, "exec", refuse)


@pytest.fixture(autouse=True)
def no_real_remote_browser(monkeypatch):
    """The remote folder picker would open an SSH connection: tests patch the view seam (``_browse_remote``)."""
    from vibe_launcher import launch_dialogs

    def refuse(self, *args, **kwargs):
        raise AssertionError("RemoteFolderDialog opened during a test; patch view._browse_remote")
    monkeypatch.setattr(launch_dialogs.RemoteFolderDialog, "exec", refuse)


@pytest.fixture(autouse=True)
def restore_controls():
    from vibe_launcher import i18n, runtime, shortcuts
    original_language = i18n.language()
    shortcuts.configure({})
    runtime._MESSAGES = i18n.catalog("zh-TW")
    yield
    i18n.set_language(original_language)
    shortcuts.configure({})


@pytest.fixture(autouse=True)
def no_automatic_update_checks(monkeypatch):
    """The window looks for new versions with git in the background; tests never reach the network."""
    from vibe_launcher import ui
    monkeypatch.setattr(ui.VibeLauncherWindow, "UPDATE_AUTOCHECK", False)


@pytest.fixture(autouse=True)
def no_real_managed_codex(monkeypatch):
    """Worker tests run stand-in CLIs: the installed Codex must not be probed or started behind them. Tests of the
    managed path patch this seam back explicitly."""
    from vibe_launcher import runtime
    monkeypatch.setattr(runtime, "managed_codex_supported", lambda *args: False)
    monkeypatch.setattr(runtime, "managed_claude_supported", lambda *args: False)
