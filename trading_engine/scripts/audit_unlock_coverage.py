#!/usr/bin/env python3
"""Audit live/Top-8 roster vs unlock_calendar coverage (in-repo sources only).

Usage (from AIOS repo root):
  PYTHONPATH=. python trading_engine/scripts/audit_unlock_coverage.py
  PYTHONPATH=. python trading_engine/scripts/audit_unlock_coverage.py --roster ARB/USDT,TIA/USDT,OP/USDT
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as script
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_engine.spot.unlock_calendar import audit_unlock_coverage  # noqa: E402
from trading_engine.spot.runner import ALL_23_HALAL_UNIVERSE  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Unlock calendar coverage audit")
    ap.add_argument(
        "--roster",
        default="",
        help="Comma-separated symbols (default: ALL_23_HALAL_UNIVERSE)",
    )
    ap.add_argument("--within-days", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="Print full JSON")
    args = ap.parse_args()

    if args.roster.strip():
        roster = [s.strip() for s in args.roster.split(",") if s.strip()]
    else:
        roster = list(ALL_23_HALAL_UNIVERSE)

    report = audit_unlock_coverage(roster, within_days=args.within_days)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"Unlock coverage audit @ {report['as_of_utc']}")
    print(f"Roster ({len(report['roster'])}): {', '.join(report['roster'][:16])}{'…' if len(report['roster'])>16 else ''}")
    print(f"Covered explicit: {report['covered_explicit']}")
    print(f"Advisory symbols: {report['advisory_symbols']}")
    print(f"Hard-pause allowlist: {report['hard_pause_allowlist']}")
    print(f"Gaps ({len(report['gaps'])}): {report['gaps']}")
    print(f"Active hard pauses: {report['active_hard_pauses']}")
    print(f"Next {args.within_days}d:")
    for row in report["next_30d"]:
        print(f"  - {row}")
    print(report["note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
