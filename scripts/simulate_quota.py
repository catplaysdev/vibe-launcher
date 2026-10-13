"""Test tool: make a managed terminal believe in a quota that is not real, for at most one hour.

The launcher never writes this file; only this script does, into one terminal's private control folder. While it is in
effect the terminal's status says so. Local terminals only (a host's control folder is on the host).

    python scripts/simulate_quota.py --session <launcher session id> --set <account id>=97 --set <other id>=10
    python scripts/simulate_quota.py --session <id> --set <id>=100:90 --recover <id>=5@90 --fail-once
    python scripts/simulate_quota.py --session <id> --clear
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from vibe_launcher import runtime  # noqa: E402
from vibe_launcher.service import data_root  # noqa: E402


def window(used: float, reset_in: float) -> list:
    return [{"slot": "primary", "used_percent": used, "window_minutes": 300, "resets_at": int(time.time() + reset_in)}]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", required=True, help="launcher session id of a managed terminal")
    parser.add_argument("--set", action="append", default=[], metavar="ACCOUNT=USED[:RESET_SECONDS]",
                        help="simulated used percent, optionally with a trusted reset this many seconds ahead (default 3600)")
    parser.add_argument("--recover", action="append", default=[], metavar="ACCOUNT=USED@SECONDS",
                        help="after SECONDS the account reports USED instead (a reset that happened)")
    parser.add_argument("--fail-once", action="store_true", help="treat the next settled turn as stopped by quota, once")
    parser.add_argument("--minutes", type=int, default=30, help="how long the simulation lasts (1-60)")
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()
    path = runtime.codex_control_path(data_root(), args.session) / "simulated-quota.json"
    if args.clear:
        path.unlink(missing_ok=True)
        print("cleared")
        return 0
    accounts = {}
    for item in args.set:
        identifier, value = item.split("=", 1)
        used, _, reset = value.partition(":")
        accounts[runtime.safe_id(identifier)] = {"windows": window(float(used), float(reset or 3600))}
    for item in args.recover:
        identifier, value = item.split("=", 1)
        used, _, delay = value.partition("@")
        entry = accounts.setdefault(runtime.safe_id(identifier), {"windows": []})
        entry["after"] = {"at": time.time() + float(delay), "windows": window(float(used), 18000)}
    minutes = max(1, min(60, args.minutes))
    runtime.write_json(path, {"label": "test", "expires_at": time.time() + minutes * 60, "accounts": accounts,
                              "quota_failure_once": bool(args.fail_once)})
    print(f"simulated quota for {minutes} min: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
