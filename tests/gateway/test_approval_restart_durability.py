"""TJS-270: a released approval card must survive a gateway restart.

TJS-226 made a card outlive the TURN that raised it. These tests cover the
next boundary: the card must also outlive the PROCESS. A restart between
"card sent" and "user taps" used to destroy the approval silently — the
buttons were still on screen, the tap found nothing, and the run waiting on
the answer never resumed.

Each test drives the real persistence path with HERMES_HOME pointed at a
tmp_path, then simulates the restart by clearing the in-process state the way
a new process starts out.
"""

import pytest

from tools import approval as A
from tools import approval_persistence as P


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def reset():
        A._gateway_queues.clear()
        A._gateway_notify_cbs.clear()
        A._gateway_wake_cbs.clear()
        A._session_approved.clear()
        A._released_once_grants.clear()
        A._permanent_approved.clear()
        A._pending.clear()
        A._gateway_wake_fallback = None

    reset()
    yield
    reset()


def _raise_released_card(session_key, command="rm -rf /tmp/thing", notify=None):
    """Raise one approval that releases the agent thread, as a turn would."""
    A.register_gateway_notify(session_key, notify or (lambda data: None))
    A.register_gateway_wake(session_key, lambda data, decision: None)
    return A._await_gateway_decision(
        session_key,
        notify or (lambda data: None),
        {
            "command": command,
            "description": "dangerous command",
            "pattern_key": "delete in root path",
            "pattern_keys": ["delete in root path"],
        },
        allow_release=True,
        raw_operation=command,
    )


def _simulate_restart():
    """Drop every piece of in-process approval state, as a new process has."""
    A._gateway_queues.clear()
    A._gateway_notify_cbs.clear()
    A._gateway_wake_cbs.clear()
    A._gateway_wake_fallback = None


@pytest.fixture
def _release_on(monkeypatch):
    monkeypatch.setattr(A, "_thread_release_enabled", lambda: True)


# ── The card itself ──────────────────────────────────────────────────────


def test_released_card_is_persisted(_release_on):
    sk = "sess-persist"
    result = _raise_released_card(sk)
    assert result.get("released") is True

    records = P.load_released()
    assert len(records) == 1
    assert records[0]["session_key"] == sk
    assert records[0]["request_id"] == result["request_id"]
    assert records[0]["data"]["command"] == "rm -rf /tmp/thing"


def test_blocking_card_is_not_persisted(monkeypatch):
    """Release disabled → nothing is released, so nothing is stored."""
    monkeypatch.setattr(A, "_thread_release_enabled", lambda: False)
    sk = "sess-blocking"
    A.register_gateway_notify(sk, lambda data: None)
    # Resolve from the notify callback so the blocking wait returns promptly.
    A._await_gateway_decision(
        sk,
        lambda data: A.resolve_gateway_approval(sk, "deny"),
        {"command": "rm -rf /tmp/x", "description": "d",
         "pattern_key": "k", "pattern_keys": ["k"]},
        allow_release=True,
        raw_operation="rm -rf /tmp/x",
    )
    assert P.load_released() == []


def test_card_survives_restart_and_is_answerable(_release_on):
    sk = "sess-restart"
    result = _raise_released_card(sk)
    request_id = result["request_id"]

    _simulate_restart()
    assert A.list_gateway_approvals(sk) == [], "queue must start empty"

    assert A.restore_released_approvals() == 1
    pending = A.list_gateway_approvals(sk)
    assert len(pending) == 1
    assert pending[0]["request_id"] == request_id
    assert A.has_blocking_approval(sk) is True


def test_restore_is_idempotent(_release_on):
    sk = "sess-idem"
    _raise_released_card(sk)
    _simulate_restart()

    assert A.restore_released_approvals() == 1
    assert A.restore_released_approvals() == 0
    assert len(A.list_gateway_approvals(sk)) == 1


# ── Resuming the run after the restart ───────────────────────────────────


