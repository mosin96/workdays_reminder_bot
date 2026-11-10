"""Сценарные тесты обработчиков без обращения к Telegram API."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import handlers as h
from bot.callbacks import (
    DeleteWorkspaceCB,
    EmployeeCB,
    RememberCB,
    UploadCB,
)
from bot.config import Config
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
