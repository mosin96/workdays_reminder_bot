"""Сценарные тесты обработчиков без обращения к Telegram API."""

from __future__ import annotations

from pathlib import Path
from datetime import date
from types import SimpleNamespace

import pytest

from bot import handlers as h
from bot.callbacks import EmployeeCB, RememberCB, UploadCB
from bot.config import Config
from bot.storage import Storage


class FakeMessage:
    def __init__(self, chat_id: int = 555) -> None:
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.sent: list[tuple[str, object]] = []

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


async def _draft(storage: Storage) -> int:
    return await storage.save_draft(
        chat_id=555,
        year=2026,
        month=11,
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


async def test_confirm_employee_remember_and_edit(storage: Storage) -> None:
    schedule_id = await _draft(storage)
    callback = FakeCallback()

    await h.on_confirm(
        callback, UploadCB(action="confirm", sid=schedule_id), storage
    )
    schedule = await storage.get_active(555, 2026, 11)
    assert schedule is not None
    assert schedule.status == "active"
    assert "Для кого" in callback.message.last_text

    await h.on_employee_selected(
        callback, EmployeeCB(sid=schedule_id, pos=1), storage
    )
    settings = await storage.get_settings(555)
    assert settings is not None
    assert settings.employee_name == "Петров Пётр"
    assert settings.remember is False
    assert "Запомнить выбор" in callback.message.last_text

    await h.on_remember(
        callback, RememberCB(sid=schedule_id, remember=1), storage
    )
    settings = await storage.get_settings(555)
    assert settings.remember is True

    await h.on_set_type(
        callback,
        UploadCB(
            action="settype",
            sid=schedule_id,
            pos=1,
            day=2,
            value="night",
        ),
        storage,
    )
    schedule = await storage.get_schedule(schedule_id, 555)
    assert schedule is not None
    assert schedule.shift(1, 2).type == "night"

    await h.on_forget(
        callback, UploadCB(action="forget", sid=schedule_id), storage
    )
    settings = await storage.get_settings(555)
    assert settings.remember is False


async def test_day_click_returns_status(storage: Storage) -> None:
    schedule_id = await _draft(storage)
    await storage.activate(schedule_id, 555)
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
    schedule_id = await _draft(storage)
    await storage.activate(schedule_id, 555)
    await storage.set_employee(555, "Петров Пётр")

    config = Config(bot_token="test", db_path=storage.path)

    message = FakeMessage()
    await h._show_day(message, storage, config, date(2026, 11, 1))
    assert "Петров Пётр" in message.last_text

    message = FakeMessage()
    message.text = "/date 15.12"
    await h.cmd_date(message, storage, config)
    assert "расписания нет" in message.last_text
