"""Runtime-backed validation behind ``hermes plugins doctor``.

The Doctor originated in #46456 / contributor PR #46457 by 峯岸 亮
(@zapabob).  This core command keeps that contribution's manifest/import/
registration validation intent while routing every check through the current
runtime contracts instead of maintaining a parallel scanner.
"""

from __future__ import annotations

import inspect
import os
import shutil
import signal
import socket
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import patch

from hermes_constants import get_hermes_home

# Scratch-dir naming and limits. The prefix carries the owning pid so an
# abandoned directory (SIGKILL leaves no chance to clean up) can be attributed
# and swept on the next run instead of sitting on disk until it fills it.
DOCTOR_TEMPDIR_PREFIX = "hermes-plugin-doctor-"
STALE_TEMPDIR_AGE_SECONDS = 6 * 60 * 60

# A plugin is source: a manifest, Python modules, maybe a few assets. Anything
# past these bounds is a checkout or a parent tree, not a plugin, and copying it
# is what turned a doctor run into tens of GB of /tmp.
MAX_COPY_BYTES = 512 * 1024 * 1024
MAX_COPY_FILES = 20_000

_MANIFEST_FILENAMES = ("plugin.yaml", "plugin.yml", "plugin.json")
_COPY_IGNORE_PATTERNS = (
    ".git",
    "__pycache__",
    ".pytest_cache",
    "*.pyc",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "build",
    "dist",
    "*.egg-info",
)


class _DoctorLoadError(RuntimeError):
    """Raised when the real plugin runtime cannot load the target."""


