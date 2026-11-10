"""Тесты SQLite-хранилища."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bot.codes import format_code
from bot.storage import Storage


@pytest.fixture()
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "test.db")
    await instance.init()
    yield instance
    await instance.close()


async def _create(
    storage: Storage, group_id: int, source: str = "test.png"
) -> int:
    return await storage.save_draft(
        group_id=group_id,
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


async def test_group_code_and_subscription(storage: Storage) -> None:
    group = await storage.create_group(owner_chat_id=1)
    assert len(group.code) == 8
    assert format_code(group.code) == f"{group.code[:4]}-{group.code[4:]}"
    assert await storage.get_group(group.id) == group
    assert await storage.get_group_by_code(group.code) == group
    assert await storage.get_group_by_code("WRONG") is None

    subscription = await storage.get_subscription(1)
    assert subscription is None
    await storage.subscribe(1, group.id)
    subscription = await storage.get_subscription(1)
    assert subscription is not None
    assert subscription.group_id == group.id
    assert subscription.employee_name is None

    await storage.set_employee(1, "Петров Пётр")
    await storage.set_remember(1, True)
    subscription = await storage.get_subscription(1)
    assert subscription.employee_name == "Петров Пётр"
    assert subscription.employee_name_norm == "петров петр"
    assert subscription.remember is True

    await storage.subscribe(2, group.id, "Иванов Иван")
    assert await storage.count_subscribers(group.id) == 2

    await storage.clear_employee(1)
    subscription = await storage.get_subscription(1)
    assert subscription.employee_name is None
    assert subscription.employee_name_norm is None
    assert subscription.remember is False

    assert await storage.unsubscribe(2) is True
    assert await storage.get_subscription(2) is None
    assert await storage.count_subscribers(group.id) == 1


async def test_save_and_access(storage: Storage) -> None:
    group = await storage.create_group(owner_chat_id=1)
    schedule_id = await _create(storage, group.id)
    await storage.subscribe(1, group.id)

    schedule = await storage.get_schedule(schedule_id)
    assert schedule is not None
    assert schedule.group_id == group.id
    assert schedule.status == "draft"
    assert [employee.name for employee in schedule.employees] == [
        "Иванов Иван",
        "Петров Пётр",
    ]
    assert schedule.days[1].weekday == 6  # 1 ноября 2026 — воскресенье
    assert schedule.days[1].weekend is True
    assert schedule.shift(0, 1).type == "night"

    assert await storage.get_schedule_for_chat(schedule_id, 1) is not None
    assert await storage.get_schedule_for_chat(schedule_id, 2) is None
    await storage.subscribe(2, group.id)
    assert await storage.get_schedule_for_chat(schedule_id, 2) is not None


async def test_activation_and_group_isolation(storage: Storage) -> None:
    group = await storage.create_group(owner_chat_id=1)
    other_group = await storage.create_group(owner_chat_id=2)
    first = await _create(storage, group.id, "first.png")
    assert await storage.get_active(group.id, 2026, 11) is None
    assert await storage.activate(first, group.id) is True
    active = await storage.get_active(group.id, 2026, 11)
    assert active is not None and active.id == first
    assert await storage.has_schedule(group.id, 2026, 11) is True
    assert await storage.has_schedule(other_group.id, 2026, 11) is False

    second = await _create(storage, group.id, "second.png")
    await storage.activate(second, group.id)
    active = await storage.get_active(group.id, 2026, 11)
    assert active is not None and active.id == second
    assert await storage.get_schedule(first) is None


async def test_employee_weekday_and_shift_updates(storage: Storage) -> None:
    group = await storage.create_group(owner_chat_id=1)
    schedule_id = await _create(storage, group.id)
    await storage.subscribe(1, group.id)

    assert await storage.rename_employee(
        schedule_id, group.id, 0, "Сидоров Сидор"
    )
    assert await storage.update_month(schedule_id, group.id, 2026, 12)
    schedule = await storage.get_schedule(schedule_id)
    assert schedule is not None
    assert schedule.employees[0].name == "Сидоров Сидор"
    assert schedule.month == 12
    assert schedule.days[1].weekday == 1  # 1 декабря 2026 — вторник
    assert schedule.days[31].weekday == 3  # 31 декабря 2026 — четверг
    assert schedule.shift(0, 31).type == "unknown"

    assert await storage.set_shift(
        schedule_id, group.id, 1, 2, "Н", "night"
    )
    schedule = await storage.get_schedule(schedule_id)
    assert schedule.shift(1, 2).type == "night"

    assert await storage.set_shift(
        schedule_id, group.id, 99, 2, "Д", "day"
    ) is False
    assert await storage.set_shift(
        schedule_id, group.id, 1, 99, "Д", "day"
    ) is False
    assert await storage.set_shift(
        schedule_id, group.id + 1, 1, 2, "Д", "day"
    ) is False


async def test_delete_month_and_group(storage: Storage) -> None:
    group = await storage.create_group(owner_chat_id=1)
    schedule_id = await _create(storage, group.id)
    await storage.activate(schedule_id, group.id)
    await storage.subscribe(1, group.id)
    await storage.subscribe(2, group.id)

    assert await storage.delete_month(group.id, 2026, 11) == 1
    assert await storage.get_latest(group.id) is None

    assert await storage.delete_group(group.id) is True
    assert await storage.get_group(group.id) is None
    assert await storage.get_schedule(schedule_id) is None
    assert await storage.get_subscription(1) is None
    assert await storage.get_subscription(2) is None


async def test_failed_code_block(storage: Storage) -> None:
    for attempt in range(1, 11):
        failed, blocked, newly_blocked = await storage.register_failed_code(
            1, limit=10
        )
        assert failed == attempt
        assert blocked is False
        assert newly_blocked is False
    assert await storage.is_chat_blocked(1) is False

    failed, blocked, newly_blocked = await storage.register_failed_code(
        1, limit=10
    )
    assert failed == 11
    assert blocked is True
    assert newly_blocked is True
    assert await storage.is_chat_blocked(1) is True

    await storage.reset_code_attempts(1)
    assert await storage.is_chat_blocked(1) is False
    failed, blocked, newly_blocked = await storage.register_failed_code(
        1, limit=10
    )
    assert failed == 1
    assert blocked is False
    assert newly_blocked is False

    await storage.unblock_chat(1)
    assert await storage.is_chat_blocked(1) is False


async def test_old_database_is_removed(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schedules (id INTEGER PRIMARY KEY, chat_id INTEGER)"
    )
    connection.execute("INSERT INTO schedules (chat_id) VALUES (1)")
    connection.commit()
    connection.close()

    storage = Storage(path)
    await storage.init()
    try:
        cursor = await storage.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        tables = {row[0] for row in await cursor.fetchall()}
        assert "schedule_groups" in tables
        assert "chat_subscriptions" in tables
        cursor = await storage.db.execute("SELECT COUNT(*) FROM schedule_groups")
        row = await cursor.fetchone()
        assert row[0] == 0
    finally:
        await storage.close()


async def test_new_database_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "keep.db"
    first = Storage(path)
    await first.init()
    group = await first.create_group(owner_chat_id=1)
    await first.close()

    second = Storage(path)
    await second.init()
    try:
        restored = await second.get_group(group.id)
        assert restored is not None
        assert restored.code == group.code
    finally:
        await second.close()


async def test_existing_db_gets_overtime_column(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schedule_groups ("
        "id INTEGER PRIMARY KEY, code TEXT, owner_chat_id INTEGER, created_at TEXT)"
    )
    connection.execute(
        "CREATE TABLE shifts ("
        "schedule_id INTEGER, position INTEGER, day INTEGER, "
        "code TEXT, type TEXT, confidence INTEGER, "
        "PRIMARY KEY (schedule_id, position, day))"
    )
    connection.commit()
    connection.close()

    storage = Storage(path)
    await storage.init()
    try:
        cursor = await storage.db.execute("PRAGMA table_info(shifts)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert "overtime" in columns
    finally:
        await storage.close()
