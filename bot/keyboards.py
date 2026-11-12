"""Inline-клавиатуры."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .callbacks import (
    CancelCB,
    DeleteWorkspaceCB,
    EmployeeCB,
    MenuCB,
    RememberCB,
    ReminderAckCB,
    ReminderCB,
    ResetCB,
    StartCB,
    SwitchCB,
    UploadCB,
)
from .models import Employee, ReminderSettings, Schedule
from .utils import (
    MONTHS_NOMINATIVE,
    days_in_month,
    month_title,
    shift_emoji,
)


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


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("❌ Отменить распознавание", CancelCB())]
        ]
    )


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "🔗 Подписаться по коду",
                    StartCB(action="subscribe"),
                ),
                _button(
                    "🆕 Создать новое",
                    StartCB(action="create"),
                ),
            ]
        ]
    )


def switch_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "🔄 Переключиться",
                    SwitchCB(),
                ),
                _button("⬅️ Отмена", StartCB(action="cancel")),
            ]
        ]
    )


def delete_workspace_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "🗑 Удалить навсегда",
                    DeleteWorkspaceCB(group_id=group_id, confirm=1),
                ),
                _button("⬅️ Отмена", StartCB(action="cancel")),
            ]
        ]
    )


def reset_keyboard(
    *,
    has_subscription: bool,
    is_owner: bool,
    has_month: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_subscription:
        rows.append(
            [
                _button(
                    "👤 Сотрудник и запоминание",
                    ResetCB(action="employee"),
                )
            ]
        )
    rows.append(
        [_button("⌨️ Текущий ввод", ResetCB(action="state"))]
    )
    if has_subscription and not is_owner:
        rows.append(
            [
                _button(
                    "🚪 Отписаться от расписания",
                    ResetCB(action="unsubscribe"),
                )
            ]
        )
    if has_subscription and is_owner:
        rows.append(
            [
                _button(
                    "🔔 Напоминания",
                    ResetCB(action="reminders"),
                )
            ]
        )
        if has_month:
            rows.append(
                [
                    _button(
                        "🗑 Удалить текущий месяц",
                        ResetCB(action="delete_month"),
                    )
                ]
            )
        rows.append(
            [
                _button(
                    "💥 Удалить всё расписание",
                    ResetCB(action="delete_workspace"),
                )
            ]
        )
    rows.append([_button("⬅️ Отмена", ResetCB(action="cancel"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reset_confirm_keyboard(confirm_action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "✅ Да, продолжить",
                    ResetCB(action=confirm_action),
                ),
                _button("⬅️ Назад", ResetCB(action="menu")),
            ]
        ]
    )


def reset_done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("🧹 Ещё сбросить", ResetCB(action="menu"))]
        ]
    )


REMINDER_MODE_LABELS = {
    "day_before": "За день до смены",
    "shift_start": "В начале смены",
    "shift_end": "В конце смены",
}

REMINDER_TYPE_LABELS = {
    "day": "🟩 Д",
    "night": "🟦 Н",
    "short_day": "🟪 ДД",
}


def reminders_keyboard(settings: ReminderSettings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for mode, label in REMINDER_MODE_LABELS.items():
        mark = "✅ " if mode in settings.modes else "▫️ "
        rows.append(
            [
                _button(
                    f"{mark}{label}",
                    ReminderCB(action="mode", value=mode),
                )
            ]
        )
    rows.append(
        [
            _button(
                f"⏰ Время за день: {settings.time}",
                ReminderCB(action="time"),
            )
        ]
    )
    type_row: list[InlineKeyboardButton] = []
    for shift_type, label in REMINDER_TYPE_LABELS.items():
        mark = "✅ " if shift_type in settings.shift_types else "▫️ "
        type_row.append(
            _button(
                f"{mark}{label}",
                ReminderCB(action="type", value=shift_type),
            )
        )
    rows.append(type_row)
    rows.append(
        [
            _button(
                "🔕 Выключить" if settings.enabled else "🔔 Включить",
                ReminderCB(action="toggle"),
            )
        ]
    )
    rows.append(
        [
            _button(
                "⬅️ Назад",
                ReminderCB(action="back"),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_time_keyboard(current: str) -> InlineKeyboardMarkup:
    presets = ["08:00", "12:00", "18:00", "20:00", "21:00"]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for preset in presets:
        mark = "✅ " if preset == current else ""
        row.append(
            _button(
                f"{mark}{preset}",
                ReminderCB(
                    action="settime",
                    value=preset.replace(":", "-"),
                ),
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            _button(
                "⌨️ Своё время",
                ReminderCB(action="custom_time"),
            )
        ]
    )
    rows.append(
        [
            _button(
                "⬅️ Назад",
                ReminderCB(action="menu"),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_ack_keyboard(delivery_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "✅ Напоминание получено",
                    ReminderAckCB(id=delivery_id),
                )
            ]
        ]
    )


def reminders_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "🔔 Настроить напоминания",
                    ReminderCB(action="prompt_yes"),
                )
            ],
            [
                _button(
                    "Позже",
                    ReminderCB(action="prompt_no"),
                )
            ],
        ]
    )


def main_menu_keyboard(*, is_owner: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            _button("📅 Сегодня", MenuCB(action="today")),
            _button("🗓 Месяц", MenuCB(action="month")),
        ],
        [
            _button("📆 Дата", MenuCB(action="date")),
            _button("👤 Сотрудник", MenuCB(action="employee")),
        ],
        [
            _button("🗂 Месяцы", MenuCB(action="months")),
            _button("🔑 Код", MenuCB(action="share")),
        ],
    ]
    if is_owner:
        rows.append(
            [
                _button("✏️ Изменить", MenuCB(action="edit")),
                _button("🔔 Напоминания", MenuCB(action="reminders")),
            ]
        )
    rows.append(
        [
            _button("🧹 Сброс", MenuCB(action="reset")),
            _button("ℹ️ Помощь", MenuCB(action="help")),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    back_action: str | None = "back",
    context: str = "menu",
    edit_position: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    limit = days_in_month(schedule.year, schedule.month)
    for day in sorted(schedule.days):
        if day > limit:
            continue
        shift = schedule.shift(position, day)
        shift_type = shift.type if shift else "unknown"
        overtime_mark = "+" if shift is not None and shift.overtime else ""
        current_row.append(
            _button(
                f"{day}{shift_emoji(shift_type)}{overtime_mark}",
                UploadCB(
                    action=action,
                    sid=schedule.id,
                    pos=position,
                    day=day,
                    value=context,
                ),
            )
        )
        if len(current_row) == 7:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    if edit_position is not None:
        rows.append(
            [
                _button(
                    "✏️ Изменить расписание",
                    UploadCB(
                        action="edit_emp",
                        sid=schedule.id,
                        pos=edit_position,
                        value="quick",
                    ),
                )
            ]
        )
    if back_action is not None:
        rows.append(
            [
                _button(
                    "⬅️ Назад",
                    UploadCB(
                        action=back_action, sid=schedule.id, pos=position
                    ),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def day_edit_keyboard(
    schedule_id: int, position: int, day: int
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "✏️ Изменить этот день",
                    UploadCB(
                        action="setday",
                        sid=schedule_id,
                        pos=position,
                        day=day,
                        value="quick",
                    ),
                )
            ]
        ]
    )


def edit_schedule_keyboard(
    schedule_id: int, position: int
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "✏️ Изменить расписание",
                    UploadCB(
                        action="edit_emp",
                        sid=schedule_id,
                        pos=position,
                        value="quick",
                    ),
                )
            ]
        ]
    )


def shift_type_keyboard(
    schedule_id: int,
    position: int,
    day: int,
    *,
    context: str = "menu",
    current_type: str | None = None,
    overtime: bool = False,
) -> InlineKeyboardMarkup:
    def value(shift_type: str) -> str:
        return f"{shift_type}|{context}"

    rows: list[list[InlineKeyboardButton]] = [
        [
            _button(
                "🟩 Д (12ч)",
                UploadCB(
                    action="settype",
                    sid=schedule_id,
                    pos=position,
                    day=day,
                    value=value("day"),
                ),
            ),
            _button(
                "🟦 Н (12ч)",
                UploadCB(
                    action="settype",
                    sid=schedule_id,
                    pos=position,
                    day=day,
                    value=value("night"),
                ),
            ),
        ],
        [
            _button(
                "🟪 ДД (8ч)",
                UploadCB(
                    action="settype",
                    sid=schedule_id,
                    pos=position,
                    day=day,
                    value=value("short_day"),
                ),
            ),
            _button(
                "🟨 Отпуск",
                UploadCB(
                    action="settype",
                    sid=schedule_id,
                    pos=position,
                    day=day,
                    value=value("vacation"),
                ),
            ),
        ],
        [
            _button(
                "⬜ Выходной",
                UploadCB(
                    action="settype",
                    sid=schedule_id,
                    pos=position,
                    day=day,
                    value=value("off"),
                ),
            )
        ],
    ]
    if current_type in {"day", "night", "short_day"}:
        rows.append(
            [
                _button(
                    "🟧 +П выключить" if overtime else "🟧 +П включить",
                    UploadCB(
                        action="toggle_overtime",
                        sid=schedule_id,
                        pos=position,
                        day=day,
                        value=context,
                    ),
                )
            ]
        )
    rows.append(
        [
            _button(
                "⬅️ К календарю",
                UploadCB(
                    action="edit_emp",
                    sid=schedule_id,
                    pos=position,
                    value="quick" if context == "quick" else "menu",
                ),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
