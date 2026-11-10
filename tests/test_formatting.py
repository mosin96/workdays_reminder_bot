"""Тесты форматирования ответов."""

from __future__ import annotations

from datetime import date

from bot.formatting import (
    day_status_text,
    month_caption_text,
    no_schedule_text,
    schedule_summary_text,
    today_report_text,
)
from bot.models import DayInfo, Employee, Schedule, Shift


def _schedule() -> Schedule:
    schedule = Schedule(
        id=1,
        group_id=10,
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
    assert "ночная 12-часовая смена" in text

    text = day_status_text(schedule, employee, date(2026, 11, 2))
    assert "Выходной" in text

    text = day_status_text(schedule, employee, date(2026, 11, 3))
    assert "Отпуск" in text

    # Дня нет в расписании.
    text = day_status_text(schedule, employee, date(2026, 11, 10))
    assert "Нет данных" in text


def test_unknown_day_status() -> None:
    schedule = _schedule()
    schedule.days[4] = DayInfo(
        day=4, weekday=date(2026, 11, 4).weekday(), weekend=False
    )
    schedule.shifts[(0, 4)] = Shift(0, 4, None, "unknown")
    text = day_status_text(
        schedule, schedule.employees[0], date(2026, 11, 4)
    )
    assert "Нет данных за этот день" in text


def test_short_day_and_overtime_status() -> None:
    schedule = _schedule()
    employee = schedule.employees[0]
    schedule.shifts[(0, 2)] = Shift(0, 2, "ДД", "short_day")
    text = day_status_text(schedule, employee, date(2026, 11, 2))
    assert "8-часовая смена" in text
    assert "ДД" in text
    assert "Переработка" not in text

    schedule.shifts[(0, 2)] = Shift(
        0, 2, "ДД+П", "short_day", overtime=True
    )
    text = day_status_text(schedule, employee, date(2026, 11, 2))
    assert "8-часовая смена" in text
    assert "Переработка (+П)" in text


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
    assert "12ч день" in text

    edit_text = month_caption_text(
        schedule, schedule.employees[1], edit=True
    )
    assert "изменить смену" in edit_text


def test_no_schedule_text() -> None:
    text = no_schedule_text(2026, 12, _schedule())
    assert "На декабрь 2026 расписания нет" in text
    assert "/month 2026-11" in text


def test_today_report_day_and_handover() -> None:
    schedule = _schedule()
    employee = schedule.employees[1]  # Петров, 1-е — дневная
    text = today_report_text(
        schedule,
        employee,
        date(2026, 11, 1),
        next_schedule=schedule,
    )
    assert "дневная 12-часовая смена" in text
    assert "В этой же смене сегодня: никого" in text
    assert "Передать смену" in text
    assert "ночная 12-часовая смена" in text
    assert "принимает: Иванов Иван" in text


def test_today_report_night_handover_next_day() -> None:
    schedule = _schedule()
    employee = schedule.employees[0]  # Иванов, 1-е — ночная
    text = today_report_text(
        schedule,
        employee,
        date(2026, 11, 1),
        next_schedule=schedule,
    )
    assert "ночная 12-часовая смена" in text
    assert "2 ноября 2026" in text
    assert "принимает: Петров Пётр" in text


def test_today_report_no_recipients_and_day_off() -> None:
    schedule = _schedule()
    employee = schedule.employees[1]
    text = today_report_text(
        schedule,
        employee,
        date(2026, 11, 2),
        next_schedule=schedule,
    )
    assert "Передать смену" in text
    assert "по расписанию никого" in text

    text = today_report_text(
        schedule,
        employee,
        date(2026, 11, 3),
        next_schedule=schedule,
    )
    assert "Выходной" in text
    assert "этой же смене" not in text
    assert "Передать смену" not in text


def test_today_report_next_month_missing() -> None:
    schedule = _schedule()
    schedule.days[30] = DayInfo(
        day=30, weekday=date(2026, 11, 30).weekday(), weekend=False
    )
    schedule.shifts[(0, 30)] = Shift(0, 30, "Н", "night")
    employee = schedule.employees[0]

    owner_text = today_report_text(
        schedule,
        employee,
        date(2026, 11, 30),
        next_month_missing=True,
        is_owner=True,
    )
    assert "Следующая смена" in owner_text
    assert "1 декабря 2026" in owner_text
    assert "Загрузите фото" in owner_text

    subscriber_text = today_report_text(
        schedule,
        employee,
        date(2026, 11, 30),
        next_month_missing=True,
        is_owner=False,
    )
    assert "Создатель ещё не загрузил" in subscriber_text

    december = Schedule(
        id=2, group_id=1, year=2026, month=12, status="active"
    )
    december.employees = [Employee(0, "Декабрьский Сотрудник")]
    december.days[1] = DayInfo(
        day=1, weekday=date(2026, 12, 1).weekday(), weekend=False
    )
    december.shifts[(0, 1)] = Shift(0, 1, "Д", "day")
    text = today_report_text(
        schedule,
        employee,
        date(2026, 11, 30),
        next_schedule=december,
    )
    assert "принимает: Декабрьский Сотрудник" in text
    assert "Загрузите фото" not in text
