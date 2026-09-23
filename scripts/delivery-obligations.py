#!/usr/bin/env python3
"""List or explicitly cancel durable gateway delivery obligations.

The command never prints response content. Run from the Hermes checkout:
  .venv/bin/python scripts/delivery-obligations.py --profile atlas list
  .venv/bin/python scripts/delivery-obligations.py --profile atlas cancel <id>
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


def _home(profile: str) -> Path:
    return Path.home() / ".hermes" / "profiles" / profile


def _rows(db: Path) -> list[dict]:
    if not db.exists():
        return []
    with sqlite3.connect(db, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_obligations'"
        ).fetchone()
        if not exists:
            return []
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")
        }
        retryable = "retryable" if "retryable" in columns else "0 AS retryable"
        failures = "failure_count" if "failure_count" in columns else "0 AS failure_count"
        next_at = "next_attempt_at" if "next_attempt_at" in columns else "NULL AS next_attempt_at"
        query = f"""SELECT obligation_id, platform, state, attempts, {retryable},
                           {failures}, {next_at}, created_at, updated_at, last_error
                    FROM delivery_obligations
                    WHERE state IN ('pending', 'attempting', 'failed')
                    ORDER BY created_at"""
        return [dict(row) for row in conn.execute(query)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("obligation_id")
    args = parser.parse_args()

    home = _home(args.profile)
    os.environ["HERMES_HOME"] = str(home)
    db = home / "state.db"

    if args.command == "list":
        print(json.dumps(_rows(db), indent=2, sort_keys=True))
        return 0

    # Import only after HERMES_HOME is selected; cancellation uses the same
    # migration/locking path as the gateway itself.
    from gateway.delivery_ledger import cancel_obligation

    cancelled = cancel_obligation(args.obligation_id)
    print(json.dumps({"obligation_id": args.obligation_id, "cancelled": cancelled}))
    return 0 if cancelled else 1


if __name__ == "__main__":
    sys.exit(main())
