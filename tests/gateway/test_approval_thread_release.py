"""TJS-226: raising an approval card must not park the agent thread.

The pre-existing contract (thread blocks on ``entry.event.wait()`` until the
answer or ``approvals.timeout``) is still covered by
``TestBlockingApprovalE2E`` in this file — these tests cover the opt-in
``approvals.release_thread`` mode that replaces the wait with an
answer-driven wake.

Regression guard: the blocking wait is coupled to ``approvals.timeout`` via
``human_wait_ceiling()``, so raising that timeout (TJS-222 set it to a year)
without this release path parks a thread for up to a year.
"""
import threading
import time

import pytest

from tools import approval as A


def _clear(session_key):
    A._gateway_queues.pop(session_key, None)
    A._gateway_notify_cbs.pop(session_key, None)
    A._gateway_wake_cbs.pop(session_key, None)


@pytest.fixture
def release_mode(monkeypatch):
    """approvals.release_thread=True with a huge timeout (TJS-222's value)."""
    monkeypatch.setattr(
        A, "_get_approval_config",
        lambda: {"mode": "manual", "timeout": 31536000, "release_thread": True},
    )


@pytest.fixture
def blocking_mode(monkeypatch):
    monkeypatch.setattr(
        A, "_get_approval_config",
        lambda: {"mode": "manual", "timeout": 300, "release_thread": False},
    )


class TestApprovalReleasesAgentThread:

    def test_release_requires_a_wake_callback(self, release_mode):
        """Without a way to resume, the thread must still block.

        Releasing a run nobody can wake would silently drop the user's answer.
        """
        sk = "tjs226-no-wake"
        _clear(sk)
        try:
            assert A._thread_release_enabled() is True
            assert A._can_release_thread(sk) is False
            A.register_gateway_wake(sk, lambda a, d: None)
            assert A._can_release_thread(sk) is True
        finally:
            _clear(sk)

    def test_card_releases_thread_and_answer_wakes_run(self, release_mode):
        """The whole round trip: release on raise, resume on answer."""
        sk = "tjs226-roundtrip"
        _clear(sk)
        try:
            notified = threading.Event()
            A.register_gateway_notify(sk, lambda d: notified.set())
            woke = {}
            done = threading.Event()

            def wake_cb(approval_data, decision):
                woke["decision"] = decision
                woke["command"] = approval_data.get("command")
                done.set()

            A.register_gateway_wake(sk, wake_cb)

            result = {}

            def agent():
                result["d"] = A._await_gateway_decision(
                    sk, lambda d: notified.set(),
                    {"command": "rm -rf /srv/x", "pattern_key": "p",
                     "pattern_keys": ["p"], "description": "danger"},
                )

            th = threading.Thread(target=agent)
            th.start()
            th.join(timeout=10)

            # The agent thread returned WITHOUT an answer having been given.
            assert not th.is_alive(), "agent thread parked instead of releasing"
            assert result["d"]["released"] is True
            assert result["d"]["resolved"] is False
            assert A.has_blocking_approval(sk) is True, "card must stay live"

            # The human answers later; that must resume the run.
            assert A.resolve_gateway_approval(sk, "once") == 1
            assert done.wait(5), "answer did not fire the wake callback"
            assert woke["decision"]["choice"] == "once"
            assert woke["decision"]["resolved"] is True
            assert woke["command"] == "rm -rf /srv/x"
        finally:
            _clear(sk)

    def test_deny_reason_reaches_the_wake(self, release_mode):
        sk = "tjs226-deny"
        _clear(sk)
        try:
            got = {}
            done = threading.Event()
            A.register_gateway_wake(
                sk, lambda a, d: (got.update(d), done.set()))
            d = A._await_gateway_decision(
                sk, lambda x: None,
                {"command": "c", "pattern_key": "p", "pattern_keys": ["p"],
                 "description": "danger"},
            )
            assert d["released"] is True
            A.resolve_gateway_approval(sk, "deny", reason="not now")
            assert done.wait(5)
            assert got["choice"] == "deny"
            assert got["reason"] == "not now"
        finally:
            _clear(sk)

    def test_released_result_is_pending_not_denied(self, release_mode):
        """A released card must never be reported to the model as a refusal."""
        r = A.released_pending_result(
            {"request_id": "abc"}, pattern_key="p", description="danger")
        assert r["approved"] is False
        assert r["outcome"] == "pending"
        assert r["status"] == "approval_released"
        assert r["user_consent"] is False
        assert "PENDING APPROVAL" in r["message"]
        assert "Do NOT retry" in r["message"]
        # Must not claim the user refused or that the request timed out.
        low = r["message"].lower()
        assert "denied" not in low and "timed out" not in low

    def test_session_clear_does_not_fake_a_denial(self, release_mode):
        """/new or /stop must not push a fabricated 'denied' wake."""
        sk = "tjs226-clear"
        _clear(sk)
        try:
            fired = []
            A.register_gateway_wake(sk, lambda a, d: fired.append(d))
            A._await_gateway_decision(
                sk, lambda x: None,
                {"command": "c", "pattern_key": "p", "pattern_keys": ["p"]},
            )
            A.clear_session(sk)
            assert fired == [], "session teardown fabricated an answer"
            assert A.has_blocking_approval(sk) is False
        finally:
            _clear(sk)

    def test_mcp_elicitation_still_blocks(self, release_mode):
        """allow_release=False keeps protocols with no 'pending' state safe."""
        sk = "tjs226-elicit"
        _clear(sk)
        try:
            A.register_gateway_wake(sk, lambda a, d: None)
            out = {}

            def agent():
                out["d"] = A._await_gateway_decision(
                    sk, lambda x: None,
                    {"command": "c", "pattern_key": "p", "pattern_keys": ["p"]},
                    allow_release=False,
                )

            th = threading.Thread(target=agent, daemon=True)
            th.start()
            th.join(timeout=2)
            assert th.is_alive(), "elicitation must still block for its answer"
            A.resolve_gateway_approval(sk, "once")
            th.join(timeout=5)
            assert out["d"]["resolved"] is True
            assert not out["d"].get("released")
        finally:
            _clear(sk)

    def test_default_config_keeps_blocking_behaviour(self, blocking_mode):
        """Opt-in only: an unconfigured fleet is unchanged."""
        sk = "tjs226-default"
        _clear(sk)
        try:
            A.register_gateway_wake(sk, lambda a, d: None)
            assert A._thread_release_enabled() is False
            assert A._can_release_thread(sk) is False
        finally:
            _clear(sk)
