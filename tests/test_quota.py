"""Account quota: Codex subscription windows via `codex app-server`; nothing for API/endpoint accounts."""
import json
import sys
import textwrap
from datetime import datetime, timedelta

import pytest
from PySide6.QtWidgets import QLabel

from vibe_launcher import runtime
from vibe_launcher import quota_panel as qp
from vibe_launcher import runtime as runtime_module
from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store
from vibe_launcher.ui import VibeLauncherWindow as MainWindow

SAMPLE = {"rateLimits": {"limitId": "codex", "planType": "plus",
                         "primary": {"usedPercent": 11, "windowDurationMins": 300, "resetsAt": 1791131403},
                         "secondary": {"usedPercent": 54, "windowDurationMins": 10080, "resetsAt": 1791614832},
                         "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                         "rateLimitReachedType": None},
          "rateLimitResetCredits": {"availableCount": 3, "credits": []}, "accountId": "never-shown"}

FAKE_SERVER = textwrap.dedent('''
    import json, sys
    mode = sys.argv[1]
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("method") == "initialize":
            # a server->client request that happens to reuse id 2 must not be taken for our answer
            print(json.dumps({"id": 2, "method": "account/chatgptAuthTokens/refresh", "params": {}}), flush=True)
            print(json.dumps({"id": message["id"], "result": {"userAgent": "x"}}), flush=True)
        elif message.get("method") == "account/rateLimits/read":
            if mode == "error":
                print(json.dumps({"id": message["id"], "error": {"code": -32600, "message": "codex account authentication required to read rate limits"}}), flush=True)
            elif mode == "silent":
                continue
            else:
                print(json.dumps({"id": message["id"], "result": json.loads(sys.argv[2])}), flush=True)
''')


@pytest.fixture
def server(tmp_path):
    path = tmp_path / "fake_server.py"
    path.write_text(FAKE_SERVER, encoding="utf-8")
    return path


# ------------------------------------------------------------------------------------- runtime
def test_summary_keeps_both_windows_and_drops_the_account_id():
    summary = runtime.quota_summary(SAMPLE)
    assert [(w["slot"], w["used_percent"], w["window_minutes"], w["resets_at"]) for w in summary["windows"]] == [
        ("primary", 11, 300, 1791131403), ("secondary", 54, 10080, 1791614832)]
    assert summary["plan"] == "plus" and summary["reset_credits"] == 3
    assert "never-shown" not in json.dumps(summary)


def test_summary_survives_missing_or_odd_fields():
    assert runtime.quota_summary({})["windows"] == []
    odd = runtime.quota_summary({"rateLimits": {"primary": {"usedPercent": 250}, "secondary": {"usedPercent": "x"}}})
    assert [w["used_percent"] for w in odd["windows"]] == [100]


@pytest.mark.parametrize("used", [None, True, False, "25", float("nan"), float("inf"), -float("inf")])
def test_summary_unknown_usage_is_not_available_capacity(used):
    summary = runtime.quota_summary({"rateLimits": {"primary": {"usedPercent": used}}})
    assert summary["windows"] == []
    assert summary["buckets"]["codex"]["windows"] == []
    json.dumps(summary, allow_nan=False)


@pytest.mark.parametrize("value", [True, False, -1, 0, "300", 3.5])
def test_summary_rejects_invalid_window_duration_and_reset(value):
    summary = runtime.quota_summary({"rateLimits": {"primary": {
        "usedPercent": 25, "windowDurationMins": value, "resetsAt": value}}})
    assert summary["windows"][0]["window_minutes"] is None
    assert summary["windows"][0]["resets_at"] is None


def test_summary_preserves_separate_buckets_without_mixing_legacy_windows():
    summary = runtime.quota_summary({**SAMPLE, "rateLimitsByLimitId": {
        "codex": {"primary": {"usedPercent": 90, "windowDurationMins": 300}},
        "review": {"limitName": "Code review", "secondary": {"usedPercent": 20},
                   "rateLimitReachedType": "weekly", "accountId": "never-shown"},
        "unknown": {}, "bad": None}})
    assert set(summary["buckets"]) == {"codex", "review", "unknown"}
    assert summary["buckets"]["codex"]["windows"][0]["used_percent"] == 90
    assert summary["buckets"]["review"]["windows"][0]["used_percent"] == 20
    assert summary["buckets"]["review"]["name"] == "Code review"
    assert summary["buckets"]["review"]["reached"] == "weekly"
    assert summary["buckets"]["unknown"]["windows"] == []
    assert summary["windows"][0]["used_percent"] == 11
    assert "never-shown" not in json.dumps(summary)


def test_summary_legacy_snapshot_still_provides_a_bucket():
    summary = runtime.quota_summary(SAMPLE)
    assert summary["buckets"]["codex"]["windows"] == summary["windows"]
    assert runtime.quota_summary({})["buckets"] == {}


def test_rpc_returns_the_answer_even_when_the_server_sends_its_own_requests(server):
    result = runtime.rpc_rate_limits([sys.executable, str(server), "ok", json.dumps(SAMPLE)], {}, timeout=20)
    assert result["rateLimits"]["primary"]["usedPercent"] == 11


def test_rpc_turns_a_server_error_into_a_readable_message(server):
    with pytest.raises(RuntimeError, match="authentication required"):
        runtime.rpc_rate_limits([sys.executable, str(server), "error"], {}, timeout=20)


def test_rpc_gives_up_when_the_server_is_silent(server):
    with pytest.raises(RuntimeError, match="in time"):
        runtime.rpc_rate_limits([sys.executable, str(server), "silent"], {}, timeout=1.5)


def test_only_codex_subscriptions_have_a_quota(tmp_path):
    api = {"id": "a", "name": "A", "tool": "codex", "auth": "api", "model": "m"}
    claude = {"id": "c", "name": "C", "tool": "claude", "auth": "subscription"}
    assert not runtime.quota_supported(api) and not runtime.quota_supported(claude)
    with pytest.raises(ValueError, match="only available"):
        runtime.fetch_quota(api, tmp_path)


def test_the_service_refuses_a_quota_job_for_an_api_account(tmp_path):
    store = Store(tmp_path / "s.db")
    api = store.save("accounts", {"name": "A", "tool": "codex", "auth": "api", "model": "m"})
    with pytest.raises(ValueError, match="only available"):
        LauncherService(store).start("quota", account_id=api["id"], environment_id="local")


def test_quota_files_are_per_account_and_environment(tmp_path):
    assert runtime.quota_file(tmp_path, "a1", "local") != runtime.quota_file(tmp_path, "a1", "gpu")
    with pytest.raises(ValueError):
        runtime.quota_file(tmp_path, "../x", "local")


# ------------------------------------------------------------------------------------- wording
def test_window_labels():
    assert [qp.window_label(m) for m in (300, 10080, 1440, 90, None)] == ["5 小時", "每週", "1 天", "90 分鐘", "用量"]


def test_reset_text_shows_clock_time_and_countdown():
    now = datetime(2026, 10, 4, 18, 0).astimezone()
    soon = (now + timedelta(hours=3, minutes=51)).timestamp()
    text = qp.reset_text(int(soon), now)
    assert "3 小時 51 分後" in text and text.startswith("今天 21:51") and "重置" in text
    assert qp.reset_text(int((now + timedelta(days=2)).timestamp()), now).startswith((now + timedelta(days=2)).strftime("%m/%d"))
    assert "已重置" in qp.reset_text(int((now - timedelta(minutes=5)).timestamp()), now)
    assert qp.reset_text(None, now) == "重置時間未提供"


def test_bar_turns_warm_as_the_allowance_runs_out():
    from vibe_launcher.theme import C
    assert (qp.bar_color(10), qp.bar_color(75), qp.bar_color(95)) == (C.accent, C.warning, C.danger)


# ------------------------------------------------------------------------------------- UI
class FakeService:
    supports_spawn = False

    def __init__(self):
        self.calls = []

    def start(self, action, **kwargs):
        self.calls.append((action, kwargs))


@pytest.fixture
def view(tmp_path, qtbot):
    store = Store(tmp_path / "gui.db")
    store.save("accounts", {"id": "codexsub", "name": "Codex 訂閱", "tool": "codex", "auth": "subscription"})
    store.save("accounts", {"id": "codexapi", "name": "Codex API", "tool": "codex", "auth": "api", "model": "m"})
    store.save("accounts", {"id": "claudesub", "name": "Claude 訂閱", "tool": "claude", "auth": "subscription"})
    store.save("environments", {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})
    window = MainWindow(store, FakeService())
    qtbot.addWidget(window)
    window.resize(1100, 800)
    window.show_page("accounts")
    window.show()
    return window


def select(view, account_id):
    view.accounts_page.select("accounts", account_id)


def texts(panel):
    return " | ".join(label.text() for label in panel.findChildren(QLabel))


def test_multi_bucket_panel_keeps_limits_separate_and_omits_legacy_duplicate(qtbot):
    panel = qp.QuotaPanel()
    qtbot.addWidget(panel)
    report = runtime.quota_summary({**SAMPLE, "rateLimitsByLimitId": {
        "codex": {"primary": {"usedPercent": 90, "windowDurationMins": 300}},
        "review": {"limitName": "Code review", "secondary": {"usedPercent": 20}},
        "unknown": {"limitName": "<b>Unknown</b>"}}})
    panel.show_account({"id": "a", "tool": "codex", "auth": "subscription"},
                       [{"id": "local", "name": "Local"}], report)
    shown = texts(panel)
    assert "codex" in shown and "Code review (review)" in shown
    assert "已用 90%" in shown and "已用 20%" in shown
    assert "已用 11%" not in shown and "已用 54%" not in shown
    assert len(panel.findChildren(qp.UsageBar)) == 2
    assert all(bar.accessibleName().startswith(prefix) for bar, prefix in
               zip(panel.findChildren(qp.UsageBar), ("codex:", "Code review (review):")))
    heading = next(item for item in panel.findChildren(QLabel) if "<b>Unknown</b>" in item.text())
    assert heading.textFormat() == qp.Qt.TextFormat.PlainText
    panel.resize(640, panel.sizeHint().height())
    assert not panel.grab().isNull()


def test_legacy_cached_report_without_buckets_still_renders(qtbot):
    panel = qp.QuotaPanel()
    qtbot.addWidget(panel)
    report = runtime.quota_summary(SAMPLE)
    report.pop("buckets")
    panel.show_account({"id": "a", "tool": "codex", "auth": "subscription"},
                       [{"id": "local", "name": "Local"}], report)
    assert "已用 11%" in texts(panel) and "已用 54%" in texts(panel)


def test_api_accounts_show_no_quota_at_all(view):
    select(view, "codexapi")
    assert view.quota_panel.isHidden()


def test_claude_subscription_points_to_the_cli_instead(view):
    select(view, "claudesub")
    assert not view.quota_panel.isHidden()
    assert "/usage" in texts(view.quota_panel) and view.quota_panel.query_button.isHidden()


def test_codex_subscription_can_be_queried_and_shows_both_windows(view, qtbot, monkeypatch):
    summary = runtime.quota_summary(SAMPLE)
    monkeypatch.setattr(runtime_module, "fetch_quota", lambda account, root, env_id="local": dict(summary, account_id=account["id"], environment_id=env_id))
    select(view, "codexsub")
    panel = view.quota_panel
    assert not panel.isHidden() and "尚未查詢" in texts(panel)
    assert panel.environment.count() >= 2 and panel.selected_environment() == "local"
    panel.query_button.click()
    qtbot.waitUntil(lambda: "已用 54%" in texts(panel), timeout=8000)
    shown = texts(panel)
    assert "5 小時 額度" in shown and "已用 11%" in shown and "每週 額度" in shown and "剩 46%" in shown
    assert "Plus" in shown and "重置" in shown
    assert panel.query_button.isEnabled()


def test_a_missing_login_is_explained_not_dumped(view, qtbot, monkeypatch):
    def fail(account, root, env_id="local"):
        raise RuntimeError("Codex reported: codex account authentication required to read rate limits")
    monkeypatch.setattr(runtime_module, "fetch_quota", fail)
    select(view, "codexsub")
    view.quota_panel.query_button.click()
    qtbot.waitUntil(lambda: "尚未登入" in texts(view.quota_panel), timeout=8000)
    assert view.quota_panel.query_button.isEnabled()


def test_an_ssh_environment_is_queried_through_a_job_and_the_result_file(view, qtbot):
    select(view, "codexsub")
    panel = view.quota_panel
    panel.environment.setCurrentIndex(panel.environment.findData("gpu"))
    panel.query_button.click()
    assert view.service.calls[-1][0] == "quota"
    assert view.service.calls[-1][1] == {"account_id": "codexsub", "environment_id": "gpu"}
    assert not panel.query_button.isEnabled()                       # waiting for the job
    runtime.write_json(runtime.quota_file(view.store.path.parent, "codexsub", "gpu"), runtime.quota_summary(SAMPLE))
    view._poll()
    assert "已用 11%" in texts(panel) and panel.query_button.isEnabled()
