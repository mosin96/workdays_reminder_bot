"""Тесты планировщика напоминаний."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot.config import Config
from bot.reminders import ReminderScheduler
from bot.storage import Storage


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return object()


@pytest.fixture()
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "reminders.db")
    await instance.init()
    yield instance
    await instance.close()


async def _setup(
    storage: Storage,
    *,
    shift_type: str = "day",
    year: int = 2026,
    month: int = 11,
    day: int = 12,
):
    group = await storage.create_group(owner_chat_id=1)
    await storage.subscribe(1, group.id)
    await storage.set_employee(1, "Иванов Иван")
    codes = {"day": "Д", "night": "Н", "short_day": "ДД"}
    schedule_id = await storage.save_draft(
        group_id=group.id,
        year=year,
        month=month,
        employee_names=["Иванов Иван"],
        days=[day],
        weekend={day: False},
        cells={(0, day): (codes[shift_type], shift_type, 100)},
    )
    await storage.activate(schedule_id, group.id)
    return group, schedule_id


def _config(storage: Storage) -> Config:
    return Config(
        bot_token="test",
        db_path=storage.path,
        timezone="UTC",
    )


async def _enable(
    storage: Storage,
    group_id: int,
    *,
    modes: tuple[str, ...],
    time: str = "20:00",
    shift_types: tuple[str, ...] = ("day", "night", "short_day"),
) -> None:
    await storage.save_reminder_settings(
        chat_id=1,
        group_id=group_id,
        modes=modes,
        time=time,
        shift_types=shift_types,
        enabled=True,
    )


async def test_shift_start_send_repeat_and_ack(storage: Storage) -> None:
    group, _ = await _setup(storage)
    await _enable(storage, group.id, modes=("shift_start",))
    bot = FakeBot()
    scheduler = ReminderScheduler(bot, storage, _config(storage))
    shift_day = date(2026, 11, 12)
    now = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)

    await scheduler.tick(now)
    assert len(bot.sent) == 1
    assert "начало смены" in bot.sent[0][1]
    delivery = await storage.get_reminder_delivery(
        1, shift_day, "shift_start"
    )
    assert delivery is not None
    assert delivery.sent_count == 1
    assert delivery.last_sent_at == now

    await scheduler.tick(now + timedelta(minutes=10))
    assert len(bot.sent) == 1

    await scheduler.tick(now + timedelta(minutes=30))
    assert len(bot.sent) == 2

    assert await storage.ack_reminder_delivery(delivery.id, 1)
    await scheduler.tick(now + timedelta(minutes=60))
    assert len(bot.sent) == 2


async def test_day_before_across_month_boundary(storage: Storage) -> None:
    group, _ = await _setup(
        storage, year=2026, month=12, day=1, shift_type="day"
    )
    await _enable(storage, group.id, modes=("day_before",), time="20:00")
    bot = FakeBot()
    scheduler = ReminderScheduler(bot, storage, _config(storage))

    await scheduler.tick(
        datetime(2026, 11, 30, 19, 59, tzinfo=timezone.utc)
    )
    assert bot.sent == []

    await scheduler.tick(
        datetime(2026, 11, 30, 20, 0, tzinfo=timezone.utc)
    )
    assert len(bot.sent) == 1
    assert "завтра смена" in bot.sent[0][1]


async def test_day_before_after_midnight_says_today(storage: Storage) -> None:
    group, _ = await _setup(storage, day=12, shift_type="day")
    await _enable(storage, group.id, modes=("day_before",), time="20:00")
    bot = FakeBot()
    scheduler = ReminderScheduler(bot, storage, _config(storage))

    await scheduler.tick(
        datetime(2026, 11, 12, 0, 30, tzinfo=timezone.utc)
    )
    assert len(bot.sent) == 1
    assert "сегодня смена" in bot.sent[0][1]
    assert "завтра смена" not in bot.sent[0][1]


async def test_end_of_night_shift(storage: Storage) -> None:
    group, _ = await _setup(storage, day=12, shift_type="night")
    await _enable(storage, group.id, modes=("shift_end",))
    bot = FakeBot()
    scheduler = ReminderScheduler(bot, storage, _config(storage))

    await scheduler.tick(
        datetime(2026, 11, 13, 7, 59, tzinfo=timezone.utc)
    )
    assert bot.sent == []

    await scheduler.tick(
        datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
    )
    assert len(bot.sent) == 1
    assert "Смена завершена" in bot.sent[0][1]


async def test_all_three_modes_work_together(storage: Storage) -> None:
    group, _ = await _setup(storage, day=12, shift_type="day")
    await _enable(
        storage,
        group.id,
        modes=("day_before", "shift_start", "shift_end"),
        time="20:00",
    )
    bot = FakeBot()
    scheduler = ReminderScheduler(bot, storage, _config(storage))

    await scheduler.tick(
        datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)
    )
    assert len(bot.sent) == 1
    assert "завтра смена" in bot.sent[0][1]

    await scheduler.tick(
        datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
    )
    assert len(bot.sent) == 2
    assert "начало смены" in bot.sent[1][1]

    await scheduler.tick(
        datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)
    )
    assert len(bot.sent) == 3
    assert "Смена завершена" in bot.sent[2][1]

    delivery = await storage.get_reminder_delivery(
        1, date(2026, 11, 12), "shift_start"
    )
    assert delivery is not None
    assert delivery.sent_count == 1


async def test_stale_reminder_is_not_sent(storage: Storage) -> None:
    group, _ = await _setup(storage)
    await _enable(storage, group.id, modes=("shift_start",))
    bot = FakeBot()
    scheduler = ReminderScheduler(bot, storage, _config(storage))

    await scheduler.tick(
        datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
    )
    assert bot.sent == []


async def test_disabled_or_filtered_shift_is_skipped(storage: Storage) -> None:
    group, _ = await _setup(storage)
    await _enable(
        storage,
        group.id,
        modes=("shift_start",),
        shift_types=("night",),
    )
    bot = FakeBot()
    scheduler = ReminderScheduler(bot, storage, _config(storage))
    now = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)

    await scheduler.tick(now)
    assert bot.sent == []

    await _enable(
        storage,
        group.id,
        modes=("shift_start",),
        shift_types=("day",),
    )
    await storage.set_reminder_enabled(1, False)
    await scheduler.tick(now)
    assert bot.sent == []


async def test_reminder_uses_selected_employee(storage: Storage) -> None:
    group, _ = await _setup(storage)
    await storage.set_employee(1, "Петров Пётр")
    await _enable(storage, group.id, modes=("shift_start",))
    bot = FakeBot()
    scheduler = ReminderScheduler(bot, storage, _config(storage))

    await scheduler.tick(
        datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
    )
    assert bot.sent == []
