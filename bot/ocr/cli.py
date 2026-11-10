"""CLI для проверки OCR без Telegram.

Пример:
    python -m bot.ocr.cli img.png --pretty
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .pipeline import parse_schedule_image


def main() -> int:
    parser = argparse.ArgumentParser(description="Распознать таблицу смен с картинки")
    parser.add_argument("image", help="путь к PNG/JPG")
    parser.add_argument(
        "--pretty", action="store_true", help="печатать JSON с отступами"
    )
    args = parser.parse_args()

    try:
        result = parse_schedule_image(
            args.image, os.getenv("TESSERACT_CMD") or None
        )
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    employees = [
        {"position": employee.position, "name": employee.name}
        for employee in result.employees
    ]
    days = {
        str(day): {
            "weekend": result.weekend.get(day, False),
            "counts": result.counts.get(day),
            "shifts": {
                employee.name or f"#{employee.position + 1}": {
                    "code": result.shift(employee.position, day).code,
                    "type": result.shift(employee.position, day).type,
                }
                for employee in result.employees
            },
        }
        for day in result.days
    }
    payload = {
        "month": result.month,
        "year": result.year,
        "employees": employees,
        "totals": result.totals(),
        "issues": result.issues,
        "warnings": result.warnings,
        "days": days,
    }
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
