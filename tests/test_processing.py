"""Тесты реестра распознавания."""

from __future__ import annotations

from bot.processing import OcrJob, ProcessingRegistry


async def test_one_job_per_chat_and_cancel() -> None:
    registry = ProcessingRegistry()

    job = await registry.try_start(1)
    assert job is not None
    assert await registry.try_start(1) is None
    assert registry.get(1) is job

    other = await registry.try_start(2)
    assert other is not None
    assert other is not job

    assert registry.cancel(1) is True
    assert job.cancelled is True
    assert registry.cancel(999) is False

    await registry.finish(1, job)
    assert registry.get(1) is None
    # После завершения можно начать новое распознавание.
    assert await registry.try_start(1) is not None


def test_notify_throttle() -> None:
    registry = ProcessingRegistry()
    job = OcrJob(chat_id=1)
    assert registry.should_notify(job, interval=10) is True
    assert registry.should_notify(job, interval=10) is False
