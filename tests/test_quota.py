"""Quota: Codex windows and caches, plans and their storage, block workflows, account checks.

- quota: Account quota: Codex subscription windows via `codex app-server`; nothing for API/endpoint accounts.
- quota policy
- quota flow: Block workflows: validation, evaluation, the templates and new-work account selection.
- account check: Account compatibility before a quota plan is saved: what the worker would refuse at a handoff, found ahead of time.
"""
import copy
import json
import sys
import textwrap
from datetime import datetime, timedelta

import pytest
from PySide6.QtWidgets import QLabel
from vibe_launcher import account_check, quota_panel as qp, runtime, runtime as runtime_module
from vibe_launcher.quota_policy import commit_cursor, Policy, PolicyStore, select as policy_select
from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store
from vibe_launcher.ui import VibeLauncherWindow as MainWindow


# --- quota ------------------------------------------------------------------------------------------------------------
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


# --- quota policy -----------------------------------------------------------------------------------------------------
def policy(**values):
    return Policy.from_dict(dict(id="flow", name="Flow", environment_id="local", account_ids=["a", "b"], **values))


ACCOUNTS = [{"id": key, "tool": "codex", "auth": "subscription", "environment_id": "local"} for key in ("a", "b")]


def snapshot(key, used, **values):
    return dict(tool="codex", account_id=key, environment_id="local", checked_at=1000,
                buckets={"codex": {"windows": [{"used_percent": used, "resets_at": 1400}]}}, **values)


def test_selection_uses_all_codex_windows_and_ignores_unrelated_products():
    first = snapshot("a", 20)
    first["buckets"]["codex"]["windows"].append({"used_percent": 95, "resets_at": 1700})
    second = snapshot("b", 30)
    second["buckets"]["other"] = {"windows": [{"used_percent": 100}]}
    result = policy_select(policy(), ACCOUNTS, {"a": first, "b": second}, "local", 1050)
    assert result.account_id == "b"
    assert result.remaining == 70


def test_sticky_account_and_round_robin_only_commit_after_success():
    quotas = {"a": snapshot("a", 40), "b": snapshot("b", 10)}
    flow = policy(strategy="round-robin")
    assert policy_select(flow, ACCOUNTS, quotas, "local", 1050, current_account_id="a", cursor="a").account_id == "a"
    assert policy_select(flow, ACCOUNTS, quotas, "local", 1050, cursor="a").account_id == "b"
    assert policy_select(flow, ACCOUNTS, quotas, "local", 1050, cursor="a").account_id == "b"
    assert commit_cursor(flow, "b") == "b"


def test_available_quota_and_threshold_boundary():
    quotas = {"a": snapshot("a", 90), "b": snapshot("b", 30)}
    assert policy_select(policy(strategy="available-quota"), ACCOUNTS, quotas, "local", 1050).account_id == "b"
    assert policy_select(policy(), ACCOUNTS, {"a": snapshot("a", 90)}, "local", 1050).account_id is None


def test_stale_unknown_and_cross_environment_are_excluded():
    assert policy_select(policy(), ACCOUNTS, {"a": snapshot("a", 10)}, "local", 1300).account_id is None
    bad = snapshot("a", 10)
    bad["environment_id"] = "remote"
    assert policy_select(policy(), ACCOUNTS, {"a": bad}, "local", 1050).account_id is None
    bad = snapshot("a", 10)
    bad["buckets"] = {"other": {"windows": [{"used_percent": 0}]}}
    assert policy_select(policy(), ACCOUNTS, {"a": bad}, "local", 1050).account_id is None


def test_wait_requires_known_reset_notify_has_no_retry():
    quotas = {"a": snapshot("a", 100)}
    assert policy_select(policy(), ACCOUNTS, quotas, "local", 1050).next_retry == 1400
    assert policy_select(policy(when_empty="notify-only"), ACCOUNTS, quotas, "local", 1050).next_retry is None
    assert policy_select(policy(), ACCOUNTS, {}, "local", 1050).next_retry is None


def test_server_reported_limit_overrides_spare_percent():
    quota = snapshot("a", 20)
    quota["buckets"]["codex"]["reached"] = "rate_limit"
    assert policy_select(policy(), ACCOUNTS, {"a": quota}, "local", 1050).account_id is None


