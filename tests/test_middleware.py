"""Тесты middleware блокировки чата на время OCR."""

from __future__ import annotations

from datetime import datetime

from aiogram.types import Chat, Message

from bot.middleware import ProcessingMiddleware
from bot.processing import ProcessingRegistry


def _message(chat_id: int, text: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        text=text,
    )


async def test_blocks_messages_but_allows_cancel(monkeypatch) -> None:
    registry = ProcessingRegistry()
    job = await registry.try_start(1)
    assert job is not None

    middleware = ProcessingMiddleware()
    handled: list[str] = []

    async def handler(event, data):
        handled.append(event.text or "")
        return "handled"

    answers: list[str] = []

    async def fake_answer(self, text, **kwargs):
        answers.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)

    result = await middleware(
        handler, _message(1, "привет"), {"processing": registry}
    )
    assert result is None
    assert handled == []
    assert "Ожидайте" in answers[-1]

    result = await middleware(
        handler, _message(1, "/cancel"), {"processing": registry}
    )
    assert result == "handled"
    assert handled == ["/cancel"]

    # Другие чаты не блокируются.
    result = await middleware(
        handler, _message(2, "привет"), {"processing": registry}
    )
    assert result == "handled"
    assert handled == ["/cancel", "привет"]
