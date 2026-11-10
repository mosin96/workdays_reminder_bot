"""Коды общих расписаний."""

from __future__ import annotations

import re
import secrets

CODE_LENGTH = 8
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def format_code(value: str) -> str:
    code = normalize_code(value)
    if len(code) > 4:
        return f"{code[:4]}-{code[4:]}"
    return code


def is_valid_code(value: str) -> bool:
    code = normalize_code(value)
    return len(code) == CODE_LENGTH and all(
        char in CODE_ALPHABET for char in code
    )
