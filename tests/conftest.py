"""Общие фикстуры и маркеры тестов."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def tesseract_binary() -> str | None:
    return os.getenv("TESSERACT_CMD") or shutil.which("tesseract")


def tesseract_has_russian() -> bool:
    binary = tesseract_binary()
    if not binary:
        return False
    try:
        completed = subprocess.run(
            [binary, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and "rus" in completed.stdout


requires_tesseract = pytest.mark.skipif(
    not tesseract_has_russian(),
    reason="Tesseract с языком rus не установлен",
)


@pytest.fixture()
def sample_image() -> Path:
    path = ROOT / "img.png"
    if not path.exists():
        pytest.skip("локальный img.png не найден (он не хранится в репозитории)")
    return path
