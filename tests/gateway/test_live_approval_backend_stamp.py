"""TJS-259 round 16: the backend stamp is authoritative, not the class name.

Round 15's opposite-model review found the last classification defect.
``_backend_shares_the_host_filesystem`` returned "local" on a class-name
substring BEFORE consulting ``_hermes_backend_name``. A plugin environment
named e.g. ``LocalisedSandboxEnvironment`` — or any duck-typed class whose
name merely contains "local" — was therefore treated as the host filesystem
even when the factory had stamped it ``docker``.

The source contract is explicit. ``agent/terminal_env_provider.py``:

    The factory stamps ``_hermes_backend_name`` on the returned object so
    file-path resolution can identify plugin backends without class-name
    sniffing.

I sniffed the class name first and consulted the stamp only as a fallback,
which is precisely backwards. A present stamp now decides; class names only
matter for the built-in environments, matched by exact identity rather than
substring.
"""

import pytest

import tools.file_tools as FT
from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import write_file_tool


@pytest.fixture
def install_env():
    """Bind an environment object to a task, and restore what was there."""
    import tools.terminal_tool as TT

    touched: list[str] = []
    evicted: dict[str, object] = {}

    def _install(task_id, env):
        with TT._env_lock:
            existing = TT._active_environments.pop(task_id, None)
        if existing is not None:
            evicted[task_id] = existing
        FT.clear_file_ops_cache(task_id)
        with TT._env_lock:
            TT._active_environments[task_id] = env
        touched.append(task_id)

    yield _install

    for task_id in touched:
        with TT._env_lock:
            TT._active_environments.pop(task_id, None)
        FT.clear_file_ops_cache(task_id)
        try:
            TT.clear_task_env_overrides(task_id)
        except Exception:
            pass
    for task_id, env in evicted.items():
        with TT._env_lock:
            TT._active_environments[task_id] = env


class NonLocalPluginEnvironment:
    """A duck-typed plugin backend whose class name contains 'local'.

    This is the shape the contract anticipates: plugin environments are NOT
    required to subclass BaseEnvironment, so their class names carry no
    guarantee at all. The stamp is what the factory promises.
    """

    def __init__(self, backend="docker"):
        self.cwd = "/workspace"
        self._hermes_backend_name = backend

    def execute(self, command, cwd=None, **kwargs):  # pragma: no cover
        raise AssertionError("the fake backend must not be executed")


# ── The stamp wins over the class name ─────────────────────────────────


@pytest.mark.parametrize("backend", ["docker", "ssh", "modal", "daytona",
                                     "singularity", "my-cloud-sandbox"])
def test_a_remote_stamp_beats_a_local_looking_class_name(
        install_env, monkeypatch, backend):
    """A stamped remote backend is remote, whatever the class is called."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    install_env("default", NonLocalPluginEnvironment(backend))

    assert FT._backend_shares_the_host_filesystem("default") is False, (
        f"a plugin environment stamped {backend!r} was classified as the "
        "host filesystem because its class name contains 'local'")


def test_stamped_plugin_symlink_to_a_protected_file_is_gated(
        install_env, monkeypatch, tmp_path):
    """The reviewer's reproduction, through the public tool.

    Active ``NonLocalPluginEnvironment`` stamped ``docker``, global config
    local, a backend-only alias into ``AGENTS.md``. Under the defect the
    class-name substring said "local", host realpath missed the alias, and
    the protected backend target was mutated with zero cards.
    """
    monkeypatch.setenv("TERMINAL_ENV", "local")

    protected = tmp_path / "AGENTS.md"
    protected.write_text("protected instructions\n")
    notes = tmp_path / "notes.md"
    notes.write_text("ordinary host file\n")

    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    inner = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))

    class _AliasingBackend:
        def __init__(self, backing):
            self._inner = backing

        def realpath(self, path, *a, **k):
            if str(path) == str(notes):
                return str(protected)
            return self._inner.realpath(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    install_env("default", NonLocalPluginEnvironment("docker"))
    monkeypatch.setattr(FT, "_get_file_ops",
                        lambda tid="default": _AliasingBackend(inner))

    with LiveApprovalGateway() as gw:
        result = gw.call(write_file_tool, path=str(notes),
                         content="INJECTED\n")
        assert gw.card_count == 1, (
            "a stamped plugin backend wrote a protected file with no card: "
            f"{result.text[:200]}")
        assert result.released

    assert notes.read_text() == "ordinary host file\n"


def test_an_unknown_stamp_fails_closed(install_env, monkeypatch):
    """A stamp nobody recognises is not permission to trust the host."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    install_env("default", NonLocalPluginEnvironment("something-new"))

    assert FT._backend_shares_the_host_filesystem("default") is False


