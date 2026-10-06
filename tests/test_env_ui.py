"""Environments page: status view-model, filtering, pinning, responsive layout and interaction."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from vibe_launcher import env_state, runtime, ssh_security
from vibe_launcher.env_state import build_env_state, format_relative, matches_filter, sort_states
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow

KNOWN = "gpu-lab ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGpmZ2h0ZXN0a2V5dGVzdGtleXRlc3RrZXl0ZXN0a2V5\n"


def iso(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


class FakeService:
    def __init__(self):
        self.calls = []

    def start(self, action, **kwargs):
        self.calls.append((action, kwargs))


def records(store):
    return {kind: store.list(kind) for kind in ("accounts", "environments", "presets", "sessions", "auth_states")}


def state_for(store, env, **kw):
    return build_env_state(env, records=records(store), root=store.path.parent, **kw)


def write_report(root, env_id, *reports):
    runtime.write_json(root / "tool-versions" / (env_id + ".json"), {"reports": list(reports)})


def report(tool, installed, latest, status, **extra):
    return {"tool": tool, "installed": installed, "latest": latest, "status": status, "source": "npm",
            "path": "C:/tools/" + tool, "checked_at": iso(hours=1), "message": "", **extra}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "gui.db")


@pytest.fixture
def ssh_ok(monkeypatch):
    monkeypatch.setattr(ssh_security, "executable", lambda name="ssh": "C:/Windows/System32/OpenSSH/ssh.exe")


# ----------------------------------------------------------------------------- view-model


def test_unverified_ssh_environment_is_unknown_not_ready(store, ssh_ok):
    env = store.save("environments", {"name": "新環境", "kind": "ssh", "ssh_target": "new-host"})
    state = state_for(store, env)
    assert (state.tone, state.status_text) == ("unknown", "未檢查")
    assert {c.key for c in state.chips} >= {"credential", "trust", "agent", "cli"}
    assert all(c.label and c.icon for c in state.chips), "no chip may rely on colour alone"


def test_local_environment_is_ready_and_builtin(store):
    state = state_for(store, store.get("environments", "local"))
    assert state.tone == "ok" and state.builtin and state.kind == "local"
    assert state.subtitle.endswith("localhost")


def test_persistent_password_is_a_warning_and_cached_policy_is_labelled(store, ssh_ok):
    forever = store.save("environments", {"name": "A", "kind": "ssh", "ssh_target": "a", "ssh_auth_mode": "password", "remember_seconds": -1})
    cached = store.save("environments", {"name": "B", "kind": "ssh", "ssh_target": "b", "ssh_auth_mode": "password", "remember_seconds": 900})
    off = store.save("environments", {"name": "C", "kind": "ssh", "ssh_target": "c", "ssh_auth_mode": "password", "remember_seconds": 0})
    s_forever, s_cached, s_off = (state_for(store, e) for e in (forever, cached, off))
    assert s_forever.credential == "password_persistent"
    assert next(c for c in s_forever.chips if c.key == "credential").tone == "warn"
    # a deliberate setting is not a health problem: it must not drag the environment into "needs attention"
    assert s_forever.tone != "warn" and not matches_filter(s_forever, "attention")
    assert s_cached.credential == "password_cached"
    assert next(c for c in s_cached.chips if c.key == "credential").label == "Credential · 15m"
    assert s_off.credential == "password_off"


def test_host_trust_comes_from_the_saved_known_hosts(store, ssh_ok):
    env = store.save("environments", {"name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})
    assert state_for(store, env).trust_ok is False
    known = store.path.parent / "ssh" / env["id"] / "known_hosts"
    known.parent.mkdir(parents=True, exist_ok=True)
    known.write_text(KNOWN, encoding="utf-8")
    runtime.protect_path(known)
    state = state_for(store, env)
    assert state.trust_ok and state.fingerprint.startswith("SHA256:")


def test_diagnostics_mark_environment_verified_and_expose_agent(store, ssh_ok):
    env = store.save("environments", {"name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab", "ssh_auth_mode": "key"})
    runtime.write_json(store.path.parent / "diagnostics" / (env["id"] + ".json"),
                       {"ssh_version": "OpenSSH_9", "agent_status": "no keys loaded", "identityfiles": ["~/.ssh/id"],
                        "proxyjump": "none", "checked_at": iso(hours=1)})
    state = state_for(store, env)
    assert state.agent_checked and state.identity == ("~/.ssh/id",)
    # key mode + empty agent is worth attention; it is not an error
    assert state.tone == "warn"


def test_failed_latest_session_and_missing_openssh_are_errors(store, monkeypatch):
    def missing(name="ssh"):
        raise RuntimeError("Install Windows OpenSSH Client optional feature")
    monkeypatch.setattr(ssh_security, "executable", missing)
    env = store.save("environments", {"name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})
    state = state_for(store, env)
    assert state.openssh_missing and state.tone == "danger" and state.status_text == "異常"

    monkeypatch.setattr(ssh_security, "executable", lambda name="ssh": "ssh.exe")
    account = store.save("accounts", {"name": "a", "tool": "codex", "auth": "subscription"})
    store.save("sessions", {"id": "ok1", "account_id": account["id"], "environment_id": env["id"], "name": "n", "path": "/p",
                            "status": "running", "updated_at": iso(hours=5)})
    store.save("sessions", {"id": "bad1", "account_id": account["id"], "environment_id": env["id"], "name": "n", "path": "/p",
                            "status": "failed", "updated_at": iso(hours=1)})
    assert state_for(store, env).tone == "danger"
    # a later successful session supersedes the failure
    store.save("sessions", {"id": "ok2", "account_id": account["id"], "environment_id": env["id"], "name": "n", "path": "/p",
                            "status": "running", "updated_at": iso(minutes=1)})
    assert state_for(store, env).tone == "ok"


def test_tool_reports_become_semantic_chips(store):
    local = store.get("environments", "local")
    write_report(store.path.parent, "local",
                 report("codex", "0.158.0", "0.160.0", "update_available", can_update=True),
                 report("claude", "2.1.4", "2.1.4", "current"))
    state = state_for(store, local)
    chips = {c.key: c for c in state.chips}
    assert chips["codex"].tone == "info" and "可更新" in chips["codex"].label
    assert chips["claude"].tone == "ok" and "2.1.4" in chips["claude"].label
    # an available update is informational: the environment itself is still ready
    assert state.tone == "ok"

    write_report(store.path.parent, "local", report("codex", "0.158.0", "0.160.0", "update_failed", message="boom"),
                 report("claude", "unknown", "2.1.4", "unknown", message="missing"))
    state = state_for(store, local)
    assert state.tone == "warn"  # the failed update, not the missing Claude CLI
    tones = {c.key: c.tone for c in state.chips}
    assert tones["codex"] == "danger"
    assert tones["claude"] == "unknown" and "未安裝" in {c.key: c.label for c in state.chips}["claude"]


def test_last_used_is_the_newest_of_projects_and_sessions(store):
    account = store.save("accounts", {"name": "a", "tool": "codex", "auth": "subscription"})
    store.save("presets", {"name": "p", "account_id": account["id"], "environment_id": "local", "path": "C:/p", "last_used": iso(days=3)})
    store.save("sessions", {"id": "s", "account_id": account["id"], "environment_id": "local", "name": "n", "path": "C:/p",
                            "status": "running", "updated_at": iso(minutes=12)})
    state = state_for(store, store.get("environments", "local"))
    assert format_relative(state.last_used) == "12 分鐘前"


def test_relative_time_buckets():
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    ago = lambda **d: (now - timedelta(**d)).isoformat()
    assert format_relative("", now) == "尚未使用"
    assert format_relative(ago(seconds=20), now) == "剛剛"
    assert format_relative(ago(minutes=5), now) == "5 分鐘前"
    assert format_relative(ago(hours=3), now) == "3 小時前"
    assert format_relative(ago(days=1, hours=1), now) == "昨天"
    assert format_relative(ago(days=4), now) == "4 天前"


def test_filters_and_pinned_first_ordering(store, ssh_ok):
    a = state_for(store, store.save("environments", {"name": "Alpha", "kind": "ssh", "ssh_target": "a"}))
    z = state_for(store, store.save("environments", {"name": "Zulu", "kind": "ssh", "ssh_target": "z"}), pinned=True)
    local = state_for(store, store.get("environments", "local"))
    assert [s.name for s in sort_states([a, local, z])][0] == "Zulu"
    assert matches_filter(local, "local") and not matches_filter(local, "ssh")
    assert matches_filter(local, "ready") and not matches_filter(a, "ready")
    assert not matches_filter(local, "attention")


# ----------------------------------------------------------------------------- window


@pytest.fixture
def window(tmp_path, qtbot, ssh_ok):
    store = Store(tmp_path / "gui.db")
    account = store.save("accounts", {"name": "工作帳號", "tool": "codex", "auth": "subscription"})
    gpu = store.save("environments", {"name": "GPU 工作站", "kind": "ssh", "ssh_target": "gpu-lab",
                                      "ssh_auth_mode": "password", "remember_seconds": 900})
    store.save("environments", {"name": "測試機", "kind": "ssh", "ssh_target": "dev@192.0.2.10"})
    service = FakeService()
    view = MainWindow(store, service)
    qtbot.addWidget(view)
    view.resize(1280, 800)
    view.show_page("environments")
    view.show()
    return view, store, service, account, gpu


def rows(view):
    lst = view.environments_list
    return [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count()) if lst.item(i).data(Qt.ItemDataRole.UserRole)]


def test_environments_render_as_cards_and_first_is_selected(window):
    view, store, *_ = window
    assert len(rows(view)) == 3
    assert set(view.env_page._cards) == set(rows(view))
    assert view.environments_list.currentItem() is not None
    card = view.env_page._cards[view._selected_record("environments")["id"]]
    assert "狀態" in card.accessibleName()


def test_search_and_filters_never_act_on_a_hidden_environment(window):
    view, store, service, *_ = window
    view.env_page.search.setText("zzz-nothing")
    assert rows(view) == []
    assert view.env_page.list_stack.currentWidget() is view.env_page.no_match
    assert view._selected_record("environments") == {}
    view.doctor_environment()
    view.check_tool_versions()
    assert service.calls == []
    view.env_page.clear_filters()
    assert len(rows(view)) == 3

    view.env_page.filters.set_current("local")
    assert [view._by_id("environments", i)["kind"] for i in rows(view)] == ["local"]
    view.env_page.filters.set_current("all")
    view.env_page.search.setText("gpu-lab")
    assert [view._by_id("environments", i)["name"] for i in rows(view)] == ["GPU 工作站"]


def test_filter_counts_reflect_search(window):
    view, *_ = window
    chips = view.env_page.filters.chips
    assert chips["all"]._count == 3 and chips["ssh"]._count == 2 and chips["local"]._count == 1
    view.env_page.search.setText("測試")
    assert chips["all"]._count == 1


def test_pin_reorders_and_is_remembered_across_restarts(window, qtbot):
    view, store, service, account, gpu = window
    test_machine = next(e for e in store.list("environments") if e["name"] == "測試機")
    view.toggle_pin(test_machine["id"])
    assert rows(view)[0] == test_machine["id"]
    assert (store.path.parent / "ui-state.json").is_file()
    again = MainWindow(store, FakeService())
    qtbot.addWidget(again)
    again.show_page("environments")
    assert rows(again)[0] == test_machine["id"]
    again.toggle_pin(test_machine["id"])
    assert test_machine["id"] not in again.prefs.pinned


def test_sidebar_collapse_is_animated_state_and_remembered(window, qtbot):
    view, store, *_ = window
    assert view.sidebar.width() == 232
    view.sidebar.set_collapsed(True, animate=False)
    assert view.sidebar.width() == 64
    assert view.nav_buttons["environments"].toolTip() != "", "collapsed rail must expose tooltips"
    view._remember_sidebar(True)
    again = MainWindow(store, FakeService())
    qtbot.addWidget(again)
    again.resize(1280, 800)
    again.show()
    assert again.sidebar.is_collapsed() and again.sidebar.width() == 64
    view.sidebar.set_collapsed(False, animate=False)
    assert view.sidebar.width() == 232 and view.nav_buttons["environments"].toolTip() == ""


def test_layout_modes_follow_window_width(window, qtbot):
    view, *_ = window
    view.resize(1300, 800)
    assert view.env_page.body.mode() == "split" and view.sidebar.width() == 232
    view.resize(1100, 800)
    assert view.env_page.body.mode() == "split" and view.sidebar.width() == 200
    view.resize(900, 700)
    assert view.sidebar.width() == 64 and view.env_page.body.mode() == "split"
    view.resize(700, 640)
    assert view.env_page.body.mode() == "drawer"
    assert not view.env_page.body.drawer_open()
    view.env_page.body.open_drawer()
    assert view.env_page.body.drawer_open()
    qtbot.keyClick(view.environments_list, Qt.Key.Key_Escape)
    assert not view.env_page.body.drawer_open()


@pytest.mark.parametrize("size", [(1280, 800), (1000, 700), (800, 600), (640, 520)])
def test_no_horizontal_overflow_with_long_text(tmp_path, qtbot, ssh_ok, size):
    store = Store(tmp_path / "gui.db")
    store.save("environments", {"name": "實驗室跳板機（國立臺灣大學資訊工程學系研究群長名稱測試環境）" * 2, "kind": "ssh",
                                "ssh_target": "researcher@very-long-hostname.cs.example.edu.tw"})
    view = MainWindow(store, FakeService())
    qtbot.addWidget(view)
    view.resize(*size)
    view.show_page("environments")
    view.show()
    qtbot.wait(50)
    lst = view.environments_list
    assert lst.horizontalScrollBar().maximum() == 0
    for card in view.env_page._cards.values():
        assert card.width() <= lst.viewport().width()
    assert view.centralWidget().width() <= view.width()


def test_delete_environment_requires_confirmation_and_clears_secret(window, monkeypatch):
    view, store, service, account, gpu = window
    revoked = []
    from vibe_launcher import askpass
    monkeypatch.setattr(askpass, "revoke_password", lambda environment_id, root: revoked.append(environment_id))
    view.env_page.select(gpu["id"])
    asked = []
    monkeypatch.setattr(view, "_confirm", lambda title, text, **kw: asked.append((title, text, kw)) or False)
    view.delete_record("environments")
    assert asked and asked[0][2].get("danger") is True and "GPU 工作站" in asked[0][1]
    assert store.get("environments", gpu["id"]) and revoked == []
    monkeypatch.setattr(view, "_confirm", lambda title, text, **kw: True)
    view.delete_record("environments")
    assert revoked == [gpu["id"]]
    with pytest.raises(KeyError):
        store.get("environments", gpu["id"])
    assert gpu["id"] not in rows(view)


def test_builtin_local_environment_cannot_be_deleted(window, monkeypatch):
    view, store, *_ = window
    view.env_page.select("local")
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: pytest.fail("must not even ask"))
    view.delete_record("environments")
    assert store.get("environments", "local")
    state = view.env_page.state_for("local")
    menu = view.env_page._env_actions(state)
    delete = next(a for a in menu if a.danger)
    assert delete.enabled is False


def test_use_environment_filters_projects_or_prefills_new_project(window, monkeypatch):
    view, store, service, account, gpu = window
    view.env_page.select(gpu["id"])
    seeds = []
    monkeypatch.setattr(view, "add_project", lambda seed=None: seeds.append(seed) or False)
    view.use_environment()
    assert seeds == [{"environment_id": gpu["id"]}]

    store.save("presets", {"name": "工程分析", "account_id": account["id"], "environment_id": gpu["id"], "path": "C:/專案"})
    view.reload()
    view.use_environment()
    assert view.current_page() == "projects"
    assert view._project_env_filter == gpu["id"]
    assert view.project_list.count() == 1
    view._set_project_env_filter(None)
    assert not view.project_chip_row.isVisible()


def test_enter_and_arrow_keys_drive_the_list(window, qtbot, monkeypatch):
    view, store, service, account, gpu = window
    monkeypatch.setattr(view, "add_project", lambda seed=None: False)  # Enter = primary action; don't open the real dialog
    lst = view.environments_list
    lst.setFocus()
    first = view._selected_record("environments")["id"]
    qtbot.keyClick(lst, Qt.Key.Key_Down)
    second = view._selected_record("environments")["id"]
    assert second != first
    qtbot.keyClick(lst, Qt.Key.Key_Up)
    assert view._selected_record("environments")["id"] == first
    seen = []
    view.env_page.primary_requested.connect(seen.append)
    qtbot.keyClick(lst, Qt.Key.Key_Return)
    assert seen == [first]


def test_checking_state_shows_until_the_report_is_written(window, qtbot):
    view, store, service, *_ = window
    view.env_page.select("local")
    view.check_tool_versions()
    assert service.calls[-1] == ("check-tools", {"environment_id": "local"})
    detail = view.env_page.detail
    assert detail.tool_rows["codex"].badge.text() == "檢查中"
    assert not detail.check_tools.isEnabled()
    write_report(store.path.parent, "local", report("codex", "0.160.0", "0.160.0", "current"),
                 report("claude", "2.1.4", "2.1.4", "current"))
    view._poll()
    assert "local" not in view._pending
    assert detail.tool_rows["codex"].badge.text() == "已是最新"
    assert detail.check_tools.isEnabled()
    assert any("CLI 版本檢查完成" in t.text.text() for t in view.toasts.toasts)


def test_update_button_only_appears_when_an_update_helps(window, qtbot):
    view, store, *_ = window
    view.env_page.select("local")
    write_report(store.path.parent, "local", report("codex", "0.158.0", "0.160.0", "update_available", can_update=True),
                 report("claude", "2.1.4", "2.1.4", "current"))
    view._poll()
    rows_ = view.env_page.detail.tool_rows
    assert rows_["codex"].update_button.isVisibleTo(view.env_page.detail) and rows_["codex"].badge.tone() == "info"
    assert not rows_["claude"].update_button.isVisibleTo(view.env_page.detail)
    # the capability stays reachable from the section menu and the command palette
    assert "更新 Codex" in [c.title for c in view._commands()]


def test_security_block_describes_credential_state_without_secrets(window):
    view, store, service, account, gpu = window
    view.env_page.select(gpu["id"])
    sec = view.env_page.detail.security_status
    assert "Credential Manager" in sec.title.text()
    assert sec.lock_button.isVisibleTo(view.env_page.detail)
    assert "15 分鐘" in sec.subtitle.text()
    other = next(e for e in store.list("environments") if e["name"] == "測試機")
    view.env_page.select(other["id"])
    assert "OTP" in sec.subtitle.text() and not sec.lock_button.isVisibleTo(view.env_page.detail)
    assert not sec.note_row.isVisibleTo(view.env_page.detail), "the Credential Manager caveat only matters when a password can be saved"
    assert "同一 Windows 使用者" in view.security_status_label.text()
    view.env_page.select("local")
    assert "本機" in sec.title.text()


def test_empty_environment_state(window):
    view, *_ = window
    view.env_page.set_states([], {})
    assert view.env_page.page_stack.currentWidget() is view.env_page.empty
    assert "尚未建立執行環境" in view.env_page.empty.title.text()
    assert view.env_page.empty.primary.text() == "新增第一個環境"
    view.reload()
    assert view.env_page.page_stack.currentWidget() is view.env_page.body


def test_command_palette_filters_and_runs(window, qtbot):
    view, store, service, account, gpu = window
    view.env_page.select("local")
    view.open_palette()
    qtbot.waitUntil(lambda: view.palette.isVisible())
    titles = [c.title for c in view.palette.visible_commands()]
    assert "快速新增環境" in titles and "更新 Claude" in titles and "切換到 GPU 工作站" in titles
    view.palette.input.setText("gpu")
    assert [c.title for c in view.palette.visible_commands()] == ["切換到 GPU 工作站"]
    qtbot.keyClick(view.palette.input, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: view._selected_record("environments").get("id") == gpu["id"])
    assert not view.palette.isVisible()
    ssh_only = next(c for c in view._commands() if c.id == "env-ssh")
    view.env_page.select("local")
    assert next(c for c in view._commands() if c.id == "env-ssh").enabled is False and ssh_only.enabled is True


def test_toasts_stack_and_error_text_is_not_leaked(window):
    view, *_ = window
    for n in range(5):
        view._toast(f"訊息 {n}")
    assert len(view.toasts.toasts) <= 3
    view._error("資料操作失敗", RuntimeError("secret-token-123"))
    last = view.toasts.toasts[-1]
    assert "secret-token-123" not in last.text.text() and "RuntimeError" in last.text.text()


def test_status_badges_always_carry_text_and_an_icon(window):
    view, *_ = window
    for card in view.env_page._cards.values():
        assert card.badge.text() and card.badge.tone() in {"ok", "warn", "danger", "unknown", "info"}
        for chip in card.state.chips:
            assert chip.label and chip.icon and chip.tone in {"ok", "warn", "danger", "unknown", "info"}


def test_tool_report_text_keeps_every_diagnostic_field(store):
    write_report(store.path.parent, "local", report("codex", "0.160.0", "0.161.0", "update_available", channel="latest/stable",
                                                    candidate="0.160.0", publisher="vendor", message="ok"))
    (r,) = env_state.load_tool_reports(store.path.parent, "local")
    text = env_state.format_tool_report(r)
    for needle in ("0.160.0", "0.161.0", "C:/tools/codex", "latest/stable", "vendor", "update_available"):
        assert needle in text


# ----------------------------------------------------------------------------- calm CLI wording, credential block, running vs stopped


def test_no_update_detected_reads_as_up_to_date_never_as_a_warning(store):
    local = store.get("environments", "local")
    # latest-version lookup failed ("unknown") but the installed version is known
    write_report(store.path.parent, "local", report("codex", "0.160.0", "unknown", "unknown"),
                 report("claude", "2.1.4", "2.1.4", "current"))
    state = state_for(store, local)
    chips = {c.key: c for c in state.chips}
    assert chips["codex"].tone == "ok" and "0.160.0" in chips["codex"].label
    assert state.tone == "ok"
    # a missing CLI is neutral information, not a yellow alarm, and does not change the environment status
    write_report(store.path.parent, "local", report("codex", "0.160.0", "0.160.0", "current"),
                 report("claude", "unknown", "unknown", "unknown", message="Cannot determine installed CLI version"))
    state = state_for(store, local)
    assert {c.key: c.tone for c in state.chips}["claude"] == "unknown" and state.tone == "ok"


def test_tool_rows_show_up_to_date_without_update_button(window):
    view, store, *_ = window
    view.env_page.select("local")
    write_report(store.path.parent, "local", report("codex", "0.160.0", "unknown", "unknown"),
                 report("claude", "unknown", "unknown", "unknown", message="x"))
    view._poll()
    rows_ = view.env_page.detail.tool_rows
    assert rows_["codex"].badge.text() == "已是最新" and rows_["codex"].badge.tone() == "ok"
    assert not rows_["codex"].update_button.isVisibleTo(view.env_page.detail)
    assert rows_["claude"].badge.text() == "未安裝" and rows_["claude"].badge.tone() == "unknown"


def test_persistent_credential_offers_a_way_to_shorten_it(window, monkeypatch):
    view, store, service, account, gpu = window
    forever = store.save("environments", {"name": "長期保存", "kind": "ssh", "ssh_target": "x", "ssh_auth_mode": "password",
                                          "remember_seconds": -1})
    view.reload()
    view.env_page.select(forever["id"])
    sec = view.env_page.detail.security_status
    assert "持續保存" in sec.title.text() and sec.adjust_button.isVisibleTo(view.env_page.detail)
    assert sec.lock_button.isVisibleTo(view.env_page.detail)
    edited = []
    monkeypatch.setattr(view, "_edit_record", lambda kind, record=None: edited.append((kind, record["id"])))
    sec.adjust_button.click()
    assert edited == [("environments", forever["id"])]


def test_lock_and_forget_are_offered_once_in_the_menu(window):
    view, store, service, account, gpu = window
    state = view.env_page.state_for(gpu["id"])
    labels = [a.text for a in view.env_page._env_actions(state)]
    assert sum("Credential" in t for t in labels) == 1
    assert not any("忘記" in t for t in labels)


def test_running_and_stopped_are_visually_distinct(window):
    view, store, service, account, gpu = window
    from vibe_launcher.kit import BADGE_ROLE
    other = next(e for e in store.list("environments") if e["name"] == "測試機")
    store.save("sessions", {"id": "run1", "account_id": account["id"], "environment_id": gpu["id"], "name": "運行的工作",
                            "path": "/a", "status": "running", "updated_at": iso(minutes=1)})
    store.save("sessions", {"id": "bg1", "account_id": account["id"], "environment_id": gpu["id"], "name": "背景的工作",
                            "path": "/b", "status": "detached", "updated_at": iso(minutes=2)})
    store.save("sessions", {"id": "end1", "account_id": account["id"], "environment_id": other["id"], "name": "停止的工作",
                            "path": "/c", "status": "ended", "updated_at": iso(minutes=3)})
    store.save("sessions", {"id": "bad1", "account_id": account["id"], "environment_id": other["id"], "name": "失敗的工作",
                            "path": "/d", "status": "failed", "updated_at": iso(minutes=4)})
    view.reload()
    badges = {}
    for row in range(view.sessions_list.count()):
        item = view.sessions_list.item(row)
        if item.data(Qt.ItemDataRole.UserRole) is not None:      # skip project headings
            badges[item.data(Qt.ItemDataRole.UserRole)] = item.data(BADGE_ROLE)
    assert tuple(badges["run1"]) == ("運行中", "ok")
    assert tuple(badges["bg1"]) == ("背景執行", "info")
    assert tuple(badges["end1"]) == ("已停止", "unknown")
    assert tuple(badges["bad1"]) == ("失敗", "danger")
    assert len({tuple(b)[1] for b in badges.values()}) == 4, "running / background / stopped / failed need four different looks"
    # the environment card says it is running; the idle one does not
    running_state = view.env_page.state_for(gpu["id"])
    assert running_state.running_sessions == 2
    assert any(c.key == "sessions" and c.tone == "ok" and "2" in c.label for c in running_state.chips)
    assert view.env_page.state_for(other["id"]).running_sessions == 0
    assert not any(c.key == "sessions" for c in view.env_page.state_for(other["id"]).chips)