def _deny_network(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("network access is disabled while Plugin Doctor runs")


def _manifest_file(path: Path) -> Path | None:
    """Return the plugin manifest directly inside *path*, if there is one."""
    for name in _MANIFEST_FILENAMES:
        candidate = path / name
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return None


def _discover_manifests(path: Path) -> list[Path]:
    """Find the manifests Hermes discovery would find under *path*.

    Mirrors ``PluginManager._scan_directory``: a manifest sits either in the
    plugin directory itself or, for the category layout
    (``image_gen/openai/plugin.yaml``), one level down. Running this *before*
    the scratch copy is what keeps Doctor from mirroring a whole checkout and
    only then reporting that it found no plugin.
    """
    direct = _manifest_file(path)
    if direct is not None:
        return [direct]
    found: list[Path] = []
    try:
        children = sorted(path.iterdir())
    except OSError:
        return found
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest = _manifest_file(child)
        if manifest is not None:
            found.append(manifest)
        if len(found) > 1:
            break  # the caller only needs to know it is more than one
    return found


def _measure_copy(root: Path) -> tuple[int, int]:
    """Size the copy Doctor is about to make, applying the copy's own filters.

    Measuring first means an oversized target costs a directory walk instead of
    a partial multi-gigabyte write that then has to be thrown away. Symlinks are
    counted as links, never followed, matching ``copytree(symlinks=True)``.
    """
    ignore = shutil.ignore_patterns(*_COPY_IGNORE_PATTERNS)
    total_bytes = 0
    total_files = 0
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        skipped = ignore(current, [*dirnames, *filenames])
        dirnames[:] = [name for name in dirnames if name not in skipped]
        for name in filenames:
            if name in skipped:
                continue
            entry = Path(current) / name
            if entry.is_symlink():
                continue
            try:
                total_bytes += entry.stat().st_size
            except OSError:
                continue
            total_files += 1
            if total_bytes > MAX_COPY_BYTES or total_files > MAX_COPY_FILES:
                return total_bytes, total_files
    return total_bytes, total_files


def _is_abandoned_tempdir(path: Path, *, now: float) -> bool:
    """True when a Doctor scratch dir has no live owner left."""
    suffix = path.name[len(DOCTOR_TEMPDIR_PREFIX) :]
    owner, _, _ = suffix.partition("-")
    if owner.isdigit():
        pid = int(owner)
        if pid == os.getpid():
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # alive, owned by another user
        except OSError:
            return False
    # Unknown or reused pid: fall back to age, so pre-fix leftovers still go.
    try:
        return (now - path.stat().st_mtime) > STALE_TEMPDIR_AGE_SECONDS
    except OSError:
        return False


def sweep_stale_doctor_tempdirs(root: Path | None = None) -> list[Path]:
    """Delete Doctor scratch dirs whose run is gone. Best effort, never raises.

    ``SIGKILL`` cannot be trapped, so no in-process handler can guarantee
    cleanup. This sweep is the backstop: every doctor run clears what earlier
    runs could not.
    """
    directory = Path(root) if root is not None else Path(tempfile.gettempdir())
    now = time.time()
    removed: list[Path] = []
    try:
        candidates = sorted(directory.glob(f"{DOCTOR_TEMPDIR_PREFIX}*"))
    except OSError:
        return removed
    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            if not _is_abandoned_tempdir(candidate, now=now):
                continue
            shutil.rmtree(candidate, ignore_errors=True)
            if not candidate.exists():
                removed.append(candidate)
        except OSError:
            continue
    return removed


@contextmanager
def _doctor_scratch_dir():
    """Yield a scratch dir that survives neither normal exit nor termination.

    ``tempfile.TemporaryDirectory`` only cleans up via its finalizer, which a
    default ``SIGTERM``/``SIGHUP`` disposition never reaches — that is how a
    killed doctor run left its whole copy behind. Trap those signals, and sweep
    for what ``SIGKILL`` leaves.
    """
    sweep_stale_doctor_tempdirs()
    path = Path(tempfile.mkdtemp(prefix=f"{DOCTOR_TEMPDIR_PREFIX}{os.getpid()}-"))
    installed: list[tuple[int, Any]] = []

    def _terminate(signum: int, _frame: Any) -> None:
        shutil.rmtree(path, ignore_errors=True)
        for number, previous in installed:
            signal.signal(number, previous)
        os.kill(os.getpid(), signum)

    for number in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if number is None:
            continue
        try:
            installed.append((number, signal.signal(number, _terminate)))
        except (ValueError, OSError):
            continue  # not the main thread, or no such signal on this platform
    try:
        yield path
    finally:
        for number, previous in installed:
            try:
                signal.signal(number, previous)
            except (ValueError, OSError):
                pass
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _doctor_runtime(plugin_path: Path):
    """Load one plugin through the real runtime and restore global state.

    This is deliberately private Doctor machinery, not a standalone plugin
    test framework. Registration code executes under a temporary HERMES_HOME
    with outbound socket connects blocked.
    """
    discovered = _discover_manifests(plugin_path)
    if not discovered:
        raise _DoctorLoadError(
            f"{plugin_path} holds no {' / '.join(_MANIFEST_FILENAMES)} manifest, "
            "here or one level down — Plugin Doctor validates one plugin "
            "directory, not a parent tree; point it at the plugin itself"
        )
    if len(discovered) > 1:
        raise _DoctorLoadError(
            f"{plugin_path} holds more than one plugin manifest — Plugin Doctor "
            "validates one plugin at a time; point it at a single plugin directory"
        )
    copy_bytes, copy_files = _measure_copy(plugin_path)
    if copy_bytes > MAX_COPY_BYTES or copy_files > MAX_COPY_FILES:
        raise _DoctorLoadError(
            f"{plugin_path} holds {copy_bytes / 1024 ** 2:.0f} MiB in {copy_files} "
            f"file(s), over the Doctor scratch-copy limit of "
            f"{MAX_COPY_BYTES / 1024 ** 2:.0f} MiB / {MAX_COPY_FILES} files — "
            "a plugin directory this large is usually a checkout; remove build "
            "output and data directories from it before validating"
        )

    # Setup runs inside the stack from here on: a failure while copying or
    # patching must still drop the scratch dir, which the old code left to the
    # TemporaryDirectory finalizer.
    stack = ExitStack()
    try:
        home = stack.enter_context(_doctor_scratch_dir())
        bundled = home / "bundled-plugins"
        plugins_root = home / "plugins"
        bundled.mkdir(parents=True)
        plugins_root.mkdir(parents=True)
        copied = plugins_root / plugin_path.name
        shutil.copytree(
            plugin_path,
            copied,
            symlinks=True,
            ignore=shutil.ignore_patterns(*_COPY_IGNORE_PATTERNS),
        )

        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "HERMES_HOME": str(home),
                    "HERMES_BUNDLED_PLUGINS": str(bundled),
                    "HERMES_ENABLE_PROJECT_PLUGINS": "0",
                },
                clear=False,
            )
        )
        stack.enter_context(patch.object(socket, "create_connection", _deny_network))
        stack.enter_context(patch.object(socket.socket, "connect", _deny_network))
        stack.enter_context(patch.object(socket.socket, "connect_ex", _deny_network))

        from hermes_cli.plugins import PluginManager
        from tools.registry import registry

        entries_before = {entry.name: entry for entry in registry._snapshot_entries()}
        policy_before = dict(registry._plugin_override_policy)
        modules_before = {
            name
            for name in sys.modules
            if name == "hermes_plugins" or name.startswith("hermes_plugins.")
        }
        manager = PluginManager()
    except BaseException:
        stack.close()
        raise

    try:
        manifests = manager._scan_directory(plugins_root, source="user")
        if not manifests:
            raise _DoctorLoadError(
                f"Hermes discovery found no valid plugin manifest under {copied}"
            )
        if len(manifests) != 1:
            raise _DoctorLoadError(
                f"Expected one plugin manifest, discovered {len(manifests)} under {copied}"
            )
        manifest = manifests[0]
        manager._load_plugin(manifest)
        loaded = manager._plugins.get(manifest.key or manifest.name)
        if loaded is None:
            raise _DoctorLoadError("Plugin registration produced no runtime record")
        if loaded.error:
            raise _DoctorLoadError(f"Plugin registration failed: {loaded.error}")
        if not loaded.enabled:
            raise _DoctorLoadError("Plugin registration did not enable the runtime record")
        yield SimpleNamespace(
            manifest=manifest,
            manager=manager,
            registered_tools=tuple(sorted(loaded.tools_registered)),
            registered_hooks=tuple(loaded.hooks_registered),
        )
    finally:
        entries_after = {entry.name: entry for entry in registry._snapshot_entries()}
        changed_names = {
            name
            for name in set(entries_before) | set(entries_after)
            if entries_after.get(name) is not entries_before.get(name)
        }
        with registry._lock:
            for name in changed_names:
                previous = entries_before.get(name)
                if previous is None:
                    registry._tools.pop(name, None)
                else:
                    registry._tools[name] = previous
            registry._plugin_override_policy.clear()
            registry._plugin_override_policy.update(policy_before)
            if changed_names:
                registry._generation += 1
        for name in list(sys.modules):
            if (
                name not in modules_before
                and (name == "hermes_plugins" or name.startswith("hermes_plugins."))
            ):
                sys.modules.pop(name, None)
        stack.close()


