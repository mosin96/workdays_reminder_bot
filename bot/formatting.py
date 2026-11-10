"""Формирование текстов для Telegram."""

from __future__ import annotations

import html
from datetime import date

from .models import Employee, Schedule, Shift
from .ocr.pipeline import OcrResult
from .services import employees_on_shift, next_shift
from .utils import (
    format_date_ru,
    format_month_ru,
    month_name,
    shift_emoji,
    shift_title,
    weekday_name,
)

LEGEND = "🟩 день · 🟦 ночь · 🟨 отпуск · ⬜ выходной"


def _shift_description(shift: Shift | None) -> str:
    if shift is None:
        return "❔ Нет данных за этот день"
    if shift.type == "day":
        return f"🟩 Рабочий день — дневная смена ({shift.code or 'Д'})"
    if shift.type == "night":
        return f"🟦 Рабочий день — ночная смена ({shift.code or 'Н'})"
    if shift.type == "vacation":
        return "🟨 Отпуск"
    if shift.type == "off":
        return "⬜ Выходной"
    return "❔ Смена не распознана — исправьте расписание"


def day_status_text(
    schedule: Schedule, employee: Employee, day: date
) -> str:
    shift = schedule.shift(employee.position, day.day)
    return (
        f"📅 <b>{format_date_ru(day)}, {weekday_name(day)}</b>\n"
        f"👤 {html.escape(employee.name or 'без имени')}\n"
        f"{_shift_description(shift)}"
    )


def today_report_text(
    schedule: Schedule,
    employee: Employee,
    day: date,
    *,
    next_schedule: Schedule | None = None,
    next_month_missing: bool = False,
    is_owner: bool = False,
) -> str:
    """Статус на сегодня + коллеги по смене + кому передавать смену."""

    shift = schedule.shift(employee.position, day.day)
    shift_type = shift.type if shift else "unknown"
    base = day_status_text(schedule, employee, day)
    if shift_type not in {"day", "night"}:
        return base

    lines = [base, ""]
    colleagues = employees_on_shift(
        schedule,
        day,
        shift_type,
        exclude_position=employee.position,
    )
    if colleagues:
        names = ", ".join(
            html.escape(colleague.name or "без имени")
            for colleague in colleagues
        )
        lines.append(f"👥 В этой же смене сегодня: {names}")
    else:
        lines.append("👥 В этой же смене сегодня: никого")

    step = next_shift(day, shift_type)
    if step is None:
        return "\n".join(lines)

    next_day, next_type = step
    label = f"{shift_emoji(next_type)} {shift_title(next_type)}"
    lines.append("")

    if next_month_missing:
        lines.append(
            f"➡️ Следующая смена: {label}, {format_date_ru(next_day)}"
        )
        month_title = f"{month_name(next_day.month)} {next_day.year}"
        if is_owner:
            lines.append(
                f"⚠️ Расписания на {month_title} нет. "
                "Загрузите фото расписания следующего месяца."
            )
        else:
            lines.append(
                f"⚠️ Создатель ещё не загрузил расписание на {month_title}."
            )
        return "\n".join(lines)

    source = next_schedule if next_schedule is not None else schedule
    if next_day.day not in source.days:
        recipients = "нет данных на этот день"
    else:
        target_employees = employees_on_shift(source, next_day, next_type)
        if target_employees:
            names = ", ".join(
                html.escape(target.name or "без имени")
                for target in target_employees
            )
            recipients = f"принимает: {names}"
        else:
            recipients = "по расписанию никого"
    lines.append(
        f"➡️ Передать смену: {label}, {format_date_ru(next_day)} — {recipients}"
    )
    return "\n".join(lines)


def month_caption_text(
    schedule: Schedule, employee: Employee, *, edit: bool = False
) -> str:
    """Короткая подпись к календарю с цветными кнопками дней."""

    if edit:
        hint = (
            "Нажмите день, чтобы изменить смену: "
            "Дневная / Ночная / Отпуск / Выходной."
        )
    else:
        hint = "Нажмите день, чтобы посмотреть статус."
    return (
        f"🗓 <b>{format_month_ru(schedule.year, schedule.month)}</b>\n"
        f"👤 {html.escape(employee.name or 'без имени')}\n\n"
        f"{shifts_totals_text(schedule, employee)}\n\n"
        f"{hint}\n"
        "🟩 день · 🟦 ночь · 🟨 отпуск · ⬜ выходной · ❔ нет данных"
    )


