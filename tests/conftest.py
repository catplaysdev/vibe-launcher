import pytest


@pytest.fixture(autouse=True)
def no_real_modal_dialogs(monkeypatch):
    """A confirmation dialog that really opens would block the whole test run. Turn that into a clear failure instead:
    tests that exercise a confirmation patch ``view._confirm`` / ``page._confirm`` explicitly."""
    from vibe_launcher import kit

    def refuse(parent, title, message, **kwargs):
        raise AssertionError(f"ConfirmDialog opened during a test ({title!r}); patch the _confirm seam")
    monkeypatch.setattr(kit.ConfirmDialog, "ask", staticmethod(refuse))


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
