"""Middleware: блокировка чата и запрет параллельного OCR."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from .processing import ProcessingRegistry
from .storage import Storage

WAIT_TEXT = "⏳ Ожидайте, идёт распознавание…\nОтменить: /cancel"


def event_chat_id(event: TelegramObject) -> int | None:
    if isinstance(event, Message):
        return event.chat.id if event.chat else None
    if isinstance(event, CallbackQuery):
        if event.message is not None:
            return event.message.chat.id
        if event.from_user is not None:
            return event.from_user.id
    return None


class BlockedChatMiddleware(BaseMiddleware):
    """Отбрасывает любые события заблокированного чата."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        storage: Storage | None = data.get("storage")
        if storage is None:
            return await handler(event, data)
        chat_id = event_chat_id(event)
        if chat_id is None:
            return await handler(event, data)
        if await storage.is_chat_blocked(chat_id):
            return None
        return await handler(event, data)


class ProcessingMiddleware(BaseMiddleware):
    """Пропускает только /cancel и кнопку отмены, пока идёт OCR."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        registry: ProcessingRegistry | None = data.get("processing")
        if registry is None:
            return await handler(event, data)

        chat_id = event_chat_id(event)
        if chat_id is None:
            return await handler(event, data)

        job = registry.get(chat_id)
        if job is None:
            return await handler(event, data)

        if isinstance(event, Message):
            text = (event.text or "").strip()
            command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
            if command == "/cancel":
                return await handler(event, data)
            if registry.should_notify(job):
                await event.answer(WAIT_TEXT)
            return None

        if isinstance(event, CallbackQuery):
            if (event.data or "").startswith("cancel"):
                return await handler(event, data)
            if registry.should_notify(job):
                await event.answer(
                    "⏳ Ожидайте, идёт распознавание…",
                    show_alert=False,
                )
            return None

        return await handler(event, data)
