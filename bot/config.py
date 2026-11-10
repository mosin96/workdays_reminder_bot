"""Конфигурация приложения из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    bot_token: str
    db_path: Path
    timezone: str = "Europe/Moscow"
    tesseract_cmd: str | None = None
    min_image_width: int = 600
    max_image_mb: int = 20

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = (os.getenv("BOT_TOKEN") or "").strip()
        if not token:
            raise ConfigError(
                "Не задан BOT_TOKEN. Скопируйте .env.example в .env и укажите токен от @BotFather."
            )
        db_path = Path(os.getenv("DB_PATH", "data/schedules.db")).expanduser()
        tesseract_cmd = (os.getenv("TESSERACT_CMD") or "").strip() or None
        try:
            min_image_width = int(os.getenv("MIN_IMAGE_WIDTH", "600"))
        except ValueError:
            min_image_width = 600
        try:
            max_image_mb = int(os.getenv("MAX_IMAGE_MB", "20"))
        except ValueError:
            max_image_mb = 20
        return cls(
            bot_token=token,
            db_path=db_path,
            timezone=os.getenv("TZ", "Europe/Moscow").strip() or "Europe/Moscow",
            tesseract_cmd=tesseract_cmd,
            min_image_width=min_image_width,
            max_image_mb=max_image_mb,
        )
