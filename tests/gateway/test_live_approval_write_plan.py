"""TJS-259 round 9: write_file's own path plan, and a digest that cannot hang.

Round 9's opposite-model review found two defects that round 8 left behind:

1. Round 8 gave ``patch_tool`` one immutable resolved-path plan, but
   ``write_file_tool`` was not changed: it resolved the raw path for
   authorization (``_resolve_write_targets``) and then resolved it a SECOND
   time after both approval gates, and locked, drift-checked and wrote THAT
   second target. Retarget a symlinked ``AGENTS.md`` in that window and the
   reviewed file is untouched while an unreviewed one is overwritten, with no
   second card.
2. ``ShellFileOperations.content_digest`` opens the path before proving it is
   a regular file. ``open(fifo, "rb")`` blocks until a writer appears rather
   than raising, so a gated FIFO strands the tool thread forever instead of
   failing closed as unreadable.
"""

import hashlib
import os
import threading
import time

import pytest

import tools.file_tools as FT
from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import write_file_tool


# ── Defect 1: write_file's reviewed path is its written path ────────────


def test_write_file_symlink_retarget_after_approval_cannot_redirect(
        monkeypatch, tmp_path):
    """The file write_file reviewed is the one it writes — or nothing is.

    ``AGENTS.md`` is a symlink. It is retargeted after both approval gates
    have passed (the released ``once`` grant is already redeemed) and before
    the write resolves its own target. On the unfixed code that second
    resolution follows the NEW link, so the bytes land in a file the user
    never saw on a card.

    The decoy holds the same content as the reviewed file so the write cannot
    fail for an unrelated reason and hide the redirection.
    """
    real_target = tmp_path / "real.md"
    real_target.write_text("reviewed contents\n")
    other = tmp_path / "unreviewed.md"
    other.write_text("reviewed contents\n")
    link = tmp_path / "AGENTS.md"
    link.symlink_to(real_target)

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(link),
                       content="approved replacement\n").released
        gw.answer("once")

        real_gate = FT._check_approval_required_write
        fired = {"n": 0}

        def _retarget_after_gate(*a, **k):
            result = real_gate(*a, **k)
            if result is None and fired["n"] == 0:
                fired["n"] = 1
                link.unlink()
                link.symlink_to(other)
            return result

        monkeypatch.setattr(FT, "_check_approval_required_write",
                            _retarget_after_gate)
        gw.call(write_file_tool, path=str(link),
                content="approved replacement\n")

        assert fired["n"] == 1, "the retarget injection point was never reached"
        assert other.read_text() == "reviewed contents\n", (
            "write_file followed a symlink retargeted AFTER approval — an "
            "unreviewed file was overwritten with no second card")


def test_write_file_backend_is_given_the_pre_gate_planned_path(
        monkeypatch, tmp_path):
    """Structural guard: the path handed to the backend is the reviewed plan.

    Asserting only on contents passes for the wrong reason the moment someone
    reintroduces a re-resolution that happens to agree on a plain path, so
    this pins the invariant itself.
    """
    agents = tmp_path / "AGENTS.md"
    agents.write_text("alpha\n")
    planned = str(FT._resolve_path_for_task(str(agents), "default"))
    got: list[str] = []

    real_ops = FT._get_file_ops

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def write_file(self, path, *a, **k):
            got.append(str(path))
            return self._inner.write_file(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(agents),
                       content="beta\n").released
        gw.answer("once")
        monkeypatch.setattr(FT, "_get_file_ops",
                            lambda tid="default": _Spy(real_ops(tid)))
        gw.call(write_file_tool, path=str(agents), content="beta\n")

    assert agents.read_text() == "beta\n", "the approved write did not land"
    assert got == [planned], (
        f"the backend was given {got}, not the reviewed plan [{planned}] — "
        "a post-authorization resolution decided the write target")


# ── Defect 2: the digest cannot block on a non-regular file ─────────────


def _digest_with_deadline(path: str, seconds: float = 20.0):
    """Run ``_path_state_digest`` on a worker and fail if it does not return.

    A hang is the defect, so the assertion has to be "it came back", not a
    value comparison — a blocked ``open()`` would otherwise wedge the whole
    test session with no diagnosis.
    """
    box: dict[str, object] = {}

    def _run():
        try:
            box["value"] = FT._path_state_digest(path)
        except BaseException as exc:  # pragma: no cover - diagnostic only
            box["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    t0 = time.time()
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        pytest.fail(
            f"_path_state_digest({path!r}) did not return within {seconds}s — "
            "the backend opened a non-regular file and blocked")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"], time.time() - t0


def test_digest_of_a_fifo_fails_closed_instead_of_hanging(tmp_path):
    """A FIFO must report unreadable, promptly.

    ``open(fifo, "rb")`` blocks until a writer opens the other end; it does
    not raise, so the "let open() raise on a non-regular path" guard never
    fires. The state of a FIFO is not something a user can review, so the
    only correct answer is unreadable (which blocks the write).
    """
    fifo = tmp_path / "AGENTS.md"
    os.mkfifo(fifo)

    value, elapsed = _digest_with_deadline(str(fifo))

    assert value == FT._STATE_UNREADABLE, (
        f"a FIFO produced state {value!r} — it must fail closed")
    assert elapsed < 20.0


def test_gated_write_to_a_fifo_blocks_without_raising_a_card(tmp_path):
    """End to end: the gated path blocks, and never strands the tool."""
    fifo = tmp_path / "AGENTS.md"
    os.mkfifo(fifo)

    box: dict[str, object] = {}

    def _run():
        with LiveApprovalGateway() as gw:
            box["result"] = gw.call(write_file_tool, path=str(fifo),
                                    content="x\n")
            box["cards"] = gw.card_count

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(30.0)
    if worker.is_alive():
        pytest.fail("a gated write to a FIFO stranded the tool thread")

    assert box["result"].blocked, box["result"]
    assert box["cards"] == 0


def test_directory_state_is_unreadable_not_a_digest(tmp_path):
    """Directories are the other non-regular case reaching this path."""
    d = tmp_path / "AGENTS.md"
    d.mkdir()

    value, _ = _digest_with_deadline(str(d))
    assert value == FT._STATE_UNREADABLE, value


def test_regular_file_digest_is_unaffected_by_the_guard(tmp_path):
    """The type guard must not change the answer for ordinary files."""
    p = tmp_path / "AGENTS.md"
    payload = b"still the real sha256\n\x00\xfe"
    p.write_bytes(payload)

    value, _ = _digest_with_deadline(str(p))
    assert value == hashlib.sha256(payload).hexdigest()


def test_symlink_to_a_regular_file_still_digests(tmp_path):
    """The guard must follow symlinks, not reject them.

    ``lstat`` alone would call every symlinked target unreadable and block
    ordinary approved writes — a fail-closed regression, not a fix.
    """
    real = tmp_path / "real.md"
    real.write_bytes(b"linked payload\n")
    link = tmp_path / "AGENTS.md"
    link.symlink_to(real)

    value, _ = _digest_with_deadline(str(link))
    assert value == hashlib.sha256(b"linked payload\n").hexdigest()