def test_retry_waits_for_all_blocking_windows_and_requires_trusted_resets():
    quota = snapshot("a", 100)
    quota["buckets"]["codex"]["windows"].append({"used_percent": 95, "resets_at": 1700})
    assert policy_select(policy(), ACCOUNTS, {"a": quota}, "local", 1050).next_retry == 1700
    quota["buckets"]["codex"]["windows"][1]["resets_at"] = None
    assert policy_select(policy(), ACCOUNTS, {"a": quota}, "local", 1050).next_retry is None


def test_identifiers_and_file_limits(tmp_path):
    values = policy().to_dict()
    values["account_ids"] = ["../secret"]
    with pytest.raises(ValueError):
        Policy.from_dict(values)
    with pytest.raises(ValueError):
        PolicyStore(tmp_path).save({str(i): policy() for i in range(129)})


@pytest.mark.parametrize("values", [{"interval": 59}, {"threshold": float("nan")}, {"threshold": True},
                                    {"strategy": "invalid"}, {"enabled": 1}, {"when_empty": "auto"}])
def test_validation(values):
    with pytest.raises(ValueError):
        policy(**values)


def test_private_store_roundtrip_and_invalid_data(tmp_path):
    store = PolicyStore(tmp_path)
    assert store.load() == {}
    flow = policy()
    store.save({flow.id: flow})
    assert store.load() == {flow.id: flow}
    with pytest.raises(ValueError):
        store.save({"other": flow})


# --- quota flow -------------------------------------------------------------------------------------------------------
@pytest.fixture
def english(restore_controls):
    """These tests read English messages (the suite otherwise runs with the Traditional Chinese runtime catalog)."""
    from vibe_launcher import i18n, runtime as engine
    i18n.set_language("en")
    engine._MESSAGES = {}

FLOW_ACCOUNTS = [{"id": key, "name": key, "tool": "codex", "auth": "subscription", "environment_id": "local"}
            for key in ("a", "b", "c")]
POOL = ("a", "b", "c")


def flow(*edges, **nodes):
    """nodes: id=(type, extra); edges: (from, branch, to)."""
    items = [dict({"id": ident, "type": kind}, **extra) for ident, (kind, extra) in nodes.items()]
    return {"nodes": items, "edges": [{"from": a, "branch": b, "to": c} for a, b, c in edges]}


def codes(value, pool=POOL):
    return sorted({code for _, code in runtime.flow_issues(value, pool)})


@pytest.mark.usefixtures("english")
@pytest.mark.parametrize("strategy", runtime.FLOW_STRATEGIES)
@pytest.mark.parametrize("empty", ["wait", "notify-only"])
def test_templates_are_valid_flows(strategy, empty):
    template = runtime.quota_template(strategy, POOL, empty)
    assert runtime.flow_issues(template, POOL) == []
    policy = runtime.checked_quota_policy({"id": "p", "name": "P", "environment_id": "local", "account_ids": list(POOL),
                                            "flow": template})
    assert runtime.quota_flow(policy) == template


GOOD = dict(start=("start", {}), low=("condition", {"test": "low"}), pick=("select", {"accounts": ["b"], "strategy": "priority"}),
            handoff=("handoff", {}), wait=("wait", {}), keep=("keep", {}))
GOOD_EDGES = [("start", "next", "low"), ("low", "yes", "pick"), ("low", "no", "keep"), ("pick", "found", "handoff"),
              ("pick", "none", "wait")]


@pytest.mark.usefixtures("english")
@pytest.mark.parametrize("change, expected", [
    (lambda n, e: e.pop(), ["unconnected"]),                                                   # a branch left open
    (lambda n, e: e.append(("low", "yes", "wait")), ["edge"]),                                 # two edges on one branch
    (lambda n, e: n.update(more=("notify", {})), ["unreachable"]),
    (lambda n, e: (e.remove(("pick", "found", "handoff")), e.append(("pick", "found", "wait"))), ["handoff"]),
    (lambda n, e: (e.remove(("low", "no", "keep")), e.append(("low", "no", "handoff"))), ["handoff"]),
    (lambda n, e: n.update(pick=("select", {"accounts": ["z"], "strategy": "priority"})), ["accounts"]),
    (lambda n, e: n.update(pick=("select", {"accounts": ["b"], "strategy": "random"})), ["strategy"]),
    (lambda n, e: n.update(low=("condition", {"test": "always"})), ["test"]),
    (lambda n, e: n.pop("handoff"), ["edge", "handoff", "unconnected"]),                         # the safe handoff stays
    (lambda n, e: (e.remove(("start", "next", "low")), e.append(("start", "next", "pick"))), ["unguarded", "unreachable"]),
])
def test_every_flow_problem_is_reported(change, expected):
    nodes, edges = dict(GOOD), list(GOOD_EDGES)
    assert codes(flow(*edges, **nodes)) == []
    change(nodes, edges)
    assert codes(flow(*edges, **nodes)) == expected


