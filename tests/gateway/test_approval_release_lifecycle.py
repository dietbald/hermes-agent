"""TJS-226 follow-up: the three release-path defects found in review (TJS-256).

Each test here fails on the first version of the release fix and passes after
the lifecycle repair. They cover the whole point of releasing a thread: the
card has to survive the turn that raised it, the release flag has to be set
before anyone can resolve the entry, and the user's answer has to actually
take effect on the resumed run.
"""

import pytest

from tools import approval as A


@pytest.fixture(autouse=True)
def _clean_state():
    def reset():
        A._gateway_queues.clear()
        A._gateway_notify_cbs.clear()
        A._gateway_wake_cbs.clear()
        A._session_approved.clear()
        getattr(A, "_released_once_grants", {}).clear()
        A._permanent_approved.clear()
        A._pending.clear()

    reset()
    yield
    reset()


def _released_entry(session_key, request_id="r1", keys=("delete in root path",)):
    """Queue an already-released entry, as a raised-and-returned card would be."""
    entry = A._ApprovalEntry({
        "request_id": request_id,
        "command": "rm -rf /tmp/thing",
        "pattern_key": keys[0],
        "pattern_keys": list(keys),
    })
    entry.released = True
    A._gateway_queues.setdefault(session_key, []).append(entry)
    return entry


# ── Defect 1: turn teardown destroyed the released card ──────────────────


def test_released_entry_survives_turn_teardown():
    """unregister_gateway_notify runs in the turn's finally, long before the
    human taps. It must not take the released card with it."""
    sk = "sess-teardown"
    wakes = []
    A.register_gateway_notify(sk, lambda data: None)
    A.register_gateway_wake(sk, lambda data, result: wakes.append(result))
    _released_entry(sk)

    A.unregister_gateway_notify(sk)

    assert len(A._gateway_queues.get(sk, [])) == 1, (
        "released card was dropped by turn teardown — a later tap would "
        "resolve nothing and the run would stay parked forever"
    )

    resolved = A.resolve_gateway_approval(sk, "once")
    assert resolved == 1
    assert len(wakes) == 1, "the user's answer produced no wake"


def test_teardown_still_unblocks_non_released_waiters():
    """The survival rule must not regress the original purpose of teardown:
    blocked threads still have to be released so they don't hang."""
    sk = "sess-mixed"
    A.register_gateway_notify(sk, lambda data: None)
    blocking = A._ApprovalEntry({"request_id": "b1", "command": "x"})
    A._gateway_queues.setdefault(sk, []).append(blocking)
    released = _released_entry(sk, request_id="r2")

    A.unregister_gateway_notify(sk)

    assert blocking.event.is_set(), "blocked waiter was left hanging"
    assert not released.event.is_set()
    assert A._gateway_queues.get(sk) == [released]


# ── Defect 2: release flag set after the entry was resolvable ────────────


def test_answer_during_notify_is_not_lost(monkeypatch):
    """The user can tap before notify_cb returns. If `released` were set after
    notify, the resolver would treat the entry as a blocking waiter, drop it,
    and fire no wake — while the caller still returned "released"."""
    sk = "sess-race"
    wakes = []
    monkeypatch.setattr(A, "_thread_release_enabled", lambda: True)
    A.register_gateway_wake(sk, lambda data, result: wakes.append(result))

    seen = {}

    def _notify_then_user_taps(data):
        # Simulates the human answering inside the notify window.
        seen["resolved"] = A.resolve_gateway_approval(sk, "session")

    A.register_gateway_notify(sk, _notify_then_user_taps)

    decision = A._await_gateway_decision(
        sk,
        _notify_then_user_taps,
        {
            "command": "rm -rf /tmp/x",
            "pattern_key": "delete in root path",
            "pattern_keys": ["delete in root path"],
        },
        surface="gateway",
    )

    assert seen["resolved"] == 1, "the tap resolved nothing"
    assert len(wakes) == 1, (
        "answer arrived during the notify window and produced no wake — "
        f"decision was {decision}"
    )


# ── Defect 3: the answered choice was never applied ──────────────────────


def test_session_choice_is_persisted_on_release_path():
    """The caller already returned, so nobody else can persist the grant. If
    it is not applied here the wake turn retries and is asked again."""
    sk = "sess-persist"
    A.register_gateway_notify(sk, lambda data: None)
    A.register_gateway_wake(sk, lambda data, result: None)
    _released_entry(sk, keys=("delete in root path",))

    A.resolve_gateway_approval(sk, "session")

    assert A.is_approved(sk, "delete in root path"), (
        "session grant was not applied — the resumed run would re-ask"
    )


def test_always_choice_persists_session_and_permanent():
    sk = "sess-always"
    A.register_gateway_notify(sk, lambda data: None)
    A.register_gateway_wake(sk, lambda data, result: None)
    _released_entry(sk, keys=("delete in root path",))

    A.resolve_gateway_approval(sk, "always")

    assert A.is_approved(sk, "delete in root path")
    assert "delete in root path" in A._permanent_approved


def test_once_grants_exactly_one_retry():
    """"once" must let the wake turn's retry through, and only that retry."""
    sk = "sess-once"
    A.register_gateway_notify(sk, lambda data: None)
    A.register_gateway_wake(sk, lambda data, result: None)
    _released_entry(sk, keys=("delete in root path",))

    A.resolve_gateway_approval(sk, "once")

    assert A.is_approved(sk, "delete in root path"), (
        "the retry after a 'once' answer was not authorized — user would be "
        "asked twice for one approval"
    )
    assert not A.is_approved(sk, "delete in root path"), (
        "'once' leaked into a second operation"
    )


def test_deny_grants_nothing():
    sk = "sess-deny"
    A.register_gateway_notify(sk, lambda data: None)
    A.register_gateway_wake(sk, lambda data, result: None)
    _released_entry(sk, keys=("delete in root path",))

    A.resolve_gateway_approval(sk, "deny")

    assert not A.is_approved(sk, "delete in root path")
    assert not A._permanent_approved


def test_clear_session_drops_once_grants():
    sk = "sess-clear"
    A.grant_released_once(sk, "delete in root path")
    A.clear_session(sk)
    assert not A.is_approved(sk, "delete in root path")


# ── Coverage-gap correction from the review ─────────────────────────────


def test_sessions_without_a_wake_callback_never_release():
    """api_server's /v1/runs registers notify but no wake (it is a synchronous
    HTTP request with nothing to resume). That must keep it on the blocking
    path rather than releasing into a void."""
    sk = "sess-no-wake"
    A.register_gateway_notify(sk, lambda data: None)
    assert A._can_release_thread(sk) is False
