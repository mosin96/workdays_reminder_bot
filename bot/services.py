"""Бизнес-логика: разбор дат, поиск сотрудника, выбор расписания."""

from __future__ import annotations

import re
from datetime import date, timedelta

from .models import Employee, Schedule
from .utils import (
    MONTH_ALIASES,
    MONTHS_GENITIVE,
    MONTHS_NOMINATIVE,
    days_in_month,
    normalize_name,
)


class QueryError(ValueError):
    """Пользователь ввёл дату/месяц в неверном формате."""


MONTH_WORDS: dict[str, int] = {}


def _build_month_words() -> None:
    for index in range(1, 13):
        MONTH_WORDS[MONTHS_NOMINATIVE[index]] = index
        MONTH_WORDS[MONTHS_GENITIVE[index]] = index
    MONTH_WORDS.update(MONTH_ALIASES)


_build_month_words()


def parse_date_query(text: str, today: date | None = None) -> date:
    today = today or date.today()
    value = (text or "").strip().lower().replace("ё", "е")
    if not value:
        raise QueryError(
            "Укажите дату: /date 15, /date 15.12 или /date 2026-12-15."
        )

    if value in {"сегодня", "today"}:
        return today
    if value in {"завтра", "tomorrow"}:
        return today + timedelta(days=1)
    if value in {"вчера", "yesterday"}:
        return today - timedelta(days=1)

    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if match:
        return _make_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    match = re.fullmatch(
        r"(\d{1,2})\s*[./\-]\s*(\d{1,2})(?:\s*[./\-]\s*(\d{2,4}))?", value
    )
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = _normalize_year(match.group(3), today.year)
        return _make_date(year, month, day)

    match = re.fullmatch(r"(\d{1,2})\s+([а-яa-z]+)(?:\s+(\d{4}))?", value)
    if match:
        day = int(match.group(1))
        month_word = match.group(2)
        month = _month_from_word(month_word)
        if month is None:
            raise QueryError("Не понял название месяца.")
        year = int(match.group(3)) if match.group(3) else today.year
        return _make_date(year, month, day)

    match = re.fullmatch(r"(\d{1,2})", value)
    if match:
        return _make_date(today.year, today.month, int(match.group(1)))

    raise QueryError(
        "Не понял дату. Примеры: /date 15, /date 15.12, /date 2026-12-15, "
        "/date 15 декабря."
    )


def parse_month_query(text: str, today: date | None = None) -> tuple[int, int]:
    today = today or date.today()
    value = (text or "").strip().lower().replace("ё", "е")
    if not value:
        return today.year, today.month

    match = re.fullmatch(r"(\d{4})-(\d{1,2})", value)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        _validate_month(month)
        return year, month

    match = re.fullmatch(r"(\d{1,2})", value)
    if match:
        month = int(match.group(1))
        _validate_month(month)
        return today.year, month

    match = re.fullmatch(r"([а-яa-z]+)(?:\s+(\d{4}))?", value)
    if match:
        month = _month_from_word(match.group(1))
        if month is None:
            raise QueryError(
                "Не понял месяц. Примеры: /month 2026-11, /month 11, /month ноябрь."
            )
        year = int(match.group(2)) if match.group(2) else today.year
        return year, month

    raise QueryError(
        "Не понял месяц. Примеры: /month 2026-11, /month 11, /month ноябрь."
    )


def _validate_month(month: int) -> None:
    if not 1 <= month <= 12:
        raise QueryError("Месяц должен быть от 1 до 12.")


def _normalize_year(raw: str | None, current: int) -> int:
    if not raw:
        return current
    year = int(raw)
    if year < 100:
        year += 2000
    return year


def _make_date(year: int, month: int, day: int) -> date:
    _validate_month(month)
    if not 1 <= day <= days_in_month(year, month):
        raise QueryError(f"В {month:02d}.{year} нет дня {day}.")
    return date(year, month, day)


def _month_from_word(word: str) -> int | None:
    word = word.lower().replace("ё", "е")
    if word in MONTH_WORDS:
        return MONTH_WORDS[word]
    for name, month in MONTH_WORDS.items():
        if len(word) >= 3 and name.startswith(word):
            return month
        if len(word) >= 3 and word.startswith(name[:3]):
            return month
    return None


def find_employee(schedule: Schedule, name: str | None) -> Employee | None:
    if not name:
        return None
    target = normalize_name(name)
    return next(
        (emp for emp in schedule.employees if normalize_name(emp.name) == target),
        None,
    )


def month_of(day: date) -> tuple[int, int]:
    return day.year, day.month
