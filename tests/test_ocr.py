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
from bot.ocr.pipeline import (
    DAY,
    UNKNOWN,
    OcrCell,
    OcrEmployee,
    OcrResult,
    complete_month,
)


def test_complete_month_adds_missing_days() -> None:
    result = OcrResult(
        month=11,
        year=2026,
        employees=[OcrEmployee(position=0, name="Сотрудник")],
        days=[1, 2, 3],
        cells={
            (0, 1): OcrCell(day=1, position=0, code="Д", type=DAY, confidence=90),
            (0, 2): OcrCell(day=2, position=0, code="Н", type="night", confidence=90),
            (0, 3): OcrCell(day=3, position=0, code=None, type="off", confidence=0),
        },
        weekend={1: True, 2: False, 3: False},
        counts={1: (1, 0), 2: (0, 1), 3: (0, 0)},
    )

    missing = complete_month(result, 2026, 11)
    assert missing == list(range(4, 31))
    assert result.days == list(range(1, 31))
    assert result.shift(0, 15).type == UNKNOWN
    assert result.shift(0, 15).code is None
    assert result.weekend[7] is True
    assert result.weekend[8] is True
    assert result.weekend[9] is False
    assert result.totals()["unknown"] == 27

    # Повторный вызов ничего не добавляет.
    assert complete_month(result, 2026, 11) == []
    assert result.days == list(range(1, 31))


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
        "short_day": 0,
        "vacation": 21,
        "off": 87,
        "unknown": 0,
        "overtime": 0,
    }

    # Несколько точечных проверок по позициям сотрудников.
    assert result.shift(0, 1).type == "night"
    assert result.shift(1, 1).type == "day"
    assert result.shift(2, 1).code == "НМ"
    assert result.shift(5, 30).code == "НМ"
    assert result.shift(2, 14).type == "day"
    assert result.shift(4, 14).type == "night"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("д", ("Д", "day", False)),
        ("н", ("Н", "night", False)),
        ("дд", ("ДД", "short_day", False)),
        ("нм", ("НМ", "night", False)),
        ("д+п", ("Д+П", "day", True)),
        ("н+п", ("Н+П", "night", True)),
        ("дд+п", ("ДД+П", "short_day", True)),
        ("нм+п", ("НМ+П", "night", True)),
        ("отпуск", ("О", "vacation", False)),
        ("", (None, "off", False)),
    ],
)
def test_classify_shift_codes(text: str, expected: tuple) -> None:
    from bot.ocr.pipeline import _classify_shift

    code, shift_type, overtime = _classify_shift(text, 90)
    assert (code, shift_type, overtime) == expected


def test_classify_overtime_without_base_is_unknown() -> None:
    from bot.ocr.pipeline import _classify_shift

    code, shift_type, overtime = _classify_shift("+п", 50)
    assert shift_type == "unknown"
    assert overtime is True
    assert code is not None
