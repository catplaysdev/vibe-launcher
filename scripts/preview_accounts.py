"""Capture the account page with synthetic identities; never capture the desktop."""
from pathlib import Path

from PySide6.QtWidgets import QApplication

from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow


def main():
    app = QApplication.instance() or QApplication([])
    root = Path(__file__).resolve().parents[1]
    out = root / ".local-test" / "previews"
    out.mkdir(parents=True, exist_ok=True)
    store = Store(root / ".benchmark" / "accounts-preview.db")
    store.save("environments", {"id": "preview-ssh", "name": "開發主機", "kind": "ssh", "ssh_target": "example"})
    for tool in ("codex", "claude", "deepseek-harness"):
        store.save("accounts", {"id": "preview-" + tool, "name": "個人 " + tool, "tool": tool, "auth": "subscription",
                                "environment_id": "preview-ssh" if tool == "deepseek-harness" else "local"})
    store.save("github_accounts", {"id": "preview-gh", "name": "GitHub 個人", "environment_id": "local", "login": "octocat", "status": "authenticated"})
    view = MainWindow(store, LauncherService(store))
    view.timer.stop()
    view.show_page("accounts")
    view.accounts_page.select("github_accounts", "preview-gh")
    view.show()
    for width, height in ((1180, 760), (640, 520)):
        view.resize(width, height)
        app.processEvents()
        page = view.accounts_page
        page.scroll.verticalScrollBar().setValue(0)
        app.processEvents()
        view.grab().save(str(out / f"accounts-main-{width}-dialog-preview.png"))
        page.scroll.verticalScrollBar().setValue(page.scroll.verticalScrollBar().maximum())
        app.processEvents()
        view.grab().save(str(out / f"accounts-details-{width}-dialog-preview.png"))
        print(width, height, "actual", view.width(), view.height(), "columns", page._columns,
              "scroll", page.scroll.verticalScrollBar().maximum())
    view.accounts_page.select("accounts", "preview-codex")
    for _ in range(3):
        app.processEvents()
        view.accounts_page.scroll.verticalScrollBar().setValue(view.accounts_page.scroll.verticalScrollBar().maximum())
    app.processEvents()
    view.grab().save(str(out / "accounts-codex-details-640-dialog-preview.png"))
    print("Codex details actual", view.width(), view.height())
    view.workspace.shutdown()
    view.close()


if __name__ == "__main__":
    main()
