"""Интеграционный тест OCR на локальном примере таблицы.

Файл ``img.png`` не хранится в репозитории: он может содержать персональные
данные. Если файла нет, тест пропускается.
"""

from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest

from conftest import requires_tesseract

from bot.ocr import OcrCancelled, parse_schedule_image


def test_cancel_before_ocr_start(sample_image: Path) -> None:
    event = Event()
    event.set()
    with pytest.raises(OcrCancelled):
        parse_schedule_image(sample_image, cancel_event=event)


@requires_tesseract
def test_parse_example_image(sample_image: Path) -> None:
    result = parse_schedule_image(sample_image)

    assert result.month == 11
    assert result.year is None
    assert len(result.employees) == 6
    assert all(employee.name for employee in result.employees)
    assert len({employee.name for employee in result.employees}) == 6
    assert result.days == list(range(1, 31))
    assert result.issues == []

    weekend = [day for day in result.days if result.weekend[day]]
    assert weekend == [1, 4, 7, 8, 14, 15, 21, 22, 28, 29]

    # Отпуска: 5-й сотрудник 2-15, 1-й сотрудник 16-22.
    assert [
        day
        for day in result.days
        if result.shift(5, day).type == "vacation"
    ] == list(range(2, 16))
    assert [
        day
        for day in result.days
        if result.shift(0, day).type == "vacation"
    ] == list(range(16, 23))

    # Контрольные суммы совпадают с тем, что напечатано в таблице.
    for day in result.days:
        stated = result.counts[day]
        calculated = (
            result.counts_for_day(day, "day"),
            result.counts_for_day(day, "night"),
        )
        assert stated == calculated, f"день {day}: {stated} != {calculated}"

    assert result.totals() == {
        "day": 41,
        "night": 31,
        "vacation": 21,
        "off": 87,
        "unknown": 0,
    }

    # Несколько точечных проверок по позициям сотрудников.
    assert result.shift(0, 1).type == "night"
    assert result.shift(1, 1).type == "day"
    assert result.shift(2, 1).code == "НМ"
    assert result.shift(5, 30).code == "НМ"
    assert result.shift(2, 14).type == "day"
    assert result.shift(4, 14).type == "night"
