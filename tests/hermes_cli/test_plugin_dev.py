from __future__ import annotations

import argparse
import os
from pathlib import Path

from hermes_cli.subcommands.plugins import build_plugins_parser


def _parse_plugins_args(*argv: str):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_plugins_parser(subparsers, cmd_plugins=lambda args: None)
    return parser.parse_args(["plugins", *argv])


def test_plugins_parser_exposes_doctor() -> None:
    doctor = _parse_plugins_args("doctor", "sample", "--ci")

    assert (doctor.plugins_action, doctor.target, doctor.ci) == (
        "doctor",
        "sample",
        True,
    )


def test_doctor_uses_registration_to_reject_bad_hook_and_callback_signature(
    tmp_path: Path,
) -> None:
    from hermes_cli.plugin_dev import doctor_plugin

    plugin = tmp_path / "bad-plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        "\n".join(
            [
                "name: bad-plugin",
                "version: 0.1.0",
                "description: broken contract",
                "provides_hooks:",
                "  - typo_hook",
                "  - pre_tool_call",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text(
        "def callback(tool_name):\n"
        "    return None\n\n"
        "def register(ctx):\n"
        "    ctx.register_hook('typo_hook', callback)\n"
        "    ctx.register_hook('pre_tool_call', callback)\n",
        encoding="utf-8",
    )

    report = doctor_plugin(plugin)
    messages = "\n".join(f.message for f in report.findings)
    assert report.ok is False
    assert "unknown hook 'typo_hook'" in messages
    assert "must accept **kwargs" in messages


def test_doctor_accepts_manifest_defaults_from_runtime_parser(tmp_path: Path) -> None:
    from hermes_cli.plugin_dev import doctor_plugin

    plugin = tmp_path / "minimal"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: minimal\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "def register(ctx):\n    pass\n", encoding="utf-8"
    )

    report = doctor_plugin(plugin)
    assert report.ok, report.format_text()
    assert report.manifest is not None
    assert report.manifest.kind == "standalone"


def test_doctor_restores_global_tool_policy_and_module_state(tmp_path: Path) -> None:
    import sys

    from hermes_cli.plugin_dev import doctor_plugin
    from tools.registry import registry

    target = tmp_path / "cleanup-plugin"
    target.mkdir()
    (target / "plugin.yaml").write_text(
        "name: cleanup-plugin\nprovides_tools: [cleanup_plugin_ping]\n",
        encoding="utf-8",
    )
    (target / "__init__.py").write_text(
        "import json\n\n"
        "def ping(args, **kwargs):\n    return json.dumps({'ok': True})\n\n"
        "def register(ctx):\n"
        "    ctx.register_tool(name='cleanup_plugin_ping', toolset='cleanup', "
        "schema={'name': 'cleanup_plugin_ping', 'description': 'test', "
        "'parameters': {'type': 'object'}}, handler=ping)\n",
        encoding="utf-8",
    )
    before_policy = dict(registry._plugin_override_policy)
    before_modules = {
        name
        for name in sys.modules
        if name == "hermes_plugins" or name.startswith("hermes_plugins.")
    }

    report = doctor_plugin(target)

    assert report.ok, report.format_text()
    assert report.registered_tools == ("cleanup_plugin_ping",)
    assert registry.get_entry("cleanup_plugin_ping") is None
    assert registry._plugin_override_policy == before_policy
    after_modules = {
        name
        for name in sys.modules
        if name == "hermes_plugins" or name.startswith("hermes_plugins.")
    }
    assert after_modules == before_modules


def test_doctor_rejects_manifest_less_target_without_copying(
    tmp_path: Path, monkeypatch
) -> None:
    """A parent tree must be refused before anything is copied (TJS-144)."""
    import shutil

    from hermes_cli import plugin_dev

    target = tmp_path / "not-a-plugin"
    (target / "deep").mkdir(parents=True)
    (target / "deep" / "payload.bin").write_bytes(b"x" * 1024)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))

    def _fail_copytree(*_args, **_kwargs):
        raise AssertionError("Doctor copied a target with no plugin manifest")

    monkeypatch.setattr(shutil, "copytree", _fail_copytree)

    report = plugin_dev.doctor_plugin(target)

    assert report.ok is False
    assert "holds no plugin.yaml" in report.format_text()
    assert list(scratch.iterdir()) == []


def test_doctor_rejects_multi_plugin_parent_without_copying(
    tmp_path: Path, monkeypatch
) -> None:
    """Pointing Doctor at a plugins root must cost nothing (TJS-144)."""
    import shutil

    from hermes_cli import plugin_dev

    root = tmp_path / "plugins-root"
    for name in ("alpha", "beta"):
        plugin = root / name
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text(f"name: {name}\n", encoding="utf-8")
    monkeypatch.setattr(
        shutil,
        "copytree",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("Doctor copied a multi-plugin parent tree")
        ),
    )

    report = plugin_dev.doctor_plugin(root)

    assert report.ok is False
    assert "more than one plugin manifest" in report.format_text()


