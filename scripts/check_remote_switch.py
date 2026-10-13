"""Read-only check after a manual SSH switching test done in the launcher: what the hosts reported, nothing else.

It reads the launcher's own records on this PC (session records and the replies of account-control requests); it does not
connect to any host and prints no credentials.

    python scripts/check_remote_switch.py --environment my-server [--hours 6]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from vibe_launcher import runtime  # noqa: E402
from vibe_launcher.service import data_root  # noqa: E402
from vibe_launcher.store import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--environment", required=True, help="environment name or id")
    parser.add_argument("--hours", type=float, default=6)
    args = parser.parse_args()
    root = data_root()
    store = Store(root / "vibe-launcher.sqlite3")
    environment = next((env for env in store.list("environments") if args.environment in (env["id"], env.get("name"))), None)
    if environment is None:
        print("No such environment")
        return 1
    names = {account["id"]: account.get("name", account["id"]) for account in store.list("accounts")}
    since = time.time() - args.hours * 3600
    print(f"Sessions on {environment.get('name')}:")
    for session in store.list("sessions"):
        if session.get("environment_id") == environment["id"] and session.get("updated_at", "") >= time.strftime("%Y-%m-%d", time.gmtime(since)):
            print(f"  {session['id'][:8]} {session.get('name', '')} · {names.get(session.get('account_id'), '?')} · {session.get('status')}")
    print("Account-control replies (newest last):")
    replies = sorted((path for path in (root / "results").glob("*.json") if path.stat().st_mtime >= since), key=lambda p: p.stat().st_mtime)
    for path in replies:
        try:
            report = json.loads(runtime.read_private(path, limit=65536))
        except (OSError, ValueError):
            continue
        result = report.get("result") or {}
        if report.get("action") != "codex-control" or result.get("environment_id") != environment["id"]:
            continue
        stamp = time.strftime("%H:%M:%S", time.localtime(path.stat().st_mtime))
        blocked = (result.get("blocked") or {}).get("gaps")
        print(f"  {stamp} {result.get('status')} · {names.get(result.get('account_id'), result.get('account_id'))}"
              f" · thread {str(result.get('thread_id'))[:8]} · accepted={result.get('accepted')}"
              + (f" · differences={[gap.get('name') for gap in blocked]}" if blocked else "")
              + (f" · {result.get('message')}" if result.get("message") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