SAME = dict(start=("start", {}), out=("condition", {"test": "exhausted"}), wait=("wait", {}), keep=("keep", {}))
SAME_EDGES = [("start", "next", "out"), ("out", "yes", "wait"), ("out", "no", "keep")]


@pytest.mark.usefixtures("english")
def test_a_flow_without_account_selection_needs_no_safe_handoff():
    same = flow(*SAME_EDGES, **SAME)
    assert codes(same) == []
    assert runtime.flow_outcome(same, {"exhausted": True}.get, lambda *args: None) == ("wait", None)
    assert runtime.flow_outcome(same, {"exhausted": False}.get, lambda *args: None) == ("keep", None)
    assert codes(flow(*SAME_EDGES, **dict(SAME, handoff=("handoff", {})))) == ["handoff"]    # nothing can reach it
    assert codes(flow(*GOOD_EDGES, **dict(GOOD, again=("handoff", {})))) == ["handoff"]       # only one handoff


@pytest.mark.usefixtures("english")
def test_the_template_of_auto_continue_is_valid_and_switches_nothing():
    from vibe_launcher.quota_policy import AUTO_CONTINUE, TEMPLATES, template_plan
    assert AUTO_CONTINUE not in TEMPLATES                       # the switching templates keep their strategy names
    plan = template_plan("p", "Auto", AUTO_CONTINUE, "local", POOL)
    assert runtime.flow_issues(plan.flow, plan.account_ids) == [] and plan.strategy == "priority"
    assert {node["type"] for node in plan.flow["nodes"]} == {"start", "condition", "wait", "keep"}
    assert runtime.flow_outcome(plan.flow, {"exhausted": True}.get, lambda *args: "b") == ("wait", None)
    with pytest.raises(ValueError):
        template_plan("p", "Bad", "auto-switch", "local", POOL)


@pytest.mark.usefixtures("english")
@pytest.mark.parametrize("kind, branch, target, same, expected", [
    ("condition", "yes", "wait", False, None),
    ("select", "found", "handoff", False, None),
    ("select", "none", "notify", False, None),
    ("select", "found", "wait", False, "handoff"),          # a found account goes through the safe handoff ...
    ("condition", "no", "handoff", False, "handoff"),       # ... and nothing else does
    ("condition", "found", "keep", False, "edge"),          # not a branch of a condition
    ("wait", "next", "keep", False, "edge"),                # an outcome ends the plan
    ("condition", "yes", "start", False, "edge"),           # the quota check is only a start
    ("condition", "yes", "condition", True, "edge"),        # itself
    ("condition", "yes", "script", False, "edge"),
])
def test_each_connection_is_judged_by_one_rule(kind, branch, target, same, expected):
    assert runtime.flow_link_issue(kind, branch, target, same) == expected


@pytest.mark.usefixtures("english")
def test_cycles_are_rejected_and_waiting_is_a_state():
    nodes = dict(GOOD, again=("condition", {"test": "available"}))
    edges = [("start", "next", "again"), ("again", "yes", "low"), ("again", "no", "keep"), ("low", "yes", "pick"),
             ("low", "no", "again"), ("pick", "found", "handoff"), ("pick", "none", "wait")]
    assert codes(flow(*edges, **nodes)) == ["cycle"]


@pytest.mark.usefixtures("english")
def test_an_available_check_alone_never_moves_a_healthy_conversation():
    nodes = dict(GOOD, avail=("condition", {"test": "available"}))
    edges = [("start", "next", "avail"), ("avail", "yes", "pick"), ("avail", "no", "low"), ("low", "yes", "pick"),
             ("low", "no", "keep"), ("pick", "found", "handoff"), ("pick", "none", "wait")]
    assert codes(flow(*edges, **nodes)) == ["unguarded"]


