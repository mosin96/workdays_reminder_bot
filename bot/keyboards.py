"""Inline-клавиатуры."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .callbacks import EmployeeCB, RememberCB, UploadCB
from .models import Employee, Schedule
from .utils import MONTHS_NOMINATIVE, days_in_month, month_title


def _button(text: str, callback: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback.pack())


def summary_keyboard(
    schedule_id: int,
    year: int,
    month: int,
    has_issues: bool,
) -> InlineKeyboardMarkup:
    rows = [
        [
            _button(
                "✅ Всё верно",
                UploadCB(action="confirm", sid=schedule_id),
            ),
            _button(
                "✏️ Исправить",
                UploadCB(action="edit", sid=schedule_id),
            ),
        ],
        [
            _button(
                f"🗓 {month_title(month)} {year}",
                UploadCB(action="edit_date", sid=schedule_id),
            ),
        ],
    ]
    if has_issues:
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    text="⚠️ Есть замечания — проверьте данные",
                    callback_data=UploadCB(
                        action="edit", sid=schedule_id
                    ).pack(),
                )
            ],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def employees_keyboard(
    schedule_id: int,
    employees: list[Employee],
    *,
    remember: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for employee in employees:
        name = employee.name or f"Сотрудник {employee.position + 1}"
        current_row.append(
            _button(
                name,
                EmployeeCB(sid=schedule_id, pos=employee.position),
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    if remember:
        rows.append(
            [
                _button(
                    "🔕 Снова спрашивать при загрузке",
                    UploadCB(action="forget", sid=schedule_id),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def remember_keyboard(schedule_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "✅ Да, запомнить",
                    RememberCB(sid=schedule_id, remember=1),
                ),
                _button(
                    "🗣 Нет, спрашивать",
                    RememberCB(sid=schedule_id, remember=0),
                ),
            ]
        ]
    )


def edit_menu_keyboard(schedule_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "✏️ Имена",
                    UploadCB(action="edit_names", sid=schedule_id),
                ),
                _button(
                    "📅 Дни",
                    UploadCB(action="edit_days", sid=schedule_id),
                ),
            ],
            [
                _button(
                    "🗓 Месяц и год",
                    UploadCB(action="edit_date", sid=schedule_id),
                ),
            ],
            [
                _button(
                    "⬅️ Назад к проверке",
                    UploadCB(action="back", sid=schedule_id),
                )
            ],
        ]
    )


def edit_names_keyboard(
    schedule_id: int, employees: list[Employee]
) -> InlineKeyboardMarkup:
    rows = [
        [
            _button(
                f"✏️ {employee.name or f'Сотрудник {employee.position + 1}'}",
                UploadCB(
                    action="rename_ask",
                    sid=schedule_id,
                    pos=employee.position,
                ),
            )
        ]
        for employee in employees
    ]
    rows.append(
        [
            _button(
                "⬅️ Назад",
                UploadCB(action="edit", sid=schedule_id),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_days_keyboard(
    schedule_id: int, employees: list[Employee]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for employee in employees:
        name = employee.name or f"Сотрудник {employee.position + 1}"
        current_row.append(
            _button(
                name,
                UploadCB(
                    action="edit_emp",
                    sid=schedule_id,
                    pos=employee.position,
                ),
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append(
        [
            _button(
                "⬅️ Назад",
                UploadCB(action="edit", sid=schedule_id),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def days_keyboard(
    schedule: Schedule,
    action: str,
    *,
    position: int = 0,
    back_action: str = "back",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    limit = days_in_month(schedule.year, schedule.month)
    for day in sorted(schedule.days):
        if day > limit:
            continue
        current_row.append(
            _button(
                str(day),
                UploadCB(
                    action=action,
                    sid=schedule.id,
                    pos=position,
                    day=day,
                ),
            )
        )
        if len(current_row) == 7:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append(
        [
            _button(
                "⬅️ Назад",
                UploadCB(action=back_action, sid=schedule.id, pos=position),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shift_type_keyboard(
    schedule_id: int, position: int, day: int
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "🟩 Дневная",
                    UploadCB(
                        action="settype",
                        sid=schedule_id,
                        pos=position,
                        day=day,
                        value="day",
                    ),
                ),
                _button(
                    "🟦 Ночная",
                    UploadCB(
                        action="settype",
                        sid=schedule_id,
                        pos=position,
                        day=day,
                        value="night",
                    ),
                ),
            ],
            [
                _button(
                    "🟨 Отпуск",
                    UploadCB(
                        action="settype",
                        sid=schedule_id,
                        pos=position,
                        day=day,
                        value="vacation",
                    ),
                ),
                _button(
                    "⬜ Выходной",
                    UploadCB(
                        action="settype",
                        sid=schedule_id,
                        pos=position,
                        day=day,
                        value="off",
                    ),
                ),
            ],
            [
                _button(
                    "⬅️ К календарю",
                    UploadCB(
                        action="edit_emp",
                        sid=schedule_id,
                        pos=position,
                    ),
                )
            ],
        ]
    )


def date_menu_keyboard(schedule: Schedule) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for month in range(1, 13):
        label = MONTHS_NOMINATIVE[month].capitalize()
        if month == schedule.month:
            label = f"✅ {label}"
        current_row.append(
            _button(
                label,
                UploadCB(
                    action="setmonth",
                    sid=schedule.id,
                    value=str(month),
                ),
            )
        )
        if len(current_row) == 4:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    years = [schedule.year - 1, schedule.year, schedule.year + 1]
    rows.append(
        [
            _button(
                f"{'✅ ' if year == schedule.year else ''}{year}",
                UploadCB(
                    action="setyear",
                    sid=schedule.id,
                    value=str(year),
                ),
            )
            for year in years
        ]
    )
    rows.append(
        [
            _button(
                "⌨️ Другой год",
                UploadCB(action="custom_year", sid=schedule.id),
            )
        ]
    )
    rows.append(
        [
            _button(
                "⬅️ Назад",
                UploadCB(action="edit", sid=schedule.id),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delete_confirm_keyboard(schedule_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "🗑 Да, удалить",
                    UploadCB(action="delete_yes", sid=schedule_id),
                ),
                _button(
                    "⬅️ Отмена",
                    UploadCB(action="back", sid=schedule_id),
                ),
            ]
        ]
    )
