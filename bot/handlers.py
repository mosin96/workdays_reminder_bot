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

from .callbacks import CancelCB, EmployeeCB, RememberCB, UploadCB
from .config import Config
from .formatting import (
    calendar_text,
    day_status_text,
    employee_picker_text,
    help_text,
    month_list_text,
    no_schedule_text,
    remember_text,
    schedule_summary_text,
    summary_text,
)
from .keyboards import (
    cancel_keyboard,
    date_menu_keyboard,
    days_keyboard,
    delete_confirm_keyboard,
    edit_days_keyboard,
    edit_menu_keyboard,
    edit_names_keyboard,
    employees_keyboard,
    remember_keyboard,
    shift_type_keyboard,
    summary_keyboard,
)
from .models import Employee, Schedule
from .ocr import OcrCancelled, OcrError, parse_schedule_image
from .processing import ProcessingRegistry
from .services import (
    QueryError,
    find_employee,
    parse_date_query,
    parse_month_query,
)
from .storage import Storage
from .utils import format_month_ru

log = logging.getLogger(__name__)
router = Router(name="shifts")


class Form(StatesGroup):
    rename = State()
    custom_year = State()


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


async def _resolve_employee(
    storage: Storage, schedule: Schedule
) -> Employee | None:
    settings = await storage.get_settings(schedule.chat_id)
    if settings is None:
        return None
    return find_employee(schedule, settings.employee_name)


async def _load_schedule(
    callback: CallbackQuery, schedule_id: int, storage: Storage
) -> Schedule | None:
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    schedule = await storage.get_schedule(schedule_id, chat_id)
    if schedule is None:
        await callback.answer(
            "Расписание не найдено. Пришлите картинку заново.", show_alert=True
        )
    return schedule


async def _safe_edit(
    callback: CallbackQuery, text: str, reply_markup=None
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=reply_markup)


async def _ask_employee(
    message: Message, storage: Storage, schedule: Schedule
) -> None:
    settings = await storage.get_settings(schedule.chat_id)
    if settings and settings.remember:
        employee = find_employee(schedule, settings.employee_name)
        if employee is not None:
            await message.answer(
                "✅ Расписание сохранено.\n"
                f"👤 Сотрудник: <b>{html.escape(employee.name)}</b> (запомнен).\n\n"
                "/today — смена сегодня\n"
                "/month — календарь месяца\n"
                "/employee — сменить сотрудника"
            )
            return
    await message.answer(
        employee_picker_text(),
        reply_markup=employees_keyboard(
            schedule.id,
            schedule.employees,
            remember=bool(settings and settings.remember),
        ),
    )


async def _send_employee_picker(
    message: Message, storage: Storage, schedule: Schedule
) -> None:
    settings = await storage.get_settings(schedule.chat_id)
    await message.answer(
        employee_picker_text(),
        reply_markup=employees_keyboard(
            schedule.id,
            schedule.employees,
            remember=bool(settings and settings.remember),
        ),
    )


async def _show_day(
    message: Message, storage: Storage, config: Config, target: date
) -> None:
    schedule = await storage.get_active(
        message.chat.id, target.year, target.month
    )
    if schedule is None:
        latest = await storage.get_latest(message.chat.id)
        await message.answer(
            no_schedule_text(target.year, target.month, latest)
        )
        return
    employee = await _resolve_employee(storage, schedule)
    if employee is None:
        await message.answer(
            f"Сначала выберите сотрудника для {format_month_ru(schedule.year, schedule.month)}."
        )
        await _send_employee_picker(message, storage, schedule)
        return
    await message.answer(day_status_text(schedule, employee, target))


