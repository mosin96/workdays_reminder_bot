"""Тесты разбора дат и месяцев."""

from __future__ import annotations

from datetime import date

import pytest

from bot.services import QueryError, parse_date_query, parse_month_query

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
