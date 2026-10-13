"""Private persistence for quota plans: the saved plan library, each terminal's applied copy and its progress.

A saved plan is a reusable template; applying it copies it to the terminal. Editing the plan changes no terminal until
the user applies it again. The three files are separate so a damaged one never takes the others with it."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from . import runtime
from .runtime import (QuotaPolicy as Policy, QuotaSelection as Selection, quota_state, quota_template, flow_issues,
                      flow_outcome, quota_flow, select_quota_account as select, commit_quota_cursor as commit_cursor)

PLANS_FILE = "quota-plans.json"         # plan id -> plan (a policy whose id and plan_id are the plan's)
COPIES_FILE = "quota-policies.json"     # terminal session id -> the copy applied to it
PROGRESS_FILE = "quota-progress.json"   # terminal session id -> {"cursor": account id}
TEMPLATES = ("priority", "round-robin", "available-quota")


class PolicyStore:
    def __init__(self, root: Path, name: str = COPIES_FILE):
        self.path = Path(root) / name

    def load(self) -> dict[str, Policy]:
        try:
            text = runtime.read_private(self.path, limit=1048576)
            data = json.loads(text)
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or len(data) > 128:
            raise ValueError("Invalid quota policy file")
        policies = {}
        for key, value in data.items():
            policy = runtime.checked_quota_policy(value)
            if key != policy.id:
                raise ValueError("Policy identifier does not match")
            policies[key] = policy
        return policies

    def save(self, policies: Mapping[str, Policy | dict]) -> None:
        if len(policies) > 128:
            raise ValueError("Too many quota policies")
        clean = {}
        for key, value in policies.items():
            policy = runtime.checked_quota_policy(value.to_dict() if isinstance(value, Policy) else value)
            if key != policy.id:
                raise ValueError("Policy identifier does not match")
            clean[key] = policy.to_dict()
        if len(json.dumps(clean, ensure_ascii=False).encode("utf-8")) > 1048576:
            raise ValueError("Quota policies exceed size limit")
        runtime.write_json(self.path, clean)


def plan_store(root: Path) -> PolicyStore:
    return PolicyStore(root, PLANS_FILE)


class ProgressStore:
    """Each terminal's round-robin position. Shared quota caches and cool-downs live with the worker, not here."""
    def __init__(self, root: Path):
        self.path = Path(root) / PROGRESS_FILE

    def load(self) -> dict[str, dict]:
        try:
            data = json.loads(runtime.read_private(self.path, limit=262144))
        except (FileNotFoundError, ValueError):
            return {}                   # progress is a convenience: unreadable means "start over"
        if not isinstance(data, dict):
            return {}
        return {key: {"cursor": value["cursor"]} for key, value in list(data.items())[:256]
                if isinstance(key, str) and isinstance(value, dict) and isinstance(value.get("cursor"), str)}

    def cursor(self, session_id: str) -> str | None:
        return self.load().get(session_id, {}).get("cursor")

    def set(self, session_id: str, cursor: str | None) -> None:
        data = self.load()
        if cursor is None:
            if data.pop(session_id, None) is None:
                return
        elif data.get(session_id, {}).get("cursor") == cursor:
            return
        else:
            data.pop(session_id, None)
            data[session_id] = {"cursor": runtime.safe_id(cursor)}
        runtime.write_json(self.path, dict(list(data.items())[-128:]))

    def move(self, old: str, new: str) -> None:
        cursor = self.cursor(old)
        self.set(old, None)
        if cursor:
            self.set(new, cursor)


def template_plan(plan_id: str, name: str, template: str, environment_id: str, account_ids, **options) -> Policy:
    """A new plan from one of the three ready-made templates (low quota 10%, check every 120 s, wait when none left)."""
    if template not in TEMPLATES:
        raise ValueError("Unknown template")
    return runtime.checked_quota_policy(dict({"id": plan_id, "name": name, "environment_id": environment_id,
                                              "account_ids": list(account_ids), "strategy": template, "threshold": 10.0,
                                              "interval": 120, "when_empty": "wait", "plan_id": plan_id, "revision": 1,
                                              "flow": quota_template(template, account_ids, "wait")}, **options))


__all__ = ["Policy", "Selection", "PolicyStore", "ProgressStore", "plan_store", "template_plan", "quota_state", "select",
           "commit_cursor", "flow_issues", "flow_outcome", "quota_flow", "quota_template", "TEMPLATES"]
