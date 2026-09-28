"""TJS-259 round 15: a registered override counts before the env exists.

Round 14's opposite-model review found the last no-card path from the
classification work. ``register_task_env_overrides(task_id, {...})`` declares
a task's backend BEFORE ``_get_file_ops(task_id)`` lazily creates the
environment object. In that window ``_active_environments`` is empty, so my
round-14 "no task override: the global answer stands" branch trusted the
global ``local`` config, host realpath cleared the write, and the remote
backend created moments later performed it.

The registered override is the DECLARED INTENT and it exists earlier than the
environment object does, so it must be consulted first. ``resolve_task_overrides``
is already the canonical reader for it — raw id first, then collapsed
container id — and it is what the terminal and file layers use so they cannot
drift.
"""

import pytest

import tools.file_tools as FT
from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import write_file_tool


@pytest.fixture
def register_override():
    """Register a task env override and clean it up.

    Deliberately leaves ``_active_environments`` EMPTY for the task — the
    defect lives in exactly the window before the environment object is
    lazily created. Earlier suites in the same session can leave a live
    environment on ``"default"``, so any existing entry is removed and
    restored around the test rather than assumed absent.
    """
    import tools.terminal_tool as TT

    registered: list[str] = []
    evicted: dict[str, object] = {}

    def _register(task_id, overrides):
        with TT._env_lock:
            existing = TT._active_environments.pop(task_id, None)
        if existing is not None:
            evicted[task_id] = existing
        FT.clear_file_ops_cache(task_id)
        TT.register_task_env_overrides(task_id, overrides)
        registered.append(task_id)

    yield _register

    for task_id in registered:
        try:
            TT.clear_task_env_overrides(task_id)
        except Exception:
            pass
        FT.clear_file_ops_cache(task_id)
    for task_id, env in evicted.items():
        with TT._env_lock:
            TT._active_environments[task_id] = env


# ── The lazy-creation window ───────────────────────────────────────────


@pytest.mark.parametrize("overrides", [
    {"env_type": "docker"},
    {"docker_image": "python:3.11"},
    {"modal_image": "python:3.11"},
    {"singularity_image": "docker://python:3.11"},
    {"daytona_image": "python:3.11"},
])
def test_a_registered_override_is_not_host_local(
        register_override, monkeypatch, overrides):
    """A declared remote backend counts before its env object exists."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    register_override("default", overrides)

    import tools.terminal_tool as TT
    with TT._env_lock:
        assert TT._active_environments.get("default") is None, (
            "precondition: the environment must NOT exist yet")

    assert FT._backend_shares_the_host_filesystem("default") is False, (
        f"override {overrides} declared a remote backend but the gate still "
        "resolved paths on the host")


def test_lazy_override_symlink_to_a_protected_file_is_gated(
        register_override, monkeypatch, tmp_path):
    """The reviewer's reproduction, through the public tool.

    Global config local, a Docker override registered, NO environment object
    yet, and a backend-only ``notes.md -> AGENTS.md`` link. Under the defect
    host realpath cleared the write and the protected file changed with zero
    cards.
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

    register_override("default", {"env_type": "docker"})
    monkeypatch.setattr(FT, "_get_file_ops",
                        lambda tid="default": _RemoteLinks(inner))

    with LiveApprovalGateway() as gw:
        result = gw.call(write_file_tool, path=str(notes),
                         content="INJECTED\n")
        assert gw.card_count == 1, (
            "a lazily-created remote backend wrote a protected file with no "
            f"card: {result.text[:200]}")
        assert result.released

    assert notes.read_text() == "ordinary host file\n"


def test_an_explicit_local_override_keeps_the_fast_path(
        register_override, monkeypatch):
    """An override that declares local is still local."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    register_override("default", {"env_type": "local"})

    assert FT._backend_shares_the_host_filesystem("default") is True


def test_a_cwd_only_override_does_not_force_remote(
        register_override, monkeypatch):
    """A cwd override is a workspace hint, not a backend declaration.

    Treating it as remote would make every ACP/gateway session pay a
    subprocess per write for no security benefit — the fail-closed rule must
    not become fail-closed-on-everything.
    """
    monkeypatch.setenv("TERMINAL_ENV", "local")
    register_override("default", {"cwd": "/tmp/somewhere"})

    assert FT._backend_shares_the_host_filesystem("default") is True


def test_an_unrecognised_override_env_type_fails_closed(
        register_override, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    register_override("default", {"env_type": "some-future-sandbox"})

    assert FT._backend_shares_the_host_filesystem("default") is False


# ── Everything established in earlier rounds must still hold ───────────


def test_no_override_and_global_local_is_still_local(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    assert FT._backend_shares_the_host_filesystem("default") is True


def test_global_docker_is_still_not_local(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    assert FT._backend_shares_the_host_filesystem("default") is False


def test_ordinary_write_is_still_ungated(tmp_path):
    plain = tmp_path / "plain.md"
    plain.write_text("before\n")

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(plain), content="after\n").ok
        assert gw.card_count == 0
    assert plain.read_text() == "after\n"


def test_local_write_still_makes_no_backend_call(monkeypatch, tmp_path):
    """The fast path's cost reason must survive this change."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    plain = tmp_path / "plain.md"
    plain.write_text("before\n")

    calls: list[str] = []
    real = FT._get_file_ops

    class _Counting:
        def __init__(self, inner):
            self._inner = inner

        def realpath(self, path, *a, **k):
            calls.append(str(path))
            return self._inner.realpath(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(FT, "_get_file_ops",
                        lambda tid="default": _Counting(real(tid)))
    FT._plan_realpaths([str(plain)], {str(plain): str(plain)}, "default")

    assert calls == [], "the local fast path asked the backend to resolve"


def test_host_symlink_to_protected_is_still_gated(tmp_path):
    protected = tmp_path / "AGENTS.md"
    protected.write_text("protected\n")
    link = tmp_path / "notes.md"
    link.symlink_to(protected)

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(link), content="x\n").released
        assert gw.card_count == 1
    assert protected.read_text() == "protected\n"
