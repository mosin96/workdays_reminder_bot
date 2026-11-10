"""Хранилище расписаний в SQLite (по одному набору на чат)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

import aiosqlite

from .models import ChatSettings, DayInfo, Employee, Schedule, Shift
from .utils import normalize_name

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedules_chat_month
    ON schedules(chat_id, year, month);
CREATE INDEX IF NOT EXISTS idx_schedules_status
    ON schedules(chat_id, status);

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

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    employee_name TEXT,
    employee_name_norm TEXT,
    remember INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
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
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

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
    # Расписания
    # ------------------------------------------------------------------

    async def save_draft(
        self,
        chat_id: int,
        year: int,
        month: int,
        employee_names: list[str],
        days: list[int],
        weekend: dict[int, bool],
        cells: dict[tuple[int, int], tuple[str | None, str, int]],
        source: str | None = None,
    ) -> int:
        """Сохраняет распознанное расписание как черновик и возвращает id."""

        async with self._lock:
            now = _now()
            cursor = await self.db.execute(
                "INSERT INTO schedules (chat_id, year, month, status, source, "
                "created_at, updated_at) VALUES (?, ?, ?, 'draft', ?, ?, ?)",
                (chat_id, year, month, source, now, now),
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

    async def get_schedule(
        self, schedule_id: int, chat_id: int | None = None
    ) -> Schedule | None:
        cursor = await self.db.execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if chat_id is not None and int(row["chat_id"]) != chat_id:
            return None
        return await self._build_schedule(row)

    async def _build_schedule(self, row: aiosqlite.Row) -> Schedule:
        schedule = Schedule(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
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
        self, chat_id: int, year: int, month: int
    ) -> Schedule | None:
        cursor = await self.db.execute(
            "SELECT * FROM schedules WHERE chat_id = ? AND year = ? AND month = ? "
            "AND status = 'active' ORDER BY id DESC LIMIT 1",
            (chat_id, year, month),
        )
        row = await cursor.fetchone()
        return await self._build_schedule(row) if row else None

    async def get_latest(
        self, chat_id: int, status: str = "active"
    ) -> Schedule | None:
        cursor = await self.db.execute(
            "SELECT * FROM schedules WHERE chat_id = ? AND status = ? "
            "ORDER BY year DESC, month DESC, id DESC LIMIT 1",
            (chat_id, status),
        )
        row = await cursor.fetchone()
        return await self._build_schedule(row) if row else None

    async def list_schedules(
        self, chat_id: int, status: str = "active"
    ) -> list[Schedule]:
        cursor = await self.db.execute(
            "SELECT * FROM schedules WHERE chat_id = ? AND status = ? "
            "ORDER BY year DESC, month DESC",
            (chat_id, status),
        )
        rows = await cursor.fetchall()
        return [await self._build_schedule(row) for row in rows]

    async def has_schedule(self, chat_id: int, year: int, month: int) -> bool:
        cursor = await self.db.execute(
            "SELECT 1 FROM schedules WHERE chat_id = ? AND year = ? AND month = ? "
            "AND status = 'active' LIMIT 1",
            (chat_id, year, month),
        )
        return await cursor.fetchone() is not None

    async def activate(self, schedule_id: int, chat_id: int) -> bool:
        async with self._lock:
            schedule = await self.get_schedule(schedule_id, chat_id)
            if schedule is None:
                return False
            await self.db.execute(
                "DELETE FROM schedules WHERE chat_id = ? AND year = ? AND month = ? "
                "AND id != ?",
                (chat_id, schedule.year, schedule.month, schedule_id),
            )
            await self.db.execute(
                "UPDATE schedules SET status = 'active', updated_at = ? WHERE id = ?",
                (_now(), schedule_id),
            )
            await self.db.commit()
            return True

    async def update_month(
        self, schedule_id: int, chat_id: int, year: int, month: int
    ) -> bool:
        async with self._lock:
            schedule = await self.get_schedule(schedule_id, chat_id)
            if schedule is None:
                return False
            await self.db.execute(
                "UPDATE schedules SET year = ?, month = ?, updated_at = ? WHERE id = ?",
                (year, month, _now(), schedule_id),
            )
            cursor = await self.db.execute(
                "SELECT day FROM days WHERE schedule_id = ?", (schedule_id,)
            )
            for row in await cursor.fetchall():
                day = int(row["day"])
                weekday = 0
                try:
                    weekday = date(year, month, day).weekday()
                except ValueError:
                    pass
                await self.db.execute(
                    "UPDATE days SET weekday = ? WHERE schedule_id = ? AND day = ?",
                    (weekday, schedule_id, day),
                )
            await self.db.commit()
            return True

    async def rename_employee(
        self, schedule_id: int, chat_id: int, position: int, name: str
    ) -> bool:
        async with self._lock:
            schedule = await self.get_schedule(schedule_id, chat_id)
            if schedule is None:
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
        chat_id: int,
        position: int,
        day: int,
        code: str | None,
        shift_type: str,
    ) -> bool:
        async with self._lock:
            schedule = await self.get_schedule(schedule_id, chat_id)
            if schedule is None:
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

    async def delete_schedule(self, schedule_id: int, chat_id: int) -> bool:
        async with self._lock:
            cursor = await self.db.execute(
                "DELETE FROM schedules WHERE id = ? AND chat_id = ?",
                (schedule_id, chat_id),
            )
            await self.db.commit()
            return bool(cursor.rowcount)

    async def delete_month(self, chat_id: int, year: int, month: int) -> int:
        async with self._lock:
            cursor = await self.db.execute(
                "DELETE FROM schedules WHERE chat_id = ? AND year = ? AND month = ?",
                (chat_id, year, month),
            )
            await self.db.commit()
            return int(cursor.rowcount)

    # ------------------------------------------------------------------
    # Настройки чата
    # ------------------------------------------------------------------

    async def get_settings(self, chat_id: int) -> ChatSettings | None:
        cursor = await self.db.execute(
            "SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ChatSettings(
            chat_id=int(row["chat_id"]),
            employee_name=row["employee_name"],
            employee_name_norm=row["employee_name_norm"],
            remember=bool(row["remember"]),
        )

    async def set_employee(
        self, chat_id: int, name: str, remember: bool | None = None
    ) -> None:
        current = await self.get_settings(chat_id)
        if remember is None:
            remember = current.remember if current else False
        async with self._lock:
            await self.db.execute(
                "INSERT INTO chat_settings "
                "(chat_id, employee_name, employee_name_norm, remember, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "employee_name = excluded.employee_name, "
                "employee_name_norm = excluded.employee_name_norm, "
                "remember = excluded.remember, updated_at = excluded.updated_at",
                (chat_id, name, normalize_name(name), int(remember), _now()),
            )
            await self.db.commit()

    async def set_remember(self, chat_id: int, remember: bool) -> None:
        async with self._lock:
            await self.db.execute(
                "INSERT INTO chat_settings (chat_id, remember, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "remember = excluded.remember, updated_at = excluded.updated_at",
                (chat_id, int(remember), _now()),
            )
            await self.db.commit()

    async def clear_employee(self, chat_id: int) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE chat_settings SET employee_name = NULL, "
                "employee_name_norm = NULL, remember = 0, updated_at = ? "
                "WHERE chat_id = ?",
                (_now(), chat_id),
            )
            await self.db.commit()
