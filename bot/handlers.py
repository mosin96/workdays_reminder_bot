"""Обработчики Telegram-бота."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

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
from .codes import format_code, is_valid_code, normalize_code
from .config import Config
from .formatting import (
    day_status_text,
    employee_picker_text,
    help_text,
    month_caption_text,
    month_list_text,
    no_schedule_text,
    remember_text,
    reminders_menu_text,
    schedule_summary_text,
    summary_text,
    today_report_text,
)
from .keyboards import (
    cancel_keyboard,
    date_menu_keyboard,
    day_edit_keyboard,
    days_keyboard,
    delete_confirm_keyboard,
    delete_workspace_keyboard,
    edit_days_keyboard,
    edit_menu_keyboard,
    edit_names_keyboard,
    edit_schedule_keyboard,
    employees_keyboard,
    main_menu_keyboard,
    remember_keyboard,
    reminder_time_keyboard,
    reminders_keyboard,
    reminders_prompt_keyboard,
    reset_confirm_keyboard,
    reset_done_keyboard,
    reset_keyboard,
    shift_type_keyboard,
    start_keyboard,
    summary_keyboard,
    switch_keyboard,
)
from .models import (
    ChatSubscription,
    Employee,
    ReminderSettings,
    Schedule,
    ScheduleGroup,
)
from .ocr import (
    OcrCancelled,
    OcrError,
    complete_month,
    parse_schedule_image,
)
from .processing import ProcessingRegistry
from .services import (
    REMINDER_MODES,
    WORKING_SHIFT_TYPES,
    QueryError,
    find_employee,
    format_time,
    next_shift,
    parse_date_query,
    parse_month_query,
    parse_time_query,
)
from .storage import Storage
from .utils import format_month_ru

log = logging.getLogger(__name__)
router = Router(name="shifts")

CODE_FAIL_LIMIT = 10
BLOCKED_TEXT = (
    "⛔ Чат заблокирован: превышено число попыток ввода кода расписания."
)
SUBSCRIBE_PROMPT = (
    "Отправьте код расписания, например <code>K7M2-QX9P</code>.\n"
    "Код передаёт создатель расписания. /cancel — отмена."
)
MAIN_MENU_TEXT = (
    "🏠 <b>Главное меню</b>\n\n"
    "Выберите действие. Все команды также доступны через меню Telegram."
)


class Form(StatesGroup):
    rename = State()
    custom_year = State()
    waiting_code = State()
    reminder_time = State()
    menu_date = State()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _today(config: Config) -> date:
    try:
        return datetime.now(ZoneInfo(config.timezone)).date()
    except ZoneInfoNotFoundError:  # pragma: no cover - зависит от окружения
        log.warning("Неизвестный часовой пояс %s, использую системный", config.timezone)
        return date.today()


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _format_day_ranges(days: list[int]) -> str:
    if not days:
        return ""
    ranges: list[tuple[int, int]] = []
    start = previous = days[0]
    for day in days[1:]:
        if day == previous + 1:
            previous = day
        else:
            ranges.append((start, previous))
            start = previous = day
    ranges.append((start, previous))
    return ", ".join(
        str(first) if first == last else f"{first}–{last}"
        for first, last in ranges
    )


async def _subscription(
    storage: Storage, chat_id: int
) -> ChatSubscription | None:
    return await storage.get_subscription(chat_id)


async def _group_for_chat(
    storage: Storage, chat_id: int
) -> tuple[ChatSubscription, ScheduleGroup] | None:
    subscription = await storage.get_subscription(chat_id)
    if subscription is None:
        return None
    group = await storage.get_group(subscription.group_id)
    if group is None:
        return None
    return subscription, group


async def _is_owner(
    storage: Storage, chat_id: int, group_id: int
) -> bool:
    group = await storage.get_group(group_id)
    return group is not None and group.owner_chat_id == chat_id


async def _owner_of_schedule(
    storage: Storage, schedule: Schedule
) -> ScheduleGroup | None:
    return await storage.get_group(schedule.group_id)


async def _resolve_employee(
    storage: Storage, chat_id: int, schedule: Schedule
) -> Employee | None:
    subscription = await storage.get_subscription(chat_id)
    if subscription is None:
        return None
    return find_employee(schedule, subscription.employee_name)


async def _load_schedule(
    callback: CallbackQuery, schedule_id: int, storage: Storage
) -> Schedule | None:
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    schedule = await storage.get_schedule_for_chat(schedule_id, chat_id)
    if schedule is None:
        await callback.answer(
            "Расписание не найдено. Проверьте подписку: /start",
            show_alert=True,
        )
    return schedule


async def _require_owner_callback(
    callback: CallbackQuery, storage: Storage, schedule: Schedule
) -> bool:
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    if await _is_owner(storage, chat_id, schedule.group_id):
        return True
    await callback.answer(
        "Изменять это расписание может только создатель.",
        show_alert=True,
    )
    return False


async def _require_owner_message(
    message: Message, storage: Storage, schedule: Schedule
) -> bool:
    if await _is_owner(storage, message.chat.id, schedule.group_id):
        return True
    await message.answer(
        "⛔ Изменять это расписание может только создатель.\n"
        "Смотреть его можно через /today и /month."
    )
    return False


async def _safe_edit(
    callback: CallbackQuery, text: str, reply_markup=None
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=reply_markup)


async def _start_prompt(message: Message) -> None:
    await message.answer(
        "👋 <b>Бот расписания смен</b>\n\n"
        "В этом чате пока нет расписания.\n"
        "Подпишитесь на общее расписание по коду или создайте новое.",
        reply_markup=start_keyboard(),
    )


async def _reset_info(
    storage: Storage, chat_id: int, config: Config
) -> tuple[ChatSubscription | None, ScheduleGroup | None, bool, bool]:
    """(подписка, группа, создатель ли, есть ли месяц сейчас)."""

    subscription = await storage.get_subscription(chat_id)
    if subscription is None:
        return None, None, False, False
    group = await storage.get_group(subscription.group_id)
    if group is None:
        return None, None, False, False
    today = _today(config)
    month_schedule = await storage.get_active(
        group.id, today.year, today.month
    )
    return (
        subscription,
        group,
        group.owner_chat_id == chat_id,
        month_schedule is not None,
    )


def _reset_menu_text(
    has_subscription: bool, is_owner: bool, has_month: bool
) -> str:
    lines = ["🧹 <b>Сброс</b>", "", "Что сбросить?"]
    if not has_subscription:
        lines.extend(
            [
                "",
                "Чат не подписан на расписание — доступен только сброс ввода.",
            ]
        )
    elif not is_owner:
        lines.extend(
            [
                "",
                "Подписка и данные у остальных подписчиков не изменятся.",
            ]
        )
    return "\n".join(lines)


async def _ask_employee(
    message: Message, storage: Storage, schedule: Schedule
) -> None:
    chat_id = message.chat.id
    subscription = await storage.get_subscription(chat_id)
    if subscription is None or subscription.group_id != schedule.group_id:
        await _start_prompt(message)
        return
    owner = await _is_owner(storage, chat_id, schedule.group_id)
    if subscription.remember:
        employee = find_employee(schedule, subscription.employee_name)
        if employee is not None:
            keyboard = (
                edit_schedule_keyboard(schedule.id, employee.position)
                if owner
                else None
            )
            await message.answer(
                "✅ Расписание сохранено.\n"
                f"👤 Сотрудник: <b>{html.escape(employee.name)}</b> (запомнен).\n\n"
                "/today — смена сегодня\n"
                "/month — календарь месяца\n"
                "/share — код расписания\n"
                "/employee — сменить сотрудника",
                reply_markup=keyboard,
            )
            return
    await message.answer(
        employee_picker_text(),
        reply_markup=employees_keyboard(
            schedule.id,
            schedule.employees,
            remember=subscription.remember,
        ),
    )


async def _send_employee_picker(
    message: Message, storage: Storage, schedule: Schedule
) -> None:
    subscription = await storage.get_subscription(message.chat.id)
    remember = bool(subscription and subscription.remember)
    await message.answer(
        employee_picker_text(),
        reply_markup=employees_keyboard(
            schedule.id,
            schedule.employees,
            remember=remember,
        ),
    )


async def _show_subscription_info(
    message: Message, storage: Storage
) -> None:
    subscription = await storage.get_subscription(message.chat.id)
    if subscription is None:
        await _start_prompt(message)
        return
    group = await storage.get_group(subscription.group_id)
    if group is None:
        await _start_prompt(message)
        return
    latest = await storage.get_latest(group.id)
    months = await storage.list_schedules(group.id)
    employee = find_employee(latest, subscription.employee_name) if latest else None

    lines = [
        f"📋 <b>Расписание</b> <code>{format_code(group.code)}</code>",
        f"🗂 Загруженных месяцев: {len(months)}",
    ]
    if employee is not None:
        lines.append(f"👤 Сотрудник: <b>{html.escape(employee.name)}</b>")
    if latest is not None:
        lines.append(
            f"📅 Последнее: {format_month_ru(latest.year, latest.month)}"
        )
    else:
        lines.append("📅 Пока нет подтверждённых месяцев.")
    lines.extend(
        [
            "",
            "/today — смена сегодня",
            "/month — календарь",
            "/share — код для других чатов",
            "/employee — сменить сотрудника",
        ]
    )
    if group.owner_chat_id == message.chat.id:
        lines.append("/edit — изменить смены")
        lines.append("/reminders — напоминания о сменах")
        lines.append("/delete_workspace — удалить расписание")
    else:
        lines.append("/unsubscribe — отписаться")
    await message.answer(
        "\n".join(lines),
        reply_markup=main_menu_keyboard(
            is_owner=group.owner_chat_id == message.chat.id
        ),
    )

    if latest is not None and employee is None:
        await _send_employee_picker(message, storage, latest)


async def _ensure_reminder_settings(
    storage: Storage, chat_id: int, group_id: int
) -> ReminderSettings:
    settings = await storage.get_reminder_settings(chat_id)
    if settings is None or settings.group_id != group_id:
        settings = await storage.save_reminder_settings(
            chat_id,
            group_id,
            modes=REMINDER_MODES,
            time="20:00",
            shift_types=WORKING_SHIFT_TYPES,
            enabled=False,
        )
    return settings


async def _render_reminders_menu(
    callback: CallbackQuery,
    storage: Storage,
    settings: ReminderSettings,
) -> None:
    subscription = await storage.get_subscription(settings.chat_id)
    employee_name = subscription.employee_name if subscription else None
    await _safe_edit(
        callback,
        reminders_menu_text(settings, employee_name),
        reply_markup=reminders_keyboard(settings),
    )


async def _maybe_offer_reminders(
    message: Message, storage: Storage, schedule: Schedule
) -> None:
    if not await _is_owner(storage, message.chat.id, schedule.group_id):
        return
    if await storage.get_reminder_settings(message.chat.id) is not None:
        return
    await _ensure_reminder_settings(
        storage, message.chat.id, schedule.group_id
    )
    await message.answer(
        "🔔 <b>Настроить напоминания о сменах?</b>\n\n"
        "Можно включить сразу три варианта: за день до смены, "
        "в начале смены и в конце смены.",
        reply_markup=reminders_prompt_keyboard(),
    )


async def _show_day(
    message: Message, storage: Storage, config: Config, target: date
) -> None:
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    _, group = context
    schedule = await storage.get_active(group.id, target.year, target.month)
    if schedule is None:
        latest = await storage.get_latest(group.id)
        if group.owner_chat_id == message.chat.id:
            await message.answer(no_schedule_text(target.year, target.month, latest))
        else:
            await message.answer(
                f"На {format_month_ru(target.year, target.month)} расписания нет. "
                "Создатель ещё не загрузил этот месяц."
            )
        return
    employee = await _resolve_employee(storage, message.chat.id, schedule)
    if employee is None:
        await message.answer("Сначала выберите сотрудника для этого расписания.")
        await _send_employee_picker(message, storage, schedule)
        return
    keyboard = None
    if await _is_owner(storage, message.chat.id, group.id):
        keyboard = day_edit_keyboard(schedule.id, employee.position, target.day)
    await message.answer(
        day_status_text(schedule, employee, target),
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Старт, справка, подписки
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(
    message: Message, storage: Storage, state: FSMContext
) -> None:
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    code_arg = parts[1].strip() if len(parts) > 1 else ""
    if code_arg:
        await _subscribe_by_code(message, storage, state, code_arg)
        return
    subscription = await storage.get_subscription(message.chat.id)
    if subscription is None:
        await _start_prompt(message)
        return
    await _show_subscription_info(message, storage)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(help_text())


async def _show_main_menu(message: Message, storage: Storage) -> None:
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    _, group = context
    await message.answer(
        MAIN_MENU_TEXT,
        reply_markup=main_menu_keyboard(
            is_owner=group.owner_chat_id == message.chat.id
        ),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, storage: Storage) -> None:
    await _show_main_menu(message, storage)


@router.callback_query(MenuCB.filter())
async def on_menu_action(
    callback: CallbackQuery,
    callback_data: MenuCB,
    state: FSMContext,
    storage: Storage,
    config: Config,
) -> None:
    if callback.message is None:
        return
    action = callback_data.action
    await callback.answer()
    await state.clear()
    if action == "today":
        await cmd_today(callback.message, storage, config)
        return
    if action == "month":
        today = _today(config)
        await _show_month(callback.message, storage, today.year, today.month)
        return
    if action == "date":
        await state.set_state(Form.menu_date)
        await callback.message.answer(
            "Введите дату:\n"
            "15 — 15-е текущего месяца\n"
            "15.12 — 15 декабря\n"
            "2026-12-15 — конкретная дата\n"
            "/cancel — отмена."
        )
        return
    if action == "employee":
        await cmd_employee(callback.message, storage)
        return
    if action == "months":
        await cmd_months(callback.message, storage)
        return
    if action == "share":
        await cmd_share(callback.message, storage)
        return
    if action == "reset":
        await cmd_reset(callback.message, storage, config)
        return
    if action == "reminders":
        await cmd_reminders(callback.message, storage)
        return
    if action == "edit":
        today = _today(config)
        await _show_edit(callback.message, storage, today.year, today.month)
        return
    if action == "help":
        await cmd_help(callback.message)
        return


@router.message(Form.menu_date, F.text)
async def on_menu_date_text(
    message: Message, state: FSMContext, storage: Storage, config: Config
) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        return
    try:
        target = parse_date_query(text, _today(config))
    except QueryError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await state.clear()
    await _show_day(message, storage, config, target)


@router.message(Command("cancel"))
async def cmd_cancel(
    message: Message,
    state: FSMContext,
    processing: ProcessingRegistry,
) -> None:
    await state.clear()
    if processing.cancel(message.chat.id):
        await message.answer("⏹ Отменяю распознавание…")
        return
    await message.answer("Действие отменено.")


@router.message(Command("reset"))
async def cmd_reset(
    message: Message, storage: Storage, config: Config
) -> None:
    subscription, _, is_owner, has_month = await _reset_info(
        storage, message.chat.id, config
    )
    await message.answer(
        _reset_menu_text(
            has_subscription=subscription is not None,
            is_owner=is_owner,
            has_month=has_month,
        ),
        reply_markup=reset_keyboard(
            has_subscription=subscription is not None,
            is_owner=is_owner,
            has_month=has_month,
        ),
    )


@router.message(Command("share"))
async def cmd_share(message: Message, storage: Storage) -> None:
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    _, group = context
    await message.answer(
        f"🔑 Код расписания: <code>{format_code(group.code)}</code>\n\n"
        "Передайте его другому чату. Там нужно нажать /start → "
        "«Подписаться по коду» и отправить этот код."
    )


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message, storage: Storage) -> None:
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    subscription, group = context
    if group.owner_chat_id == message.chat.id:
        await message.answer(
            "Вы создатель этого расписания. Чтобы выйти, удалите его "
            "командой /delete_workspace (у всех подписчиков)."
        )
        return
    await storage.unsubscribe(message.chat.id)
    await message.answer(
        f"🚪 Чат отписан от расписания <code>{format_code(group.code)}</code>.\n"
        "Подписаться снова: /start."
    )


@router.message(Command("delete_workspace"))
async def cmd_delete_workspace(message: Message, storage: Storage) -> None:
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    _, group = context
    if group.owner_chat_id != message.chat.id:
        await message.answer("Удалить расписание может только создатель.")
        return
    await message.answer(
        "🗑 Удалить расписание <code>"
        f"{format_code(group.code)}</code> вместе со всеми месяцами?\n"
        "Это затронет всех подписчиков. Действие необратимо.",
        reply_markup=delete_workspace_keyboard(group.id),
    )


@router.callback_query(StartCB.filter(F.action == "subscribe"))
async def on_start_subscribe(
    callback: CallbackQuery, state: FSMContext, storage: Storage
) -> None:
    if callback.message is None:
        return
    if await storage.get_subscription(callback.message.chat.id):
        await callback.answer("Вы уже подписаны на расписание.")
        return
    await state.set_state(Form.waiting_code)
    await callback.answer()
    await callback.message.answer(SUBSCRIBE_PROMPT)


@router.callback_query(StartCB.filter(F.action == "create"))
async def on_start_create(
    callback: CallbackQuery, state: FSMContext, storage: Storage
) -> None:
    if callback.message is None:
        return
    if await storage.get_subscription(callback.message.chat.id):
        await callback.answer("Вы уже подписаны на расписание.")
        return
    await state.clear()
    await callback.answer()
    await callback.message.answer(
        "📷 Пришлите скриншот таблицы смен (лучше файлом PNG/JPG).\n"
        "После распознавания бот создаст общее расписание и выдаст код."
    )


@router.callback_query(StartCB.filter(F.action == "cancel"))
async def on_start_cancel(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await _safe_edit(callback, "Отменено.")


@router.message(Form.waiting_code, F.text)
async def on_code_text(
    message: Message, state: FSMContext, storage: Storage
) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer("Ввод кода отменён.")
        return
    await _subscribe_by_code(message, storage, state, text)


async def _subscribe_by_code(
    message: Message,
    storage: Storage,
    state: FSMContext,
    raw_code: str,
) -> None:
    chat_id = message.chat.id
    code = normalize_code(raw_code)
    group = await storage.get_group_by_code(code) if is_valid_code(code) else None

    if group is None:
        failed, blocked, newly_blocked = await storage.register_failed_code(
            chat_id, CODE_FAIL_LIMIT
        )
        if blocked:
            await state.clear()
            if newly_blocked:
                await message.answer(BLOCKED_TEXT)
            return
        remaining = max(0, CODE_FAIL_LIMIT - failed)
        text = f"❌ Код не найден. Осталось попыток: {remaining}."
        if remaining == 0:
            text += "\nСледующая ошибка заблокирует чат."
        await message.answer(text)
        return

    await storage.reset_code_attempts(chat_id)
    subscription = await storage.get_subscription(chat_id)
    if subscription is not None:
        if subscription.group_id == group.id:
            await state.clear()
            await message.answer(
                f"Вы уже подписаны на расписание <code>{format_code(group.code)}</code>."
            )
            await _show_subscription_info(message, storage)
            return
        await state.clear()
        await state.set_data({"pending_group_id": group.id})
        await message.answer(
            "Вы уже подписаны на другое расписание.\n"
            f"Переключиться на <code>{format_code(group.code)}</code>?",
            reply_markup=switch_keyboard(),
        )
        return

    await state.clear()
    await storage.subscribe(chat_id, group.id)
    await message.answer(
        f"✅ Вы подписались на расписание <code>{format_code(group.code)}</code>."
    )
    latest = await storage.get_latest(group.id)
    if latest is None:
        await message.answer(
            "Пока нет подтверждённых месяцев. Как только создатель загрузит "
            "расписание, оно появится здесь."
        )
        return
    await _send_employee_picker(message, storage, latest)


@router.callback_query(SwitchCB.filter())
async def on_switch_subscription(
    callback: CallbackQuery,
    state: FSMContext,
    storage: Storage,
) -> None:
    if callback.message is None:
        return
    data = await state.get_data()
    group_id = data.get("pending_group_id")
    if not group_id:
        await callback.answer(
            "Кнопка устарела. Введите код заново: /start", show_alert=True
        )
        return
    group = await storage.get_group(int(group_id))
    if group is None:
        await state.clear()
        await callback.answer("Это расписание больше не существует.", show_alert=True)
        return
    await storage.subscribe(callback.message.chat.id, group.id)
    await storage.reset_code_attempts(callback.message.chat.id)
    await state.clear()
    await callback.answer("Готово")
    await _safe_edit(
        callback,
        f"✅ Теперь чат подписан на расписание "
        f"<code>{format_code(group.code)}</code>.",
    )
    await _show_subscription_info(callback.message, storage)


@router.callback_query(DeleteWorkspaceCB.filter(F.confirm == 1))
async def on_delete_workspace(
    callback: CallbackQuery,
    callback_data: DeleteWorkspaceCB,
    storage: Storage,
) -> None:
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    subscription = await storage.get_subscription(chat_id)
    if (
        subscription is None
        or subscription.group_id != callback_data.group_id
        or not await _is_owner(storage, chat_id, callback_data.group_id)
    ):
        await callback.answer("Удалить может только создатель.", show_alert=True)
        return
    await storage.delete_group(callback_data.group_id)
    await callback.answer("Удалено")
    await _safe_edit(
        callback,
        "🗑 Расписание удалено вместе со всеми месяцами и подписками.\n"
        "/start — создать новое.",
    )


async def _show_reset_menu(
    callback: CallbackQuery, storage: Storage, config: Config
) -> None:
    if callback.message is None:
        return
    subscription, _, is_owner, has_month = await _reset_info(
        storage, callback.message.chat.id, config
    )
    await _safe_edit(
        callback,
        _reset_menu_text(
            has_subscription=subscription is not None,
            is_owner=is_owner,
            has_month=has_month,
        ),
        reply_markup=reset_keyboard(
            has_subscription=subscription is not None,
            is_owner=is_owner,
            has_month=has_month,
        ),
    )


@router.callback_query(ResetCB.filter())
async def on_reset_action(
    callback: CallbackQuery,
    callback_data: ResetCB,
    state: FSMContext,
    storage: Storage,
    config: Config,
) -> None:
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    action = callback_data.action

    if action == "cancel":
        await callback.answer()
        await _safe_edit(callback, "Отменено.")
        return

    if action == "menu":
        await callback.answer()
        await _show_reset_menu(callback, storage, config)
        return

    if action == "state":
        await state.clear()
        await callback.answer("Готово")
        await _safe_edit(
            callback,
            "⌨️ Текущий ввод сброшен.",
            reply_markup=reset_done_keyboard(),
        )
        return

    subscription, group, is_owner, _ = await _reset_info(
        storage, chat_id, config
    )
    if subscription is None or group is None:
        await callback.answer("Чат не подписан на расписание.", show_alert=True)
        return

    today = _today(config)

    if action == "reminders":
        if not is_owner:
            await callback.answer(
                "Настраивать напоминания может только создатель.",
                show_alert=True,
            )
            return
        settings = await _ensure_reminder_settings(storage, chat_id, group.id)
        await callback.answer()
        await _render_reminders_menu(callback, storage, settings)
        return

    if action == "employee":
        await storage.clear_employee(chat_id)
        await storage.set_reminder_enabled(chat_id, False)
        await storage.cancel_pending_reminders(chat_id)
        await callback.answer("Готово")
        await _safe_edit(
            callback,
            "👤 Выбранный сотрудник и запоминание сброшены.\n"
            "Бот снова спросит, для кого показывать расписание.",
            reply_markup=reset_done_keyboard(),
        )
        return

    if action == "unsubscribe":
        if is_owner:
            await callback.answer(
                "Вы создатель расписания. Его можно только удалить.",
                show_alert=True,
            )
            return
        await callback.answer()
        await _safe_edit(
            callback,
            "🚪 Отписаться от расписания "
            f"<code>{format_code(group.code)}</code>?\n"
            "Данные у остальных подписчиков останутся.",
            reply_markup=reset_confirm_keyboard("unsubscribe_confirm"),
        )
        return

    if action == "unsubscribe_confirm":
        if is_owner:
            await callback.answer("Вы создатель расписания.", show_alert=True)
            return
        await storage.unsubscribe(chat_id)
        await callback.answer("Готово")
        await _safe_edit(
            callback,
            f"🚪 Чат отписан от расписания "
            f"<code>{format_code(group.code)}</code>.\n"
            "/start — подписаться снова или создать новое.",
        )
        return

    if action == "delete_month":
        if not is_owner:
            await callback.answer("Удалять может только создатель.", show_alert=True)
            return
        schedule = await storage.get_active(group.id, today.year, today.month)
        if schedule is None:
            await callback.answer(
                "За текущий месяц расписания нет.", show_alert=True
            )
            return
        await callback.answer()
        await _safe_edit(
            callback,
            f"🗑 Удалить расписание за "
            f"<b>{format_month_ru(today.year, today.month)}</b>?\n"
            "Месяц исчезнет у всех подписчиков.",
            reply_markup=reset_confirm_keyboard("delete_month_confirm"),
        )
        return

    if action == "delete_month_confirm":
        if not is_owner:
            await callback.answer("Удалять может только создатель.", show_alert=True)
            return
        schedule = await storage.get_active(group.id, today.year, today.month)
        if schedule is None:
            await callback.answer(
                "За текущий месяц расписания нет.", show_alert=True
            )
            return
        await storage.delete_month(group.id, today.year, today.month)
        await callback.answer("Удалено")
        await _safe_edit(
            callback,
            f"🗑 Расписание за "
            f"<b>{format_month_ru(today.year, today.month)}</b> "
            "удалено у всех подписчиков.",
            reply_markup=reset_done_keyboard(),
        )
        return

    if action == "delete_workspace":
        if not is_owner:
            await callback.answer("Удалять может только создатель.", show_alert=True)
            return
        await callback.answer()
        await _safe_edit(
            callback,
            "💥 Удалить всё расписание "
            f"<code>{format_code(group.code)}</code>?\n"
            "Все месяцы и подписки будут удалены безвозвратно.",
            reply_markup=reset_confirm_keyboard("delete_workspace_confirm"),
        )
        return

    if action == "delete_workspace_confirm":
        if not is_owner:
            await callback.answer("Удалять может только создатель.", show_alert=True)
            return
        await storage.delete_group(group.id)
        await callback.answer("Удалено")
        await _safe_edit(
            callback,
            "💥 Расписание удалено вместе со всеми месяцами и подписками.\n"
            "/start — создать новое.",
        )
        return

    await callback.answer()


# ---------------------------------------------------------------------------
# Загрузка картинки
# ---------------------------------------------------------------------------


@router.message(F.photo)
async def on_photo(
    message: Message,
    bot: Bot,
    config: Config,
    storage: Storage,
    processing: ProcessingRegistry,
    state: FSMContext,
) -> None:
    if await state.get_state() == Form.waiting_code.state:
        await message.answer("Сначала введите код расписания или /cancel.")
        return
    await _process_image(
        message, bot, config, storage, processing, document=False
    )


@router.message(F.document)
async def on_document(
    message: Message,
    bot: Bot,
    config: Config,
    storage: Storage,
    processing: ProcessingRegistry,
    state: FSMContext,
) -> None:
    if await state.get_state() == Form.waiting_code.state:
        await message.answer("Сначала введите код расписания или /cancel.")
        return
    document = message.document
    mime = (document.mime_type or "").lower() if document else ""
    if not mime.startswith("image/"):
        await message.answer(
            "Нужна картинка с таблицей смен (PNG или JPG). "
            "Пришлите её как файл-изображение."
        )
        return
    await _process_image(
        message, bot, config, storage, processing, document=True
    )


async def _finish_status(message: Message | None, text: str) -> None:
    if message is None:
        return
    try:
        await message.edit_text(text, reply_markup=None)
    except TelegramBadRequest:
        pass


async def _process_image(
    message: Message,
    bot: Bot,
    config: Config,
    storage: Storage,
    processing: ProcessingRegistry,
    *,
    document: bool,
) -> None:
    chat_id = message.chat.id
    context = await _group_for_chat(storage, chat_id)
    group: ScheduleGroup | None = None
    if context is not None:
        _, group = context
        if group.owner_chat_id != chat_id:
            await message.answer(
                "⛔ Обновлять это расписание может только создатель.\n"
                f"Код: <code>{format_code(group.code)}</code>.\n"
                "Вы можете смотреть его через /today и /month."
            )
            return

    job = await processing.try_start(chat_id)
    if job is None:
        await message.answer(
            "⏳ В этом чате уже идёт распознавание. "
            "Дождитесь окончания или отмените: /cancel"
        )
        return

    status: Message | None = None
    temp_dir = Path(tempfile.mkdtemp(prefix="shift_ocr_"))
    try:
        status = await message.answer(
            "⏳ Ожидайте, распознаю таблицу…",
            reply_markup=cancel_keyboard(),
        )
        if document:
            meta = message.document
            file_id = meta.file_id
            size = meta.file_size or 0
            width = 0
            filename = meta.file_name or "schedule.png"
        else:
            photo = message.photo[-1]
            file_id = photo.file_id
            size = photo.file_size or 0
            width = photo.width
            filename = "photo.jpg"

        if size and size > config.max_image_mb * 1024 * 1024:
            await _finish_status(
                status,
                f"Файл больше {config.max_image_mb} МБ — пришлите картинку поменьше.",
            )
            return

        file = await bot.get_file(file_id)
        suffix = Path(file.file_path or "").suffix or ".png"
        path = temp_dir / f"image{suffix}"
        await bot.download_file(file.file_path, destination=path)

        result = await asyncio.to_thread(
            parse_schedule_image,
            path,
            config.tesseract_cmd,
            job.cancel_event,
        )
        if job.cancelled:
            raise OcrCancelled("Распознавание отменено.")

        today = _today(config)
        year = result.year or today.year
        month = result.month or today.month
        if result.month is None:
            result.warnings.append(
                f"Подставлен текущий месяц: {format_month_ru(year, month)} — проверьте."
            )
        if width and width < config.min_image_width:
            result.warnings.append(
                "Картинка низкого разрешения — проверьте распознанные дни."
            )

        if job.cancelled:
            raise OcrCancelled("Распознавание отменено.")

        missing_days = complete_month(result, year, month)
        if missing_days:
            result.warnings.append(
                "На скриншоте нет дней: "
                f"{_format_day_ranges(missing_days)}. Они отмечены как "
                "«нет данных» (❔). Заполнить вручную: /edit"
            )

        if group is None:
            group = await storage.create_group(chat_id)
            await storage.subscribe(chat_id, group.id)

        existing = await storage.has_schedule(group.id, year, month)
        shared = await storage.count_subscribers(group.id) > 1

        cells: dict[tuple[int, int], tuple[str | None, str, int]] = {}
        for position in range(len(result.employees)):
            for day in result.days:
                cell = result.shift(position, day)
                cells[(position, day)] = (
                    cell.code,
                    cell.type,
                    cell.confidence,
                    cell.overtime,
                )

        if job.cancelled:
            raise OcrCancelled("Распознавание отменено.")

        schedule_id = await storage.save_draft(
            group_id=group.id,
            year=year,
            month=month,
            employee_names=[employee.name for employee in result.employees],
            days=result.days,
            weekend=result.weekend,
            cells=cells,
            source=filename,
        )

        try:
            await status.delete()
        except TelegramBadRequest:
            pass
        status = None

        await message.answer(
            summary_text(
                result,
                year,
                month,
                year_was_unknown=result.year is None,
                replaces_existing=existing,
                shared=shared,
            ),
            reply_markup=summary_keyboard(
                schedule_id,
                year,
                month,
                has_issues=bool(result.issues),
            ),
        )
    except OcrCancelled:
        log.info("OCR отменён пользователем в чате %s", chat_id)
        await _finish_status(status, "⏹ Распознавание отменено.")
    except OcrError as exc:
        log.warning("OCR error: %s", exc)
        await _finish_status(status, f"⚠️ {exc}")
    except Exception:
        log.exception("Ошибка обработки картинки")
        await _finish_status(
            status,
            "⚠️ Не удалось распознать таблицу. Пришлите скриншот целиком "
            "файлом PNG/JPG или попробуйте картинку лучшего качества.",
        )
    finally:
        await processing.finish(chat_id, job)
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.callback_query(CancelCB.filter())
async def on_cancel_click(
    callback: CallbackQuery, processing: ProcessingRegistry
) -> None:
    if callback.message is not None:
        chat_id = callback.message.chat.id
    else:
        chat_id = callback.from_user.id
    if not processing.cancel(chat_id):
        await callback.answer("Распознавание уже завершено.")
        return
    await callback.answer("⏹ Отменяю…")
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass


# ---------------------------------------------------------------------------
# Подтверждение и выбор сотрудника
# ---------------------------------------------------------------------------


@router.callback_query(UploadCB.filter(F.action == "confirm"))
async def on_confirm(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None:
        return
    if not await _require_owner_callback(callback, storage, schedule):
        return
    if schedule.status != "active":
        await storage.activate(schedule.id, schedule.group_id)
        schedule = await storage.get_schedule(schedule.id)
        if schedule is None:
            await callback.answer("Не удалось сохранить расписание", show_alert=True)
            return
    group = await _owner_of_schedule(storage, schedule)
    await callback.answer("Расписание сохранено")
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        if group is not None:
            await callback.message.answer(
                f"🔑 Код расписания: <code>{format_code(group.code)}</code>\n"
                "Передайте его другим чатам: /start → «Подписаться по коду».\n"
                "Показать код: /share"
            )
        await _ask_employee(callback.message, storage, schedule)
        await _maybe_offer_reminders(callback.message, storage, schedule)


@router.callback_query(EmployeeCB.filter())
async def on_employee_selected(
    callback: CallbackQuery,
    callback_data: EmployeeCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None:
        return
    employee = schedule.employee(callback_data.pos)
    if employee is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    subscription = await storage.get_subscription(chat_id)
    if subscription is None:
        await callback.answer("Сначала подпишитесь: /start", show_alert=True)
        return
    was_remembered = subscription.remember
    await storage.set_employee(chat_id, employee.name)
    await callback.answer("Выбрано")
    name = html.escape(employee.name or "без имени")
    try:
        await callback.message.edit_text(f"👤 Выбран сотрудник: <b>{name}</b>")
    except TelegramBadRequest:
        pass
    group = await storage.get_group(schedule.group_id)
    keyboard = None
    if group is not None and group.owner_chat_id == chat_id:
        keyboard = edit_schedule_keyboard(schedule.id, employee.position)
    if was_remembered:
        await callback.message.answer(
            f"👤 Сотрудник: <b>{name}</b>.\n\n"
            "/today — смена сегодня\n"
            "/month — календарь месяца\n"
            "/edit — изменить смены вручную\n"
            "/share — код расписания\n"
            "/employee — сменить сотрудника",
            reply_markup=keyboard,
        )
    else:
        await callback.message.answer(
            remember_text(employee),
            reply_markup=remember_keyboard(schedule.id),
        )


@router.callback_query(RememberCB.filter())
async def on_remember(
    callback: CallbackQuery,
    callback_data: RememberCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None:
        return
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    remember = bool(callback_data.remember)
    await storage.set_remember(chat_id, remember)
    employee = await _resolve_employee(storage, chat_id, schedule)
    name = html.escape(employee.name) if employee else "сотрудник"
    await callback.answer("Готово")
    if remember:
        text = f"✅ Запомнил: <b>{name}</b>. Больше не спрашиваю."
    else:
        text = "🗣 Хорошо, спрошу при следующей загрузке расписания."
    try:
        await callback.message.edit_text(text)
    except TelegramBadRequest:
        await callback.message.answer(text)
    keyboard = None
    if employee is not None and await _is_owner(storage, chat_id, schedule.group_id):
        keyboard = edit_schedule_keyboard(schedule.id, employee.position)
    await callback.message.answer(
        "/today — смена сегодня\n"
        "/date 15 — конкретная дата\n"
        "/month — календарь месяца\n"
        "/edit — изменить смены вручную\n"
        "/share — код расписания",
        reply_markup=keyboard,
    )


@router.message(Command("employee"))
async def cmd_employee(message: Message, storage: Storage) -> None:
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    _, group = context
    schedule = await storage.get_latest(group.id)
    if schedule is None:
        await message.answer("В расписании пока нет подтверждённых месяцев.")
        return
    await _send_employee_picker(message, storage, schedule)


@router.callback_query(UploadCB.filter(F.action == "forget"))
async def on_forget(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or callback.message is None:
        return
    await storage.set_remember(callback.message.chat.id, False)
    await callback.answer("Готово")
    await _safe_edit(
        callback,
        "🗣 Хорошо, при следующей загрузке расписания спрошу сотрудника снова.",
    )


@router.message(Command("reminders"))
async def cmd_reminders(message: Message, storage: Storage) -> None:
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    _, group = context
    if group.owner_chat_id != message.chat.id:
        await message.answer(
            "⛔ Настраивать напоминания может только создатель расписания."
        )
        return
    settings = await _ensure_reminder_settings(
        storage, message.chat.id, group.id
    )
    subscription = await storage.get_subscription(message.chat.id)
    employee_name = subscription.employee_name if subscription else None
    await message.answer(
        reminders_menu_text(settings, employee_name),
        reply_markup=reminders_keyboard(settings),
    )


@router.callback_query(ReminderCB.filter())
async def on_reminder_action(
    callback: CallbackQuery,
    callback_data: ReminderCB,
    state: FSMContext,
    storage: Storage,
    config: Config,
) -> None:
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    context = await _group_for_chat(storage, chat_id)
    if context is None:
        await callback.answer(
            "Сначала подпишитесь на расписание: /start", show_alert=True
        )
        return
    _, group = context
    if group.owner_chat_id != chat_id:
        await callback.answer(
            "Настраивать напоминания может только создатель.",
            show_alert=True,
        )
        return

    settings = await _ensure_reminder_settings(storage, chat_id, group.id)
    action = callback_data.action

    async def save(**changes) -> ReminderSettings:
        return await storage.save_reminder_settings(
            settings.chat_id,
            settings.group_id,
            modes=changes.get("modes", settings.modes),
            time=changes.get("time", settings.time),
            shift_types=changes.get("shift_types", settings.shift_types),
            enabled=changes.get("enabled", settings.enabled),
        )

    if action == "prompt_yes":
        await callback.answer()
        await _render_reminders_menu(callback, storage, settings)
        return

    if action == "prompt_no":
        await callback.answer()
        await _safe_edit(
            callback,
            "Хорошо. Настроить напоминания можно позже командой /reminders.",
        )
        return

    if action == "mode":
        mode = callback_data.value
        if mode not in REMINDER_MODES:
            await callback.answer("Неизвестный режим", show_alert=True)
            return
        current = set(settings.modes)
        if mode in current:
            if len(current) == 1:
                await callback.answer(
                    "Оставьте включённым хотя бы один режим.",
                    show_alert=True,
                )
                return
            current.remove(mode)
        else:
            current.add(mode)
        ordered = tuple(item for item in REMINDER_MODES if item in current)
        settings = await save(modes=ordered)
        await callback.answer("Сохранено")
        await _render_reminders_menu(callback, storage, settings)
        return

    if action == "time":
        await callback.answer()
        await _safe_edit(
            callback,
            "⏰ Выберите время напоминания за день до смены:",
            reply_markup=reminder_time_keyboard(settings.time),
        )
        return

    if action == "settime":
        try:
            value = format_time(parse_time_query(callback_data.value))
        except QueryError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        settings = await save(time=value)
        await callback.answer("Сохранено")
        await _render_reminders_menu(callback, storage, settings)
        return

    if action == "custom_time":
        await state.set_state(Form.reminder_time)
        await state.set_data({"group_id": group.id})
        await callback.answer()
        await callback.message.answer(
            "Введите время напоминания в формате <code>ЧЧ:ММ</code>, "
            "например <code>20:00</code>.\n/cancel — отмена."
        )
        return

    if action == "type":
        shift_type = callback_data.value
        if shift_type not in WORKING_SHIFT_TYPES:
            await callback.answer("Неизвестный тип смены", show_alert=True)
            return
        current = set(settings.shift_types)
        if shift_type in current:
            if len(current) == 1:
                await callback.answer(
                    "Оставьте включённым хотя бы один тип смены.",
                    show_alert=True,
                )
                return
            current.remove(shift_type)
        else:
            current.add(shift_type)
        ordered = tuple(
            item for item in WORKING_SHIFT_TYPES if item in current
        )
        settings = await save(shift_types=ordered)
        await callback.answer("Сохранено")
        await _render_reminders_menu(callback, storage, settings)
        return

    if action == "toggle":
        enabling = not settings.enabled
        if enabling:
            subscription = await storage.get_subscription(chat_id)
            if subscription is None or not subscription.employee_name:
                await callback.answer(
                    "Сначала выберите сотрудника командой /employee.",
                    show_alert=True,
                )
                return
        settings = await save(enabled=enabling)
        if not enabling:
            await storage.cancel_pending_reminders(chat_id)
        await callback.answer(
            "Напоминания включены" if enabling else "Напоминания выключены"
        )
        await _render_reminders_menu(callback, storage, settings)
        return

    if action == "menu":
        await callback.answer()
        await _render_reminders_menu(callback, storage, settings)
        return

    if action == "back":
        await callback.answer()
        await _show_reset_menu(callback, storage, config)
        return

    await callback.answer()


@router.message(Form.reminder_time, F.text)
async def on_reminder_time_text(
    message: Message, state: FSMContext, storage: Storage
) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer("Ввод времени отменён.")
        return
    try:
        value = format_time(parse_time_query(text))
    except QueryError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    data = await state.get_data()
    await state.clear()
    group_id = int(data.get("group_id", 0))
    if not await _is_owner(storage, message.chat.id, group_id):
        await message.answer("Настраивать напоминания может только создатель.")
        return
    settings = await storage.get_reminder_settings(message.chat.id)
    if settings is None or settings.group_id != group_id:
        await message.answer("Настройки напоминаний не найдены. Откройте /reminders.")
        return
    settings = await storage.save_reminder_settings(
        settings.chat_id,
        settings.group_id,
        modes=settings.modes,
        time=value,
        shift_types=settings.shift_types,
        enabled=settings.enabled,
    )
    subscription = await storage.get_subscription(message.chat.id)
    employee_name = subscription.employee_name if subscription else None
    await message.answer(
        f"✅ Время напоминания за день до смены: <b>{value}</b>"
    )
    await message.answer(
        reminders_menu_text(settings, employee_name),
        reply_markup=reminders_keyboard(settings),
    )


@router.callback_query(ReminderAckCB.filter())
async def on_reminder_ack(
    callback: CallbackQuery,
    callback_data: ReminderAckCB,
    storage: Storage,
) -> None:
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    delivery = await storage.get_reminder_delivery_by_id(callback_data.id)
    if delivery is None or delivery.chat_id != chat_id:
        await callback.answer("Напоминание устарело.", show_alert=True)
        return
    if delivery.acked_at is not None:
        await callback.answer("Это напоминание уже подтверждено.")
        return
    await storage.ack_reminder_delivery(delivery.id, chat_id)
    await callback.answer("✅ Напоминание получено")
    text = (
        getattr(callback.message, "html_text", None)
        or callback.message.text
        or ""
    )
    if text:
        try:
            await callback.message.edit_text(
                text + "\n\n✅ <b>Напоминание получено.</b>"
            )
            return
        except TelegramBadRequest:
            pass
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


# ---------------------------------------------------------------------------
# Запросы: сегодня, дата, месяц
# ---------------------------------------------------------------------------


@router.message(Command("today"))
async def cmd_today(
    message: Message, storage: Storage, config: Config
) -> None:
    today = _today(config)
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    _, group = context
    schedule = await storage.get_active(group.id, today.year, today.month)
    if schedule is None:
        latest = await storage.get_latest(group.id)
        if group.owner_chat_id == message.chat.id:
            await message.answer(no_schedule_text(today.year, today.month, latest))
        else:
            await message.answer(
                f"На {format_month_ru(today.year, today.month)} расписания нет. "
                "Создатель ещё не загрузил этот месяц."
            )
        return
    employee = await _resolve_employee(storage, message.chat.id, schedule)
    if employee is None:
        await message.answer("Сначала выберите сотрудника для этого расписания.")
        await _send_employee_picker(message, storage, schedule)
        return

    shift = schedule.shift(employee.position, today.day)
    shift_type = shift.type if shift else "unknown"
    next_schedule: Schedule | None = None
    next_month_missing = False
    if shift_type in {"day", "night"}:
        step = next_shift(today, shift_type)
        if step is not None:
            next_day, _ = step
            if (next_day.year, next_day.month) == (
                schedule.year,
                schedule.month,
            ):
                next_schedule = schedule
            else:
                next_schedule = await storage.get_active(
                    group.id, next_day.year, next_day.month
                )
                next_month_missing = next_schedule is None

    keyboard = None
    if group.owner_chat_id == message.chat.id:
        keyboard = day_edit_keyboard(
            schedule.id, employee.position, today.day
        )
    await message.answer(
        today_report_text(
            schedule,
            employee,
            today,
            next_schedule=next_schedule,
            next_month_missing=next_month_missing,
            is_owner=group.owner_chat_id == message.chat.id,
        ),
        reply_markup=keyboard,
    )


@router.message(Command("date"))
async def cmd_date(
    message: Message, storage: Storage, config: Config
) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Укажите дату:\n"
            "/date 15 — 15-е текущего месяца\n"
            "/date 15.12 — 15 декабря\n"
            "/date 2026-12-15 — конкретная дата\n"
            "/date 15 декабря"
        )
        return
    try:
        target = parse_date_query(parts[1], _today(config))
    except QueryError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await _show_day(message, storage, config, target)


async def _show_month(
    message: Message, storage: Storage, year: int, month: int
) -> None:
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    _, group = context
    schedule = await storage.get_active(group.id, year, month)
    if schedule is None:
        latest = await storage.get_latest(group.id)
        if group.owner_chat_id == message.chat.id:
            await message.answer(no_schedule_text(year, month, latest))
        else:
            await message.answer(
                f"На {format_month_ru(year, month)} расписания нет. "
                "Создатель ещё не загрузил этот месяц."
            )
        return
    employee = await _resolve_employee(storage, message.chat.id, schedule)
    if employee is None:
        await message.answer("Выберите сотрудника для этого расписания.")
        await _send_employee_picker(message, storage, schedule)
        return
    owner = await _is_owner(storage, message.chat.id, group.id)
    await message.answer(
        month_caption_text(schedule, employee),
        reply_markup=days_keyboard(
            schedule,
            action="day",
            position=employee.position,
            edit_position=employee.position if owner else None,
            back_action=None,
        ),
    )


@router.message(Command("month"))
async def cmd_month(
    message: Message, storage: Storage, config: Config
) -> None:
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1] if len(parts) > 1 else ""
    try:
        year, month = parse_month_query(arg, _today(config))
    except QueryError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await _show_month(message, storage, year, month)


async def _show_edit(
    message: Message, storage: Storage, year: int, month: int
) -> None:
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    _, group = context
    if group.owner_chat_id != message.chat.id:
        await message.answer("⛔ Изменять расписание может только создатель.")
        return
    schedule = await storage.get_active(group.id, year, month)
    if schedule is None:
        latest = await storage.get_latest(group.id)
        await message.answer(no_schedule_text(year, month, latest))
        return
    employee = await _resolve_employee(storage, message.chat.id, schedule)
    if employee is None:
        await message.answer("Сначала выберите сотрудника для этого расписания.")
        await _send_employee_picker(message, storage, schedule)
        return
    await message.answer(
        month_caption_text(schedule, employee, edit=True),
        reply_markup=days_keyboard(
            schedule,
            action="setday",
            position=employee.position,
            back_action="edit_done",
            context="quick",
        ),
    )


@router.message(Command("edit"))
async def cmd_edit(
    message: Message, storage: Storage, config: Config
) -> None:
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1] if len(parts) > 1 else ""
    try:
        year, month = parse_month_query(arg, _today(config))
    except QueryError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await _show_edit(message, storage, year, month)


@router.message(Command("months"))
async def cmd_months(message: Message, storage: Storage) -> None:
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    _, group = context
    schedules = await storage.list_schedules(group.id)
    await message.answer(month_list_text(schedules))


@router.message(Command("delete"))
async def cmd_delete(
    message: Message, storage: Storage, config: Config
) -> None:
    context = await _group_for_chat(storage, message.chat.id)
    if context is None:
        await _start_prompt(message)
        return
    _, group = context
    if group.owner_chat_id != message.chat.id:
        await message.answer("Удалять месяцы может только создатель.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите месяц: /delete 2026-11 (или /delete 11).")
        return
    try:
        year, month = parse_month_query(parts[1], _today(config))
    except QueryError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    schedule = await storage.get_active(group.id, year, month)
    if schedule is None:
        await message.answer(f"На {format_month_ru(year, month)} расписания нет.")
        return
    await message.answer(
        f"Удалить расписание за <b>{format_month_ru(year, month)}</b>?\n"
        "Месяц исчезнет у всех подписчиков.",
        reply_markup=delete_confirm_keyboard(schedule.id),
    )


@router.callback_query(UploadCB.filter(F.action == "delete_yes"))
async def on_delete_yes(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    await storage.delete_month(schedule.group_id, schedule.year, schedule.month)
    await callback.answer("Удалено")
    await _safe_edit(
        callback,
        f"🗑 Расписание за <b>{format_month_ru(schedule.year, schedule.month)}</b> "
        "удалено у всех подписчиков.",
    )


@router.callback_query(UploadCB.filter(F.action == "day"))
async def on_day_click(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or callback.message is None:
        return
    employee = await _resolve_employee(
        storage, callback.message.chat.id, schedule
    )
    if employee is None:
        await callback.answer("Сначала выберите сотрудника: /employee", show_alert=True)
        return
    try:
        target = date(schedule.year, schedule.month, callback_data.day)
    except ValueError:
        await callback.answer("Некорректный день", show_alert=True)
        return
    text = _plain(day_status_text(schedule, employee, target))
    await callback.answer(text[:190], show_alert=True)


# ---------------------------------------------------------------------------
# Редактирование (только создатель)
# ---------------------------------------------------------------------------


@router.callback_query(UploadCB.filter(F.action == "edit"))
async def on_edit(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    await callback.answer()
    await _safe_edit(
        callback,
        f"✏️ <b>{format_month_ru(schedule.year, schedule.month)}</b>\n\n"
        "Что исправить?",
        reply_markup=edit_menu_keyboard(schedule.id),
    )


@router.callback_query(UploadCB.filter(F.action == "back"))
async def on_back(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    await callback.answer()
    await _safe_edit(
        callback,
        schedule_summary_text(schedule),
        reply_markup=summary_keyboard(
            schedule.id, schedule.year, schedule.month, has_issues=False
        ),
    )


@router.callback_query(UploadCB.filter(F.action == "edit_names"))
async def on_edit_names(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    await callback.answer()
    await _safe_edit(
        callback,
        "Выберите сотрудника, чтобы изменить имя:",
        reply_markup=edit_names_keyboard(schedule.id, schedule.employees),
    )


@router.callback_query(UploadCB.filter(F.action == "rename_ask"))
async def on_rename_ask(
    callback: CallbackQuery,
    callback_data: UploadCB,
    state: FSMContext,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    employee = schedule.employee(callback_data.pos)
    if employee is None or callback.message is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    await state.set_state(Form.rename)
    await state.set_data(
        {
            "sid": schedule.id,
            "group_id": schedule.group_id,
            "pos": employee.position,
            "chat_id": callback.message.chat.id,
        }
    )
    await callback.answer()
    await callback.message.answer(
        f"Введите новое имя для <b>{html.escape(employee.name or 'сотрудника')}</b> "
        "одним сообщением.\n/cancel — отмена."
    )


@router.message(Form.rename, F.text)
async def on_rename_text(
    message: Message, state: FSMContext, storage: Storage
) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer("Изменение имени отменено.")
        return
    name = " ".join(text.split())
    if len(name) < 2 or len(name) > 100:
        await message.answer("Имя должно быть от 2 до 100 символов. Попробуйте ещё раз.")
        return
    data = await state.get_data()
    await state.clear()
    group_id = int(data["group_id"])
    if not await _is_owner(storage, message.chat.id, group_id):
        await message.answer("Изменять расписание может только создатель.")
        return
    ok = await storage.rename_employee(
        int(data["sid"]), group_id, int(data["pos"]), name
    )
    if not ok:
        await message.answer("Не удалось изменить имя. Пришлите картинку заново.")
        return
    schedule = await storage.get_schedule(int(data["sid"]))
    if schedule is None:
        await message.answer("Расписание не найдено.")
        return
    await message.answer(f"✅ Имя изменено: <b>{html.escape(name)}</b>")
    await message.answer(
        schedule_summary_text(schedule),
        reply_markup=summary_keyboard(
            schedule.id, schedule.year, schedule.month, has_issues=False
        ),
    )


@router.callback_query(UploadCB.filter(F.action == "edit_days"))
async def on_edit_days(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    await callback.answer()
    await _safe_edit(
        callback,
        "Выберите сотрудника, чьи дни хотите изменить:",
        reply_markup=edit_days_keyboard(schedule.id, schedule.employees),
    )


@router.callback_query(UploadCB.filter(F.action == "edit_emp"))
async def on_edit_employee(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    employee = schedule.employee(callback_data.pos)
    if employee is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    context = callback_data.value or "menu"
    back_action = "edit_done" if context == "quick" else "edit_days"
    await callback.answer()
    await _safe_edit(
        callback,
        month_caption_text(schedule, employee, edit=True),
        reply_markup=days_keyboard(
            schedule,
            action="setday",
            position=employee.position,
            back_action=back_action,
            context=context,
        ),
    )


@router.callback_query(UploadCB.filter(F.action == "edit_done"))
async def on_edit_done(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    employee = schedule.employee(callback_data.pos)
    if employee is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    await callback.answer("Изменения сохранены")
    await _safe_edit(
        callback,
        "✅ Изменения сохранены.\n\n"
        + month_caption_text(schedule, employee),
        reply_markup=days_keyboard(
            schedule,
            action="day",
            position=employee.position,
            edit_position=employee.position,
            back_action=None,
        ),
    )


async def _render_shift_editor(
    callback: CallbackQuery,
    schedule: Schedule,
    employee: Employee,
    day: int,
    context: str,
) -> None:
    try:
        target = date(schedule.year, schedule.month, day)
    except ValueError:
        return
    current = schedule.shift(employee.position, day)
    await _safe_edit(
        callback,
        day_status_text(schedule, employee, target)
        + "\n\nЧто поставить в этот день?",
        reply_markup=shift_type_keyboard(
            schedule.id,
            employee.position,
            day,
            context=context,
            current_type=current.type if current else None,
            overtime=bool(current and current.overtime),
        ),
    )


@router.callback_query(UploadCB.filter(F.action == "setday"))
async def on_set_day(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    employee = schedule.employee(callback_data.pos)
    if employee is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    await callback.answer()
    await _render_shift_editor(
        callback,
        schedule,
        employee,
        callback_data.day,
        callback_data.value or "menu",
    )


@router.callback_query(UploadCB.filter(F.action == "settype"))
async def on_set_type(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    employee = schedule.employee(callback_data.pos)
    if employee is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    codes = {
        "day": "Д",
        "night": "Н",
        "short_day": "ДД",
        "vacation": "О",
        "off": None,
    }
    parts = (callback_data.value or "").split("|", 1)
    shift_type = parts[0]
    context = parts[1] if len(parts) > 1 and parts[1] else "menu"
    if shift_type not in codes:
        await callback.answer("Неизвестный тип смены", show_alert=True)
        return
    current = schedule.shift(employee.position, callback_data.day)
    overtime = bool(current and current.overtime)
    if shift_type not in {"day", "night", "short_day"}:
        overtime = False
    ok = await storage.set_shift(
        schedule.id,
        schedule.group_id,
        employee.position,
        callback_data.day,
        codes[shift_type],
        shift_type,
        overtime,
    )
    if not ok:
        await callback.answer("Не удалось сохранить", show_alert=True)
        return
    await callback.answer("Сохранено")
    schedule = await storage.get_schedule(schedule.id)
    if schedule is None:
        return
    await _render_shift_editor(
        callback,
        schedule,
        employee,
        callback_data.day,
        context,
    )


@router.callback_query(UploadCB.filter(F.action == "toggle_overtime"))
async def on_toggle_overtime(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    employee = schedule.employee(callback_data.pos)
    if employee is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    current = schedule.shift(employee.position, callback_data.day)
    if current is None or current.type not in {"day", "night", "short_day"}:
        await callback.answer(
            "Переработка доступна только для рабочих смен.",
            show_alert=True,
        )
        return
    overtime = not current.overtime
    base_code = (current.code or "").replace("+П", "").strip() or "Д"
    code = base_code + ("+П" if overtime else "")
    ok = await storage.set_shift(
        schedule.id,
        schedule.group_id,
        employee.position,
        callback_data.day,
        code,
        current.type,
        overtime,
    )
    if not ok:
        await callback.answer("Не удалось сохранить", show_alert=True)
        return
    await callback.answer("Переработка включена" if overtime else "Переработка выключена")
    schedule = await storage.get_schedule(schedule.id)
    if schedule is None:
        return
    await _render_shift_editor(
        callback,
        schedule,
        employee,
        callback_data.day,
        callback_data.value or "menu",
    )


@router.callback_query(UploadCB.filter(F.action == "edit_date"))
async def on_edit_date(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    await callback.answer()
    await _safe_edit(
        callback,
        "Выберите месяц и год расписания:",
        reply_markup=date_menu_keyboard(schedule),
    )


@router.callback_query(UploadCB.filter(F.action.in_({"setmonth", "setyear"})))
async def on_set_date(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    try:
        value = int(callback_data.value)
    except ValueError:
        await callback.answer("Некорректное значение", show_alert=True)
        return
    year, month = schedule.year, schedule.month
    if callback_data.action == "setmonth":
        if not 1 <= value <= 12:
            await callback.answer("Месяц от 1 до 12", show_alert=True)
            return
        month = value
    else:
        if not 2000 <= value <= 2100:
            await callback.answer("Год от 2000 до 2100", show_alert=True)
            return
        year = value
    await storage.update_month(
        schedule.id, schedule.group_id, year, month
    )
    await callback.answer("Сохранено")
    updated = await storage.get_schedule(schedule.id)
    if updated is None:
        return
    await _safe_edit(
        callback,
        f"🗓 <b>{format_month_ru(updated.year, updated.month)}</b>\n\n"
        "Выберите месяц и год расписания:",
        reply_markup=date_menu_keyboard(updated),
    )


@router.callback_query(UploadCB.filter(F.action == "custom_year"))
async def on_custom_year(
    callback: CallbackQuery,
    callback_data: UploadCB,
    state: FSMContext,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None or not await _require_owner_callback(
        callback, storage, schedule
    ):
        return
    if callback.message is None:
        return
    await state.set_state(Form.custom_year)
    await state.set_data(
        {
            "sid": schedule.id,
            "group_id": schedule.group_id,
            "chat_id": callback.message.chat.id,
        }
    )
    await callback.answer()
    await callback.message.answer(
        "Введите год числом, например 2026.\n/cancel — отмена."
    )


@router.message(Form.custom_year, F.text)
async def on_custom_year_text(
    message: Message, state: FSMContext, storage: Storage
) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer("Изменение года отменено.")
        return
    if not re.fullmatch(r"\d{4}", text):
        await message.answer("Введите год из четырёх цифр, например 2026.")
        return
    year = int(text)
    if not 2000 <= year <= 2100:
        await message.answer("Год должен быть от 2000 до 2100.")
        return
    data = await state.get_data()
    await state.clear()
    group_id = int(data["group_id"])
    if not await _is_owner(storage, message.chat.id, group_id):
        await message.answer("Изменять расписание может только создатель.")
        return
    schedule = await storage.get_schedule(int(data["sid"]))
    if schedule is None:
        await message.answer("Расписание не найдено.")
        return
    await storage.update_month(
        schedule.id, group_id, year, schedule.month
    )
    updated = await storage.get_schedule(schedule.id)
    if updated is None:
        return
    await message.answer(
        schedule_summary_text(updated),
        reply_markup=summary_keyboard(
            updated.id, updated.year, updated.month, has_issues=False
        ),
    )


# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------


@router.message(F.text, F.chat.type == "private")
async def fallback(
    message: Message, storage: Storage, config: Config
) -> None:
    text = (message.text or "").strip()
    if text and not text.startswith("/"):
        try:
            target = parse_date_query(text, _today(config))
        except QueryError:
            pass
        else:
            await _show_day(message, storage, config, target)
            return
    await message.answer(
        "Не понял сообщение. Пришлите картинку с таблицей смен, "
        "напишите дату (например, 15 или 15.12) или /help."
    )
