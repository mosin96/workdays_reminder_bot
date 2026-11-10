"""Тесты разбора дат и месяцев."""

from __future__ import annotations

from datetime import date

import pytest

from bot.services import (
    QueryError,
    employees_on_shift,
    next_shift,
    parse_date_query,
    parse_month_query,
)
from bot.models import DayInfo, Employee, Schedule, Shift

TODAY = date(2026, 11, 10)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15", date(2026, 11, 15)),
        ("15.12", date(2026, 12, 15)),
        ("15.12.2027", date(2027, 12, 15)),
        ("15/12/2027", date(2027, 12, 15)),
        ("2026-12-15", date(2026, 12, 15)),
        ("15 декабря", date(2026, 12, 15)),
        ("15 декабря 2027", date(2027, 12, 15)),
        ("сегодня", date(2026, 11, 10)),
        ("завтра", date(2026, 11, 11)),
        ("вчера", date(2026, 11, 9)),
    ],
)
def test_parse_date(text: str, expected: date) -> None:
    assert parse_date_query(text, TODAY) == expected


@pytest.mark.parametrize("text", ["", "32", "31.02", "не дата", "2026-13-01"])
def test_parse_date_errors(text: str) -> None:
    with pytest.raises(QueryError):
        parse_date_query(text, TODAY)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", (2026, 11)),
        ("2026-12", (2026, 12)),
        ("12", (2026, 12)),
        ("ноябрь", (2026, 11)),
        ("ноябрь 2025", (2025, 11)),
        ("дек 2027", (2027, 12)),
    ],
)
def test_parse_month(text: str, expected: tuple[int, int]) -> None:
    assert parse_month_query(text, TODAY) == expected


@pytest.mark.parametrize("text", ["13", "2026-13", "непонятно"])
def test_parse_month_errors(text: str) -> None:
    with pytest.raises(QueryError):
        parse_month_query(text, TODAY)


def _shift_schedule() -> Schedule:
    schedule = Schedule(
        id=1, group_id=1, year=2026, month=11, status="active"
    )
    schedule.employees = [
        Employee(0, "Первый"),
        Employee(1, "Второй"),
        Employee(2, "Третий"),
    ]
    for day in (1, 2):
        schedule.days[day] = DayInfo(
            day=day, weekday=date(2026, 11, day).weekday(), weekend=False
        )
    schedule.shifts[(0, 1)] = Shift(0, 1, "Д", "day")
    schedule.shifts[(1, 1)] = Shift(1, 1, "Д", "day")
    schedule.shifts[(2, 1)] = Shift(2, 1, "Н", "night")
    schedule.shifts[(0, 2)] = Shift(0, 2, "Н", "night")
    schedule.shifts[(2, 2)] = Shift(2, 2, "Д", "day")
    return schedule


def test_employees_on_shift() -> None:
    schedule = _shift_schedule()
    day = date(2026, 11, 1)
    assert [
        employee.name for employee in employees_on_shift(schedule, day, "day")
    ] == ["Первый", "Второй"]
    assert [
        employee.name
        for employee in employees_on_shift(
            schedule, day, "day", exclude_position=0
        )
    ] == ["Второй"]
    assert employees_on_shift(schedule, day, "vacation") == []


def test_next_shift_sequence() -> None:
    day = date(2026, 11, 30)
    assert next_shift(day, "day") == (day, "night")
    assert next_shift(day, "night") == (date(2026, 12, 1), "day")
    assert next_shift(day, "off") is None
    assert next_shift(day, "vacation") is None
