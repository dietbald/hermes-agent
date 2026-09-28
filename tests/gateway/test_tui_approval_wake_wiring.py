"""TJS-253: the dashboard/TUI gateway must register an approval WAKE callback.

``tools.approval._can_release_thread`` only releases the agent thread when a
wake callback exists for the session key. ``tui_gateway/server.py`` used to
register ``register_gateway_notify`` alone, so every dashboard-raised approval
took the blocking fallback bounded by ``approvals.timeout`` — which expires
into a DENY (the original TJS-222 complaint).

These tests check the real source wiring (server.py cannot be imported in a
bare interpreter — it pulls the whole CLI env loader), plus a live behavioural
round trip of the callback body against the real approval module.
"""
import ast
import pathlib
import threading

import pytest

from tools import approval as A

SRC = pathlib.Path(__file__).resolve().parents[2] / "tui_gateway" / "server.py"


def _source() -> str:
    return SRC.read_text(encoding="utf-8")


def test_tui_gateway_registers_an_approval_wake_callback():
    text = _source()
    assert "register_gateway_wake(" in text, (
        "tui_gateway/server.py registers no approval wake callback — a "
        "dashboard-raised card cannot release the thread and expires into a "
        "denial (TJS-253)"
    )
    assert "def _register_approval_callbacks(" in text
    assert "def _emit_approval_wake(" in text


def test_every_notify_registration_also_registers_wake():
    """No path may register notify alone — that is the broken half-wiring."""
    tree = ast.parse(_source())
    helper = "_register_approval_callbacks"
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "register_gateway_notify":
            continue
        offenders.append(getattr(node, "lineno", -1))
    # The only permitted call site is inside the shared helper.
    allowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == helper:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = getattr(sub.func, "id", None) or getattr(
                        sub.func, "attr", None
                    )
                    if fn == "register_gateway_notify":
                        allowed.add(getattr(sub, "lineno", -1))
    stray = sorted(set(offenders) - allowed)
    assert not stray, (
        "register_gateway_notify called outside "
        f"{helper}() at line(s) {stray} — that session gets notify without a "
        "wake callback and falls back to the blocking/expiring path (TJS-253)"
    )


def test_wake_is_unregistered_wherever_notify_is():
    """A stale wake cb on a dead session would point at a gone session dict."""
    text = _source()
    assert text.count("unregister_gateway_wake(") >= text.count(
        "unregister_gateway_notify("
    ) - 1, (
        "unregister_gateway_wake is not paired with unregister_gateway_notify "
        "on every teardown path (TJS-253)"
    )


def test_tui_server_still_parses():
    ast.parse(_source())


def _clear(sk):
    A._gateway_queues.pop(sk, None)
    A._gateway_notify_cbs.pop(sk, None)
    A._gateway_wake_cbs.pop(sk, None)


@pytest.fixture
def release_mode(monkeypatch):
    monkeypatch.setattr(
        A, "_get_approval_config",
        lambda: {"mode": "manual", "timeout": 31536000, "release_thread": True},
    )


def test_dashboard_session_releases_and_resumes(release_mode):
    """Behavioural round trip with the real server-side callback body.

    Loads ``_approval_wake_text`` and ``_emit_approval_wake`` out of
    server.py's AST (the module itself needs the CLI env loader), wires them to
    a fake session registry + fake ``_run_prompt_submit``, and drives a full
    raise -> release -> answer -> resume cycle through ``tools.approval``.
    """
    tree = ast.parse(_source())
    wanted = {"_approval_wake_text", "_emit_approval_wake"}
    picked = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in wanted
    ]
    assert {n.name for n in picked} == wanted, (
        f"missing {wanted - {n.name for n in picked}} in tui_gateway/server.py"
    )

    submitted = []
    emitted = []
    session = {"history_lock": threading.RLock(), "running": False,
               "transport": None}
    ns = {
        "_sessions": {"sid-1": session},
        "_sessions_lock": threading.RLock(),
        "_emit": lambda ev, sid, *a: emitted.append(ev),
        "_run_prompt_submit": lambda rid, sid, sess, text: submitted.append(text),
        "_enqueue_prompt": lambda sess, text, tr: submitted.append(("queued", text)),
        "logger": __import__("logging").getLogger("tjs253-test"),
        "time": __import__("time"),
    }
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(SRC), "exec"), ns)

    sk = "tjs253-dashboard"
    _clear(sk)
    try:
        A.register_gateway_notify(sk, lambda d: None)
        A.register_gateway_wake(
            sk, lambda data, dec: ns["_emit_approval_wake"]("sid-1", data, dec)
        )
        assert A._can_release_thread(sk) is True, (
            "dashboard session still cannot release the thread"
        )

        result = {}

        def agent():
            result["d"] = A._await_gateway_decision(
                sk, lambda d: None,
                {"command": "systemctl --user restart nginx", "pattern_key": "p",
                 "pattern_keys": ["p"], "description": "restart nginx"},
            )

        th = threading.Thread(target=agent)
        th.start()
        th.join(timeout=10)
        assert not th.is_alive(), "agent thread parked instead of releasing"
        assert result["d"]["released"] is True
        assert result["d"]["resolved"] is False

        assert A.resolve_gateway_approval(sk, "once") == 1
        assert submitted, "the answer did not resume the dashboard session"
        assert "APPROVED" in submitted[0]
        assert "restart nginx" in submitted[0]
        assert "message.start" in emitted
        assert session["running"] is True
    finally:
        _clear(sk)


def test_wake_queues_when_the_session_is_busy(release_mode):
    tree = ast.parse(_source())
    picked = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name in {"_approval_wake_text", "_emit_approval_wake"}
    ]
    submitted, queued = [], []
    session = {"history_lock": threading.RLock(), "running": True,
               "transport": None}
    ns = {
        "_sessions": {"sid-1": session},
        "_sessions_lock": threading.RLock(),
        "_emit": lambda *a: None,
        "_run_prompt_submit": lambda *a: submitted.append(a),
        "_enqueue_prompt": lambda sess, text, tr: queued.append(text),
        "logger": __import__("logging").getLogger("tjs253-test"),
        "time": __import__("time"),
    }
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(SRC), "exec"), ns)
    ns["_emit_approval_wake"]("sid-1", {"description": "d"}, {"choice": "deny"})
    assert queued and not submitted, (
        "a busy session must queue the decision, not race the live turn"
    )
    assert "DENIED" in queued[0]
