"""Офлайн-утилиты администратора (не команды бота)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

from .storage import Storage


async def _unblock(db_path: str, chat_id: int) -> int:
    storage = Storage(db_path)
    await storage.init()
    try:
        if await storage.is_chat_blocked(chat_id):
            await storage.unblock_chat(chat_id)
            print(f"Чат {chat_id} разблокирован.")
            return 0
        ok = await storage.unblock_chat(chat_id)
        if ok:
            print(f"Чат {chat_id}: счётчик попыток сброшен.")
            return 0
        print(f"Чат {chat_id} не найден в таблице блокировок.")
        return 1
    finally:
        await storage.close()


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Администрирование бота расписаний")
    sub = parser.add_subparsers(dest="command", required=True)

    unblock = sub.add_parser("unblock", help="Разблокировать чат")
    unblock.add_argument("chat_id", type=int)
    unblock.add_argument(
        "--db",
        default=os.getenv("DB_PATH", "data/schedules.db"),
        help="путь к SQLite (по умолчанию DB_PATH)",
    )

    args = parser.parse_args()
    if args.command == "unblock":
        return asyncio.run(_unblock(args.db, args.chat_id))
    return 1


if __name__ == "__main__":
    sys.exit(main())
