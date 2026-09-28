"""Authorization tests for Telegram Paperclip decision-card callbacks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )
    adapter._message_handler = None
    adapter._handle_paperclip_card_callback = AsyncMock()
    return adapter


def _make_update(user_id: int):
    query = SimpleNamespace(
        data="pcd:accept:deadbeef",
        from_user=SimpleNamespace(id=user_id, first_name="Callback User"),
        message=SimpleNamespace(
            chat_id=12345,
            chat=SimpleNamespace(type="private"),
            message_thread_id=None,
        ),
        answer=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query), query


@pytest.mark.asyncio
async def test_paperclip_card_callback_rejects_unauthorized_sender(monkeypatch):
    adapter = _make_adapter()
    update, query = _make_update(user_id=222)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111")

    await adapter._handle_callback_query(update, SimpleNamespace())

    query.answer.assert_awaited_once()
    assert "not authorized" in query.answer.await_args.kwargs["text"].lower()
    adapter._handle_paperclip_card_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_paperclip_card_callback_allows_authorized_sender(monkeypatch):
    adapter = _make_adapter()
    update, query = _make_update(user_id=111)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111")

    await adapter._handle_callback_query(update, SimpleNamespace())

    query.answer.assert_not_awaited()
    adapter._handle_paperclip_card_callback.assert_awaited_once_with(
        query, "pcd:accept:deadbeef"
    )
