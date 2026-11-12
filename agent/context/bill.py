"""Print the context bill for a session.

Usage:
    python -m agent.context.bill <session_id>
    python -m agent.context.bill --last 5
    python -m agent.context.bill <session_id> --by-turn

Reads the injection_events table written by agent.context.events.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List

from daclaw_state import SessionDB


def _fmt_row(row: Dict[str, Any]) -> str:
    return "  %-16s %-28s %6d %9d" % (
        row.get("kind", ""),
        (row.get("source", "") or "")[:28],
        row.get("events", 0),
        row.get("est_tokens", 0),
    )


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description="Context bill for a DAClaw session")
    parser.add_argument("session_id", nargs="?", help="session id or prefix")
    parser.add_argument("--last", type=int, default=0, help="show the N most recent sessions")
    parser.add_argument("--by-turn", action="store_true", help="break the bill down per turn")
    args = parser.parse_args(argv)

    db = SessionDB()

    session_ids: List[str] = []
    if args.session_id:
        resolved = db.resolve_session_id(args.session_id) or args.session_id
        session_ids = [resolved]
    elif args.last:
        for row in db.list_sessions_rich(limit=args.last):
            session_ids.append(row["id"] if isinstance(row, dict) else row[0])
    else:
        parser.error("pass a session id or --last N")

    for sid in session_ids:
        rows = db.injection_bill(sid)
        print("=" * 62)
        print("session %s" % sid)
        print("=" * 62)
        if not rows:
            print("  (no injection events recorded)")
            continue
        print("  %-16s %-28s %6s %9s" % ("kind", "source", "events", "est_tok"))
        total_events = 0
        total_tokens = 0
        for row in rows:
            print(_fmt_row(row))
            total_events += int(row.get("events") or 0)
            total_tokens += int(row.get("est_tokens") or 0)
        print("  " + "-" * 60)
        print("  %-45s %6d %9d" % ("TOTAL", total_events, total_tokens))

        if args.by_turn:
            print("")
            print("  per turn:")
            for row in db.injection_bill_by_turn(sid):
                print(
                    "    turn %-3s iter %-2s %-14s %6d tok  (%s)"
                    % (
                        row.get("turn"),
                        row.get("iteration"),
                        row.get("kind"),
                        row.get("est_tokens", 0),
                        (row.get("sources") or "")[:40],
                    )
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
