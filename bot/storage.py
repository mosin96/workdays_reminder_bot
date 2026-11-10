"""Хранилище общих расписаний, подписок и блокировок в SQLite."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import aiosqlite

from .codes import generate_code
from .models import (
    ChatSubscription,
    DayInfo,
    Employee,
    Schedule,
    ScheduleGroup,
    Shift,
)
from .utils import days_in_month, normalize_name

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schedule_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    owner_chat_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES schedule_groups(id) ON DELETE CASCADE,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedules_group_month
    ON schedules(group_id, year, month);
CREATE INDEX IF NOT EXISTS idx_schedules_group_status
    ON schedules(group_id, status);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_unique
    ON employees(schedule_id, position);

CREATE TABLE IF NOT EXISTS shifts (
    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    day INTEGER NOT NULL,
    code TEXT,
    type TEXT NOT NULL,
    confidence INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (schedule_id, position, day)
);

CREATE TABLE IF NOT EXISTS days (
    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    day INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    is_weekend INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (schedule_id, day)
);

CREATE TABLE IF NOT EXISTS chat_subscriptions (
    chat_id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES schedule_groups(id) ON DELETE CASCADE,
    employee_name TEXT,
    employee_name_norm TEXT,
    remember INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_group
    ON chat_subscriptions(group_id);

CREATE TABLE IF NOT EXISTS chat_blocks (
    chat_id INTEGER PRIMARY KEY,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    blocked_at TEXT,
    updated_at TEXT NOT NULL
);

PRAGMA user_version = 2;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await self._drop_old_schema_if_needed()
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def _drop_old_schema_if_needed(self) -> None:
        """Удаляет базу старого формата (до появления schedule_groups)."""

        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        tables: set[str] = set()
        corrupt = False
        try:
            async with aiosqlite.connect(self.path) as probe:
                cursor = await probe.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
                tables = {str(row[0]) for row in await cursor.fetchall()}
        except sqlite3.DatabaseError:
            corrupt = True
        except OSError:
            corrupt = True

        if corrupt:
            log.warning(
                "Обнаружена повреждённая база %s — удаляю и создаю новую.",
                self.path,
            )
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(self.path) + suffix)
                if candidate.exists():
                    candidate.unlink()
            return

        if not tables or "schedule_groups" in tables:
            return
        log.warning(
            "Обнаружена старая база %s — удаляю и создаю новую.",
            self.path,
        )
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                candidate.unlink()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage не инициализирован: вызовите init()")
        return self._db

    # ------------------------------------------------------------------
    # Группы расписаний и коды
    # ------------------------------------------------------------------

    async def create_group(self, owner_chat_id: int) -> ScheduleGroup:
        async with self._lock:
            now = _now()
            for _ in range(20):
                code = generate_code()
                try:
                    cursor = await self.db.execute(
                        "INSERT INTO schedule_groups (code, owner_chat_id, created_at) "
                        "VALUES (?, ?, ?)",
                        (code, owner_chat_id, now),
                    )
                except sqlite3.IntegrityError:
                    continue
                await self.db.commit()
                return ScheduleGroup(
                    id=int(cursor.lastrowid),
                    code=code,
                    owner_chat_id=owner_chat_id,
                    created_at=now,
                )
        raise RuntimeError("Не удалось сгенерировать уникальный код расписания")

    async def get_group(self, group_id: int) -> ScheduleGroup | None:
        cursor = await self.db.execute(
            "SELECT * FROM schedule_groups WHERE id = ?", (group_id,)
        )
        row = await cursor.fetchone()
        return self._group_from_row(row) if row else None

    async def get_group_by_code(self, code: str) -> ScheduleGroup | None:
        cursor = await self.db.execute(
            "SELECT * FROM schedule_groups WHERE code = ?", (code,)
        )
        row = await cursor.fetchone()
        return self._group_from_row(row) if row else None

    @staticmethod
    def _group_from_row(row: aiosqlite.Row) -> ScheduleGroup:
        return ScheduleGroup(
            id=int(row["id"]),
            code=str(row["code"]),
            owner_chat_id=int(row["owner_chat_id"]),
            created_at=str(row["created_at"]),
        )

    async def count_subscribers(self, group_id: int) -> int:
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM chat_subscriptions WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def delete_group(self, group_id: int) -> bool:
        async with self._lock:
            cursor = await self.db.execute(
                "DELETE FROM schedule_groups WHERE id = ?", (group_id,)
            )
            await self.db.commit()
            return bool(cursor.rowcount)

    # ------------------------------------------------------------------
    # Подписки чата
    # ------------------------------------------------------------------

    async def get_subscription(
        self, chat_id: int
    ) -> ChatSubscription | None:
        cursor = await self.db.execute(
            "SELECT * FROM chat_subscriptions WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ChatSubscription(
            chat_id=int(row["chat_id"]),
            group_id=int(row["group_id"]),
            employee_name=row["employee_name"],
            employee_name_norm=row["employee_name_norm"],
            remember=bool(row["remember"]),
        )

    async def subscribe(
        self,
        chat_id: int,
        group_id: int,
        employee_name: str | None = None,
        remember: bool = False,
    ) -> None:
        async with self._lock:
            await self.db.execute(
                "INSERT INTO chat_subscriptions "
                "(chat_id, group_id, employee_name, employee_name_norm, remember, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "group_id = excluded.group_id, "
                "employee_name = excluded.employee_name, "
                "employee_name_norm = excluded.employee_name_norm, "
                "remember = excluded.remember, "
                "updated_at = excluded.updated_at",
                (
                    chat_id,
                    group_id,
                    employee_name,
                    normalize_name(employee_name) if employee_name else None,
                    int(remember),
                    _now(),
                ),
            )
            await self.db.commit()

    async def unsubscribe(self, chat_id: int) -> bool:
        async with self._lock:
            cursor = await self.db.execute(
                "DELETE FROM chat_subscriptions WHERE chat_id = ?", (chat_id,)
            )
            await self.db.commit()
            return bool(cursor.rowcount)

    async def set_employee(
        self, chat_id: int, name: str, remember: bool | None = None
    ) -> None:
        current = await self.get_subscription(chat_id)
        if current is None:
            raise RuntimeError("Чат не подписан на расписание")
        if remember is None:
            remember = current.remember
        async with self._lock:
            await self.db.execute(
                "UPDATE chat_subscriptions SET employee_name = ?, "
                "employee_name_norm = ?, remember = ?, updated_at = ? "
                "WHERE chat_id = ?",
                (name, normalize_name(name), int(remember), _now(), chat_id),
            )
            await self.db.commit()

    async def set_remember(self, chat_id: int, remember: bool) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE chat_subscriptions SET remember = ?, updated_at = ? "
                "WHERE chat_id = ?",
                (int(remember), _now(), chat_id),
            )
            await self.db.commit()

    async def clear_employee(self, chat_id: int) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE chat_subscriptions SET employee_name = NULL, "
                "employee_name_norm = NULL, remember = 0, updated_at = ? "
                "WHERE chat_id = ?",
                (_now(), chat_id),
            )
            await self.db.commit()

    # ------------------------------------------------------------------
    # Блокировка чата
    # ------------------------------------------------------------------

    async def is_chat_blocked(self, chat_id: int) -> bool:
        cursor = await self.db.execute(
            "SELECT blocked FROM chat_blocks WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        return bool(row and row["blocked"])

    async def register_failed_code(
        self, chat_id: int, limit: int = 10
    ) -> tuple[int, bool, bool]:
        async with self._lock:
            now = _now()
            cursor = await self.db.execute(
                "SELECT failed_attempts, blocked FROM chat_blocks WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cursor.fetchone()
            if row and row["blocked"]:
                return int(row["failed_attempts"]), True, False
            failed = (int(row["failed_attempts"]) if row else 0) + 1
            blocked = failed > limit
            await self.db.execute(
                "INSERT INTO chat_blocks "
                "(chat_id, failed_attempts, blocked, blocked_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "failed_attempts = excluded.failed_attempts, "
                "blocked = excluded.blocked, "
                "blocked_at = excluded.blocked_at, "
                "updated_at = excluded.updated_at",
                (
                    chat_id,
                    failed,
                    int(blocked),
                    now if blocked else None,
                    now,
                ),
            )
            await self.db.commit()
            return failed, blocked, blocked

    async def reset_code_attempts(self, chat_id: int) -> None:
        async with self._lock:
            await self.db.execute(
                "INSERT INTO chat_blocks (chat_id, failed_attempts, blocked, updated_at) "
                "VALUES (?, 0, 0, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "failed_attempts = 0, blocked = 0, updated_at = excluded.updated_at",
                (chat_id, _now()),
            )
            await self.db.commit()

    async def unblock_chat(self, chat_id: int) -> bool:
        async with self._lock:
            cursor = await self.db.execute(
                "UPDATE chat_blocks SET blocked = 0, failed_attempts = 0, "
                "blocked_at = NULL, updated_at = ? WHERE chat_id = ?",
                (_now(), chat_id),
            )
            await self.db.commit()
            return bool(cursor.rowcount)

    # ------------------------------------------------------------------
    # Расписания
    # ------------------------------------------------------------------

    async def save_draft(
        self,
        group_id: int,
        year: int,
        month: int,
        employee_names: list[str],
        days: list[int],
        weekend: dict[int, bool],
        cells: dict[tuple[int, int], tuple[str | None, str, int]],
        source: str | None = None,
    ) -> int:
        async with self._lock:
            now = _now()
            cursor = await self.db.execute(
                "INSERT INTO schedules (group_id, year, month, status, source, "
                "created_at, updated_at) VALUES (?, ?, ?, 'draft', ?, ?, ?)",
                (group_id, year, month, source, now, now),
            )
            schedule_id = int(cursor.lastrowid)

            for position, name in enumerate(employee_names):
                await self.db.execute(
                    "INSERT INTO employees (schedule_id, position, name, name_norm) "
                    "VALUES (?, ?, ?, ?)",
                    (schedule_id, position, name, normalize_name(name)),
                )

            for day in days:
                weekday = 0
                try:
                    weekday = date(year, month, day).weekday()
                except ValueError:
                    pass
                await self.db.execute(
                    "INSERT INTO days (schedule_id, day, weekday, is_weekend) "
                    "VALUES (?, ?, ?, ?)",
                    (schedule_id, day, weekday, int(weekend.get(day, False))),
                )

            for (position, day), (code, shift_type, confidence) in cells.items():
                await self.db.execute(
                    "INSERT OR REPLACE INTO shifts "
                    "(schedule_id, position, day, code, type, confidence) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (schedule_id, position, day, code, shift_type, confidence),
                )

            await self.db.commit()
            return schedule_id

    async def get_schedule(self, schedule_id: int) -> Schedule | None:
        cursor = await self.db.execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
        )
        row = await cursor.fetchone()
        return await self._build_schedule(row) if row else None

    async def get_schedule_for_chat(
        self, schedule_id: int, chat_id: int
    ) -> Schedule | None:
        schedule = await self.get_schedule(schedule_id)
        if schedule is None:
            return None
        subscription = await self.get_subscription(chat_id)
        if subscription is None or subscription.group_id != schedule.group_id:
            return None
        return schedule

    async def _build_schedule(self, row: aiosqlite.Row) -> Schedule:
        schedule = Schedule(
            id=int(row["id"]),
            group_id=int(row["group_id"]),
            year=int(row["year"]),
            month=int(row["month"]),
            status=str(row["status"]),
        )

        cursor = await self.db.execute(
            "SELECT position, name FROM employees WHERE schedule_id = ? "
            "ORDER BY position",
            (schedule.id,),
        )
        for employee in await cursor.fetchall():
            schedule.employees.append(
                Employee(
                    position=int(employee["position"]),
                    name=str(employee["name"]),
                )
            )

        cursor = await self.db.execute(
            "SELECT day, weekday, is_weekend FROM days WHERE schedule_id = ?",
            (schedule.id,),
        )
        for day in await cursor.fetchall():
            schedule.days[int(day["day"])] = DayInfo(
                day=int(day["day"]),
                weekday=int(day["weekday"]),
                weekend=bool(day["is_weekend"]),
            )

        cursor = await self.db.execute(
            "SELECT position, day, code, type FROM shifts WHERE schedule_id = ?",
            (schedule.id,),
        )
        for shift in await cursor.fetchall():
            position = int(shift["position"])
            day = int(shift["day"])
            schedule.shifts[(position, day)] = Shift(
                position=position,
                day=day,
                code=shift["code"],
                type=str(shift["type"]),
            )
        return schedule

    async def get_active(
        self, group_id: int, year: int, month: int
    ) -> Schedule | None:
        cursor = await self.db.execute(
            "SELECT * FROM schedules WHERE group_id = ? AND year = ? AND month = ? "
            "AND status = 'active' ORDER BY id DESC LIMIT 1",
            (group_id, year, month),
        )
        row = await cursor.fetchone()
        return await self._build_schedule(row) if row else None

    async def get_latest(
        self, group_id: int, status: str = "active"
    ) -> Schedule | None:
        cursor = await self.db.execute(
            "SELECT * FROM schedules WHERE group_id = ? AND status = ? "
            "ORDER BY year DESC, month DESC, id DESC LIMIT 1",
            (group_id, status),
        )
        row = await cursor.fetchone()
        return await self._build_schedule(row) if row else None

    async def list_schedules(
        self, group_id: int, status: str = "active"
    ) -> list[Schedule]:
        cursor = await self.db.execute(
            "SELECT * FROM schedules WHERE group_id = ? AND status = ? "
            "ORDER BY year DESC, month DESC",
            (group_id, status),
        )
        rows = await cursor.fetchall()
        return [await self._build_schedule(row) for row in rows]

    async def has_schedule(self, group_id: int, year: int, month: int) -> bool:
        cursor = await self.db.execute(
            "SELECT 1 FROM schedules WHERE group_id = ? AND year = ? AND month = ? "
            "AND status = 'active' LIMIT 1",
            (group_id, year, month),
        )
        return await cursor.fetchone() is not None

    async def activate(self, schedule_id: int, group_id: int) -> bool:
        async with self._lock:
            schedule = await self.get_schedule(schedule_id)
            if schedule is None or schedule.group_id != group_id:
                return False
            await self.db.execute(
                "DELETE FROM schedules WHERE group_id = ? AND year = ? AND month = ? "
                "AND id != ?",
                (group_id, schedule.year, schedule.month, schedule_id),
            )
            await self.db.execute(
                "UPDATE schedules SET status = 'active', updated_at = ? WHERE id = ?",
                (_now(), schedule_id),
            )
            await self.db.commit()
            return True

    async def update_month(
        self, schedule_id: int, group_id: int, year: int, month: int
    ) -> bool:
        async with self._lock:
            schedule = await self.get_schedule(schedule_id)
            if schedule is None or schedule.group_id != group_id:
                return False
            await self.db.execute(
                "UPDATE schedules SET year = ?, month = ?, updated_at = ? WHERE id = ?",
                (year, month, _now(), schedule_id),
            )
            cursor = await self.db.execute(
                "SELECT day FROM days WHERE schedule_id = ?", (schedule_id,)
            )
            existing_days: set[int] = set()
            for row in await cursor.fetchall():
                day = int(row["day"])
                existing_days.add(day)
                weekday = 0
                try:
                    weekday = date(year, month, day).weekday()
                except ValueError:
                    pass
                await self.db.execute(
                    "UPDATE days SET weekday = ? WHERE schedule_id = ? AND day = ?",
                    (weekday, schedule_id, day),
                )

            cursor = await self.db.execute(
                "SELECT position FROM employees WHERE schedule_id = ?",
                (schedule_id,),
            )
            positions = [int(row["position"]) for row in await cursor.fetchall()]
            limit = days_in_month(year, month)
            for day in range(1, limit + 1):
                if day not in existing_days:
                    weekday = date(year, month, day).weekday()
                    await self.db.execute(
                        "INSERT INTO days (schedule_id, day, weekday, is_weekend) "
                        "VALUES (?, ?, ?, ?)",
                        (schedule_id, day, weekday, int(weekday >= 5)),
                    )
                    for position in positions:
                        await self.db.execute(
                            "INSERT OR IGNORE INTO shifts "
                            "(schedule_id, position, day, code, type, confidence) "
                            "VALUES (?, ?, ?, NULL, 'unknown', 0)",
                            (schedule_id, position, day),
                        )
            await self.db.commit()
            return True

    async def rename_employee(
        self,
        schedule_id: int,
        group_id: int,
        position: int,
        name: str,
    ) -> bool:
        async with self._lock:
            schedule = await self.get_schedule(schedule_id)
            if schedule is None or schedule.group_id != group_id:
                return False
            await self.db.execute(
                "UPDATE employees SET name = ?, name_norm = ? "
                "WHERE schedule_id = ? AND position = ?",
                (name, normalize_name(name), schedule_id, position),
            )
            await self.db.commit()
            return True

    async def set_shift(
        self,
        schedule_id: int,
        group_id: int,
        position: int,
        day: int,
        code: str | None,
        shift_type: str,
    ) -> bool:
        async with self._lock:
            schedule = await self.get_schedule(schedule_id)
            if schedule is None or schedule.group_id != group_id:
                return False
            if position not in {emp.position for emp in schedule.employees}:
                return False
            if day not in schedule.days:
                return False
            await self.db.execute(
                "INSERT OR REPLACE INTO shifts "
                "(schedule_id, position, day, code, type, confidence) "
                "VALUES (?, ?, ?, ?, ?, 100)",
                (schedule_id, position, day, code, shift_type),
            )
            await self.db.commit()
            return True

    async def delete_schedule(self, schedule_id: int, group_id: int) -> bool:
        async with self._lock:
            cursor = await self.db.execute(
                "DELETE FROM schedules WHERE id = ? AND group_id = ?",
                (schedule_id, group_id),
            )
            await self.db.commit()
            return bool(cursor.rowcount)

    async def delete_month(
        self, group_id: int, year: int, month: int
    ) -> int:
        async with self._lock:
            cursor = await self.db.execute(
                "DELETE FROM schedules WHERE group_id = ? AND year = ? AND month = ?",
                (group_id, year, month),
            )
            await self.db.commit()
            return int(cursor.rowcount)
