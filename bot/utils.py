"""Общие утилиты: имена, даты, русские названия месяцев."""

from __future__ import annotations

import re
from datetime import date

MONTHS_NOMINATIVE = [
    "",
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
]

MONTHS_GENITIVE = [
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]

WEEKDAYS_NOMINATIVE = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]

WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

MONTH_ALIASES = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "май": 5,
    "мая": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "сент": 9,
    "окт": 10,
    "ноя": 11,
    "нояб": 11,
    "дек": 12,
}

_SHIFT_EMOJI = {
    "day": "🟩",
    "night": "🟦",
    "vacation": "🟨",
    "off": "⬜",
    "unknown": "❔",
}

_SHIFT_TITLES = {
    "day": "дневная смена",
    "night": "ночная смена",
    "vacation": "отпуск",
    "off": "выходной",
    "unknown": "не распознано",
}


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    text = name.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s\-]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text, flags=re.UNICODE).strip()


def month_name(month: int, genitive: bool = False) -> str:
    names = MONTHS_GENITIVE if genitive else MONTHS_NOMINATIVE
    if 1 <= month <= 12:
        return names[month]
    return ""


def month_title(month: int) -> str:
    name = month_name(month)
    return name.capitalize() if name else ""


def weekday_name(day: date) -> str:
    return WEEKDAYS_NOMINATIVE[day.weekday()]


def weekday_short(weekday: int) -> str:
    return WEEKDAYS_SHORT[weekday % 7]


def format_date_ru(day: date) -> str:
    return f"{day.day} {month_name(day.month, genitive=True)} {day.year}"


def format_month_ru(year: int, month: int) -> str:
    return f"{month_title(month)} {year}"


def shift_emoji(shift_type: str) -> str:
    return _SHIFT_EMOJI.get(shift_type, "❔")


def shift_title(shift_type: str) -> str:
    return _SHIFT_TITLES.get(shift_type, "неизвестно")


def shift_badge(shift_type: str, code: str | None = None) -> str:
    emoji = shift_emoji(shift_type)
    if shift_type in {"day", "night"} and code:
        return f"{emoji} {code}"
    return emoji


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    from datetime import date as date_cls

    return (date_cls(year, month + 1, 1) - date_cls(year, month, 1)).days
