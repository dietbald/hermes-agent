"""TJS-259 round 12: the backend owns path semantics, not the host.

Round 11's opposite-model review found two defects. They are the same root
cause as rounds 9, 10 and 11: host-side file semantics applied to a path the
task backend owns.

1. ``_plan_realpaths`` calls host ``os.path.realpath``. A container/SSH
   backend can have ``notes.md -> AGENTS.md`` while the host sees an ordinary
   or absent ``notes.md``. The gate decision then answers "not protected"
   about the host's view while the write follows the backend's symlink into
   the protected file — a no-card protected-instruction write.

2. ``_current_file_text`` now reads through ``read_file_raw``, which checks
   ``-f``/size and then opens the SAME PATHNAME again in separate ``head``
   and ``cat`` commands. Retarget a symlink from a regular file to a FIFO
   after the size probe and the later ``head`` blocks. The non-blocking
   descriptor fix went to ``content_digest`` and not to the preview read,
   which round 10 made security-critical.
"""

import hashlib
import os
import threading
import time

import tools.file_tools as FT
from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import write_file_tool


def _with_deadline(fn, seconds=15.0):
    box: dict[str, object] = {}

    def _run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # pragma: no cover - diagnostic only
            box["error"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(seconds)
    return box, t.is_alive()


# ── Defect 1: realpath must be the BACKEND's, not the host's ───────────


def test_backend_only_symlink_to_a_protected_file_is_gated(
        monkeypatch, tmp_path):
    """The reviewer's reproduction, through the public tool.

    The backend sees ``notes.md -> AGENTS.md``; the host sees a plain file of
    that name. A host-side realpath answers "notes.md", which is not a
    protected basename, so both gates are skipped — while the write, which
    goes through the backend, lands in the protected file.
    """
    protected = tmp_path / "AGENTS.md"
    protected.write_text("protected instructions\n")
    notes = tmp_path / "notes.md"
    notes.write_text("ordinary host file\n")

    real = FT._get_file_ops

    class _RemoteLinks:
        """A backend whose symlink graph the host cannot see."""

        def __init__(self, inner):
            self._inner = inner

        def realpath(self, path, *a, **k):
            if str(path) == str(notes):
                return str(protected)
            return self._inner.realpath(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(FT, "_backend_shares_the_host_filesystem",
                        lambda tid="default": False)
    monkeypatch.setattr(FT, "_get_file_ops",
                        lambda tid="default": _RemoteLinks(real(tid)))

    with LiveApprovalGateway() as gw:
        result = gw.call(write_file_tool, path=str(notes),
                         content="INJECTED\n")
        assert gw.card_count == 1, (
            "a backend-only symlink into a protected file raised no card — "
            f"the gate used the host's view of the path: {result.text[:200]}")
        assert result.released


def test_plan_realpaths_asks_the_backend(monkeypatch, tmp_path):
    """Structural: resolution goes through the task file backend."""
    p = tmp_path / "AGENTS.md"
    p.write_text("x\n")
    seen: list[str] = []
    real = FT._get_file_ops

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def realpath(self, path, *a, **k):
            seen.append(str(path))
            return self._inner.realpath(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(FT, "_backend_shares_the_host_filesystem",
                        lambda tid="default": False)
    monkeypatch.setattr(FT, "_get_file_ops", lambda tid="default": _Spy(real(tid)))
    FT._plan_realpaths([str(p)], {str(p): str(p)}, "default")

    assert seen, ("path resolution for the gate decision did not go through "
                  "the task file backend")


def test_backend_realpath_failure_fails_closed(monkeypatch, tmp_path):
    """A backend that cannot resolve must gate, not wave the write through.

    "Cannot tell whether this is protected" has exactly one safe answer.
    """
    p = tmp_path / "notes.md"
    p.write_text("x\n")
    real = FT._get_file_ops

    class _Broken:
        def __init__(self, inner):
            self._inner = inner

        def realpath(self, path, *a, **k):
            raise OSError("backend unreachable")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(FT, "_backend_shares_the_host_filesystem",
                        lambda tid="default": False)
    monkeypatch.setattr(FT, "_get_file_ops",
                        lambda tid="default": _Broken(real(tid)))

    assert FT._write_gate_applies(
        [str(p)], "default",
        realpaths=FT._plan_realpaths([str(p)], {str(p): str(p)}, "default")), (
        "an unresolvable path was treated as ungated")


def test_ordinary_write_is_still_ungated(tmp_path):
    """Fail-open guard: the backend realpath must not gate everything."""
    plain = tmp_path / "plain.md"
    plain.write_text("before\n")

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(plain), content="after\n").ok
        assert gw.card_count == 0
    assert plain.read_text() == "after\n"


def test_host_symlink_to_protected_still_gated(tmp_path):
    """And the local-backend case must keep working."""
    protected = tmp_path / "AGENTS.md"
    protected.write_text("protected\n")
    link = tmp_path / "notes.md"
    link.symlink_to(protected)

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(link), content="x\n").released
        assert gw.card_count == 1
    assert protected.read_text() == "protected\n"


# ── Defect 2: the preview read is one descriptor, not three pathnames ──


def test_preview_read_survives_a_retarget_after_the_size_probe(tmp_path):
    """The reviewer's deterministic race, as a regression.

    ``read_file_raw`` probes ``-f``/size, then re-opens the same PATHNAME for
    ``head`` and ``cat``. Flipping the symlink to a FIFO after the probe makes
    the later open block. The preview read must hold ONE non-blocking
    descriptor across the type check and the read.
    """
    real = tmp_path / "real.md"
    real.write_bytes(b"regular contents\n")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    link = tmp_path / "AGENTS.md"
    link.symlink_to(real)

    ops = FT._get_file_ops("default")
    original_exec = ops._exec
    flipped = {"n": 0}

    def _exec_then_retarget(command, *a, **k):
        result = original_exec(command, *a, **k)
        # Flip AFTER the first probe answers about the regular file — the
        # window every separate-pathname reopen leaves open.
        if flipped["n"] == 0 and str(link) in str(command):
            flipped["n"] = 1
            link.unlink()
            link.symlink_to(fifo)
        return result

    ops._exec = _exec_then_retarget
    monkeypatched = FT._get_file_ops
    try:
        FT._get_file_ops = lambda tid="default": ops  # type: ignore[assignment]
        box, still_running = _with_deadline(
            lambda: FT._current_file_text(str(link), "default"))
    finally:
        ops._exec = original_exec
        FT._get_file_ops = monkeypatched  # type: ignore[assignment]

    assert flipped["n"] == 1, "the retarget injection point was never reached"
    assert not still_running, (
        "the preview read blocked after the target became a FIFO — separate "
        "pathname probes still leave a check-then-open window")


def test_preview_read_of_a_fifo_fails_closed(tmp_path):
    """A FIFO has no reviewable text; it must block, not hang."""
    fifo = tmp_path / "AGENTS.md"
    os.mkfifo(fifo)

    box, still_running = _with_deadline(
        lambda: FT._current_file_text(str(fifo), "default"))

    assert not still_running, "the preview read hung on a FIFO"
    assert isinstance(box.get("error"), FT._PreimageUnavailable), box


def test_gated_write_to_a_fifo_blocks_without_a_card(tmp_path):
    """End to end: no card, no hang, nothing written."""
    fifo = tmp_path / "AGENTS.md"
    os.mkfifo(fifo)

    box: dict[str, object] = {}

    def _run():
        with LiveApprovalGateway() as gw:
            box["result"] = gw.call(write_file_tool, path=str(fifo),
                                    content="x\n")
            box["cards"] = gw.card_count

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(30.0)
    assert not t.is_alive(), "a gated write to a FIFO stranded the tool thread"
    assert box["result"].blocked, box["result"]
    assert box["cards"] == 0


def test_preview_text_of_a_regular_file_is_unchanged(tmp_path):
    """The read must still return the file's actual text."""
    p = tmp_path / "AGENTS.md"
    p.write_text("line one\nline two\n")

    assert FT._current_file_text(str(p), "default") == "line one\nline two\n"


def test_absent_target_still_previews_as_a_creation(tmp_path):
    """Fail-open guard: a genuinely new file stays creatable."""
    p = tmp_path / "AGENTS.md"
    assert FT._current_file_text(str(p), "default") == ""

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(p), content="new\n").released
        assert gw.card_count == 1


