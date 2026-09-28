"""TJS-259 round 13: backend classification, tested against the real config.

Round 12's opposite-model review found that
``_backend_shares_the_host_filesystem`` read ``_get_env_config()["type"]``
while the real key is ``env_type``. The lookup returned ``None`` for EVERY
backend, defaulted to ``"local"``, and so the host fast path was taken
unconditionally — including under Docker, which is exactly the case the
backend realpath exists for. A protected write through a backend-only
symlink completed with zero cards.

My round-12 tests monkeypatched the helper to ``False``, so they proved the
non-local branch works and never tested which branch is chosen. These tests
drive the real ``_get_env_config`` shape instead.

The rule being pinned: a misspelled or missing key must fail CLOSED (treat
the backend as non-local and ask it), never fall back to "local, trust the
host".
"""

import os

import pytest

import tools.file_tools as FT
from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import write_file_tool


# ── Classification against the real config contract ────────────────────


def test_the_config_key_this_helper_reads_actually_exists():
    """Pin the contract with _get_env_config, not a guess at its shape.

    The round-12 defect was a key that does not exist. Asserting the key name
    against the real producer is what makes a rename fail here rather than in
    a review six rounds later.
    """
    from tools.terminal_tool import _get_env_config

    config = _get_env_config()
    assert "env_type" in config, (
        "_get_env_config no longer reports 'env_type'; "
        "_backend_shares_the_host_filesystem reads that key to decide whether "
        "the host filesystem may be trusted for approval gating")


@pytest.mark.parametrize("env_type", [
    "docker", "ssh", "modal", "daytona", "singularity", "vercel_sandbox",
])
def test_non_local_backends_are_not_treated_as_the_host(monkeypatch, env_type):
    """Every remote/container backend must be asked, not assumed."""
    monkeypatch.setenv("TERMINAL_ENV", env_type)
    assert FT._backend_shares_the_host_filesystem("default") is False, (
        f"backend {env_type!r} was classified as sharing the host filesystem, "
        "so the approval gate would resolve paths on the wrong machine")


def test_local_backend_is_recognised(monkeypatch):
    """The fast path must still engage where it is correct."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    assert FT._backend_shares_the_host_filesystem("default") is True


def test_an_unknown_backend_fails_closed(monkeypatch):
    """A backend nobody anticipated is asked, not trusted."""
    monkeypatch.setenv("TERMINAL_ENV", "some-future-sandbox")
    assert FT._backend_shares_the_host_filesystem("default") is False


def test_a_config_read_failure_fails_closed(monkeypatch):
    """If the config cannot be read, the host must not be trusted."""
    import tools.terminal_tool as TT

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(TT, "_get_env_config", _boom)
    assert FT._backend_shares_the_host_filesystem("default") is False


def test_a_missing_or_renamed_key_fails_closed(monkeypatch):
    """The round-12 defect itself, as a regression.

    A config that does not carry the expected key must not read as "local".
    This is the shape of the bug: a silent ``None`` that defaulted to the
    most permissive answer.
    """
    import tools.terminal_tool as TT

    monkeypatch.setattr(TT, "_get_env_config",
                        lambda: {"some_other_key": "docker"})
    assert FT._backend_shares_the_host_filesystem("default") is False, (
        "a config without the expected key was treated as local — a missing "
        "key must never mean 'trust the host filesystem'")


# ── End to end: the reviewer's Docker reproduction ─────────────────────


def test_docker_backend_symlink_to_a_protected_file_is_gated(
        monkeypatch, tmp_path):
    """The reviewer's reproduction, with the REAL config shape.

    ``TERMINAL_ENV=docker`` is set through the environment that
    ``_get_env_config`` actually reads — the helper is NOT monkeypatched, so
    this exercises classification. The backend reports
    ``notes.md -> AGENTS.md``; the host sees an ordinary file. Under the
    round-12 defect the host fast path was taken, the gate saw a
    non-protected basename, and the protected file was written with no card.
    """
    monkeypatch.setenv("TERMINAL_ENV", "docker")

    protected = tmp_path / "AGENTS.md"
    protected.write_text("protected instructions\n")
    notes = tmp_path / "notes.md"
    notes.write_text("ordinary host file\n")

    # A working file backend that is NOT built from TERMINAL_ENV: the point of
    # this test is that classification says "docker" (so the backend must be
    # asked to resolve) while the object answering is one we control. Building
    # it from the env would need a real Docker daemon.
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    inner = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))

    class _RemoteLinks:
        """Reports a symlink graph the host cannot see."""

        def __init__(self, backing):
            self._inner = backing

        def realpath(self, path, *a, **k):
            if str(path) == str(notes):
                return str(protected)
            return self._inner.realpath(path, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(FT, "_get_file_ops",
                        lambda tid="default": _RemoteLinks(inner))

    with LiveApprovalGateway() as gw:
        result = gw.call(write_file_tool, path=str(notes),
                         content="INJECTED\n")
        assert gw.card_count == 1, (
            "a Docker backend-only symlink into a protected file raised no "
            f"card — backend classification chose the host: {result.text[:200]}")
        assert result.released

    assert notes.read_text() == "ordinary host file\n"


def test_local_backend_write_still_costs_no_backend_call(monkeypatch, tmp_path):
    """The fast path must still avoid a subprocess per ordinary write.

    This is the cost guard the fast path exists for. If it regresses, every
    write on every local install pays a backend round trip.
    """
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

    assert calls == [], (
        "the local fast path asked the backend to resolve a path — that is a "
        "subprocess on every write")


def test_local_and_backend_realpath_agree_locally(monkeypatch, tmp_path):
    """The fast path is only sound if both answers match on a local backend.

    Asserting the equivalence directly means the optimisation cannot quietly
    diverge from the thing it is standing in for.
    """
    monkeypatch.setenv("TERMINAL_ENV", "local")
    target = tmp_path / "AGENTS.md"
    target.write_text("x\n")
    link = tmp_path / "notes.md"
    link.symlink_to(target)

    ops = FT._get_file_ops("default")
    assert ops.realpath(str(link)) == os.path.realpath(str(link))


def test_docker_classification_survives_a_gated_write_end_to_end(
        monkeypatch, tmp_path):
    """A protected target under Docker still gates through the normal path."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    protected = tmp_path / "AGENTS.md"
    protected.write_text("before\n")

    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations
    inner = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    monkeypatch.setattr(FT, "_get_file_ops", lambda tid="default": inner)

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(protected),
                       content="after\n").released
        assert gw.card_count == 1
    assert protected.read_text() == "before\n"
