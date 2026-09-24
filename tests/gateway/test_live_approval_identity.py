"""TJS-258: real approval cards driven end to end through the harness.

Every test here raises a REAL card through a REAL gateway session, answers it
through the button handler's own entry point, resumes, and asserts on what is
on disk. The five TJS-256 review defects all passed unit tests; these are the
checks that see them.
"""

import pytest

from tests.harness.live_approval_gateway import LiveApprovalGateway
from tools.file_tools import patch_tool, write_file_tool


@pytest.fixture
def agents_md(tmp_path):
    """A protected agent-instruction file — the gate's real trigger."""
    p = tmp_path / "AGENTS.md"
    p.write_text("alpha\nbeta\nalpha\n")
    return p


# ── The harness itself works: a card really goes out and back ───────────


def test_round_trip_once_writes_exactly_the_approved_content(tmp_path, agents_md):
    """Baseline: raise → release → answer 'once' → resume → file changed."""
    with LiveApprovalGateway() as gw:
        first = gw.call(write_file_tool, path=str(agents_md),
                        content="approved body\n")
        assert first.released, first.text
        assert gw.card_count == 1
        assert agents_md.read_text() == "alpha\nbeta\nalpha\n", (
            "the write must NOT land before the user answers")

        assert gw.answer("once") == 1
        assert len(gw.wakes) == 1, "answering a released card produced no wake"

        second = gw.call(write_file_tool, path=str(agents_md),
                         content="approved body\n")
        assert second.ok, second.text
        assert agents_md.read_text() == "approved body\n"
        assert gw.card_count == 1, "the resumed write re-asked for approval"


def test_deny_leaves_the_file_untouched(tmp_path, agents_md):
    original = agents_md.read_text()
    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(agents_md),
                       content="nope\n").released
        gw.answer("deny")
        again = gw.call(write_file_tool, path=str(agents_md), content="nope\n")
        assert not again.ok
        assert agents_md.read_text() == original


# ── Round 5: replace_all must not ride in on a one-occurrence grant ─────


def test_once_grant_for_one_occurrence_does_not_authorize_replace_all(agents_md):
    """The defect this issue exists for, checked on disk.

    Approve "replace ONE occurrence of alpha", then attempt
    ``replace_all=True``. The second attempt is a different authorization and
    must raise its own card; the file must still hold the untouched second
    ``alpha``.
    """
    with LiveApprovalGateway() as gw:
        first = gw.call(patch_tool, mode="replace", path=str(agents_md),
                        old_string="alpha", new_string="GAMMA",
                        replace_all=False)
        assert first.released, first.text
        gw.answer("once")

        escalated = gw.call(patch_tool, mode="replace", path=str(agents_md),
                            old_string="alpha", new_string="GAMMA",
                            replace_all=True)

        assert agents_md.read_text().count("alpha") == 2, (
            "replace_all was applied on a grant the user gave for ONE "
            "occurrence — the file was rewritten without a second card")
        assert escalated.released, (
            "escalating to replace_all must raise a fresh card")
        assert gw.card_count == 2


def test_once_grant_is_redeemed_by_the_identical_replace(agents_md):
    """The flip side: the SAME operation must not re-ask."""
    with LiveApprovalGateway() as gw:
        assert gw.call(patch_tool, mode="replace", path=str(agents_md),
                       old_string="beta", new_string="DELTA",
                       replace_all=False).released
        gw.answer("once")
        resumed = gw.call(patch_tool, mode="replace", path=str(agents_md),
                          old_string="beta", new_string="DELTA",
                          replace_all=False)
        assert resumed.ok, resumed.text
        assert "DELTA" in agents_md.read_text()
        assert gw.card_count == 1


# ── Round 3/4: identity must survive redaction, and must reach the gate ─


def test_two_secrets_that_redact_identically_need_two_cards(tmp_path):
    """Round 3's collision, on the path round 4 showed was unprotected.

    Both bodies render to the same masked preview. Approving one must not
    write the other.
    """
    target = tmp_path / "AGENTS.md"
    target.write_text("")
    a = "DEPLOY_KEY=AKIAAAAAAAAAAAAAAAAA\n"
    b = "DEPLOY_KEY=AKIABBBBBBBBBBBBBBBB\n"

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(target), content=a).released
        card_a = gw.card.rendered
        gw.answer("once")

        other = gw.call(write_file_tool, path=str(target), content=b)
        card_b = gw.card.rendered

        assert card_a == card_b, (
            "precondition: the two writes must render identically for this "
            "to be testing the collision at all")
        assert target.read_text() != b, (
            "a write the user never approved landed on disk by reusing "
            "another write's grant")
        assert other.released
        assert gw.card_count == 2


def test_identity_digest_never_appears_on_the_card(agents_md):
    """Identity is a digest and must stay off the wire entirely."""
    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(agents_md),
                       content="secret body\n").released
        card = gw.card
        assert card.fingerprint, "no fingerprint was bound to the card entry"
        assert not card.contains(card.fingerprint), (
            "the grant digest was rendered into the card payload")


def test_different_target_path_needs_its_own_card(tmp_path):
    """Same content, different file: a separate authorization."""
    one = tmp_path / "a" / "AGENTS.md"
    two = tmp_path / "b" / "AGENTS.md"
    for p in (one, two):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(one), content="same\n").released
        gw.answer("once")
        other = gw.call(write_file_tool, path=str(two), content="same\n")
        assert other.released, "a write to a DIFFERENT file reused the grant"
        assert two.read_text() == ""
        assert gw.card_count == 2


def test_write_mode_and_patch_mode_are_different_authorizations(tmp_path):
    """A `write` grant must not authorize an equivalent `patch`."""
    target = tmp_path / "AGENTS.md"
    target.write_text("old\n")

    with LiveApprovalGateway() as gw:
        assert gw.call(write_file_tool, path=str(target),
                       content="new\n").released
        gw.answer("once")
        as_patch = gw.call(patch_tool, mode="replace", path=str(target),
                           old_string="old", new_string="new")
        assert as_patch.released, (
            "a patch consumed a grant the user gave for a whole-file write")
        assert target.read_text() == "old\n"


# ── Point 3 of the ask: a broken preview must fail closed ──────────────


def test_preview_failure_fails_closed(monkeypatch, agents_md):
    """A preview that blows up must not degrade to a weaker identity.

    Today the gate falls back to target-only identity, which would let a
    later, different write to the same file redeem this grant.
    """
    import tools.file_tools as FT

    monkeypatch.setattr(
        FT, "_build_write_preview_inner",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    with LiveApprovalGateway() as gw:
        first = gw.call(write_file_tool, path=str(agents_md), content="one\n")
        assert first.blocked, (
            "a preview-construction failure silently weakened the "
            "authorization instead of failing closed")
        assert agents_md.read_text() == "alpha\nbeta\nalpha\n"
