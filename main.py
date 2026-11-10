"""Точка входа бота."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.config import Config, ConfigError
from bot.handlers import router
from bot.storage import Storage

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="today", description="Смена сегодня"),
    BotCommand(command="date", description="Смена на дату (например, /date 15.12)"),
    BotCommand(command="month", description="Календарь месяца"),
    BotCommand(command="employee", description="Сменить сотрудника"),
    BotCommand(command="months", description="Загруженные месяцы"),
    BotCommand(command="help", description="Справка"),
]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    storage = Storage(config.db_path)
    await storage.init()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher["storage"] = storage
    dispatcher["config"] = config
    dispatcher.include_router(router)

    try:
        await bot.set_my_commands(COMMANDS)
    except Exception:  # pragma: no cover - сеть/права
        log.warning("Не удалось установить меню команд", exc_info=True)

    log.info("Бот запущен, часовой пояс: %s", config.timezone)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await storage.close()
        await bot.session.close()
        log.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