# ---------------------------------------------------------------------------
# Старт и справка
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, storage: Storage) -> None:
    text = help_text()
    settings = await storage.get_settings(message.chat.id)
    if settings and settings.employee_name:
        text += (
            f"\n\nТекущий сотрудник: <b>{html.escape(settings.employee_name)}</b>"
        )
    await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(help_text())


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
) -> None:
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
) -> None:
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
    job = await processing.try_start(message.chat.id)
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

        existing = await storage.has_schedule(message.chat.id, year, month)
        cells: dict[tuple[int, int], tuple[str | None, str, int]] = {}
        for position in range(len(result.employees)):
            for day in result.days:
                cell = result.shift(position, day)
                cells[(position, day)] = (
                    cell.code,
                    cell.type,
                    cell.confidence,
                )

        if job.cancelled:
            raise OcrCancelled("Распознавание отменено.")

        schedule_id = await storage.save_draft(
            chat_id=message.chat.id,
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
            ),
            reply_markup=summary_keyboard(
                schedule_id,
                year,
                month,
                has_issues=bool(result.issues),
            ),
        )
    except OcrCancelled:
        log.info("OCR отменён пользователем в чате %s", message.chat.id)
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
        await processing.finish(message.chat.id, job)
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
    if schedule.status != "active":
        await storage.activate(schedule.id, schedule.chat_id)
        schedule = await storage.get_schedule(schedule.id, schedule.chat_id)
        if schedule is None:
            await callback.answer("Не удалось сохранить расписание", show_alert=True)
            return
    await callback.answer("Расписание сохранено")
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _ask_employee(callback.message, storage, schedule)


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
    settings = await storage.get_settings(schedule.chat_id)
    was_remembered = bool(settings and settings.remember)
    await storage.set_employee(schedule.chat_id, employee.name)
    await callback.answer("Выбрано")
    name = html.escape(employee.name or "без имени")
    if callback.message is not None:
        try:
            await callback.message.edit_text(
                f"👤 Выбран сотрудник: <b>{name}</b>"
            )
        except TelegramBadRequest:
            pass
    if was_remembered and callback.message is not None:
        await callback.message.answer(
            f"👤 Сотрудник: <b>{name}</b>.\n\n"
            "/today — смена сегодня\n"
            "/month — календарь месяца\n"
            "/employee — сменить сотрудника"
        )
    elif callback.message is not None:
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
    remember = bool(callback_data.remember)
    await storage.set_remember(schedule.chat_id, remember)
    employee = await _resolve_employee(storage, schedule)
    name = html.escape(employee.name) if employee else "сотрудник"
    await callback.answer("Готово")
    if callback.message is not None:
        if remember:
            text = f"✅ Запомнил: <b>{name}</b>. Больше не спрашиваю."
        else:
            text = "🗣 Хорошо, спрошу при следующей загрузке расписания."
        try:
            await callback.message.edit_text(text)
        except TelegramBadRequest:
            await callback.message.answer(text)
        await callback.message.answer(
            "/today — смена сегодня\n"
            "/date 15 — конкретная дата\n"
            "/month — календарь месяца\n"
            "/help — все команды"
        )


@router.message(Command("employee"))
async def cmd_employee(message: Message, storage: Storage) -> None:
    schedule = await storage.get_latest(message.chat.id)
    if schedule is None:
        await message.answer(
            "В этом чате пока нет расписаний.\n"
            "Пришлите скриншот таблицы смен (лучше файлом PNG)."
        )
        return
    await _send_employee_picker(message, storage, schedule)


@router.callback_query(UploadCB.filter(F.action == "forget"))
async def on_forget(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None:
        return
    await storage.set_remember(schedule.chat_id, False)
    await callback.answer("Готово")
    await _safe_edit(
        callback,
        "🗣 Хорошо, при следующей загрузке расписания спрошу сотрудника снова.",
    )


# ---------------------------------------------------------------------------
# Запросы: сегодня, дата, месяц
# ---------------------------------------------------------------------------


@router.message(Command("today"))
async def cmd_today(
    message: Message, storage: Storage, config: Config
) -> None:
    today = _today(config)
    await _show_day(message, storage, config, today)


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

    schedule = await storage.get_active(message.chat.id, year, month)
    if schedule is None:
        latest = await storage.get_latest(message.chat.id)
        await message.answer(no_schedule_text(year, month, latest))
        return
    employee = await _resolve_employee(storage, schedule)
    if employee is None:
        await message.answer(
            f"Для {format_month_ru(schedule.year, schedule.month)} выберите сотрудника."
        )
        await _send_employee_picker(message, storage, schedule)
        return
    await message.answer(
        calendar_text(schedule, employee, schedule.year, schedule.month),
        reply_markup=days_keyboard(schedule, action="day"),
    )


@router.message(Command("months"))
async def cmd_months(message: Message, storage: Storage) -> None:
    schedules = await storage.list_schedules(message.chat.id)
    await message.answer(month_list_text(schedules))


@router.message(Command("delete"))
async def cmd_delete(
    message: Message, storage: Storage, config: Config
) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Укажите месяц: /delete 2026-11 (или /delete 11)."
        )
        return
    try:
        year, month = parse_month_query(parts[1], _today(config))
    except QueryError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    schedule = await storage.get_active(message.chat.id, year, month)
    if schedule is None:
        await message.answer(f"На {format_month_ru(year, month)} расписания нет.")
        return
    await message.answer(
        f"Удалить расписание за <b>{format_month_ru(year, month)}</b>?",
        reply_markup=delete_confirm_keyboard(schedule.id),
    )


