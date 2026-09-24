"""TJS-226: verify the gateway ACTUALLY registers the wake callback.

The approval-layer tests use a hand-made wake_cb. This one checks the real
gateway/run.py source wiring, so a future refactor that drops the
registration is caught.
"""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "gateway" / "run.py"


def test_gateway_registers_an_approval_wake_callback():
    text = SRC.read_text(encoding="utf-8")
    assert "register_gateway_wake(_approval_session_key" in text, (
        "gateway/run.py no longer registers an approval wake callback — a "
        "released approval would have no way to resume the run (TJS-226)"
    )
    assert "def _approval_wake_sync(" in text


def test_wake_callback_is_not_torn_down_with_the_turn():
    """The answer arrives after the turn ends, so the callback must outlive it.

    ``unregister_gateway_notify`` runs in the turn's finally block; an
    equivalent unregister of the wake callback there would defeat the whole
    mechanism.
    """
    text = SRC.read_text(encoding="utf-8")
    assert "unregister_gateway_wake(_approval_session_key" not in text, (
        "the approval wake callback is unregistered at end of turn — the "
        "user's answer would arrive with nothing left to resume (TJS-226)"
    )


def test_gateway_run_still_parses():
    ast.parse(SRC.read_text(encoding="utf-8"))
