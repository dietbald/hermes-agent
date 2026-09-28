"""TJS-259 round 14: task-scoped backend, and a preimage that cannot lie.

Round 13's opposite-model review found two defects, both mine.

1. ``_backend_shares_the_host_filesystem(task_id)`` accepts a task id and
   ignores it, reading only the GLOBAL ``_get_env_config()``. A session can
   have global ``env_type=local`` while ``_active_environments[task_id]`` is
   Docker/SSH via a task override. ``_terminal_env_type_for_task`` — which
   already lived in the same module — detects exactly that. So the gate took
   the host fast path against a container backend and a backend-only
   ``notes.md -> AGENTS.md`` link was written with zero cards.

2. ``read_text_bounded`` returns the first 4 MiB and never marks the result
   truncated, so ``_current_file_text`` treats a prefix as the complete
   preimage. Write content equal to a 5 MiB file's first 4 MiB — deleting
   only the unseen tail — and the diff is empty, the preview collapses to
   ``None``, and the gate degrades to a target-only card with no change
   summary. That is the preview-failure degradation the gate exists to block,
   reached without an exception being raised.
"""

import os

import pytest

import tools.file_tools as FT
from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import write_file_tool


# ── Defect 1: classification must follow the TASK's backend ────────────


#: Dedicated task id for the fake-backend tests. The "default" task's
#: file-ops object is shared by every other test in the session, and a fake
#: environment that cannot execute must never become its cached backend.
TASK = "tjs259-round14-task"


class _FakeDockerEnvironment:
    """Stands in for a task-scoped DockerEnvironment entry.

    Classification is by class name, which is how ``_terminal_env_type_for_task``
    already identifies backends.
    """

    def __init__(self, cwd="/workspace"):
        self.cwd = cwd

    def execute(self, command, cwd=None, **kwargs):  # pragma: no cover
        raise AssertionError("the fake backend must not be executed")


@pytest.fixture
def install_task_environment(request):
    """Install a task-scoped environment, and clean up everything it touches.

    Two pieces of bookkeeping are easy to miss and both cause cross-test
    bleed:

    * ``_get_file_ops`` caches a ShellFileOperations per task id, so a fake
      environment left behind poisons every later test in the session.
    * ``_resolve_container_task_id`` collapses an arbitrary task id back to
      ``"default"`` unless a per-task env override is registered — that
      collapsing is deliberate (subagents share the parent container), so a
      task-scoped backend is only reachable through the override mechanism.
      Registering it is what makes this test exercise the real path rather
      than a fiction.
    """
    import tools.terminal_tool as TT

    installed: list[str] = []

    def _install(task_id, env, env_type="docker"):
        # The override is what makes ``_resolve_container_task_id`` keep this
        # task id distinct instead of collapsing it to "default", so it has to
        # be registered — and it must DECLARE THE SAME BACKEND as the
        # environment object, or the fixture is asserting two different
        # things at once.
        TT.register_task_env_overrides(task_id, {"env_type": env_type})
        FT.clear_file_ops_cache(task_id)
        with TT._env_lock:
            TT._active_environments[task_id] = env
        installed.append(task_id)

    yield _install

    for task_id in installed:
        with TT._env_lock:
            TT._active_environments.pop(task_id, None)
        FT.clear_file_ops_cache(task_id)
        try:
            # ``register_task_env_overrides(task_id, {})`` leaves an empty
            # dict behind, which still reads as "registered". Clear it
            # properly or the docker declaration leaks into every later test
            # on the same task id.
            TT.clear_task_env_overrides(task_id)
        except Exception:
            pass


def test_task_scoped_docker_is_not_treated_as_the_host(install_task_environment, monkeypatch):
    """Global config says local; the TASK's environment says Docker.

    The gate must follow the task, because the task's environment is what
    performs the write.
    """
    monkeypatch.setenv("TERMINAL_ENV", "local")
    install_task_environment(TASK, _FakeDockerEnvironment())

    assert FT._terminal_env_type_for_task(TASK) == "docker", (
        "precondition: the task environment should classify as docker")
    assert FT._backend_shares_the_host_filesystem(TASK) is False, (
        "a task-scoped Docker backend was classified as sharing the host "
        "filesystem, so approval gating resolved paths on the wrong machine")