@dataclass(frozen=True)
class DoctorFinding:
    level: Literal["error", "warning"]
    message: str


@dataclass
class DoctorReport:
    path: Path
    manifest: Any | None = None
    findings: list[DoctorFinding] = field(default_factory=list)
    registered_tools: tuple[str, ...] = ()
    registered_hooks: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(finding.level == "error" for finding in self.findings)

    def error(self, message: str) -> None:
        self.findings.append(DoctorFinding("error", message))

    def warning(self, message: str) -> None:
        self.findings.append(DoctorFinding("warning", message))

    def format_text(self) -> str:
        lines = [f"Plugin Doctor: {self.path}"]
        if self.manifest is not None:
            lines.append(
                f"  manifest: {self.manifest.name} "
                f"{self.manifest.version or '(no version)'} ({self.manifest.kind})"
            )
        for finding in self.findings:
            marker = "ERROR" if finding.level == "error" else "WARN"
            lines.append(f"  {marker}: {finding.message}")
        if self.ok:
            lines.append(
                "  OK: runtime discovery, manifest parsing, import, and registration passed"
            )
        lines.append(
            f"  registrations: {len(self.registered_tools)} tool(s), "
            f"{len(self.registered_hooks)} hook(s)"
        )
        return "\n".join(lines)


def resolve_plugin_path(target: str | os.PathLike[str] | None = None) -> Path:
    """Resolve an explicit path or an installed/bundled plugin id."""
    raw = os.fspath(target or ".")
    direct = Path(raw).expanduser()
    if direct.is_dir():
        return direct.resolve()

    candidates: list[Path] = []
    user_root = get_hermes_home() / "plugins"
    candidates.append(user_root / raw)
    try:
        from hermes_cli.plugins import get_bundled_plugins_dir

        bundled = get_bundled_plugins_dir()
        candidates.extend(
            [
                bundled / raw,
                bundled / "platforms" / raw,
                bundled / "model-providers" / raw,
            ]
        )
    except Exception:
        pass
    candidates.append(Path.cwd() / ".hermes" / "plugins" / raw)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    raise FileNotFoundError(
        f"Plugin {raw!r} was not found as a path or installed plugin id"
    )


def _accepts_var_kwargs(callback: Any) -> bool:
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)