def test_large_preview_read_is_bounded(tmp_path):
    """A huge target must not be pulled whole into the agent process."""
    import resource

    big = tmp_path / "AGENTS.md"
    with open(big, "wb") as fh:
        fh.write(b"a" * (8 * 1024 * 1024))

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.time()
    text = FT._current_file_text(str(big), "default")
    elapsed = time.time() - t0
    growth_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before

    assert isinstance(text, str)
    assert elapsed < 10.0, f"preview read took {elapsed:.1f}s"
    assert growth_kib < 128 * 1024, (
        f"preview read grew peak RSS by {growth_kib} KiB")


def test_binary_target_is_unreviewable_not_empty(tmp_path):
    """Binary bytes have no reviewable diff; must not look like a creation."""
    p = tmp_path / "AGENTS.md"
    p.write_bytes(b"\x00\x01\x02\xff" * 512)

    try:
        text = FT._current_file_text(str(p), "default")
    except FT._PreimageUnavailable:
        return
    assert text != "", (
        "a binary target previewed as empty — an overwrite would render as a "
        "creation")


def test_digest_still_matches_the_real_sha256(tmp_path):
    """Round 11's digest fix must survive this change."""
    p = tmp_path / "AGENTS.md"
    payload = b"unchanged\n\x00\xfe"
    p.write_bytes(payload)

    assert FT._path_state_digest(str(p)) == hashlib.sha256(payload).hexdigest()
