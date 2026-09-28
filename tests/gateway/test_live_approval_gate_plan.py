"""TJS-259 round 10: the gate DECISION must come from the one path plan.

Rounds 8 and 9 made authorization identity, state capture, locking, drift
checking and mutation all read one immutable resolved-path plan. What they
did not change is the decision of *whether a gate applies at all*:
``_write_gate_applies``, ``_protected_instruction_reason`` and
``is_write_approval_required`` each called ``realpath`` for themselves, after
the plan was built.

That is strictly worse than the round-9 defect. Round 9 redirected an
*approved* write to an unreviewed file. This one makes the gate answer "not
protected" about a file the write never touches, so a protected target is
overwritten with **no approval card at all** — the released-grant machinery
is never even reached.

Every test here drives the public tool entry point and asserts on disk.
"""

import os


import tools.file_tools as FT
from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import patch_tool, write_file_tool


def _retarget_at(monkeypatch, attr, link, new_target, fired):
    """Flip *link* to *new_target* the first time *attr* is called."""
    real = getattr(FT, attr)

    def _wrapper(*a, **k):
        if fired["n"] == 0:
            fired["n"] = 1
            link.unlink()
            link.symlink_to(new_target)
        return real(*a, **k)

    monkeypatch.setattr(FT, attr, _wrapper)


# ── write_file ─────────────────────────────────────────────────────────


def test_write_file_gate_decision_cannot_be_dodged_by_retargeting(
        monkeypatch, tmp_path):
    """A protected target must never be written without a card.

    The raw path (``notes.md``) is not a protected name; it resolves to one.
    The link is flipped to a harmless file after the plan is built but before
    the gate decides, so the gate's own ``realpath`` sees the harmless file.
    On the unfixed code the gate answered "not gated", both gates were
    skipped entirely, and ``AGENTS.md`` was overwritten with no card.
    """
    protected = tmp_path / "AGENTS.md"
    protected.write_text("protected instructions\n")
    harmless = tmp_path / "plain.md"
    harmless.write_text("nothing special\n")
    link = tmp_path / "notes.md"
    link.symlink_to(protected)

    fired = {"n": 0}
    _retarget_at(monkeypatch, "_write_gate_applies", link, harmless, fired)
    write_file_tool(path=str(link), content="INJECTED\n", task_id="default")

    assert fired["n"] == 1, "the retarget injection point was never reached"
    assert protected.read_text() == "protected instructions\n", (
        "a protected file was written with NO approval gate — the gate "
        "decided on a resolution the write did not use")


def test_write_file_gate_sees_the_planned_target_not_a_later_one(
        monkeypatch, tmp_path):
    """Same race one step later: inside the gate rather than before it."""
    protected = tmp_path / "AGENTS.md"
    protected.write_text("protected instructions\n")
    harmless = tmp_path / "plain.md"
    harmless.write_text("nothing special\n")
    link = tmp_path / "notes.md"
    link.symlink_to(protected)

    fired = {"n": 0}
    _retarget_at(monkeypatch, "_check_protected_instruction_write",
                 link, harmless, fired)
    write_file_tool(path=str(link), content="INJECTED\n", task_id="default")

    assert fired["n"] == 1
    assert protected.read_text() == "protected instructions\n", (
        "the protected-write gate resolved the path again and cleared a "
        "write that still landed on the protected file")


# ── patch ──────────────────────────────────────────────────────────────


def test_patch_gate_decision_cannot_be_dodged_by_retargeting(
        monkeypatch, tmp_path):
    """patch_tool has the same gate-decision path and the same exposure."""
    protected = tmp_path / "AGENTS.md"
    protected.write_text("protected instructions\n")
    harmless = tmp_path / "plain.md"
    harmless.write_text("protected instructions\n")
    link = tmp_path / "notes.md"
    link.symlink_to(protected)

    fired = {"n": 0}
    _retarget_at(monkeypatch, "_write_gate_applies", link, harmless, fired)
    patch_tool(mode="replace", path=str(link),
               old_string="protected instructions",
               new_string="INJECTED", task_id="default")

    assert fired["n"] == 1
    assert protected.read_text() == "protected instructions\n", (
        "a protected file was patched with NO approval gate")


# ── the gate must still fire normally ──────────────────────────────────


def test_symlink_to_a_protected_file_still_raises_a_card(tmp_path):
    """The fix must not be a fail-open: the honest case still gates.

    A non-protected raw name resolving to a protected file is the case the
    realpath matching exists for; binding it to the plan must not lose it.
    """
    protected = tmp_path / "AGENTS.md"
    protected.write_text("protected instructions\n")
    link = tmp_path / "notes.md"
    link.symlink_to(protected)

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(link),
                       content="reviewed change\n").released
        assert gw.card_count == 1
    assert protected.read_text() == "protected instructions\n"


def test_protected_write_still_lands_after_approval(tmp_path):
    """And the approved write must still go through, to the reviewed file."""
    protected = tmp_path / "AGENTS.md"
    protected.write_text("before\n")

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(protected),
                       content="after\n").released
        gw.answer("once")
        assert gw.call(write_file_tool, path=str(protected),
                       content="after\n").ok

    assert protected.read_text() == "after\n"


def test_ungated_write_is_unaffected(tmp_path):
    """An ordinary write must not acquire a gate from this change."""
    plain = tmp_path / "plain.md"
    plain.write_text("before\n")

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(plain),
                       content="after\n").ok
        assert gw.card_count == 0
    assert plain.read_text() == "after\n"


def test_plan_realpaths_follows_the_plan_and_survives_bad_paths():
    """Unit-level contract of the helper the gates now read."""
    planned = {"a.md": "/tmp/a-resolved.md", "b.md": None}
    out = FT._plan_realpaths(["a.md", "b.md"], planned, "default")

    assert out["a.md"] == os.path.realpath("/tmp/a-resolved.md")
    # An unresolvable entry still produces an answer rather than vanishing —
    # a missing key would make the gate fall back to its own resolution.
    assert "b.md" in out and out["b.md"]


def test_ssh_config_predicate_accepts_a_caller_resolution():
    """``is_write_approval_required`` honors the caller's resolved path."""
    from agent.file_safety import is_write_approval_required

    ssh_config = os.path.realpath(os.path.expanduser("~/.ssh/config"))
    assert is_write_approval_required("/some/unrelated/name", ssh_config)
    assert not is_write_approval_required(
        ssh_config, os.path.realpath("/tmp/not-ssh-config"))
