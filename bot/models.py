"""Модели данных приложения."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Employee:
    position: int
    name: str


@dataclass
class Shift:
    position: int
    day: int
    code: str | None
    type: str
    overtime: bool = False


@dataclass
class DayInfo:
    day: int
    weekday: int
    weekend: bool


@dataclass
class ScheduleGroup:
    id: int
    code: str
    owner_chat_id: int
    created_at: str


@dataclass
class ChatSubscription:
    chat_id: int
    group_id: int
    employee_name: str | None = None
    employee_name_norm: str | None = None
    remember: bool = False


@dataclass
class Schedule:
    id: int
    group_id: int
    year: int
    month: int
    status: str
    employees: list[Employee] = field(default_factory=list)
    shifts: dict[tuple[int, int], Shift] = field(default_factory=dict)
    days: dict[int, DayInfo] = field(default_factory=dict)

    def employee(self, position: int) -> Employee | None:
        return next(
            (item for item in self.employees if item.position == position), None
        )

    def shift(self, position: int, day: int) -> Shift | None:
        return self.shifts.get((position, day))


@dataclass
class ReminderSettings:
    chat_id: int
    group_id: int
    mode: str
    time: str
    shift_types: tuple[str, ...]
    enabled: bool = False


@dataclass
class ReminderDelivery:
    id: int
    chat_id: int
    group_id: int
    employee_position: int
    shift_date: date
    shift_type: str
    mode: str
    due_at: datetime
    last_sent_at: datetime | None = None
    sent_count: int = 0
    acked_at: datetime | None = None
