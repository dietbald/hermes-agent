"""TJS-259 round 8: one immutable path plan, and a bounded-cost digest.

Round 8's opposite-model review found two defects:

1. ``patch_tool`` resolved the raw paths a SECOND time after the locks were
   held. Retarget a symlinked ``AGENTS.md`` while the card waits and the code
   locked and revalidated the original target while the later resolution
   rewrote the V4A header to the new one — the unreviewed file was written
   with no fresh card. Authorization, locking and mutation must all use one
   resolved plan.
2. The state digest pulled whole-file base64 across the backend boundary and
   hashed it in the agent process, materializing the file several times over
   (32 MiB probe: peak RSS ~51 MB → ~223 MB, 2.37 s). Hashing belongs on the
   backend, with only the hex digest crossing.
"""

import os
import resource
import time

import pytest

import tools.file_tools as FT
from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import patch_tool, write_file_tool


# ── Defect 1: the reviewed path is the locked path is the written path ──


def test_symlink_retarget_after_approval_cannot_redirect_the_write(
        monkeypatch, tmp_path):
    """The reviewed file is the one written — or nothing is.

    ``AGENTS.md`` is a symlink. It is retargeted after the state drift check,
    i.e. after the grant is redeemed and the locks are held, and before the
    bytes land. The second resolution used to happen after that point, so the
    write followed the NEW target while the gate had reviewed and locked the
    old one.

    The decoy deliberately holds the SAME content as the reviewed file: with a
    different body the patch fails to apply for an unrelated reason (hunk
    mismatch) and the redirection is hidden. Verified against the unfixed
    parent, where the backend really was handed the decoy path.
    """
    real_target = tmp_path / "real.md"
    real_target.write_text("reviewed contents\n")
    other = tmp_path / "unreviewed.md"
    other.write_text("reviewed contents\n")
    link = tmp_path / "AGENTS.md"
    link.symlink_to(real_target)

    patch_text = (f"*** Begin Patch\n*** Update File: {link}\n"
                  "@@\n-reviewed contents\n+approved replacement\n"
                  "*** End Patch\n")

    with LiveApprovalGateway() as gw:
        assert gw.call(patch_tool, mode="patch", patch=patch_text).released
        gw.answer("once")

        real_drift = FT._state_drift_error
        fired = {"n": 0}

        def _retarget_after_drift_check(*a, **k):
            result = real_drift(*a, **k)
            if fired["n"] == 0:
                fired["n"] = 1
                link.unlink()
                link.symlink_to(other)
            return result

        monkeypatch.setattr(FT, "_state_drift_error",
                            _retarget_after_drift_check)
        gw.call(patch_tool, mode="patch", patch=patch_text)

        assert fired["n"] == 1, "the retarget injection point was never reached"
        assert other.read_text() == "reviewed contents\n", (
            "the write followed a symlink retargeted AFTER approval — an "
            "unreviewed file was modified with no second card")


def test_backend_is_given_the_pre_lock_planned_path(monkeypatch, tmp_path):
    """Structural guard: the path handed to the backend is the reviewed plan.

    The defect was a second resolution deciding the write target after the
    locks were held. Asserting only on file contents passes for the wrong
    reason as soon as someone reintroduces a re-resolution that happens to
    agree on a non-symlinked path, so this pins the actual invariant: whatever
    reaches the backend equals the plan resolved before the gate ran.

    Note this does NOT forbid every post-lock resolution — staleness warnings
    and read-timestamp bookkeeping legitimately resolve paths and cannot
    redirect the write. Only the target handed to the mutation matters.
    """
    agents = tmp_path / "AGENTS.md"
    agents.write_text("alpha\n")
    planned = str(FT._resolve_path_for_task(str(agents), "default"))
    got: list[str] = []

    real_ops = FT._get_file_ops

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def patch_replace(self, path, *a, **k):
            got.append(str(path))
            return self._inner.patch_replace(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with LiveApprovalGateway() as gw:
        assert gw.call(patch_tool, mode="replace", path=str(agents),
                       old_string="alpha", new_string="beta").released
        gw.answer("once")
        monkeypatch.setattr(FT, "_get_file_ops",
                            lambda tid="default": _Spy(real_ops(tid)))
        gw.call(patch_tool, mode="replace", path=str(agents),
                old_string="alpha", new_string="beta")

    assert agents.read_text() == "beta\n", "the approved write did not land"
    assert got == [planned], (
        f"the backend was given {got}, not the reviewed/locked plan "
        f"[{planned}] — a post-lock resolution decided the write target")


# ── Defect 2: hashing happens on the backend, at bounded cost ───────────


def test_digest_does_not_transport_file_contents(monkeypatch, tmp_path):
    """The whole-file byte read must not be how state is hashed."""
    p = tmp_path / "AGENTS.md"
    p.write_text("x" * 4096)

    real = FT._get_file_ops
    used: list[str] = []

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def read_file_bytes(self, path, *a, **k):
            used.append("read_file_bytes")
            return self._inner.read_file_bytes(path, *a, **k)

        def content_digest(self, path, *a, **k):
            used.append("content_digest")
            return self._inner.content_digest(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(FT, "_get_file_ops", lambda tid="default": _Spy(real(tid)))
    FT._path_state_digest(str(p))

    assert "content_digest" in used, "state was not hashed on the backend"
    assert "read_file_bytes" not in used, (
        "state hashing transported the whole file across the backend "
        "boundary — that is the memory amplification this fixes")


def test_digest_of_a_large_file_is_bounded_in_memory_and_time(tmp_path):
    """The reviewer's 32 MiB probe, as a regression.

    Thresholds are deliberately loose: this asserts the amplification is gone
    (previously +170 MB RSS and 2.37 s), not a precise figure that would make
    the test flaky on a different box.
    """
    big = tmp_path / "AGENTS.md"
    with open(big, "wb") as fh:
        fh.truncate(32 * 1024 * 1024)

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.time()
    digest = FT._path_state_digest(str(big))
    elapsed = time.time() - t0
    growth_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before

    assert digest not in (FT._STATE_UNREADABLE, FT._STATE_ABSENT), digest
    assert len(digest) == 64
    assert growth_kib < 32 * 1024, (
        f"hashing a 32 MiB file grew peak RSS by {growth_kib} KiB — the file "
        "is still being materialized in this process")
    assert elapsed < 2.0, f"32 MiB digest took {elapsed:.2f}s"


def test_backend_digest_matches_the_files_real_sha256(tmp_path):
    """The digest must be the file's actual SHA-256, not a re-encoding."""
    import hashlib

    p = tmp_path / "AGENTS.md"
    payload = b"bytes that must hash exactly\n\x00\xff"
    p.write_bytes(payload)

    assert FT._path_state_digest(str(p)) == hashlib.sha256(payload).hexdigest()


def test_unreadable_still_blocks_through_the_backend_digest(tmp_path):
    """Round 7's defect 2 must stay fixed on the new digest path."""
    import stat

    p = tmp_path / "AGENTS.md"
    p.write_text("secret\n")
    os.chmod(p, stat.S_IWUSR)
    try:
        if os.access(p, os.R_OK):
            pytest.skip("running as root — unreadable state cannot be staged")
        assert FT._path_state_digest(str(p)) == FT._STATE_UNREADABLE
        with LiveApprovalGateway() as gw:
            assert gw.call(write_file_tool, path=str(p), content="x\n").blocked
            assert gw.card_count == 0
    finally:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


def test_absent_is_still_distinct_from_unreadable(tmp_path):
    assert FT._path_state_digest(str(tmp_path / "nope.md")) == FT._STATE_ABSENT
