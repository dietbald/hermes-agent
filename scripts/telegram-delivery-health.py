#!/usr/bin/env python3
"""Diagnose Telegram send-path health without exposing bot tokens.

Read-only by default. ``--send-test`` performs one silent sendMessage per
selected profile to its configured home channel; use it only during an incident.
JSON output and the process exit code are intentionally suitable for a
non-Telegram monitor/alert path (0 healthy, 1 indeterminate, 2 send failure).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROFILE_ROOT = Path.home() / ".hermes" / "profiles"
TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
HOME_KEY = "TELEGRAM_HOME_CHANNEL"
RETRY_RE = re.compile(r"(?:retry[_ ]after|retry in)[^0-9]*(\d+)", re.I)


def _env_value(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip().strip('"').strip("'")
        return value or None
    return None


def _bot_api(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
            return {"http_status": response.status, "ok": bool(body.get("ok"))}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        retry_after = (body.get("parameters") or {}).get("retry_after")
        if retry_after is None:
            match = RETRY_RE.search(str(body.get("description") or raw))
            retry_after = int(match.group(1)) if match else None
        return {
            "http_status": exc.code,
            "ok": False,
            "error_code": body.get("error_code") or exc.code,
            "description": str(body.get("description") or "HTTP error")[:200],
            "retry_after": retry_after,
        }
    except Exception as exc:  # network/DNS/TLS errors, never include request URL
        return {
            "http_status": None,
            "ok": False,
            "error_code": exc.__class__.__name__,
            "description": str(exc)[:200],
            "retry_after": None,
        }


def _unit_state(profile: str) -> dict[str, str | None]:
    unit = f"hermes-gateway-{profile}.service"
    proc = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=ActiveState,SubState,MainPID",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    values = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return {
        "unit": unit,
        "active_state": values.get("ActiveState"),
        "sub_state": values.get("SubState"),
        "main_pid": values.get("MainPID"),
    }


def _recent_journal_counts(profile: str, minutes: int) -> dict[str, int]:
    proc = subprocess.run(
        [
            "journalctl",
            "--user",
            "-u",
            f"hermes-gateway-{profile}.service",
            "--since",
            f"{minutes} minutes ago",
            "--output=cat",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    text = proc.stdout
    return {
        "lines": len(text.splitlines()),
        "send_failures": len(re.findall(r"send.*(?:fail|timed out|timeout)", text, re.I)),
        "rate_limits": len(re.findall(r"(?:\b429\b|flood.control|retry.after)", text, re.I)),
        "delivery_alerts": len(re.findall(r"DELIVERY NEEDS ATTENTION", text)),
    }


def _ledger_counts(profile_dir: Path) -> dict[str, int | str]:
    db = profile_dir / "state.db"
    if not db.exists():
        return {"status": "missing", "retryable_failed": 0, "cancelled": 0}
    try:
        uri = f"file:{urllib.parse.quote(str(db))}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as conn:
            tables = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_obligations'"
            ).fetchone()
            if not tables:
                return {"status": "no_table", "retryable_failed": 0, "cancelled": 0}
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")
            }
            retryable_failed = 0
            if "retryable" in columns:
                retryable_failed = conn.execute(
                    "SELECT count(*) FROM delivery_obligations "
                    "WHERE state='failed' AND retryable=1"
                ).fetchone()[0]
            cancelled = conn.execute(
                "SELECT count(*) FROM delivery_obligations WHERE state='cancelled'"
            ).fetchone()[0] if "cancelled_at" in columns else 0
            return {
                "status": "ok",
                "retryable_failed": int(retryable_failed),
                "cancelled": int(cancelled),
            }
    except Exception as exc:
        return {
            "status": f"error:{exc.__class__.__name__}",
            "retryable_failed": 0,
            "cancelled": 0,
        }


def _profile_result(profile: str, *, send_test: bool, minutes: int) -> dict[str, Any]:
    profile_dir = PROFILE_ROOT / profile
    env_path = profile_dir / ".env"
    token = _env_value(env_path, TOKEN_KEY)
    chat_id = _env_value(env_path, HOME_KEY)
    result: dict[str, Any] = {
        "profile": profile,
        "configured": bool(token),
        "home_channel_configured": bool(chat_id),
        "unit": _unit_state(profile),
        "recent_journal": _recent_journal_counts(profile, minutes),
        "ledger": _ledger_counts(profile_dir),
    }
    if not token:
        result["classification"] = "not_configured"
        return result

    result["getMe"] = _bot_api(token, "getMe")
    result["getWebhookInfo"] = _bot_api(token, "getWebhookInfo")
    if send_test:
        if not chat_id:
            result["sendMessage"] = {
                "ok": False,
                "error_code": "home_channel_missing",
                "retry_after": None,
            }
        else:
            stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
            result["sendMessage"] = _bot_api(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": f"Hermes Telegram delivery health probe ({profile}, {stamp})",
                    "disable_notification": "true",
                },
            )

    send = result.get("sendMessage")
    if send_test and send and not send.get("ok"):
        result["classification"] = (
            "send_rate_limited"
            if send.get("error_code") == 429 or send.get("http_status") == 429
            else "send_failed"
        )
    elif send_test:
        result["classification"] = "send_healthy"
    elif result["getMe"].get("ok"):
        result["classification"] = "control_plane_healthy_send_path_untested"
    else:
        result["classification"] = "telegram_api_unreachable_or_auth_failed"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profiles", nargs="+", help="Hermes profile slugs")
    parser.add_argument("--send-test", action="store_true", help="perform one silent sendMessage per profile")
    parser.add_argument("--journal-minutes", type=int, default=10)
    parser.add_argument("--output", type=Path, help="also atomically write the JSON report here")
    args = parser.parse_args()

    profiles = [
        _profile_result(name, send_test=args.send_test, minutes=max(1, args.journal_minutes))
        for name in args.profiles
    ]
    send_results = [p.get("sendMessage") for p in profiles if p.get("sendMessage")]
    rate_limited = [
        p for p in profiles
        if p.get("classification") == "send_rate_limited" and p.get("getMe", {}).get("ok")
    ]
    if args.send_test and len(rate_limited) >= 2:
        aggregate = "multi_bot_send_throttle_with_control_plane_healthy"
        conclusion = (
            "Multiple independent bot tokens can reach getMe but sendMessage is 429. "
            "If recent local send volume is low and no duplicate token owner exists, "
            "Telegram-side, recipient-scoped, or external credential use is more likely "
            "than this gateway flooding. This probe cannot exclude use from another host."
        )
    elif args.send_test and rate_limited:
        aggregate = "single_bot_send_throttle"
        conclusion = "One bot is send-throttled; compare another bot before calling it server-wide."
    elif args.send_test and send_results and all(r.get("ok") for r in send_results):
        aggregate = "send_path_healthy"
        conclusion = "All tested sendMessage calls succeeded."
    elif args.send_test:
        aggregate = "send_path_failure"
        conclusion = "At least one send test failed; inspect per-profile evidence."
    else:
        aggregate = "send_path_not_tested"
        conclusion = (
            "Read-only control-plane checks cannot prove sendMessage health. Re-run with "
            "--send-test during an incident after checking the runbook."
        )

    report = {
        "schema": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "send_test": args.send_test,
        "aggregate_classification": aggregate,
        "conclusion": conclusion,
        "profiles": profiles,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_suffix(args.output.suffix + ".tmp")
        tmp.write_text(rendered + "\n", encoding="utf-8")
        os.replace(tmp, args.output)

    if args.send_test and any(not r.get("ok") for r in send_results):
        return 2
    if any(not p.get("getMe", {}).get("ok", False) for p in profiles if p["configured"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