def test_answer_after_restart_fires_the_wake_fallback(_release_on):
    sk = "sess-wake"
    _raise_released_card(sk)
    _simulate_restart()
    A.restore_released_approvals()

    seen = []
    A.register_gateway_wake_fallback(
        lambda session_key, data, decision: seen.append((session_key, decision))
    )

    assert A.resolve_gateway_approval(sk, "session") == 1
    assert len(seen) == 1, "the restored card's answer must resume the run"
    assert seen[0][0] == sk
    assert seen[0][1]["choice"] == "session"
    # The scope the user chose is applied even though the caller that raised
    # the card died with the previous process.
    assert A.is_approved(sk, "delete in root path") is True


def test_answered_card_is_dropped_from_the_store(_release_on):
    sk = "sess-drop"
    _raise_released_card(sk)
    _simulate_restart()
    A.restore_released_approvals()
    A.register_gateway_wake_fallback(lambda *a: None)

    A.resolve_gateway_approval(sk, "deny")
    assert P.load_released() == [], (
        "a resolved card left on disk would be restored and re-answered "
        "after the next restart"
    )


def test_session_reset_drops_the_store_record(_release_on):
    sk = "sess-cleared"
    _raise_released_card(sk)
    assert len(P.load_released()) == 1

    A.clear_session(sk)
    assert P.load_released() == [], (
        "/new and /stop cancel the card; the next start must not revive it"
    )


# ── The "once" grant across the restart ──────────────────────────────────


def test_once_answer_after_restart_is_redeemable(_release_on):
    """The whole point of persisting the fingerprint.

    A "once" answer authorises exactly the operation the user reviewed. The
    resumed turn retries that operation and recomputes the fingerprint in the
    NEW process, so the salt has to be durable or the user gets asked twice
    for the command they just approved.
    """
    sk = "sess-once"
    command = "rm -rf /tmp/once-thing"
    _raise_released_card(sk, command=command)

    _simulate_restart()
    A.restore_released_approvals()
    A.register_gateway_wake_fallback(lambda *a: None)
    A.resolve_gateway_approval(sk, "once")

    # The wake turn retries the same operation: it must be let through
    # without raising a second card.
    retry = A._await_gateway_decision(
        sk,
        lambda data: None,
        {"command": command, "description": "dangerous command",
         "pattern_key": "delete in root path",
         "pattern_keys": ["delete in root path"]},
        allow_release=True,
        raw_operation=command,
    )
    assert retry.get("redeemed_released_once") is True
    assert retry.get("resolved") is True


def test_fingerprint_salt_is_stable_across_processes(tmp_path, monkeypatch):
    """A fresh process must derive the same digest for the same operation."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(A, "_OPERATION_FINGERPRINT_SALT", {})
    first = A._operation_fingerprint("rm -rf /tmp/x", ["k"])

    # New process: nothing is cached in memory, only the salt file remains.
    monkeypatch.setattr(A, "_OPERATION_FINGERPRINT_SALT", {})
    second = A._operation_fingerprint("rm -rf /tmp/x", ["k"])

    assert first == second
    assert "rm -rf" not in first, "the digest must not carry the raw command"


# ── The platform buttons that outlived the process ───────────────────────


def test_button_binding_survives_restart(_release_on):
    """A tap resolves the card its button was bound to, not a newer one.

    The Telegram adapter's in-memory id → session map dies with the process,
    so the card's durable record carries the binding instead.
    """
    sk = "sess-buttons"
    result = _raise_released_card(sk)
    P.attach_platform_ref(
        sk, result["request_id"],
        {"platform": "telegram", "approval_id": 1234567},
    )

    _simulate_restart()
    A.restore_released_approvals()

    matches = P.platform_refs_for("telegram")
    assert len(matches) == 1
    assert matches[0]["platform_ref"]["approval_id"] == 1234567
    assert matches[0]["session_key"] == sk
    assert matches[0]["request_id"] == result["request_id"]
    assert P.platform_refs_for("slack") == []

    # And that binding resolves exactly this card.
    A.register_gateway_wake_fallback(lambda *a: None)
    assert A.resolve_gateway_approval(
        matches[0]["session_key"], "deny",
        request_id=matches[0]["request_id"],
    ) == 1
    assert P.platform_refs_for("telegram") == []


def test_stale_records_are_pruned(_release_on):
    sk = "sess-stale"
    _raise_released_card(sk)

    records = P._read_records()
    records[0]["created_at"] = 0.0  # epoch — far past any sane retention
    P._write_records(records)

    assert P.load_released() == []
    assert A.restore_released_approvals() == 0
