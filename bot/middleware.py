"""Middleware, блокирующий чат на время распознавания."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from .processing import ProcessingRegistry

WAIT_TEXT = "⏳ Ожидайте, идёт распознавание…\nОтменить: /cancel"


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

        chat_id: int | None = None
        if isinstance(event, Message):
            chat_id = event.chat.id
        elif isinstance(event, CallbackQuery):
            if event.message is not None:
                chat_id = event.message.chat.id
            elif event.from_user is not None:
                chat_id = event.from_user.id

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