def test_an_empty_stamp_falls_back_to_class_identity(install_env, monkeypatch):
    """A blank stamp is not a claim; the class still has to be recognised."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    install_env("default", NonLocalPluginEnvironment("   "))

    assert FT._backend_shares_the_host_filesystem("default") is False


def test_a_local_stamp_is_honoured(install_env, monkeypatch):
    """A plugin that genuinely runs on the host may say so."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    install_env("default", NonLocalPluginEnvironment("local"))

    assert FT._backend_shares_the_host_filesystem("default") is True


# ── Unstamped classes: exact identity, not substring ───────────────────


def test_the_real_local_environment_is_still_recognised(
        install_env, monkeypatch, tmp_path):
    """The built-in local backend must keep the fast path."""
    from tools.environments.local import LocalEnvironment

    monkeypatch.setenv("TERMINAL_ENV", "local")
    install_env("default", LocalEnvironment(cwd=str(tmp_path)))

    assert FT._backend_shares_the_host_filesystem("default") is True


def test_an_unstamped_local_sounding_class_is_not_trusted(
        install_env, monkeypatch):
    """Substring matching is the defect; exact identity is the rule."""
    class LocalisedSandboxEnvironment:
        cwd = "/workspace"

        def execute(self, command, cwd=None, **kwargs):  # pragma: no cover
            raise AssertionError("must not execute")

    monkeypatch.setenv("TERMINAL_ENV", "local")
    install_env("default", LocalisedSandboxEnvironment())

    assert FT._backend_shares_the_host_filesystem("default") is False, (
        "an unstamped class whose name merely CONTAINS 'local' was treated "
        "as the host filesystem")


@pytest.mark.parametrize("cls_name", [
    "DockerEnvironment", "SSHEnvironment", "ModalEnvironment",
])
def test_unstamped_remote_built_ins_are_not_local(
        install_env, monkeypatch, cls_name):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    env = type(cls_name, (), {"cwd": "/workspace"})()
    install_env("default", env)

    assert FT._backend_shares_the_host_filesystem("default") is False


# ── Earlier rounds must still hold ─────────────────────────────────────


def test_no_environment_and_global_local_is_still_local(monkeypatch):
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


def test_host_symlink_to_protected_is_still_gated(tmp_path):
    protected = tmp_path / "AGENTS.md"
    protected.write_text("protected\n")
    link = tmp_path / "notes.md"
    link.symlink_to(protected)

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(link), content="x\n").released
        assert gw.card_count == 1
    assert protected.read_text() == "protected\n"


def test_path_resolution_also_honours_the_stamp_first(install_env, monkeypatch):
    """The same inverted precedence existed in container path resolution.

    ``_terminal_env_type_for_task`` drives ``_uses_container_paths``, which
    decides whether a path is mapped into the container. It consulted the
    stamp only as a fallback too, so a plugin stamped ``docker`` with a
    local-sounding class name got HOST path semantics. Same defect class,
    and the contract names this exact caller.
    """
    monkeypatch.setenv("TERMINAL_ENV", "local")
    install_env("default", NonLocalPluginEnvironment("docker"))

    assert FT._terminal_env_type_for_task("default") == "docker"
    assert FT._uses_container_paths("default") is True


def test_unstamped_built_ins_still_classify_by_name(install_env, monkeypatch):
    """The substring fallback must still serve the built-in classes."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    env = type("DockerEnvironment", (), {"cwd": "/workspace"})()
    install_env("default", env)

    assert FT._terminal_env_type_for_task("default") == "docker"
