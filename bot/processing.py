"""Реестр задач распознавания: один OCR на чат + отмена."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from threading import Event


@dataclass
class OcrJob:
    chat_id: int
    cancel_event: Event = field(default_factory=Event)
    started_at: float = field(default_factory=time.monotonic)
    last_notice: float = 0.0

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class ProcessingRegistry:
    """Хранит активные распознавания по chat_id."""

    def __init__(self) -> None:
        self._jobs: dict[int, OcrJob] = {}
        self._lock = asyncio.Lock()

    async def try_start(self, chat_id: int) -> OcrJob | None:
        async with self._lock:
            if chat_id in self._jobs:
                return None
            job = OcrJob(chat_id=chat_id)
            self._jobs[chat_id] = job
            return job

    def get(self, chat_id: int) -> OcrJob | None:
        return self._jobs.get(chat_id)

    async def finish(self, chat_id: int, job: OcrJob) -> None:
        async with self._lock:
            if self._jobs.get(chat_id) is job:
                self._jobs.pop(chat_id, None)

    def cancel(self, chat_id: int) -> bool:
        job = self._jobs.get(chat_id)
        if job is None:
            return False
        job.cancel()
        return True

    def should_notify(self, job: OcrJob, interval: float = 2.0) -> bool:
        now = time.monotonic()
        if now - job.last_notice < interval:
            return False
        job.last_notice = now
        return True
