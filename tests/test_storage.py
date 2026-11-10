"""Тесты SQLite-хранилища."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.storage import Storage


@pytest.fixture()
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "test.db")
    await instance.init()
    yield instance
    await instance.close()


async def _create(storage: Storage, source: str = "test.png") -> int:
    return await storage.save_draft(
        chat_id=1,
        year=2026,
        month=11,
        employee_names=["Иванов Иван", "Петров Пётр"],
        days=[1, 2, 3],
        weekend={1: True, 2: False, 3: False},
        cells={
            (0, 1): ("Н", "night", 90),
            (1, 1): ("Д", "day", 90),
            (0, 2): (None, "off", 0),
            (1, 2): ("Д", "day", 90),
            (0, 3): ("О", "vacation", 0),
            (1, 3): (None, "off", 0),
        },
        source=source,
    )


async def test_save_and_read(storage: Storage) -> None:
    schedule_id = await _create(storage)
    schedule = await storage.get_schedule(schedule_id, chat_id=1)
    assert schedule is not None
    assert schedule.status == "draft"
    assert [employee.name for employee in schedule.employees] == [
        "Иванов Иван",
        "Петров Пётр",
    ]
    assert schedule.days[1].weekday == 6  # 1 ноября 2026 — воскресенье
    assert schedule.days[1].weekend is True
    assert schedule.shift(0, 1).type == "night"
    assert schedule.shift(1, 3).type == "off"

    # Чужой чат не должен видеть расписание.
    assert await storage.get_schedule(schedule_id, chat_id=2) is None


async def test_activation_and_replace(storage: Storage) -> None:
    first = await _create(storage, source="first.png")
    assert await storage.get_active(1, 2026, 11) is None
    assert await storage.activate(first, 1) is True
    active = await storage.get_active(1, 2026, 11)
    assert active is not None and active.id == first
    assert await storage.has_schedule(1, 2026, 11) is True

    second = await _create(storage, source="second.png")
    await storage.activate(second, 1)
    active = await storage.get_active(1, 2026, 11)
    assert active is not None and active.id == second
    assert await storage.get_schedule(first, 1) is None


async def test_employee_name_and_weekday_update(storage: Storage) -> None:
    schedule_id = await _create(storage)
    await storage.rename_employee(schedule_id, 1, 0, "Сидоров Сидор")
    assert await storage.update_month(schedule_id, 1, 2026, 12) is True
    schedule = await storage.get_schedule(schedule_id, 1)
    assert schedule is not None
    assert schedule.employees[0].name == "Сидоров Сидор"
    assert schedule.month == 12
    assert schedule.days[1].weekday == 1  # 1 декабря 2026 — вторник


async def test_shift_update(storage: Storage) -> None:
    schedule_id = await _create(storage)
    ok = await storage.set_shift(schedule_id, 1, 1, 2, "Н", "night")
    assert ok is True
    schedule = await storage.get_schedule(schedule_id, 1)
    assert schedule.shift(1, 2).type == "night"

    assert await storage.set_shift(schedule_id, 1, 99, 2, "Д", "day") is False
    assert await storage.set_shift(schedule_id, 1, 1, 99, "Д", "day") is False


async def test_chat_settings(storage: Storage) -> None:
    settings = await storage.get_settings(1)
    assert settings is None

    await storage.set_employee(1, "Иванов Иван")
    settings = await storage.get_settings(1)
    assert settings is not None
    assert settings.employee_name == "Иванов Иван"
    assert settings.employee_name_norm == "иванов иван"
    assert settings.remember is False

    await storage.set_remember(1, True)
    settings = await storage.get_settings(1)
    assert settings.remember is True
    assert settings.employee_name == "Иванов Иван"

    await storage.clear_employee(1)
    settings = await storage.get_settings(1)
    assert settings.employee_name is None
    assert settings.remember is False


async def test_delete_month(storage: Storage) -> None:
    schedule_id = await _create(storage)
    await storage.activate(schedule_id, 1)
    assert await storage.delete_month(1, 2026, 11) == 1
    assert await storage.get_latest(1) is None
    assert await storage.get_schedule(schedule_id, 1) is None