def summary_text(
    result: OcrResult,
    year: int,
    month: int,
    *,
    year_was_unknown: bool,
    replaces_existing: bool,
    shared: bool = False,
) -> str:
    totals = result.totals()
    names = result.employees
    shown = names[:15]
    lines = [
        f"🧾 <b>Распознано: {format_month_ru(year, month)}</b>",
        f"👥 Сотрудников: {len(names)}",
    ]
    if shown:
        lines.append("")
        for employee in shown:
            lines.append(
                f"{employee.position + 1}. {html.escape(employee.name or '— не распознано —')}"
            )
        if len(names) > len(shown):
            lines.append(f"… и ещё {len(names) - len(shown)}")

    lines.extend(
        [
            "",
            "📊 <b>Итого</b>",
            f"🟩 дневных: {totals.get('day', 0)}",
            f"🟦 ночных: {totals.get('night', 0)}",
            f"🟨 отпуск: {totals.get('vacation', 0)}",
            f"⬜ выходных: {totals.get('off', 0)}",
        ]
    )
    if totals.get("unknown"):
        lines.append(f"❔ не распознано: {totals['unknown']}")

    if result.issues:
        lines.append("")
        lines.append("⚠️ <b>Требует проверки</b>")
        for issue in result.issues[:8]:
            lines.append(f"• {html.escape(issue)}")
        if len(result.issues) > 8:
            lines.append(f"• … и ещё {len(result.issues) - 8}")
    else:
        lines.append("")
        lines.append("✅ Контрольные суммы сходятся по всем дням.")

    if result.warnings:
        lines.append("")
        for warning in result.warnings[:4]:
            lines.append(f"ℹ️ {html.escape(warning)}")

    if year_was_unknown:
        lines.append("")
        lines.append(
            "ℹ️ Год на картинке не указан — подставлен текущий. "
            "Проверьте кнопкой ниже."
        )

    if replaces_existing:
        lines.append("")
        lines.append(
            "♻️ Расписание за этот месяц уже есть — после подтверждения оно будет заменено."
        )
    if shared:
        lines.append("")
        lines.append(
            "👥 Изменения увидят все чаты, подписанные на это расписание."
        )

    lines.append("")
    lines.append("Если всё верно, нажмите «Всё верно».")
    return "\n".join(lines)


def shifts_totals_text(schedule: Schedule, employee: Employee) -> str:
    counts = {"day": 0, "night": 0, "vacation": 0, "off": 0, "unknown": 0}
    for day in schedule.days:
        shift = schedule.shift(employee.position, day)
        shift_type = shift.type if shift else "unknown"
        counts[shift_type] = counts.get(shift_type, 0) + 1
    return (
        f"{shift_emoji('day')} {counts['day']} дн. · "
        f"{shift_emoji('night')} {counts['night']} ноч. · "
        f"{shift_emoji('vacation')} {counts['vacation']} отп. · "
        f"{shift_emoji('off')} {counts['off']} вых."
    )


def _schedule_totals(schedule: Schedule) -> dict[str, int]:
    totals = {"day": 0, "night": 0, "vacation": 0, "off": 0, "unknown": 0}
    for shift in schedule.shifts.values():
        totals[shift.type] = totals.get(shift.type, 0) + 1
    return totals


