"""TJS-259 round 11: the preview must read the same filesystem as the write.

Round 10's opposite-model review found two defects, both in code I wrote in
round 9.

1. ``_current_file_text`` builds the approval card's preview from the HOST
   filesystem, while the authorization identity and the mutation both go
   through ``_get_file_ops(task_id)``. Under a container/SSH backend the
   target can be absent on the host and present on the backend, so the card
   renders an overwrite of a real remote file as ``+1/-0`` with no preimage:
   the user approves a change that is not the one that will be written.
   Round 9's non-blocking host read fixed the FIFO hang but left the wrong
   filesystem.

2. ``content_digest`` checks ``os.stat`` and then separately calls ``open``.
   A path that changes type between the two — a symlink retargeted from a
   regular file to a FIFO — still blocks in ``open``. I used the correct
   fstat-on-the-descriptor pattern in ``_current_file_text`` in the same
   commit and did not apply it here.
"""

import hashlib
import os
import threading
import time

import tools.file_tools as FT
from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import write_file_tool


# ── Defect 1: preview reads the task backend, not the host ─────────────


def test_preview_text_is_read_through_the_task_file_backend(
        monkeypatch, tmp_path):
    """Structural: the preimage comes from `_get_file_ops`, not host I/O.

    This is the invariant the identity path already holds. Asserting it here
    is what stops the card and the write describing two different
    filesystems.
    """
    p = tmp_path / "AGENTS.md"
    p.write_text("host state\n")
    seen: list[str] = []
    real = FT._get_file_ops

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def read_file_raw(self, path, *a, **k):
            seen.append(str(path))
            return self._inner.read_file_raw(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(FT, "_get_file_ops", lambda tid="default": _Spy(real(tid)))
    FT._current_file_text(str(p), "default")

    assert any(str(p) in s for s in seen), (
        "the preview preimage was read from the host filesystem — under a "
        "container backend the card would describe a different file from "
        "the one the write touches")


def test_container_card_shows_the_backend_preimage_not_plus_one_minus_zero(
        monkeypatch, tmp_path):
    """The reviewer's reproduction, end to end through a real card.

    The target is ABSENT on the host and PRESENT on the backend — exactly the
    container/SSH shape. A host-read preview renders this as a brand new file
    (+1/-0) and omits the remote contents the write is about to destroy.
    """
    host_absent = tmp_path / "AGENTS.md"          # never created on the host
    backend_text = "REMOTE STATE THE USER MUST SEE\n"
    real = FT._get_file_ops

    class _RemoteBackend:
        """Reports a real file the host cannot see."""

        def __init__(self, inner):
            self._inner = inner

        def read_file_raw(self, path, *a, **k):
            if str(host_absent) in str(path):
                from tools.file_operations import ReadResult
                return ReadResult(content=backend_text)
            return self._inner.read_file_raw(path, *a, **k)

        def content_digest(self, path, *a, **k):
            if str(host_absent) in str(path):
                return hashlib.sha256(backend_text.encode()).hexdigest()
            return self._inner.content_digest(path, *a, **k)

        def path_exists(self, path, *a, **k):
            if str(host_absent) in str(path):
                return True
            return self._inner.path_exists(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(FT, "_get_file_ops",
                        lambda tid="default": _RemoteBackend(real(tid)))

    with LiveApprovalGateway() as gw:
        result = gw.call(write_file_tool, path=str(host_absent),
                         content="replacement\n")
        assert result.released, result.text
        assert gw.card_count == 1
        rendered = gw.cards[-1].rendered

    assert "REMOTE STATE THE USER MUST SEE" in rendered, (
        "the card omitted the backend preimage entirely — the user was "
        f"shown a creation, not the overwrite that will happen:\n{rendered}")


def test_preview_fails_closed_when_the_backend_cannot_be_read(
        monkeypatch, tmp_path):
    """A backend that cannot supply the reviewed text must block, not guess.

    Falling back to "" renders an existing-file overwrite as a creation —
    the dishonest card this defect is about. `PREVIEW_FAILED` already makes
    both gates block, so the failure has somewhere correct to go.
    """
    p = tmp_path / "AGENTS.md"
    p.write_text("host state\n")
    real = FT._get_file_ops

    class _Broken:
        def __init__(self, inner):
            self._inner = inner

        def read_file_raw(self, path, *a, **k):
            raise OSError("backend unreachable")

        def content_digest(self, path, *a, **k):
            return self._inner.content_digest(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(FT, "_get_file_ops",
                        lambda tid="default": _Broken(real(tid)))

    with LiveApprovalGateway() as gw:
        result = gw.call(write_file_tool, path=str(p), content="x\n")
        assert result.blocked, result.text
        assert gw.card_count == 0, (
            "a card was raised from a preview the backend could not supply")
    assert p.read_text() == "host state\n"


def test_absent_backend_target_still_previews_as_a_creation(
        monkeypatch, tmp_path):
    """Fail-open guard: a genuinely new file must still be previewable.

    "Cannot read" must mean unreachable, not missing — otherwise creating a
    protected file becomes impossible instead of merely gated.
    """
    p = tmp_path / "AGENTS.md"  # does not exist anywhere

    with LiveApprovalGateway() as gw:
        result = gw.call(write_file_tool, path=str(p), content="brand new\n")
        assert result.released, result.text
        assert gw.card_count == 1
    assert not p.exists()


# ── Defect 2: no check-then-open window in the digest ──────────────────


def _with_deadline(fn, seconds=20.0):
    box: dict[str, object] = {}

    def _run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # pragma: no cover - diagnostic only
            box["error"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t0 = time.time()
    t.start()
    t.join(seconds)
    return (box, t.is_alive(), time.time() - t0)


def test_digest_survives_a_regular_file_becoming_a_fifo_after_the_stat(
        tmp_path):
    """The reviewer's deterministic race, as a regression.

    The backend snippet is what runs in production, so the race is staged
    inside it: a ``sitecustomize``-free shim wraps the interpreter's
    ``os.stat`` so that the first call on the target returns regular-file
    metadata and THEN retargets the symlink to a FIFO. A check-then-open
    implementation blocks forever in ``open``; opening O_NONBLOCK first and
    fstat-ing the descriptor has no such window.

    The snippet is extracted from the production method rather than
    reimplemented, so this cannot pass against a snippet that was changed
    back.
    """
    real = tmp_path / "real.md"
    real.write_bytes(b"regular contents\n")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    link = tmp_path / "AGENTS.md"
    link.symlink_to(real)

    ops = FT._get_file_ops("default")
    captured: dict[str, str] = {}
    original_exec = ops._exec

    def _capture(command, *a, **k):
        if "hashlib" in str(command):
            captured["cmd"] = str(command)
        return original_exec(command, *a, **k)

    ops._exec = _capture
    try:
        ops.content_digest(str(real))
    finally:
        ops._exec = original_exec
    assert "cmd" in captured, "did not capture the production digest snippet"

    # Pull the snippet back out of the shell command exactly as it will run.
    import shlex
    parts = shlex.split(captured["cmd"])
    snippet = parts[-1]
    snippet = snippet.replace(repr(str(real)), repr(str(link)))
    assert str(link) in snippet

    # The shim lets the real syscall answer FIRST, then retargets the symlink
    # to a FIFO — which is exactly the race: the type check sees a regular
    # file, and whatever runs next meets a FIFO. Under stat-then-open the
    # following builtin ``open`` blocks forever. Under open-then-fstat the
    # descriptor is already held on the regular file, so the retarget cannot
    # reach it and the digest is of the file that was actually checked.
    shim = (
        "import os\n"
        "_real_stat, _real_open = os.stat, os.open\n"
        "_fired = []\n"
        f"_link = {str(link)!r}\n"
        f"_fifo = {str(fifo)!r}\n"
        "def _flip(path):\n"
        "    if not _fired and str(path) == _link:\n"
        "        _fired.append(1)\n"
        "        os.unlink(_link)\n"
        "        os.symlink(_fifo, _link)\n"
        "def _stat(path, *a, **k):\n"
        "    r = _real_stat(path, *a, **k)\n"
        "    _flip(path)\n"
        "    return r\n"
        "def _open(path, *a, **k):\n"
        "    r = _real_open(path, *a, **k)\n"
        "    _flip(path)\n"
        "    return r\n"
        "os.stat, os.open = _stat, _open\n"
    )

    import subprocess
    import sys
    try:
        proc = subprocess.run(
            [sys.executable, "-c", shim + snippet],
            capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "the digest snippet blocked after the target became a FIFO — "
            "the check-then-open window is still there")

    out = proc.stdout.strip().splitlines()
    answer = out[-1] if out else ""
    assert answer != "", f"snippet produced nothing: {proc.stderr[:400]}"
    # The honest outcomes are: the digest of the regular file the type check
    # actually held, or a fail-closed sentinel. Never a hang, and never a
    # digest of the FIFO.
    assert answer in (hashlib.sha256(b"regular contents\n").hexdigest(),
                      ops._DIGEST_UNREADABLE), answer


def test_digest_of_a_plain_fifo_still_fails_closed(tmp_path):
    fifo = tmp_path / "AGENTS.md"
    os.mkfifo(fifo)

    box, still_running, _ = _with_deadline(
        lambda: FT._path_state_digest(str(fifo)))

    assert not still_running, "digest of a FIFO hung"
    assert box.get("value") == FT._STATE_UNREADABLE


def test_regular_file_digest_is_still_the_real_sha256(tmp_path):
    p = tmp_path / "AGENTS.md"
    payload = b"unchanged behaviour\n\x00\xff"
    p.write_bytes(payload)

    assert FT._path_state_digest(str(p)) == hashlib.sha256(payload).hexdigest()


def test_symlink_to_a_regular_file_still_digests(tmp_path):
    real = tmp_path / "real.md"
    real.write_bytes(b"linked\n")
    link = tmp_path / "AGENTS.md"
    link.symlink_to(real)

    assert FT._path_state_digest(str(link)) == \
        hashlib.sha256(b"linked\n").hexdigest()
