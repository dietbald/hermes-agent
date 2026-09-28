"""Regression tests for the token-safe Telegram delivery health probe."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "telegram-delivery-health.py"
SPEC = importlib.util.spec_from_file_location("telegram_delivery_health", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_send_429_is_classified_without_exposing_token(tmp_path, monkeypatch):
    profile_root = tmp_path / "profiles"
    profile = profile_root / "atlas"
    profile.mkdir(parents=True)
    secret = "123456:super-secret-bot-token"
    (profile / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={secret}\nTELEGRAM_HOME_CHANNEL=123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE, "PROFILE_ROOT", profile_root)
    monkeypatch.setattr(
        MODULE,
        "_unit_state",
        lambda _profile: {"active_state": "active", "sub_state": "running"},
    )
    monkeypatch.setattr(MODULE, "_recent_journal_counts", lambda *_: {})
    monkeypatch.setattr(MODULE, "_ledger_counts", lambda *_: {"status": "ok"})

    def bot_api(_token, method, payload=None):
        assert _token == secret
        if method == "sendMessage":
            return {
                "http_status": 429,
                "ok": False,
                "error_code": 429,
                "retry_after": 8,
                "description": "Too Many Requests",
            }
        return {"http_status": 200, "ok": True}

    monkeypatch.setattr(MODULE, "_bot_api", bot_api)

    result = MODULE._profile_result("atlas", send_test=True, minutes=10)

    assert result["classification"] == "send_rate_limited"
    assert result["sendMessage"]["retry_after"] == 8
    assert secret not in json.dumps(result)