def test_doctor_accepts_category_layout_plugin_directory(tmp_path: Path) -> None:
    """``<category>/<plugin>/plugin.yaml`` is how most bundled plugins ship."""
    from hermes_cli.plugin_dev import doctor_plugin

    plugin = tmp_path / "category" / "leaf"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: leaf\n", encoding="utf-8")
    (plugin / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")

    report = doctor_plugin(tmp_path / "category")

    assert report.ok, report.format_text()
    assert report.manifest is not None and report.manifest.name == "leaf"


def test_doctor_refuses_target_over_copy_limit(tmp_path: Path, monkeypatch) -> None:
    from hermes_cli import plugin_dev

    plugin = tmp_path / "fat-plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: fat-plugin\n", encoding="utf-8")
    (plugin / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    (plugin / "blob.bin").write_bytes(b"x" * 4096)
    monkeypatch.setattr(plugin_dev, "MAX_COPY_BYTES", 1024)

    report = plugin_dev.doctor_plugin(plugin)

    assert report.ok is False
    assert "over the Doctor scratch-copy limit" in report.format_text()


def test_doctor_copy_skips_heavy_build_directories(tmp_path: Path) -> None:
    from hermes_cli.plugin_dev import _measure_copy

    plugin = tmp_path / "plugin-with-junk"
    (plugin / "node_modules").mkdir(parents=True)
    (plugin / ".venv").mkdir()
    (plugin / "node_modules" / "dep.bin").write_bytes(b"x" * 4096)
    (plugin / ".venv" / "lib.bin").write_bytes(b"x" * 4096)
    (plugin / "plugin.yaml").write_text("name: plugin-with-junk\n", encoding="utf-8")

    measured_bytes, measured_files = _measure_copy(plugin)

    assert measured_files == 1
    assert measured_bytes == len("name: plugin-with-junk\n")


def test_doctor_scratch_dir_is_removed_when_the_run_is_sigtermed(tmp_path: Path) -> None:
    """SIGTERM used to bypass cleanup entirely and leave the whole copy behind.

    Only a real killed process proves this, so drive one: the in-process
    ``finally`` would pass even with no signal handler installed.
    """
    import signal
    import subprocess
    import sys
    import time

    scratch = tmp_path / "tmp"
    scratch.mkdir()
    program = (
        "import sys, time\n"
        "from hermes_cli.plugin_dev import _doctor_scratch_dir\n"
        "with _doctor_scratch_dir() as path:\n"
        "    (path / 'payload.bin').write_bytes(b'x' * 1024)\n"
        "    print(path, flush=True)\n"
        "    time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", program],
        cwd=str(Path(__file__).resolve().parents[2]),
        env={**os.environ, "TMPDIR": str(scratch)},
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        created = Path(child.stdout.readline().strip())
        assert created.exists()
        child.send_signal(signal.SIGTERM)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    deadline = time.monotonic() + 5
    while created.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not created.exists(), "SIGTERM left the Doctor scratch dir on disk"
    # The process must still die by the signal, not swallow it.
    assert child.returncode == -signal.SIGTERM


def test_sweep_removes_only_abandoned_scratch_dirs(tmp_path: Path) -> None:
    import os

    from hermes_cli.plugin_dev import DOCTOR_TEMPDIR_PREFIX, sweep_stale_doctor_tempdirs

    dead_pid = 2**22 - 1  # above /proc/sys/kernel/pid_max, cannot be running
    abandoned = tmp_path / f"{DOCTOR_TEMPDIR_PREFIX}{dead_pid}-abc123"
    (abandoned / "plugins").mkdir(parents=True)
    mine = tmp_path / f"{DOCTOR_TEMPDIR_PREFIX}{os.getpid()}-live456"
    mine.mkdir()
    unrelated = tmp_path / "some-other-tempdir"
    unrelated.mkdir()

    removed = sweep_stale_doctor_tempdirs(tmp_path)

    assert removed == [abandoned]
    assert not abandoned.exists()
    assert mine.exists() and unrelated.exists()


def test_doctor_blocks_live_network(tmp_path: Path) -> None:
    from hermes_cli.plugin_dev import doctor_plugin

    plugin = tmp_path / "network-plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: network-plugin\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "import socket\n\n"
        "def register(ctx):\n"
        "    socket.create_connection(('example.com', 443))\n",
        encoding="utf-8",
    )

    report = doctor_plugin(plugin)
    assert report.ok is False
    assert "network access is disabled while Plugin Doctor runs" in report.format_text()