def _check_manifest_v2(report: "DoctorReport", manifest: Any) -> None:
    """Manifest v2 (#64165) checks: versions, deps, pip declarations, schema."""
    import importlib.metadata
    import re as _re

    from hermes_cli.plugins import SUPPORTED_MANIFEST_VERSION

    mv = getattr(manifest, "manifest_version", 1)
    if mv > SUPPORTED_MANIFEST_VERSION:
        report.warning(
            f"manifest_version {mv} is newer than this Hermes supports "
            f"({SUPPORTED_MANIFEST_VERSION}); unknown fields are ignored"
        )

    api_version = getattr(manifest, "api_version", None)
    if api_version is not None and api_version < 1:
        report.warning(f"api_version {api_version} is not a valid API generation (>= 1)")

    for dep in getattr(manifest, "requires_plugins", []) or []:
        dep_id = dep.get("id") if isinstance(dep, dict) else None
        if not dep_id:
            report.warning(f"requires_plugins entry {dep!r} has no plugin id")
            continue
        vr = dep.get("version_range")
        if vr:
            report.warning(
                f"requires plugin {dep_id!r} ({vr}) — version ranges are "
                "advisory; a missing dependency logs a warning at load"
            )

    pydeps = getattr(manifest, "python_dependencies", []) or []
    missing: list[str] = []
    unpinned: list[str] = []
    for req in pydeps:
        dist = _re.split(r"[<>=!~\[;\s]", req, maxsplit=1)[0].strip()
        if not _re.search(r"<|==|~=", req):
            unpinned.append(req)
        if not dist:
            continue
        try:
            importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            missing.append(req)
        except Exception:
            continue
    for req in unpinned:
        report.warning(
            f"python_dependencies entry {req!r} has no upper bound — "
            "pin an upper bound (e.g. 'pkg>=1.0,<2') per the dependency policy"
        )
    if missing:
        report.warning(
            "declared python_dependencies not installed: "
            + ", ".join(missing)
            + " — Hermes never auto-installs plugin dependencies; "
            + "install manually: pip install "
            + " ".join(f"'{m}'" for m in missing)
        )

    schema = getattr(manifest, "config_schema", {}) or {}
    if schema:
        from hermes_cli.plugins import _CONFIG_SCHEMA_TYPES

        for skey, spec in schema.items():
            if not isinstance(spec, dict):
                continue
            stype = spec.get("type")
            if stype is not None and str(stype).lower() not in _CONFIG_SCHEMA_TYPES:
                report.warning(
                    f"config_schema key {skey!r} declares unknown type {stype!r}"
                )


def doctor_plugin(target: str | os.PathLike[str] | None = None) -> DoctorReport:
    """Validate one plugin through Hermes' real scanner and registration path."""
    try:
        path = resolve_plugin_path(target)
    except FileNotFoundError as exc:
        report = DoctorReport(Path(os.fspath(target or ".")).expanduser())
        report.error(str(exc))
        return report

    report = DoctorReport(path)
    try:
        with _doctor_runtime(path) as host:
            report.manifest = host.manifest
            report.registered_tools = host.registered_tools
            report.registered_hooks = host.registered_hooks

            from hermes_cli.plugins import VALID_HOOKS

            declared_hooks = host.manifest.provides_hooks
            declared_tools = host.manifest.provides_tools
            if not isinstance(declared_hooks, list):
                report.error("provides_hooks must be a list")
                declared_hooks = []
            if not isinstance(declared_tools, list):
                report.error("provides_tools must be a list")
                declared_tools = []

            for name in declared_hooks:
                if not isinstance(name, str):
                    report.error("provides_hooks entries must be strings")
                elif name not in VALID_HOOKS:
                    report.error(f"unknown hook {name!r} in provides_hooks")

            for hook_name, callbacks in host.manager._hooks.items():
                if hook_name not in VALID_HOOKS:
                    report.error(f"registered unknown hook {hook_name!r}")
                for callback in callbacks:
                    if not _accepts_var_kwargs(callback):
                        callback_name = getattr(callback, "__name__", repr(callback))
                        report.error(
                            f"hook callback {callback_name!r} for {hook_name!r} "
                            "must accept **kwargs for forward compatibility"
                        )

            declared_hook_names = {name for name in declared_hooks if isinstance(name, str)}
            registered_hook_names = set(host.registered_hooks)
            for name in sorted(declared_hook_names - registered_hook_names):
                report.warning(f"manifest declares hook {name!r} but registration did not add it")
            for name in sorted(registered_hook_names - declared_hook_names):
                report.warning(f"registration adds hook {name!r} not listed in provides_hooks")

            declared_tool_names = {name for name in declared_tools if isinstance(name, str)}
            registered_tool_names = set(host.registered_tools)
            for name in sorted(declared_tool_names - registered_tool_names):
                report.warning(f"manifest declares tool {name!r} but registration did not add it")
            for name in sorted(registered_tool_names - declared_tool_names):
                report.warning(f"registration adds tool {name!r} not listed in provides_tools")

            _check_manifest_v2(report, host.manifest)
    except _DoctorLoadError as exc:
        report.error(str(exc))
    except Exception as exc:
        report.error(f"unexpected validation failure: {type(exc).__name__}: {exc}")
    return report


__all__ = [
    "DoctorFinding",
    "DoctorReport",
    "doctor_plugin",
    "resolve_plugin_path",
    "sweep_stale_doctor_tempdirs",
]