def schedule_summary_text(
    schedule: Schedule, *, replaces_existing: bool = False
) -> str:
    totals = _schedule_totals(schedule)
    shown = schedule.employees[:15]
    lines = [
        f"🧾 <b>{format_month_ru(schedule.year, schedule.month)}</b>",
        f"👥 Сотрудников: {len(schedule.employees)}",
    ]
    if shown:
        lines.append("")
        for employee in shown:
            lines.append(
                f"{employee.position + 1}. {html.escape(employee.name or '— не распознано —')}"
            )
        if len(schedule.employees) > len(shown):
            lines.append(f"… и ещё {len(schedule.employees) - len(shown)}")
    lines.extend(
        [
            "",
            "📊 <b>Итого</b>",
            f"🟩 дневных: {totals.get('day', 0)}",
            f"🟦 ночных: {totals.get('night', 0)}",
            f"🟨 отпуск: {totals.get('vacation', 0)}",
            f"⬜ выходных: {totals.get('off', 0)}",
            "",
        ]
    )
    if totals.get("unknown"):
        lines.insert(-1, f"❔ не распознано: {totals['unknown']}")
    if schedule.status == "draft":
        lines.append("Если всё верно, нажмите «Всё верно».")
    else:
        lines.append("Расписание сохранено.")
    if replaces_existing:
        lines.append(
            "♻️ Расписание за этот месяц уже есть — после подтверждения оно будет заменено."
        )
    return "\n".join(lines)


def month_list_text(schedules: list[Schedule]) -> str:
    if not schedules:
        return (
            "В этом чате пока нет расписаний.\n\n"
            "Пришлите скриншот таблицы смен (лучше как файл PNG) — я распознаю её."
        )
    lines = ["🗂 <b>Загруженные расписания</b>", ""]
    for schedule in schedules:
        status = "✅" if schedule.status == "active" else "📝"
        lines.append(
            f"{status} <b>{format_month_ru(schedule.year, schedule.month)}</b> — "
            f"{len(schedule.employees)} сотр. · {len(schedule.days)} дн."
        )
    lines.append("")
    lines.append("Открыть месяц: /month 2026-11")
    return "\n".join(lines)


def employee_picker_text() -> str:
    return "Для кого показывать расписание? Выберите сотрудника:"


def remember_text(employee: Employee) -> str:
    return (
        f"Выбран сотрудник: <b>{html.escape(employee.name or 'без имени')}</b>.\n\n"
        "Запомнить выбор и больше не спрашивать?"
    )


def no_schedule_text(year: int, month: int, latest: Schedule | None) -> str:
    title = format_month_ru(year, month)
    title = title[0].lower() + title[1:]
    lines = [f"На {title} расписания нет."]
    if latest is not None:
        lines.append(
            f"Последнее загруженное: <b>{format_month_ru(latest.year, latest.month)}</b>."
        )
        lines.append(f"Открыть: /month {latest.year}-{latest.month:02d}")
    lines.append("Пришлите скриншот таблицы смен — я распознаю её.")
    return "\n".join(lines)


def help_text() -> str:
    return (
        "🤖 <b>Бот расписания смен</b>\n\n"
        "Одно расписание можно сделать общим для нескольких чатов. "
        "Создатель загружает таблицы, остальные подписываются по коду.\n\n"
        "<b>Подписка</b>\n"
        "/start — подписаться по коду или создать новое расписание\n"
        "/share — показать код расписания\n"
        "/unsubscribe — отписаться от общего расписания\n"
        "После 10 неудачных вводов кода чат блокируется.\n\n"
        "<b>Сброс</b>\n"
        "/reset — меню: сотрудник, ввод, подписка, удаление месяца/расписания\n\n"
        "<b>Просмотр</b>\n"
        "/today — смена сегодня, коллеги и передача смены\n"
        "/date 15 — 15-е число текущего месяца\n"
        "/date 15.12 — 15 декабря\n"
        "/date 2026-12-15 — конкретная дата\n"
        "/month — календарь месяца\n"
        "/month 2026-12 — календарь конкретного месяца\n"
        "/months — загруженные месяцы\n"
        "/employee — сменить сотрудника\n\n"
        "<b>Только создатель</b>\n"
        "загрузка картинок, /edit, /delete, /delete_workspace\n\n"
        "/cancel — отменить ввод или распознавание\n\n"
        f"Обозначения: {LEGEND}\n\n"
        "В личном чате можно просто написать дату: <code>15</code> или <code>15.12</code>."
    )
