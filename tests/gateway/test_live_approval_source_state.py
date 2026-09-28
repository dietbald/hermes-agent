"""TJS-258 round 6: a released grant must not outlive the state it reviewed.

The identity fix keyed authorization on the write REQUEST (mode, targets,
replace_all, occurrence count, input digests). That closed the five defects
found in TJS-256, but the opposite-model review (TJS-259) showed the request
is only half of what the user actually authorized: they reviewed a *change*,
which is the request applied to a particular **source state**.

A released card has no expiry. So if the file the card described changes
while the card waits, the resumed call recomputes the same request identity,
redeems the old grant, and writes a change the user never saw.

Two live reproductions, both asserted on disk:

* whole-file write — the card showed removal of one line; the file is edited
  before the answer, and the grant overwrites the unseen state;
* V4A ``*** Move File:`` — the card names the move but not the source's
  contents; the source is edited before the answer, and the grant moves the
  changed bytes into the protected target.
"""

import pytest

from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import patch_tool, write_file_tool


@pytest.fixture
def agents_md(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("state shown on card\n")
    return p


def test_target_changing_while_the_card_waits_forces_a_fresh_card(agents_md):
    """Defect 1: whole-file write, preimage omitted from the identity."""
    with LiveApprovalGateway() as gw:
        first = gw.call(write_file_tool, path=str(agents_md),
                        content="replacement body\n")
        assert first.released, first.text
        assert "state shown on card" in gw.card.rendered, (
            "the card must show the state the user is deciding about")

        # The user has not answered yet; something else changes the file.
        agents_md.write_text("new state not shown on card\n")

        assert gw.answer("once") == 1

        resumed = gw.call(write_file_tool, path=str(agents_md),
                          content="replacement body\n")

        assert agents_md.read_text() == "new state not shown on card\n", (
            "a grant given against the OLD file state overwrote a state the "
            "user was never shown")
        assert resumed.released, (
            "the reviewed source state changed, so this is a different "
            "decision and must raise its own card")
        assert gw.card_count == 2


def test_v4a_move_source_changing_while_the_card_waits_forces_a_fresh_card(
        tmp_path):
    """Defect 2: V4A Move, source contents omitted from the identity.

    The destination must NOT pre-exist: V4A Move refuses to overwrite, so an
    existing target would abort the move for an unrelated reason and hide the
    defect. A not-yet-existing ``AGENTS.md`` is still protected — the gate is
    keyed on the path, not on the file being there.
    """
    agents_md = tmp_path / "AGENTS.md"
    draft = tmp_path / "draft.txt"
    draft.write_text("draft contents shown at review time\n")
    patch_text = f"*** Begin Patch\n*** Move File: {draft} -> {agents_md}\n*** End Patch\n"

    with LiveApprovalGateway() as gw:
        first = gw.call(patch_tool, mode="patch", patch=patch_text)
        assert first.released, first.text

        # Source swapped under the waiting card.
        draft.write_text("ATTACKER CONTENT\n")

        assert gw.answer("once") == 1

        resumed = gw.call(patch_tool, mode="patch", patch=patch_text)

        landed = agents_md.read_text() if agents_md.exists() else ""
        assert "ATTACKER CONTENT" not in landed, (
            "a Move grant carried changed source contents into the protected "
            "target with no second card")
        assert resumed.released, (
            "the move's source content changed, so it must raise a fresh card")
        assert gw.card_count == 2


def test_unchanged_source_still_redeems_the_grant_exactly_once(agents_md):
    """Guard against over-tightening: a stable file must not re-ask."""
    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(agents_md),
                       content="approved body\n").released
        gw.answer("once")
        resumed = gw.call(write_file_tool, path=str(agents_md),
                          content="approved body\n")
        assert resumed.ok, resumed.text
        assert agents_md.read_text() == "approved body\n"
        assert gw.card_count == 1, "the unchanged resume re-asked for approval"
