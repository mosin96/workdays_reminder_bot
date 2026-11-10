"""Callback-данные inline-кнопок."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class UploadCB(CallbackData, prefix="up"):
    action: str
    sid: int = 0
    pos: int = 0
    day: int = 0
    value: str = ""


class EmployeeCB(CallbackData, prefix="emp"):
    sid: int
    pos: int


class RememberCB(CallbackData, prefix="rem"):
    sid: int
    remember: int = 0


class CancelCB(CallbackData, prefix="cancel"):
    pass


class StartCB(CallbackData, prefix="start"):
    action: str


class SwitchCB(CallbackData, prefix="switch"):
    pass


class DeleteWorkspaceCB(CallbackData, prefix="delws"):
    group_id: int
    confirm: int = 0