@pytest.mark.usefixtures("english")
@pytest.mark.parametrize("bad", [None, [], {"nodes": []}, {"nodes": [{"id": "x" * 40, "type": "start"}], "edges": []},
                                 {"nodes": [{"id": "s", "type": "start", "script": "rm"}], "edges": []},
                                 {"nodes": [{"id": "s", "type": "start", "x": -1}], "edges": []}])
def test_malformed_flows_and_custom_code_are_rejected(bad):
    assert codes(bad) == ["invalid"]
    if bad is None:
        return                      # a policy without a flow runs its template
    with pytest.raises(ValueError):
        runtime.checked_quota_policy({"id": "p", "name": "P", "environment_id": "local", "account_ids": ["a"], "flow": bad})


@pytest.mark.usefixtures("english")
def test_outcome_walks_conditions_and_the_select_block():
    custom = flow(("start", "next", "out"), ("out", "yes", "pick"), ("out", "no", "low"), ("low", "yes", "tell"),
                  ("low", "no", "keep"), ("pick", "found", "handoff"), ("pick", "none", "wait"),
                  start=("start", {}), out=("condition", {"test": "exhausted"}), low=("condition", {"test": "low"}),
                  pick=("select", {"accounts": ["b", "c"], "strategy": "available-quota"}), handoff=("handoff", {}),
                  wait=("wait", {}), tell=("notify", {}), keep=("keep", {}))
    assert codes(custom) == []
    asked = []

    def choose(pool, strategy):
        asked.append((pool, strategy))
        return "c"
    assert runtime.flow_outcome(custom, {"exhausted": True, "low": True}.get, choose) == ("handoff", "c")
    assert asked == [(("b", "c"), "available-quota")]
    assert runtime.flow_outcome(custom, {"exhausted": False, "low": True}.get, choose) == ("notify", None)
    assert runtime.flow_outcome(custom, {"exhausted": False, "low": False}.get, choose) == ("keep", None)
    assert runtime.flow_outcome(custom, {"exhausted": True}.get, lambda *args: None) == ("wait", None)


# --- new work starts on the account the flow selects; resumed work keeps its account ---------------------------------
def flow_snapshot(identifier, used, checked):
    return {"tool": "codex", "account_id": identifier, "environment_id": "local", "checked_at": checked,
            "buckets": {"codex": {"windows": [{"used_percent": used, "resets_at": checked + 9000}]}}}


def launch(tmp_path, strategy, **extra):
    policy = {"id": "s", "name": "P", "environment_id": "local", "account_ids": list(POOL), "strategy": strategy}
    return dict({"session_id": "s", "environment_id": "local", "account": FLOW_ACCOUNTS[0], "path": str(tmp_path),
                 "quota_policy": policy, "quota_accounts": copy.deepcopy(FLOW_ACCOUNTS)}, **extra)


@pytest.fixture
def cached(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.time, "time", lambda: 1000.0)
    for identifier, used in (("a", 95), ("b", 60), ("c", 20)):
        runtime.write_json(runtime.quota_file(tmp_path, identifier, "local"), flow_snapshot(identifier, used, 990))
    return tmp_path


@pytest.mark.usefixtures("english")
@pytest.mark.parametrize("strategy, extra, expected", [
    ("priority", {}, "b"),                               # a is low: the first available one
    ("available-quota", {}, "c"),                        # the most headroom
    ("round-robin", {"quota_cursor": "b"}, "c"),         # after this terminal's last switch
    ("priority", {"resume_thread_id": "thread"}, "a"),   # an exact conversation keeps its account
    ("priority", {"resume": True}, "a"),
])
def test_new_work_selects_its_account_before_starting(cached, strategy, extra, expected):
    request = runtime.initial_quota_account(launch(cached, strategy, **extra), cached)
    assert request["account"]["id"] == expected
    assert request.get("quota_cursor") == (expected if expected != "a" else extra.get("quota_cursor"))


@pytest.mark.usefixtures("english")
def test_unknown_quota_keeps_the_requested_account(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.time, "time", lambda: 1000.0)
    request = launch(tmp_path, "priority")
    assert runtime.initial_quota_account(request, tmp_path) is request


@pytest.mark.usefixtures("english")
def test_round_robin_position_is_validated(tmp_path):
    request = dict(launch(tmp_path, "round-robin", quota_cursor="z"), action="launch", nonce="n", login_method="device")
    with pytest.raises(ValueError):
        runtime.validate_request(request)


