import pytest

from vibe_launcher.quota_policy import Policy, PolicyStore, commit_cursor, select


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
    result = select(policy(), ACCOUNTS, {"a": first, "b": second}, "local", 1050)
    assert result.account_id == "b"
    assert result.remaining == 70


def test_sticky_account_and_round_robin_only_commit_after_success():
    quotas = {"a": snapshot("a", 40), "b": snapshot("b", 10)}
    flow = policy(strategy="round-robin")
    assert select(flow, ACCOUNTS, quotas, "local", 1050, current_account_id="a", cursor="a").account_id == "a"
    assert select(flow, ACCOUNTS, quotas, "local", 1050, cursor="a").account_id == "b"
    assert select(flow, ACCOUNTS, quotas, "local", 1050, cursor="a").account_id == "b"
    assert commit_cursor(flow, "b") == "b"


def test_available_quota_and_threshold_boundary():
    quotas = {"a": snapshot("a", 90), "b": snapshot("b", 30)}
    assert select(policy(strategy="available-quota"), ACCOUNTS, quotas, "local", 1050).account_id == "b"
    assert select(policy(), ACCOUNTS, {"a": snapshot("a", 90)}, "local", 1050).account_id is None


def test_stale_unknown_and_cross_environment_are_excluded():
    assert select(policy(), ACCOUNTS, {"a": snapshot("a", 10)}, "local", 1300).account_id is None
    bad = snapshot("a", 10)
    bad["environment_id"] = "remote"
    assert select(policy(), ACCOUNTS, {"a": bad}, "local", 1050).account_id is None
    bad = snapshot("a", 10)
    bad["buckets"] = {"other": {"windows": [{"used_percent": 0}]}}
    assert select(policy(), ACCOUNTS, {"a": bad}, "local", 1050).account_id is None


def test_wait_requires_known_reset_notify_has_no_retry():
    quotas = {"a": snapshot("a", 100)}
    assert select(policy(), ACCOUNTS, quotas, "local", 1050).next_retry == 1400
    assert select(policy(when_empty="notify-only"), ACCOUNTS, quotas, "local", 1050).next_retry is None
    assert select(policy(), ACCOUNTS, {}, "local", 1050).next_retry is None


def test_server_reported_limit_overrides_spare_percent():
    quota = snapshot("a", 20)
    quota["buckets"]["codex"]["reached"] = "rate_limit"
    assert select(policy(), ACCOUNTS, {"a": quota}, "local", 1050).account_id is None


def test_retry_waits_for_all_blocking_windows_and_requires_trusted_resets():
    quota = snapshot("a", 100)
    quota["buckets"]["codex"]["windows"].append({"used_percent": 95, "resets_at": 1700})
    assert select(policy(), ACCOUNTS, {"a": quota}, "local", 1050).next_retry == 1700
    quota["buckets"]["codex"]["windows"][1]["resets_at"] = None
    assert select(policy(), ACCOUNTS, {"a": quota}, "local", 1050).next_retry is None


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
