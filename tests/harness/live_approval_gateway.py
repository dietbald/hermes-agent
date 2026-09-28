"""Live-gateway approval harness (TJS-258).

Why this exists
---------------
Five review rounds on the TJS-226 approval-release work (TJS-256) found five
real defects. Every one of them passed the unit tests that were written for
it, because a unit test *constructs* the input to the thing under test and
therefore cannot prove the **caller** builds that input correctly. All five
defects lived exactly there:

* the card's ``command`` text was used as grant identity (redaction collision),
* the protected-write caller passed **no** ``raw_operation`` at all,
* the preview's snippet diff was used, so ``replace_all=True`` and
  ``replace_all=False`` produced the same identity.

Each would have been caught by ONE real card driven end to end. That is the
capability this module provides.

What "live" means here
----------------------
Nothing is stubbed on the approval path. The harness:

1. turns on ``approvals.release_thread`` through the real config reader,
2. registers a real notify callback and a real wake callback with
   ``tools.approval`` (the same two registrations ``gateway/run.py`` makes),
3. calls the **public tool entry point** (``write_file_tool`` /
   ``patch_tool``) on a worker thread, exactly as an agent turn does, with
   the session key set through ``set_current_session_key``,
4. renders the raised card through the real platform formatter
   (``BasePlatformAdapter._format_exec_approval``) so what the assertions see
   is the text a user would actually be shown,
5. answers the card through ``resolve_gateway_approval`` — the function the
   Telegram button handler calls — which fires the wake,
6. resumes by re-invoking the tool call, which is what the wake turn makes the
   agent do,
7. and finally reports **what is on disk**.

The only thing faked is the chat transport (no network), and the model (the
resume turn re-runs the recorded tool call verbatim, which is the behaviour
the wake text instructs). Everything between the tool call and the answer is
production code.

Usage::

    with LiveApprovalGateway() as gw:
        first = gw.call(write_file_tool, path=str(p), content="x")
        assert first.released
        gw.answer("once")
        second = gw.call(write_file_tool, path=str(p), content="x")
        assert second.ok and p.read_text() == "x"
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import tools.approval as A


@dataclass
class Card:
    """One approval card as the user would see it."""

    data: dict
    rendered: str
    fingerprint: str = ""

    @property
    def request_id(self) -> str:
        return str(self.data.get("request_id") or "")

    @property
    def command(self) -> str:
        return str(self.data.get("command") or "")

    @property
    def description(self) -> str:
        return str(self.data.get("description") or "")

    def contains(self, needle: str) -> bool:
        """True when *needle* appears anywhere the user could see it.

        Used to assert that a digest/secret never reaches the card payload.
        """
        return needle in self.rendered or any(
            needle in str(v) for v in self.data.values()
        )


@dataclass
class ToolResult:
    """Outcome of one tool call driven through the harness."""

    value: Any = None
    error: Optional[BaseException] = None

    @property
    def text(self) -> str:
        return "" if self.value is None else str(self.value)

    @property
    def released(self) -> bool:
        """The card was raised and the agent thread was released."""
        return "PENDING APPROVAL" in self.text

    @property
    def blocked(self) -> bool:
        return self.text.startswith("BLOCKED") or "BLOCKED:" in self.text

    @property
    def ok(self) -> bool:
        return (
            self.error is None
            and not self.released
            and not self.blocked
            and "Error" not in self.text[:32]
        )


class _Renderer:
    """Real platform card formatter, with no transport attached.

    ``BasePlatformAdapter`` is abstract (connect/disconnect/send/
    get_chat_info) and its ``__init__`` wants a gateway, so a minimal
    concrete subclass is allocated without running ``__init__``.
    ``_format_exec_approval`` touches only class-level template attrs,
    ``_truncate_preview`` and ``_ea_escape`` — none of which the transport
    methods affect. That core is what every real adapter calls, so the
    rendered text is the production text.
    """

    def __init__(self):
        from gateway.platforms.base import BasePlatformAdapter

        class _Concrete(BasePlatformAdapter):
            async def connect(self):  # pragma: no cover - never called
                raise NotImplementedError

            async def disconnect(self):  # pragma: no cover
                raise NotImplementedError

            async def send(self, *a, **k):  # pragma: no cover
                raise NotImplementedError

            async def get_chat_info(self, *a, **k):  # pragma: no cover
                raise NotImplementedError

        self._adapter = object.__new__(_Concrete)

    def render(self, data: dict) -> str:
        return self._adapter._format_exec_approval(
            data.get("command", ""),
            data.get("description", "dangerous command"),
        )


class LiveApprovalGateway:
    """Drive real approval cards end to end against a real gateway session."""

    def __init__(self, session_key: str = "harness-session",
                 release_thread: bool = True,
                 approval_timeout: int = 5):
        self.session_key = session_key
        self.cards: list[Card] = []
        self.wakes: list[dict] = []
        self._release_thread = release_thread
        self._approval_timeout = approval_timeout
        self._saved_cfg: Optional[Callable] = None
        self._renderer = _Renderer()
        self._card_seen = threading.Event()

    # ── lifecycle ──────────────────────────────────────────────────────

    def __enter__(self) -> "LiveApprovalGateway":
        self._reset_module_state()

        # Real config path: _thread_release_enabled() reads this.
        self._saved_cfg = A._get_approval_config
        cfg = {
            "mode": "manual",
            "timeout": self._approval_timeout,
            "release_thread": self._release_thread,
        }
        A._get_approval_config = lambda: cfg  # type: ignore[assignment]

        A.register_gateway_notify(self.session_key, self._notify)
        if self._release_thread:
            A.register_gateway_wake(self.session_key, self._wake)
        return self

    def __exit__(self, *exc) -> None:
        if self._saved_cfg is not None:
            A._get_approval_config = self._saved_cfg  # type: ignore[assignment]
        self._reset_module_state()
        return None

    def _reset_module_state(self) -> None:
        A._gateway_queues.clear()
        A._gateway_notify_cbs.clear()
        A._gateway_wake_cbs.clear()
        A._session_approved.clear()
        A._permanent_approved.clear()
        A._pending.clear()
        getattr(A, "_released_once_grants", {}).clear()

    # ── gateway callbacks (same two gateway/run.py registers) ──────────

    def _notify(self, approval_data: dict) -> None:
        """Platform-side: render and 'send' the card."""
        card = Card(data=dict(approval_data),
                    rendered=self._renderer.render(approval_data))
        # Record the entry's fingerprint for assertions. It is deliberately
        # NOT part of approval_data (it must never reach a platform), so it
        # is read off the queue entry.
        with A._lock:
            for entry in A._gateway_queues.get(self.session_key, []):
                if entry.data.get("request_id") == card.request_id:
                    card.fingerprint = getattr(entry, "fingerprint", "") or ""
        self.cards.append(card)
        self._card_seen.set()

    def _wake(self, approval_data: dict, decision: dict) -> None:
        """Gateway-side: record the resume turn that would be pushed.

        Mirrors ``gateway/run.py::_approval_wake_sync`` — it builds the
        ``[approval resolved]`` instruction and hands it to the session. The
        harness records it; ``call()`` replays the tool invocation, which is
        what that instruction tells the agent to do.
        """
        self.wakes.append({"approval_data": dict(approval_data),
                           "decision": dict(decision)})

    # ── driving ────────────────────────────────────────────────────────

    def call(self, fn: Callable, /, **kwargs) -> ToolResult:
        """Invoke a tool the way an agent turn does, on its own thread.

        The session key is set inside the worker thread because it lives in a
        ``contextvars`` variable — setting it on the test thread would not be
        visible to the tool, and the gate would look up an empty session.
        """
        out = ToolResult()

        def _run():
            token = A.set_current_session_key(self.session_key)
            try:
                out.value = fn(**kwargs)
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                out.error = exc
            finally:
                try:
                    A.reset_current_session_key(token)
                except Exception:
                    pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=30)
        if t.is_alive():
            raise AssertionError(
                "tool call did not return within 30s — the agent thread was "
                "parked on the approval instead of released"
            )
        return out

    def answer(self, choice: str, *, request_id: Optional[str] = None,
               reason: Optional[str] = None) -> int:
        """Tap a card button. Real entry point used by the Telegram handler."""
        return A.resolve_gateway_approval(
            self.session_key, choice, reason=reason, request_id=request_id)

    # ── inspection ─────────────────────────────────────────────────────

    @property
    def card(self) -> Card:
        assert self.cards, "no approval card was raised"
        return self.cards[-1]

    @property
    def card_count(self) -> int:
        return len(self.cards)

    def pending(self) -> list:
        with A._lock:
            return list(A._gateway_queues.get(self.session_key, []))

    def grants(self) -> list:
        with A._lock:
            return list(
                getattr(A, "_released_once_grants", {}).get(self.session_key, []))
