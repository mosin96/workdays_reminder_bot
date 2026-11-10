"""Тесты форматирования ответов."""

from __future__ import annotations

from datetime import date

from bot.formatting import (
    day_status_text,
    month_caption_text,
    no_schedule_text,
    schedule_summary_text,
)
from bot.models import DayInfo, Employee, Schedule, Shift


def _schedule() -> Schedule:
    schedule = Schedule(
        id=1,
        chat_id=10,
        year=2026,
        month=11,
        status="draft",
        employees=[
            Employee(position=0, name="Иванов Иван"),
            Employee(position=1, name="Петров Пётр"),
        ],
    )
    for day in range(1, 4):
        schedule.days[day] = DayInfo(
            day=day, weekday=date(2026, 11, day).weekday(), weekend=day == 1
        )
    schedule.shifts[(0, 1)] = Shift(0, 1, "Н", "night")
    schedule.shifts[(1, 1)] = Shift(1, 1, "Д", "day")
    schedule.shifts[(0, 2)] = Shift(0, 2, None, "off")
    schedule.shifts[(1, 2)] = Shift(1, 2, "Д", "day")
    schedule.shifts[(0, 3)] = Shift(0, 3, "О", "vacation")
    schedule.shifts[(1, 3)] = Shift(1, 3, None, "off")
    return schedule


def test_day_status_text() -> None:
    schedule = _schedule()
    employee = schedule.employees[0]
    text = day_status_text(schedule, employee, date(2026, 11, 1))
    assert "1 ноября 2026" in text
    assert "Иванов Иван" in text
    assert "ночная смена" in text

    text = day_status_text(schedule, employee, date(2026, 11, 2))
    assert "Выходной" in text

    text = day_status_text(schedule, employee, date(2026, 11, 3))
    assert "Отпуск" in text

    # Дня нет в расписании.
    text = day_status_text(schedule, employee, date(2026, 11, 10))
    assert "Нет данных" in text


def test_summary_text() -> None:
    text = schedule_summary_text(_schedule())
    assert "Иванов Иван" in text
    assert "Петров Пётр" in text
    assert "🟩 дневных: 2" in text
    assert "🟦 ночных: 1" in text
    assert "🟨 отпуск: 1" in text
    assert "Если всё верно" in text


def test_month_caption_text() -> None:
    schedule = _schedule()
    text = month_caption_text(schedule, schedule.employees[1])
    assert "Ноябрь 2026" in text
    assert "Петров Пётр" in text
    assert "Нажмите день" in text
    assert "🟩 день" in text

    edit_text = month_caption_text(
        schedule, schedule.employees[1], edit=True
    )
    assert "изменить смену" in edit_text


def test_no_schedule_text() -> None:
    text = no_schedule_text(2026, 12, _schedule())
    assert "На декабрь 2026 расписания нет" in text
    assert "/month 2026-11" in text
