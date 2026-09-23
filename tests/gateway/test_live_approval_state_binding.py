"""TJS-259 round 7: the state binding must be real, cheap and lock-safe.

Round 6 bound authorization to the source state. Round 7's opposite-model
review found four ways that binding did not hold:

1. the state was hashed on the HOST while the write goes through the
   task's file backend — under a container those are different filesystems;
2. the ``unreadable`` sentinel was a stable literal, so it matched itself and
   two different unreadable states shared a grant;
3. the state was captured before ``file_state.lock_path``, leaving a window
   for a concurrent subagent — the writer the lock exists to serialize;
4. identity (and its hashing) was built before either gate decided whether
   approval even applied, so every ordinary write paid for it.

Each test below asserts on what is on disk, or on the backend actually used.
"""

import os
import stat

import pytest

import tools.file_tools as FT
from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import patch_tool, write_file_tool


@pytest.fixture
def agents_md(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("state shown on card\n")
    return p


# ── Defect 1: state must be read through the same backend that writes ──


def test_state_is_read_through_the_task_file_backend(monkeypatch, agents_md):
    """The digest must come from `_get_file_ops`, not the host filesystem.

    A host-read digest is the container defect: two different remote states
    both map to the same host path, so their digests collide. Asserting the
    read goes through the task's own file-ops object is what makes "the state
    I hashed" and "the state I overwrite" the same file by construction.
    """
    seen: list[str] = []
    real = FT._get_file_ops

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def content_digest(self, path, *a, **k):
            seen.append(path)
            return self._inner.content_digest(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(FT, "_get_file_ops", lambda tid="default": _Spy(real(tid)))

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(agents_md),
                       content="x\n").released

    assert any(str(agents_md) in s for s in seen), (
        "the approval state was not read through the task's file backend, so "
        "under a container backend it would hash a different filesystem")


def test_backend_state_change_invalidates_the_grant(monkeypatch, agents_md):
    """Backend-visible state moves ⇒ the released grant must not be redeemed.

    Drives the same collision the container defect produces: the backend
    reports a different state on resume, while nothing the host sees changed.
    """
    real = FT._get_file_ops
    swapped = {"done": False}

    class _Shifting:
        def __init__(self, inner):
            self._inner = inner

        def content_digest(self, path, *a, **k):
            if str(agents_md) in str(path) and swapped["done"]:
                return "d" * 64  # a state the user never reviewed
            return self._inner.content_digest(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(FT, "_get_file_ops",
                        lambda tid="default": _Shifting(real(tid)))

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(agents_md),
                       content="x\n").released
        gw.answer("once")
        swapped["done"] = True  # backend now reports a state never reviewed
        resumed = gw.call(write_file_tool, path=str(agents_md), content="x\n")

        assert agents_md.read_text() == "state shown on card\n", (
            "a grant was redeemed against a backend state the user never saw")
        assert not resumed.ok, resumed.text


# ── Defect 2: 'unreadable' must never be a reusable identity ────────────


def test_unreadable_state_blocks_instead_of_matching_itself(tmp_path):
    """A write-only protected file must not authorize anything.

    The old sentinel was a stable string, so unreadable-then-different-
    unreadable compared equal and reused the grant.
    """
    p = tmp_path / "AGENTS.md"
    p.write_text("secret\n")
    os.chmod(p, stat.S_IWUSR)  # 0200: write-only
    try:
        if os.access(p, os.R_OK):
            pytest.skip("running as root — unreadable state cannot be staged")
        with LiveApprovalGateway() as gw:
            first = gw.call(write_file_tool, path=str(p), content="x\n")
            assert first.blocked, first.text
            assert gw.card_count == 0, (
                "a card was raised for a state that cannot be read, so the "
                "user would be approving a change nobody can describe")
    finally:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


def test_unreadable_state_yields_no_identity():
    """Unit-level backstop for the sentinel rule itself."""
    assert FT._write_request_identity(
        ["/tmp/whatever"], mode="write", content="x",
        states=[FT._STATE_UNREADABLE]) is None


# ── Defect 3: the state must be re-verified under the write lock ────────


def test_state_change_between_gate_and_lock_aborts_the_write(
        monkeypatch, agents_md):
    """A concurrent writer inside the gate→lock window must not be overwritten.

    The change is injected at lock entry: after the grant is redeemed, before
    the bytes are written. That is exactly the window the lock exists to
    serialize, so the check has to live inside it.
    """
    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(agents_md),
                       content="approved body\n").released
        gw.answer("once")

        real_lock = FT.file_state.lock_path
        fired = {"n": 0}

        def _lock(p, *a, **k):
            if fired["n"] == 0:
                fired["n"] = 1
                agents_md.write_text("a concurrent subagent wrote this\n")
            return real_lock(p, *a, **k)

        monkeypatch.setattr(FT.file_state, "lock_path", _lock)
        resumed = gw.call(write_file_tool, path=str(agents_md),
                          content="approved body\n")

        assert fired["n"] == 1, "the injection point was never reached"
        assert agents_md.read_text() == "a concurrent subagent wrote this\n", (
            "the write landed on top of a state that changed after approval")
        assert not resumed.ok, resumed.text


# ── Defect 4: ungated writes must not pay for state hashing ─────────────


def test_ungated_write_does_not_hash_any_state(monkeypatch, tmp_path):
    """An ordinary write touches no approval gate, so it must hash nothing."""
    calls: list[str] = []
    monkeypatch.setattr(
        FT, "_path_state_digest",
        lambda p, t="default": calls.append(p) or "x")

    plain = tmp_path / "notes.txt"
    plain.write_text("hello\n")
    out = write_file_tool(path=str(plain), content="goodbye\n")

    assert "error" not in out.lower() or plain.read_text() == "goodbye\n"
    assert calls == [], (
        f"an ungated write hashed {calls} — on a large file this is an "
        "unbounded read that was previously metadata-bound")


def test_ungated_v4a_delete_does_not_hash_state(monkeypatch, tmp_path):
    """V4A Delete of an unprotected file must stay metadata-bound."""
    calls: list[str] = []
    monkeypatch.setattr(
        FT, "_path_state_digest",
        lambda p, t="default": calls.append(p) or "x")

    victim = tmp_path / "bulk.bin"
    victim.write_text("payload\n")
    patch_tool(mode="patch",
               patch=f"*** Begin Patch\n*** Delete File: {victim}\n*** End Patch\n")

    assert calls == [], f"an ungated V4A Delete hashed {calls}"


def test_gated_write_still_hashes_state(monkeypatch, agents_md):
    """The cheap path must not have disabled the protection it pays for."""
    calls: list[str] = []
    real = FT._path_state_digest
    monkeypatch.setattr(
        FT, "_path_state_digest",
        lambda p, t="default": (calls.append(p), real(p, t))[1])

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(agents_md),
                       content="x\n").released
    assert calls, "a protected write skipped state hashing entirely"
