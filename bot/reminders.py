"""Фоновый планировщик напоминаний о сменах."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from .config import Config
from .formatting import reminder_message_text
from .keyboards import reminder_ack_keyboard
from .models import Employee, ReminderSettings, Shift
from .services import (
    find_employee,
    reminder_due_at,
    resolve_timezone,
)
from .storage import Storage

log = logging.getLogger(__name__)


class ReminderScheduler:
    """Раз в полминуты проверяет, кому пора отправить напоминание."""

    def __init__(
        self,
        bot: Bot,
        storage: Storage,
        config: Config,
        *,
        interval: float = 30.0,
        repeat_after: timedelta = timedelta(minutes=30),
        stale_after: timedelta = timedelta(hours=6),
    ) -> None:
        self.bot = bot
        self.storage = storage
        self.config = config
        self.interval = interval
        self.repeat_after = repeat_after
        self.stale_after = stale_after

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - защита фонового цикла
                log.exception("Ошибка планировщика напоминаний")
            await asyncio.sleep(self.interval)

    async def tick(self, now: datetime | None = None) -> None:
        tz = resolve_timezone(self.config.timezone)
        if tz is None:
            log.warning(
                "Неизвестный часовой пояс %s, напоминания используют локальное время",
                self.config.timezone,
            )
            tz = datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
        if now is None:
            now = datetime.now(tz)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=tz)
        else:
            now = now.astimezone(tz)

        settings_list = await self.storage.list_enabled_reminder_settings()
        for settings in settings_list:
            try:
                await self._tick_settings(settings, now, tz)
            except Exception:  # pragma: no cover - один чат не должен ломать других
                log.exception(
                    "Ошибка напоминания для чата %s", settings.chat_id
                )

    async def _tick_settings(
        self,
        settings: ReminderSettings,
        now: datetime,
        tz: ZoneInfo,
    ) -> None:
        group = await self.storage.get_group(settings.group_id)
        if group is None:
            return
        subscription = await self.storage.get_subscription(settings.chat_id)
        if (
            subscription is None
            or subscription.group_id != group.id
            or not subscription.employee_name
        ):
            return

        today = now.date()
        # За день до смены смотрим на завтрашнюю дату, для конца ночной смены —
        # на вчерашнюю. Запаса в два дня хватает для всех режимов.
        for offset in range(-2, 2):
            shift_day = today + timedelta(days=offset)
            schedule = await self.storage.get_active(
                group.id, shift_day.year, shift_day.month
            )
            if schedule is None:
                continue
            employee = find_employee(schedule, subscription.employee_name)
            if employee is None:
                continue
            shift = schedule.shift(employee.position, shift_day.day)
            if shift is None or shift.type not in settings.shift_types:
                continue

            due_at = reminder_due_at(
                shift_day, shift.type, settings.mode, settings.time, tz
            )
            if due_at is None or now < due_at:
                continue
            if now > due_at + self.stale_after:
                continue

            delivery = await self.storage.get_reminder_delivery(
                settings.chat_id, shift_day, settings.mode
            )
            if delivery is not None:
                if delivery.acked_at is not None:
                    continue
                if (
                    delivery.last_sent_at is not None
                    and now < delivery.last_sent_at + self.repeat_after
                ):
                    continue
            else:
                delivery = await self.storage.create_reminder_delivery(
                    chat_id=settings.chat_id,
                    group_id=group.id,
                    employee_position=employee.position,
                    shift_date=shift_day,
                    shift_type=shift.type,
                    mode=settings.mode,
                    due_at=due_at,
                )

            await self._send(
                delivery.id,
                settings.chat_id,
                employee,
                shift_day,
                shift,
                settings.mode,
                now,
            )

    async def _send(
        self,
        delivery_id: int,
        chat_id: int,
        employee: Employee,
        shift_day: date,
        shift: Shift,
        mode: str,
        now: datetime,
    ) -> None:
        # Пока планировщик собирался отправить сообщение, пользователь мог
        # нажать кнопку подтверждения.
        fresh = await self.storage.get_reminder_delivery_by_id(delivery_id)
        if fresh is None or fresh.acked_at is not None:
            return

        try:
            await self.bot.send_message(
                chat_id,
                reminder_message_text(
                    employee, shift_day, shift, mode, today=now.date()
                ),
                reply_markup=reminder_ack_keyboard(delivery_id),
            )
        except TelegramForbiddenError:
            log.warning(
                "Не могу отправить напоминание в чат %s — выключаю напоминания",
                chat_id,
            )
            await self.storage.set_reminder_enabled(chat_id, False)
            await self.storage.cancel_pending_reminders(chat_id)
            return
        except TelegramBadRequest as exc:
            log.warning(
                "Telegram отклонил напоминание для чата %s: %s", chat_id, exc
            )
            return
        except Exception:  # pragma: no cover - сетевые ошибки
            log.exception("Не удалось отправить напоминание в чат %s", chat_id)
            return

        await self.storage.touch_reminder_delivery(delivery_id, now)
