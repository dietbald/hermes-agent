"""Regression coverage for the delivery-obligations operator CLI."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "delivery-obligations.py"
SPEC = importlib.util.spec_from_file_location("delivery_obligations", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_default_profile_uses_root_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE.Path, "home", classmethod(lambda cls: tmp_path))

    assert MODULE._home("default") == tmp_path / ".hermes"
    assert MODULE._home("atlas") == tmp_path / ".hermes" / "profiles" / "atlas"
