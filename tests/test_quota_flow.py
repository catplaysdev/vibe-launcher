"""Block workflows: validation, evaluation, the three templates and new-work account selection."""
import copy
import json

import pytest

from vibe_launcher import runtime


@pytest.fixture(autouse=True)
def english(restore_controls):
    """These tests read English messages (the suite otherwise runs with the Traditional Chinese runtime catalog)."""
    from vibe_launcher import i18n, runtime as engine
    i18n.set_language("en")
    engine._MESSAGES = {}

ACCOUNTS = [{"id": key, "name": key, "tool": "codex", "auth": "subscription", "environment_id": "local"}
            for key in ("a", "b", "c")]
POOL = ("a", "b", "c")


def flow(*edges, **nodes):
    """nodes: id=(type, extra); edges: (from, branch, to)."""
    items = [dict({"id": ident, "type": kind}, **extra) for ident, (kind, extra) in nodes.items()]
    return {"nodes": items, "edges": [{"from": a, "branch": b, "to": c} for a, b, c in edges]}


def codes(value, pool=POOL):
    return sorted({code for _, code in runtime.flow_issues(value, pool)})


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


def test_cycles_are_rejected_and_waiting_is_a_state():
    nodes = dict(GOOD, again=("condition", {"test": "available"}))
    edges = [("start", "next", "again"), ("again", "yes", "low"), ("again", "no", "keep"), ("low", "yes", "pick"),
             ("low", "no", "again"), ("pick", "found", "handoff"), ("pick", "none", "wait")]
    assert codes(flow(*edges, **nodes)) == ["cycle"]


def test_an_available_check_alone_never_moves_a_healthy_conversation():
    nodes = dict(GOOD, avail=("condition", {"test": "available"}))
    edges = [("start", "next", "avail"), ("avail", "yes", "pick"), ("avail", "no", "low"), ("low", "yes", "pick"),
             ("low", "no", "keep"), ("pick", "found", "handoff"), ("pick", "none", "wait")]
    assert codes(flow(*edges, **nodes)) == ["unguarded"]


@pytest.mark.parametrize("bad", [None, [], {"nodes": []}, {"nodes": [{"id": "x" * 40, "type": "start"}], "edges": []},
                                 {"nodes": [{"id": "s", "type": "start", "script": "rm"}], "edges": []},
                                 {"nodes": [{"id": "s", "type": "start", "x": -1}], "edges": []}])
def test_malformed_flows_and_custom_code_are_rejected(bad):
    assert codes(bad) == ["invalid"]
    if bad is None:
        return                      # a policy without a flow runs its template
    with pytest.raises(ValueError):
        runtime.checked_quota_policy({"id": "p", "name": "P", "environment_id": "local", "account_ids": ["a"], "flow": bad})


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
def snapshot(identifier, used, checked):
    return {"tool": "codex", "account_id": identifier, "environment_id": "local", "checked_at": checked,
            "buckets": {"codex": {"windows": [{"used_percent": used, "resets_at": checked + 9000}]}}}


def launch(tmp_path, strategy, **extra):
    policy = {"id": "s", "name": "P", "environment_id": "local", "account_ids": list(POOL), "strategy": strategy}
    return dict({"session_id": "s", "environment_id": "local", "account": ACCOUNTS[0], "path": str(tmp_path),
                 "quota_policy": policy, "quota_accounts": copy.deepcopy(ACCOUNTS)}, **extra)


@pytest.fixture
def cached(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.time, "time", lambda: 1000.0)
    for identifier, used in (("a", 95), ("b", 60), ("c", 20)):
        runtime.write_json(runtime.quota_file(tmp_path, identifier, "local"), snapshot(identifier, used, 990))
    return tmp_path


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


def test_unknown_quota_keeps_the_requested_account(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.time, "time", lambda: 1000.0)
    request = launch(tmp_path, "priority")
    assert runtime.initial_quota_account(request, tmp_path) is request


def test_round_robin_position_is_validated(tmp_path):
    request = dict(launch(tmp_path, "round-robin", quota_cursor="z"), action="launch", nonce="n", login_method="device")
    with pytest.raises(ValueError):
        runtime.validate_request(request)