# --- account check ----------------------------------------------------------------------------------------------------
A = {"id": "a", "name": "A", "tool": "claude", "auth": "subscription", "environment_id": "local"}
B = dict(A, id="b", name="B")
X = {"id": "x", "name": "X", "tool": "codex", "auth": "subscription", "environment_id": "local"}
Y = dict(X, id="y", name="Y")


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Profiles under data/, the user's own files under native-home/ (conftest); sign-in is reported, never read."""
    monkeypatch.setattr(runtime, "check_login", lambda account, root, env: account["id"] != "signed-out")
    return tmp_path / "data"


def native(tmp_path, tool="claude"):
    return tmp_path / "native-home" / (".claude" if tool == "claude" else ".codex")


def keys(report, accepted=None):
    return {(finding.account_id, finding.key) for finding in report.open(accepted)}


def test_profiles_refreshed_from_the_same_user_files_are_interchangeable(tmp_path, root):
    write(native(tmp_path) / "settings.json", {"permissions": {"allow": ["Bash(ls)"]}, "theme": "dark"})
    write(native(tmp_path) / "skills" / "review" / "SKILL.md", "# review")
    report = account_check.check([A, B], "a", root, tmp_path)
    assert report.findings == [] and report.reference == "a" and set(report.signature) == {"a", "b"}