@router.callback_query(UploadCB.filter(F.action == "delete_yes"))
async def on_delete_yes(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None:
        return
    await storage.delete_month(schedule.chat_id, schedule.year, schedule.month)
    await callback.answer("Удалено")
    await _safe_edit(
        callback,
        f"🗑 Расписание за <b>{format_month_ru(schedule.year, schedule.month)}</b> удалено.",
    )


@router.callback_query(UploadCB.filter(F.action == "day"))
async def on_day_click(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None:
        return
    employee = await _resolve_employee(storage, schedule)
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
# Редактирование
# ---------------------------------------------------------------------------


@router.callback_query(UploadCB.filter(F.action == "edit"))
async def on_edit(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None:
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
    if schedule is None:
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
    if schedule is None:
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
    if schedule is None:
        return
    employee = schedule.employee(callback_data.pos)
    if employee is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    await state.set_state(Form.rename)
    await state.set_data(
        {
            "sid": schedule.id,
            "pos": employee.position,
            "chat_id": schedule.chat_id,
        }
    )
    await callback.answer()
    if callback.message is not None:
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
    ok = await storage.rename_employee(
        int(data["sid"]), int(data["chat_id"]), int(data["pos"]), name
    )
    if not ok:
        await message.answer("Не удалось изменить имя. Пришлите картинку заново.")
        return
    schedule = await storage.get_schedule(int(data["sid"]), int(data["chat_id"]))
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
    if schedule is None:
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
    if schedule is None:
        return
    employee = schedule.employee(callback_data.pos)
    if employee is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    await callback.answer()
    await _safe_edit(
        callback,
        calendar_text(schedule, employee, schedule.year, schedule.month)
        + "\n\nНажмите день, чтобы изменить смену.",
        reply_markup=days_keyboard(
            schedule,
            action="setday",
            position=employee.position,
            back_action="edit_days",
        ),
    )


@router.callback_query(UploadCB.filter(F.action == "setday"))
async def on_set_day(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None:
        return
    employee = schedule.employee(callback_data.pos)
    if employee is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    await callback.answer()
    try:
        target = date(schedule.year, schedule.month, callback_data.day)
    except ValueError:
        return
    await _safe_edit(
        callback,
        day_status_text(schedule, employee, target)
        + "\n\nЧто поставить в этот день?",
        reply_markup=shift_type_keyboard(
            schedule.id, employee.position, callback_data.day
        ),
    )


@router.callback_query(UploadCB.filter(F.action == "settype"))
async def on_set_type(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None:
        return
    employee = schedule.employee(callback_data.pos)
    if employee is None:
        await callback.answer("Сотрудник не найден", show_alert=True)
        return
    codes = {
        "day": "Д",
        "night": "Н",
        "vacation": "О",
        "off": None,
    }
    shift_type = callback_data.value
    if shift_type not in codes:
        await callback.answer("Неизвестный тип смены", show_alert=True)
        return
    ok = await storage.set_shift(
        schedule.id,
        schedule.chat_id,
        employee.position,
        callback_data.day,
        codes[shift_type],
        shift_type,
    )
    if not ok:
        await callback.answer("Не удалось сохранить", show_alert=True)
        return
    await callback.answer("Сохранено")
    schedule = await storage.get_schedule(schedule.id, schedule.chat_id)
    if schedule is None:
        return
    await _safe_edit(
        callback,
        calendar_text(schedule, employee, schedule.year, schedule.month)
        + "\n\nНажмите день, чтобы изменить смену.",
        reply_markup=days_keyboard(
            schedule,
            action="setday",
            position=employee.position,
            back_action="edit_days",
        ),
    )


@router.callback_query(UploadCB.filter(F.action == "edit_date"))
async def on_edit_date(
    callback: CallbackQuery,
    callback_data: UploadCB,
    storage: Storage,
) -> None:
    schedule = await _load_schedule(callback, callback_data.sid, storage)
    if schedule is None:
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
    if schedule is None:
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
    await storage.update_month(schedule.id, schedule.chat_id, year, month)
    await callback.answer("Сохранено")
    updated = await storage.get_schedule(schedule.id, schedule.chat_id)
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
    if schedule is None:
        return
    await state.set_state(Form.custom_year)
    await state.set_data(
        {"sid": schedule.id, "chat_id": schedule.chat_id}
    )
    await callback.answer()
    if callback.message is not None:
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
    schedule = await storage.get_schedule(int(data["sid"]), int(data["chat_id"]))
    if schedule is None:
        await message.answer("Расписание не найдено.")
        return
    await storage.update_month(
        schedule.id, schedule.chat_id, year, schedule.month
    )
    updated = await storage.get_schedule(schedule.id, schedule.chat_id)
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
