"""Сценарные тесты обработчиков без обращения к Telegram API."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import handlers as h
from bot.callbacks import (
    DeleteWorkspaceCB,
    EmployeeCB,
    RememberCB,
    ReminderAckCB,
    ReminderCB,
    ResetCB,
    UploadCB,
)
from bot.config import Config
from bot.ocr.pipeline import OcrCell, OcrEmployee, OcrResult
from bot.processing import ProcessingRegistry
from bot.storage import Storage


class FakeState:
    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def clear(self) -> None:
        self.state = None
        self.data = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def get_state(self):
        return self.state

    async def set_data(self, data: dict) -> None:
        self.data = data

    async def get_data(self) -> dict:
        return self.data


class FakeMessage:
    def __init__(self, chat_id: int = 555) -> None:
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.sent: list[tuple[str, object]] = []
        self.text = ""

    async def answer(self, text: str, reply_markup=None):
        self.sent.append((text, reply_markup))
        return self

    async def edit_text(self, text: str, reply_markup=None):
        self.sent.append((text, reply_markup))
        return self

    async def edit_reply_markup(self, reply_markup=None):
        return self

    async def delete(self):
        return True

    @property
    def last_text(self) -> str:
        return self.sent[-1][0]


class FakeCallback:
    def __init__(self, chat_id: int = 555) -> None:
        self.message = FakeMessage(chat_id)
        self.from_user = SimpleNamespace(id=1)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False):
        self.answers.append((text, show_alert))


class FakeBot:
    async def get_file(self, file_id: str):
        return SimpleNamespace(file_path="photos/test.jpg")

    async def download_file(self, file_path: str, destination):
        Path(destination).write_bytes(b"fake-image")


@pytest.fixture()
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "flow.db")
    await instance.init()
    yield instance
    await instance.close()


async def _draft(
    storage: Storage,
    owner_chat_id: int = 555,
    year: int = 2026,
    month: int = 11,
):
    group = await storage.create_group(owner_chat_id)
    await storage.subscribe(owner_chat_id, group.id)
    schedule_id = await storage.save_draft(
        group_id=group.id,
        year=year,
        month=month,
        employee_names=["Иванов Иван", "Петров Пётр"],
        days=[1, 2, 3],
        weekend={1: True, 2: False, 3: False},
        cells={
            (0, 1): ("Н", "night", 90),
            (1, 1): ("Д", "day", 90),
            (0, 2): (None, "off", 0),
            (1, 2): ("Д", "day", 90),
            (0, 3): ("О", "vacation", 0),
            (1, 3): (None, "off", 0),
        },
    )
    return group, schedule_id


async def test_confirm_employee_remember_and_edit(storage: Storage) -> None:
    group, schedule_id = await _draft(storage)
    callback = FakeCallback()

    await h.on_confirm(
        callback, UploadCB(action="confirm", sid=schedule_id), storage
    )
    schedule = await storage.get_active(group.id, 2026, 11)
    assert schedule is not None
    assert schedule.status == "active"
    texts = "\n".join(text for text, _ in callback.message.sent)
    assert "Код расписания" in texts

    callback.message.sent.clear()
    await h.on_employee_selected(
        callback, EmployeeCB(sid=schedule_id, pos=1), storage
    )
    subscription = await storage.get_subscription(555)
    assert subscription is not None
    assert subscription.employee_name == "Петров Пётр"
    assert subscription.remember is False
    assert "Запомнить выбор" in callback.message.last_text

    await h.on_remember(
        callback, RememberCB(sid=schedule_id, remember=1), storage
    )
    subscription = await storage.get_subscription(555)
    assert subscription.remember is True

    await h.on_set_type(
        callback,
        UploadCB(
            action="settype",
            sid=schedule_id,
            pos=1,
            day=2,
            value="night|quick",
        ),
        storage,
    )
    schedule = await storage.get_schedule(schedule_id)
    assert schedule is not None
    assert schedule.shift(1, 2).type == "night"

    await h.on_forget(
        callback, UploadCB(action="forget", sid=schedule_id), storage
    )
    subscription = await storage.get_subscription(555)
    assert subscription.remember is False


async def test_share_and_unsubscribe(storage: Storage) -> None:
    group, _ = await _draft(storage)
    message = FakeMessage()
    await h.cmd_share(message, storage)
    assert group.code[:4] in message.last_text

    await storage.subscribe(777, group.id)
    subscriber = FakeMessage(777)
    await h.cmd_unsubscribe(subscriber, storage)
    assert await storage.get_subscription(777) is None

    owner = FakeMessage()
    await h.cmd_unsubscribe(owner, storage)
    assert "создатель" in owner.last_text
    assert await storage.get_subscription(555) is not None


async def test_subscriber_cannot_edit_or_upload(storage: Storage) -> None:
    group, schedule_id = await _draft(storage)
    await storage.activate(schedule_id, group.id)
    await storage.subscribe(777, group.id)

    callback = FakeCallback(777)
    await h.on_set_type(
        callback,
        UploadCB(
            action="settype",
            sid=schedule_id,
            pos=1,
            day=2,
            value="night|quick",
        ),
        storage,
    )
    schedule = await storage.get_schedule(schedule_id)
    assert schedule.shift(1, 2).type == "day"
    assert "создатель" in callback.answers[-1][0]

    message = FakeMessage(777)
    message.text = "/edit 2026-11"
    config = Config(bot_token="test", db_path=storage.path)
    await h.cmd_edit(message, storage, config)
    assert "только создатель" in message.last_text


async def test_day_click_returns_status(storage: Storage) -> None:
    group, schedule_id = await _draft(storage)
    await storage.activate(schedule_id, group.id)
    await storage.set_employee(555, "Петров Пётр")

    callback = FakeCallback()
    await h.on_day_click(
        callback,
        UploadCB(action="day", sid=schedule_id, day=3),
        storage,
    )
    assert callback.answers
    assert "Выходной" in callback.answers[-1][0]
    assert callback.answers[-1][1] is True


async def test_today_and_date_commands(storage: Storage) -> None:
    group, schedule_id = await _draft(storage)
    await storage.activate(schedule_id, group.id)
    await storage.set_employee(555, "Петров Пётр")

    config = Config(bot_token="test", db_path=storage.path)

    message = FakeMessage()
    await h._show_day(message, storage, config, date(2026, 11, 1))
    assert "Петров Пётр" in message.last_text
    assert message.sent[-1][1] is not None

    message = FakeMessage()
    message.text = "/date 15.12"
    await h.cmd_date(message, storage, config)
    assert "расписания нет" in message.last_text


async def test_month_shows_colored_day_buttons(storage: Storage) -> None:
    group, schedule_id = await _draft(storage)
    await storage.activate(schedule_id, group.id)
    await storage.set_employee(555, "Петров Пётр")
    config = Config(bot_token="test", db_path=storage.path)

    message = FakeMessage()
    message.text = "/month 2026-11"
    await h.cmd_month(message, storage, config)
    assert "Нажмите день" in message.last_text
    keyboard = message.sent[-1][1]
    assert keyboard is not None
    labels = [
        button.text for row in keyboard.inline_keyboard for button in row
    ]
    assert "1🟩" in labels
    assert "2🟩" in labels
    assert "3⬜" in labels


async def test_edit_current_month_for_selected_employee(
    storage: Storage,
) -> None:
    group, schedule_id = await _draft(storage)
    await storage.activate(schedule_id, group.id)
    await storage.set_employee(555, "Петров Пётр")
    config = Config(bot_token="test", db_path=storage.path)

    message = FakeMessage()
    message.text = "/edit 2026-11"
    await h.cmd_edit(message, storage, config)
    assert "Нажмите день" in message.last_text
    keyboard = message.sent[-1][1]
    assert keyboard is not None
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert any("setday" in value for value in callbacks)

    callback = FakeCallback()
    await h.on_set_day(
        callback,
        UploadCB(
            action="setday", sid=schedule_id, pos=1, day=2, value="quick"
        ),
        storage,
    )
    assert "Что поставить" in callback.message.last_text

    await h.on_set_type(
        callback,
        UploadCB(
            action="settype",
            sid=schedule_id,
            pos=1,
            day=2,
            value="night|quick",
        ),
        storage,
    )
    schedule = await storage.get_schedule(schedule_id)
    assert schedule is not None
    assert schedule.shift(1, 2).type == "night"

    await h.on_edit_done(
        callback,
        UploadCB(action="edit_done", sid=schedule_id, pos=1),
        storage,
    )
    assert "Изменения сохранены" in callback.message.last_text


async def test_subscribe_flow_and_bruteforce(storage: Storage) -> None:
    group, _ = await _draft(storage)
    state = FakeState()

    message = FakeMessage(700)
    await h._subscribe_by_code(message, storage, state, group.code)
    subscription = await storage.get_subscription(700)
    assert subscription is not None
    assert subscription.group_id == group.id
    texts = "\n".join(text for text, _ in message.sent).lower()
    assert "подписались" in texts

    # Неверный код 10 раз: ещё не блокировка, 11-й — блокировка.
    state2 = FakeState()
    message2 = FakeMessage(701)
    for attempt in range(1, 12):
        await h._subscribe_by_code(message2, storage, state2, "ZZZZ-ZZZZ")
        if attempt <= 10:
            assert await storage.is_chat_blocked(701) is False
        else:
            assert await storage.is_chat_blocked(701) is True
    assert "заблокирован" in message2.last_text.lower()


async def test_switch_subscription_requires_pending_state(
    storage: Storage,
) -> None:
    first = await storage.create_group(owner_chat_id=1)
    second = await storage.create_group(owner_chat_id=2)
    await storage.subscribe(3, first.id)

    message = FakeMessage(3)
    state = FakeState()
    await h._subscribe_by_code(message, storage, state, second.code)
    subscription = await storage.get_subscription(3)
    assert subscription.group_id == first.id
    assert state.data.get("pending_group_id") == second.id

    callback = FakeCallback(3)
    await h.on_switch_subscription(callback, state, storage)
    subscription = await storage.get_subscription(3)
    assert subscription.group_id == second.id

    forged = FakeCallback(3)
    await h.on_switch_subscription(forged, FakeState(), storage)
    assert "устарела" in forged.answers[-1][0].lower()


async def test_today_handover_across_month(
    storage: Storage, monkeypatch
) -> None:
    group = await storage.create_group(555)
    await storage.subscribe(555, group.id)
    november = await storage.save_draft(
        group_id=group.id,
        year=2026,
        month=11,
        employee_names=["Ночной Сотрудник", "Дневной Сотрудник"],
        days=[30],
        weekend={30: False},
        cells={
            (0, 30): ("Н", "night", 90),
            (1, 30): ("Д", "day", 90),
        },
    )
    await storage.activate(november, group.id)
    await storage.set_employee(555, "Ночной Сотрудник")
    config = Config(bot_token="test", db_path=storage.path)
    monkeypatch.setattr(h, "_today", lambda cfg: date(2026, 11, 30))

    message = FakeMessage()
    await h.cmd_today(message, storage, config)
    assert "Следующая смена" in message.last_text
    assert "Загрузите фото" in message.last_text

    december = await storage.save_draft(
        group_id=group.id,
        year=2026,
        month=12,
        employee_names=["Декабрьский Сотрудник"],
        days=[1],
        weekend={1: False},
        cells={(0, 1): ("Д", "day", 90)},
    )
    await storage.activate(december, group.id)

    message = FakeMessage()
    await h.cmd_today(message, storage, config)
    assert "принимает: Декабрьский Сотрудник" in message.last_text
    assert "Загрузите фото" not in message.last_text


async def test_upload_completes_month_with_unknown_days(
    storage: Storage, monkeypatch
) -> None:
    def fake_parse(path, tesseract_cmd, cancel_event):
        return OcrResult(
            month=11,
            year=2026,
            employees=[
                OcrEmployee(0, "Иванов Иван"),
                OcrEmployee(1, "Петров Пётр"),
            ],
            days=[1, 2, 3],
            cells={
                (0, 1): OcrCell(1, 0, "Н", "night", 90),
                (1, 1): OcrCell(1, 1, "Д", "day", 90),
                (0, 2): OcrCell(2, 0, None, "off", 0),
                (1, 2): OcrCell(2, 1, "Д", "day", 90),
                (0, 3): OcrCell(3, 0, "О", "vacation", 0),
                (1, 3): OcrCell(3, 1, None, "off", 0),
            },
            weekend={1: True, 2: False, 3: False},
            counts={},
        )

    monkeypatch.setattr(h, "parse_schedule_image", fake_parse)
    message = FakeMessage()
    message.photo = [
        SimpleNamespace(file_id="photo", file_size=10, width=872)
    ]
    message.document = None
    config = Config(bot_token="test", db_path=storage.path)

    await h._process_image(
        message,
        FakeBot(),
        config,
        storage,
        ProcessingRegistry(),
        document=False,
    )

    subscription = await storage.get_subscription(555)
    assert subscription is not None
    draft = await storage.get_latest(subscription.group_id, status="draft")
    assert draft is not None
    assert sorted(draft.days) == list(range(1, 31))
    assert draft.shift(0, 15).type == "unknown"
    assert draft.shift(1, 15).type == "unknown"
    assert "На скриншоте нет дней: 4–30" in message.last_text
    assert "❔" in message.last_text


async def test_short_day_and_overtime_editing(storage: Storage) -> None:
    group, schedule_id = await _draft(storage)
    await storage.activate(schedule_id, group.id)
    await storage.set_employee(555, "Петров Пётр")
    callback = FakeCallback()

    await h.on_set_type(
        callback,
        UploadCB(
            action="settype",
            sid=schedule_id,
            pos=1,
            day=2,
            value="short_day|menu",
        ),
        storage,
    )
    schedule = await storage.get_schedule(schedule_id)
    assert schedule.shift(1, 2).type == "short_day"
    assert schedule.shift(1, 2).code == "ДД"
    assert schedule.shift(1, 2).overtime is False

    await h.on_toggle_overtime(
        callback,
        UploadCB(
            action="toggle_overtime",
            sid=schedule_id,
            pos=1,
            day=2,
            value="menu",
        ),
        storage,
    )
    schedule = await storage.get_schedule(schedule_id)
    assert schedule.shift(1, 2).overtime is True
    assert schedule.shift(1, 2).code == "ДД+П"

    await h.on_toggle_overtime(
        callback,
        UploadCB(
            action="toggle_overtime",
            sid=schedule_id,
            pos=1,
            day=2,
            value="menu",
        ),
        storage,
    )
    schedule = await storage.get_schedule(schedule_id)
    assert schedule.shift(1, 2).overtime is False
    assert schedule.shift(1, 2).code == "ДД"


async def test_delete_workspace_only_owner(storage: Storage) -> None:
    group, _ = await _draft(storage)
    await storage.subscribe(777, group.id)

    subscriber = FakeCallback(777)
    await h.on_delete_workspace(
        subscriber,
        DeleteWorkspaceCB(group_id=group.id, confirm=1),
        storage,
    )
    assert await storage.get_group(group.id) is not None
    assert "создатель" in subscriber.answers[-1][0].lower()

    owner = FakeCallback(555)
    await h.on_delete_workspace(
        owner,
        DeleteWorkspaceCB(group_id=group.id, confirm=1),
        storage,
    )
    assert await storage.get_group(group.id) is None
    assert await storage.get_subscription(555) is None
    assert await storage.get_subscription(777) is None


async def test_reset_menu_owner_employee_and_month(
    storage: Storage, monkeypatch
) -> None:
    monkeypatch.setattr(h, "_today", lambda cfg: date(2026, 11, 10))
    group, schedule_id = await _draft(storage)
    await storage.activate(schedule_id, group.id)
    await storage.set_employee(555, "Петров Пётр")
    await storage.set_remember(555, True)
    config = Config(bot_token="test", db_path=storage.path)

    message = FakeMessage()
    await h.cmd_reset(message, storage, config)
    assert "Что сбросить" in message.last_text
    keyboard = message.sent[-1][1]
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert any("reset:employee" in value for value in callbacks)
    assert any("reset:reminders" in value for value in callbacks)
    assert any("reset:delete_month" in value for value in callbacks)
    assert any("reset:delete_workspace" in value for value in callbacks)
    assert not any("reset:unsubscribe" in value for value in callbacks)

    callback = FakeCallback()
    state = FakeState()
    await h.on_reset_action(
        callback, ResetCB(action="reminders"), state, storage, config
    )
    assert "Напоминания о сменах" in callback.message.last_text

    callback = FakeCallback()
    state = FakeState()
    await h.on_reset_action(
        callback, ResetCB(action="employee"), state, storage, config
    )
    subscription = await storage.get_subscription(555)
    assert subscription.employee_name is None
    assert subscription.remember is False
    assert "сброшены" in callback.message.last_text

    await h.on_reset_action(
        callback, ResetCB(action="delete_month"), state, storage, config
    )
    assert "Удалить расписание за" in callback.message.last_text
    await h.on_reset_action(
        callback,
        ResetCB(action="delete_month_confirm"),
        state,
        storage,
        config,
    )
    assert await storage.get_active(group.id, 2026, 11) is None
    assert "удалено у всех" in callback.message.last_text


async def test_reset_state_and_subscriber_actions(
    storage: Storage, monkeypatch
) -> None:
    monkeypatch.setattr(h, "_today", lambda cfg: date(2026, 11, 10))
    group, schedule_id = await _draft(storage)
    await storage.activate(schedule_id, group.id)
    await storage.subscribe(777, group.id)
    config = Config(bot_token="test", db_path=storage.path)

    state = FakeState()
    state.state = "waiting"
    callback = FakeCallback(777)
    await h.on_reset_action(
        callback, ResetCB(action="state"), state, storage, config
    )
    assert state.state is None
    assert "ввод сброшен" in callback.message.last_text

    message = FakeMessage(777)
    await h.cmd_reset(message, storage, config)
    callbacks = [
        button.callback_data
        for row in message.sent[-1][1].inline_keyboard
        for button in row
    ]
    assert any("reset:unsubscribe" in value for value in callbacks)
    assert not any("reset:delete_workspace" in value for value in callbacks)

    await h.on_reset_action(
        callback, ResetCB(action="unsubscribe"), state, storage, config
    )
    assert "Отписаться" in callback.message.last_text
    await h.on_reset_action(
        callback,
        ResetCB(action="unsubscribe_confirm"),
        state,
        storage,
        config,
    )
    assert await storage.get_subscription(777) is None

    forged = FakeCallback(777)
    await h.on_reset_action(
        forged,
        ResetCB(action="delete_workspace_confirm"),
        state,
        storage,
        config,
    )
    assert await storage.get_group(group.id) is not None
    assert forged.answers[-1][1] is True


async def test_reminders_command_and_settings(storage: Storage) -> None:
    group, schedule_id = await _draft(storage)
    await storage.activate(schedule_id, group.id)
    await storage.set_employee(555, "Петров Пётр")

    owner = FakeMessage()
    await h.cmd_reminders(owner, storage)
    assert "Напоминания о сменах" in owner.last_text
    settings = await storage.get_reminder_settings(555)
    assert settings is not None
    assert settings.mode == "day_before"
    assert settings.enabled is False

    await storage.subscribe(777, group.id)
    subscriber = FakeMessage(777)
    await h.cmd_reminders(subscriber, storage)
    assert "только создатель" in subscriber.last_text

    config = Config(bot_token="test", db_path=storage.path)
    state = FakeState()
    callback = FakeCallback()

    await h.on_reminder_action(
        callback,
        ReminderCB(action="mode", value="same_day"),
        state,
        storage,
        config,
    )
    settings = await storage.get_reminder_settings(555)
    assert settings is not None
    assert settings.mode == "same_day"

    await h.on_reminder_action(
        callback,
        ReminderCB(action="type", value="night"),
        state,
        storage,
        config,
    )
    settings = await storage.get_reminder_settings(555)
    assert settings is not None
    assert "night" not in settings.shift_types

    await h.on_reminder_action(
        callback,
        ReminderCB(action="time"),
        state,
        storage,
        config,
    )
    assert "Выберите время" in callback.message.last_text

    await h.on_reminder_action(
        callback,
        ReminderCB(action="settime", value="21-00"),
        state,
        storage,
        config,
    )
    settings = await storage.get_reminder_settings(555)
    assert settings is not None
    assert settings.time == "21:00"

    await h.on_reminder_action(
        callback,
        ReminderCB(action="toggle"),
        state,
        storage,
        config,
    )
    settings = await storage.get_reminder_settings(555)
    assert settings is not None
    assert settings.enabled is True


async def test_reminder_custom_time_and_ack(storage: Storage) -> None:
    group, schedule_id = await _draft(storage)
    await storage.activate(schedule_id, group.id)
    await storage.set_employee(555, "Петров Пётр")
    await storage.save_reminder_settings(
        chat_id=555,
        group_id=group.id,
        mode="same_day",
        time="20:00",
        shift_types=("day",),
        enabled=True,
    )

    state = FakeState()
    state.data = {"group_id": group.id}
    message = FakeMessage()
    message.text = "25:00"
    await h.on_reminder_time_text(message, state, storage)
    assert "⚠️" in message.last_text
    settings = await storage.get_reminder_settings(555)
    assert settings is not None
    assert settings.time == "20:00"

    message.text = "9:05"
    await h.on_reminder_time_text(message, state, storage)
    settings = await storage.get_reminder_settings(555)
    assert settings is not None
    assert settings.time == "09:05"

    delivery = await storage.create_reminder_delivery(
        chat_id=555,
        group_id=group.id,
        employee_position=1,
        shift_date=date(2026, 11, 12),
        shift_type="day",
        mode="same_day",
        due_at=datetime(2026, 11, 12, 9, 5, tzinfo=timezone.utc),
    )
    callback = FakeCallback()
    callback.message.text = "🔔 Напоминание о смене"
    await h.on_reminder_ack(
        callback, ReminderAckCB(id=delivery.id), storage
    )
    delivery = await storage.get_reminder_delivery_by_id(delivery.id)
    assert delivery is not None
    assert delivery.acked_at is not None
    assert "Напоминание получено" in callback.message.last_text