def test_a_personal_edit_is_found_and_aligned_to_the_reference_with_a_backup(tmp_path, root):
    write(native(tmp_path) / "settings.json", {"permissions": {"allow": ["Bash(ls)"]}, "theme": "dark"})
    write(native(tmp_path) / "skills" / "review" / "SKILL.md", "# review")
    account_check.check([A, B], "a", root, tmp_path)
    profile = runtime.profile_dir(root, B)
    write(profile / "skills" / "review" / "SKILL.md", "# edited in B only")
    settings = json.loads((profile / "settings.json").read_text(encoding="utf-8"))
    write(profile / "settings.json", dict(settings, theme="light", model="opus"))
    report = account_check.check([A, B], "a", root, tmp_path)           # the refresh keeps personal edits
    assert keys(report) == {("b", "settings:skills/review/SKILL.md"), ("b", "settings:settings.json")}
    assert account_check.check([A, B], "b", root, tmp_path).reference == "b"
    changed = account_check.align(B, A, root)
    assert set(changed) == {"skills/review/SKILL.md", "settings.json"}
    assert (profile / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8") == "# review"
    aligned = json.loads((profile / "settings.json").read_text(encoding="utf-8"))
    assert aligned["theme"] == "dark" and aligned["model"] == "opus"     # its own default model is not the layer
    [saved] = (root / "alignment-backups" / "b").rglob("SKILL.md")
    assert saved.read_text(encoding="utf-8") == "# edited in B only"
    assert account_check.check([A, B], "a", root, tmp_path).findings == []   # and the next refresh keeps it aligned
    write(native(tmp_path) / "skills" / "review" / "SKILL.md", "# review, updated by the user")
    account_check.check([A, B], "a", root, tmp_path)
    assert (profile / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8") == "# review, updated by the user"


def codex_answers(plugins=(), apps=None, mcp=None):
    return [{"marketplaces": [{"plugins": [{"id": item, "version": "1", "installed": True, "enabled": True}
                                           for item in plugins]}]},
            {"apps": [{"id": key, "runtimeName": name, "enabled": True, "callable": True} for key, name in (apps or {}).items()]},
            {"data": [{"name": key, "authStatus": state} for key, state in (mcp or {}).items()]}]


@pytest.fixture
def codex(monkeypatch):
    answers = {}
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: name)
    monkeypatch.setattr(runtime, "rpc_requests", lambda command, env, calls, cwd=None, timeout=30: answers[cwd.name])
    return answers


def test_codex_rules_must_match_but_account_capabilities_can_be_accepted(tmp_path, root, codex):
    codex["x"] = codex_answers(["pages@market", "docs@market"], {"gh": "GitHub"}, {"notion": "oAuth", "wiki": "unknown"})
    codex["y"] = codex_answers(["pages@market"], {}, {"notion": "notLoggedIn"})
    account_check.check([X, Y], "x", root, tmp_path)
    write(runtime.profile_dir(root, X) / "rules" / "default.rules", 'prefix_rule(pattern=["ls"])')
    write(runtime.profile_dir(root, Y) / "rules" / "default.rules", 'prefix_rule(pattern=["rm"])')
    report = account_check.check([X, Y], "x", root, tmp_path)
    assert keys(report) == {("y", "rules:default.rules"), ("y", "plugin:docs"), ("y", "app:GitHub"), ("y", "mcp:notion"),
                            ("y", "mcp_unknown:wiki")}             # the names the worker uses at a handoff
    assert next(f for f in report.findings if f.key == "plugin:docs").holders == ("x",)
    accepted = {"y": ["plugin:docs", "app:GitHub", "mcp:notion", "mcp_unknown:wiki"]}
    assert keys(report, accepted) == {("y", "rules:default.rules")}      # rules cannot be accepted, only aligned
    assert account_check.align(Y, X, root) == ["rules/default.rules"]
    assert (runtime.profile_dir(root, Y) / "rules" / "default.rules").read_text(encoding="utf-8") == 'prefix_rule(pattern=["ls"])'
    assert keys(account_check.check([X, Y], "x", root, tmp_path), accepted) == set()
    codex["x"] = codex_answers(["pages@market", "docs@market", "slides@market"], {"gh": "GitHub"}, {"notion": "oAuth"})
    assert keys(account_check.check([X, Y], "x", root, tmp_path), accepted) == {("y", "plugin:slides")}   # added later


def test_codex_shared_config_is_aligned_but_its_own_trust_and_login_stay(tmp_path, root, codex):
    codex["x"] = codex["y"] = codex_answers()
    account_check.check([X, Y], "x", root, tmp_path)
    write(runtime.profile_dir(root, X) / "config.toml", 'model = "m1"\n[features]\nweb = true\n')
    write(runtime.profile_dir(root, Y) / "config.toml",
          'model = "m2"\nforced_login_method = "chatgpt"\n[projects."C:/work"]\ntrust_level = "trusted"\n')
    assert keys(account_check.check([X, Y], "x", root, tmp_path, refresh=False)) == {("y", "settings:config.toml")}
    assert account_check.align(Y, X, root) == ["config.toml"]
    import tomllib
    aligned = tomllib.loads((runtime.profile_dir(root, Y) / "config.toml").read_text(encoding="utf-8"))
    assert aligned["model"] == "m1" and aligned["features"] == {"web": True}
    assert aligned["forced_login_method"] == "chatgpt" and aligned["projects"]["C:/work"]["trust_level"] == "trusted"
    assert keys(account_check.check([X, Y], "x", root, tmp_path, refresh=False)) == set()


def test_claude_plugins_and_mcp_servers_belong_to_each_account(tmp_path, root):
    account_check.check([A, B], "a", root, tmp_path)
    first, second = runtime.profile_dir(root, A), runtime.profile_dir(root, B)
    settings = json.loads((first / "settings.json").read_text(encoding="utf-8")) if (first / "settings.json").exists() else {}
    write(first / "settings.json", dict(settings, enabledPlugins={"pages@market": True}))
    write(first / ".claude.json", {"mcpServers": {"docs": {"type": "http", "url": "https://example.com/mcp"},
                                                  "local": {"command": "tool"}}})
    write(second / ".claude.json", {"mcpServers": {"docs": {"type": "http", "url": "https://example.com/mcp"}}})
    report = account_check.check([A, B], "a", root, tmp_path, refresh=False)
    assert keys(report) == {("b", "plugin:pages"), ("b", "mcp:local"), ("b", "mcp_unknown:docs"), ("a", "mcp_unknown:docs")}


def test_signed_out_accounts_and_changed_profiles(tmp_path, root, monkeypatch):
    signed_out = dict(B, id="signed-out")
    report = account_check.check([A, signed_out], "a", root, tmp_path)
    assert keys(report) == {("signed-out", "login:sign-in")}
    before = account_check.signature([A], root)
    assert account_check.signature([A], root) == before                  # nothing changed, nothing to check again
    write(runtime.profile_dir(root, A) / "skills" / "new" / "SKILL.md", "# new")
    assert account_check.signature([A], root) != before
    monkeypatch.setattr(runtime, "account_in_use", lambda root, account_id, session_id: True)
    with pytest.raises(ValueError):
        account_check.align(B, A, root)                                  # never under another terminal's feet