def test_task_scoped_backend_symlink_to_a_protected_file_is_gated(
        install_task_environment, monkeypatch, tmp_path):
    """The reviewer's reproduction, through the public tool.

    Global config local, a task-scoped Docker environment, backend-only
    ``notes.md -> AGENTS.md``. Under the defect the host fast path missed the
    link entirely and the protected file was written with no card.

    The public tool runs on the ``"default"`` task, so the Docker environment
    is installed there — that is the exact shape the reviewer reproduced (a
    live task environment that the global config does not describe).
    """
    monkeypatch.setenv("TERMINAL_ENV", "local")

    protected = tmp_path / "AGENTS.md"
    protected.write_text("protected instructions\n")
    notes = tmp_path / "notes.md"
    notes.write_text("ordinary host file\n")

    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    inner = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))

    class _RemoteLinks:
        def __init__(self, backing):
            self._inner = backing

        def realpath(self, path, *a, **k):
            if str(path) == str(notes):
                return str(protected)
            return self._inner.realpath(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    install_task_environment("default", _FakeDockerEnvironment())
    monkeypatch.setattr(FT, "_get_file_ops",
                        lambda tid="default": _RemoteLinks(inner))

    with LiveApprovalGateway() as gw:
        result = gw.call(write_file_tool, path=str(notes),
                         content="INJECTED\n")
        assert gw.card_count == 1, (
            "a task-scoped backend symlink into a protected file raised no "
            f"card: {result.text[:200]}")
        assert result.released

    assert notes.read_text() == "ordinary host file\n"


def test_task_scoped_local_still_uses_the_fast_path(install_task_environment, monkeypatch, tmp_path):
    """A task-scoped LOCAL environment keeps the optimisation."""
    from tools.environments.local import LocalEnvironment

    monkeypatch.setenv("TERMINAL_ENV", "local")
    install_task_environment(TASK, LocalEnvironment(cwd=str(tmp_path)),
                             env_type="local")

    assert FT._backend_shares_the_host_filesystem(TASK) is True


def test_classification_failure_fails_closed(install_task_environment, monkeypatch):
    """An environment nobody can classify must not read as local."""
    class _Inscrutable:
        cwd = "/"

    monkeypatch.setenv("TERMINAL_ENV", "local")
    install_task_environment(TASK, _Inscrutable())

    assert FT._backend_shares_the_host_filesystem(TASK) is False


def test_global_local_with_no_task_environment_is_local(monkeypatch):
    """The ordinary case: no task override, global local."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    assert FT._backend_shares_the_host_filesystem("default") is True


# ── Defect 2: a truncated preimage must never look complete ────────────


def test_over_cap_preimage_is_reported_truncated(tmp_path):
    """The backend must say when it returned only a prefix."""
    big = tmp_path / "AGENTS.md"
    big.write_bytes(b"x" * (3 * 1024 * 1024))

    ops = FT._get_file_ops("default")
    result = ops.read_text_bounded(str(big), max_bytes=1024 * 1024)

    assert result.truncated is True, (
        "a bounded read returned a prefix without marking it truncated — the "
        "caller cannot tell a complete file from the head of a large one")
    assert len(result.content) <= 1024 * 1024


def test_under_cap_preimage_is_not_marked_truncated(tmp_path):
    """And a whole file must not be marked truncated."""
    p = tmp_path / "AGENTS.md"
    p.write_text("small\n")

    ops = FT._get_file_ops("default")
    result = ops.read_text_bounded(str(p), max_bytes=1024 * 1024)

    assert result.truncated is False
    assert result.content == "small\n"


def test_current_file_text_fails_closed_on_a_truncated_preimage(tmp_path):
    """An incomplete preimage is not a preimage.

    The user cannot review a change against bytes nobody read, so this must
    raise rather than hand back a prefix that looks whole.
    """
    big = tmp_path / "AGENTS.md"
    big.write_bytes(b"x" * (3 * 1024 * 1024))

    ops = FT._get_file_ops("default")
    original = ops.read_text_bounded

    def _tiny_cap(path, max_bytes=4 * 1024 * 1024):
        return original(path, max_bytes=1024 * 1024)

    ops.read_text_bounded = _tiny_cap
    saved = FT._get_file_ops
    try:
        FT._get_file_ops = lambda tid="default": ops  # type: ignore[assignment]
        with pytest.raises(FT._PreimageUnavailable):
            FT._current_file_text(str(big), "default")
    finally:
        ops.read_text_bounded = original
        FT._get_file_ops = saved  # type: ignore[assignment]


def test_tail_deleting_write_to_an_over_cap_file_is_blocked(
        monkeypatch, tmp_path):
    """The reviewer's reproduction, end to end.

    A 5 MiB ``AGENTS.md``; the requested content is exactly its first 4 MiB,
    so the mutation is "delete the unseen final 1 MiB". Under the defect the
    diff was empty, the preview collapsed to ``None``, and the gate rendered
    a target-only card with no change summary — then the approved write
    truncated the file.

    Correct behaviour is to BLOCK: the preimage could not be established, so
    no honest card can be built.
    """
    big = tmp_path / "AGENTS.md"
    head = b"h" * (4 * 1024 * 1024)
    big.write_bytes(head + b"t" * (1024 * 1024))
    original_size = big.stat().st_size

    with LiveApprovalGateway() as gw:
        result = gw.call(write_file_tool, path=str(big),
                         content=head.decode())
        assert result.blocked, (
            "a write that deletes only the unreadable tail of an over-cap "
            f"file was not blocked: {result.text[:300]}")
        assert gw.card_count == 0, (
            "a card was raised for a change the user could not have seen")

    assert big.stat().st_size == original_size, "the file was truncated"


def test_a_normal_sized_protected_write_still_shows_its_diff(tmp_path):
    """Fail-open guard: ordinary gated writes still render a real diff."""
    p = tmp_path / "AGENTS.md"
    p.write_text("alpha\n")

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(p), content="beta\n").released
        assert gw.card_count == 1
        rendered = gw.cards[-1].rendered

    assert "alpha" in rendered and "beta" in rendered, rendered


def test_an_under_cap_large_file_still_previews(tmp_path):
    """A big-but-readable file must still be previewable, not blocked."""
    p = tmp_path / "AGENTS.md"
    p.write_bytes(b"a" * (2 * 1024 * 1024))

    text = FT._current_file_text(str(p), "default")
    assert len(text) == 2 * 1024 * 1024


def test_absent_target_still_previews_as_a_creation(tmp_path):
    """The fail-closed rule must not swallow honest creations."""
    p = tmp_path / "AGENTS.md"
    assert FT._current_file_text(str(p), "default") == ""
