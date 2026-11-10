"""Модели данных приложения."""

from __future__ import annotations

from dataclasses import dataclass, field


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


@dataclass
class DayInfo:
    day: int
    weekday: int
    weekend: bool


@dataclass
class Schedule:
    id: int
    chat_id: int
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
class ChatSettings:
    chat_id: int
    employee_name: str | None = None
    employee_name_norm: str | None = None
    remember: bool = False
