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


# ── Round 19: path resolution in the lazy-override window ──────────────


@pytest.mark.parametrize("overrides", [
    {"env_type": "docker"},
    {"docker_image": "python:3.11"},
    {"modal_image": "python:3.11"},
])
def test_path_resolution_honours_a_registered_override(
        register_override, monkeypatch, overrides):
    """A declared remote backend must drive path semantics too.

    Round 15 taught ``_backend_shares_the_host_filesystem`` that a registered
    override exists BEFORE the environment object is lazily created. The
    sibling ``_terminal_env_type_for_task`` never learned it, so in that same
    window path resolution used HOST semantics for a task whose backend is a
    container.
    """
    monkeypatch.setenv("TERMINAL_ENV", "local")
    register_override("default", overrides)

    import tools.terminal_tool as TT
    with TT._env_lock:
        assert TT._active_environments.get("default") is None

    assert FT._uses_container_paths("default") is True, (
        f"override {overrides} declared a container backend but path "
        "resolution used host semantics")


def test_host_symlinks_are_not_dereferenced_during_the_lazy_window(
        register_override, monkeypatch, tmp_path):
    """The behavioural regression: the RESOLVED PATH, not the classification.

    ``_resolve_path_for_task`` dereferenced a HOST symlink while the task's
    backend is Docker, so the requested path was rewritten in the wrong
    filesystem namespace before the gated write ever obtained its backend.
    The container namespace is not the host's — a host symlink there means
    nothing and must not be followed.
    """
    host_target = tmp_path / "host_target.md"
    host_target.write_text("host side\n")
    link = tmp_path / "notes.md"
    link.symlink_to(host_target)

    monkeypatch.setenv("TERMINAL_ENV", "local")
    register_override("default", {"env_type": "docker"})

    resolved = str(FT._resolve_path_for_task(str(link), "default"))

    assert resolved != str(host_target), (
        "a host symlink was dereferenced to its host target while the task's "
        f"backend is Docker — the path was rewritten in the wrong filesystem "
        f"namespace (got {resolved})")


def test_local_task_still_resolves_host_symlinks(monkeypatch, tmp_path):
    """Fail-open guard: a genuinely local task keeps host path semantics."""
    host_target = tmp_path / "host_target.md"
    host_target.write_text("host side\n")
    link = tmp_path / "notes.md"
    link.symlink_to(host_target)

    monkeypatch.setenv("TERMINAL_ENV", "local")

    assert FT._uses_container_paths("default") is False
    assert FT._resolve_path_for_task(str(link), "default")


def test_a_cwd_only_override_does_not_change_path_semantics(
        register_override, monkeypatch):
    """A workspace hint is not a backend declaration, here either."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    register_override("default", {"cwd": "/tmp/somewhere"})

    assert FT._uses_container_paths("default") is False


def test_an_explicit_local_override_keeps_host_path_semantics(
        register_override, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    register_override("default", {"env_type": "local"})

    assert FT._uses_container_paths("default") is False


def test_a_live_environment_still_wins_over_the_override(
        register_override, monkeypatch):
    """Once the object exists it is the ground truth, not the declaration.

    The override says what was ASKED for; a live environment is what was
    BUILT. If they disagree, the built one is performing the operations.
    """
    import tools.terminal_tool as TT

    monkeypatch.setenv("TERMINAL_ENV", "local")
    register_override("default", {"env_type": "docker"})
    env = type("LocalEnvironment", (), {"cwd": "/tmp"})()
    with TT._env_lock:
        TT._active_environments["default"] = env
    try:
        assert FT._terminal_env_type_for_task("default") == "local"
    finally:
        with TT._env_lock:
            TT._active_environments.pop("default", None)
        FT.clear_file_ops_cache("default")


def test_every_isolation_override_key_maps_to_a_backend():
    """A new isolation key must not silently leave paths on host semantics.

    ``_ISOLATION_OVERRIDE_KEYS`` is the terminal layer's own list of keys
    that declare a remote backend. Every one except ``env_type`` (read
    directly) must appear in the image-key map, or a task declaring its
    backend that way gets host path resolution.

    Behavioural, not structural: each mapping is exercised through
    ``_uses_container_paths`` so a present-but-wrong value fails too — the
    round-18 lesson.
    """
    from tools.terminal_tool import _ISOLATION_OVERRIDE_KEYS, _is_container_backend

    expected = set(_ISOLATION_OVERRIDE_KEYS) - {"env_type"}
    missing = expected - set(FT._OVERRIDE_IMAGE_KEY_BACKENDS)
    assert not missing, (
        f"isolation override keys with no backend mapping: {sorted(missing)}")

    for key, backend in FT._OVERRIDE_IMAGE_KEY_BACKENDS.items():
        assert _is_container_backend(backend), (
            f"{key} maps to {backend!r}, which is not a container backend")


@pytest.mark.parametrize("key", ["docker_image", "modal_image",
                                 "singularity_image", "daytona_image"])
def test_each_image_override_key_forces_container_paths(
        register_override, monkeypatch, key):
    """Each mapping proven by behaviour, not by reading the table."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    register_override("default", {key: "some-image"})

    assert FT._uses_container_paths("default") is True, key
