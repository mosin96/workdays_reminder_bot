"""OCR-конвейер: картинка таблицы смен -> структурированные данные."""

from __future__ import annotations

import logging
import os
import re
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytesseract
from PIL import Image

log = logging.getLogger(__name__)

# Типы смен.
DAY = "day"
NIGHT = "night"
SHORT_DAY = "short_day"
OFF = "off"
VACATION = "vacation"
UNKNOWN = "unknown"

MONTH_NAMES = [
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
]

# Сколько потоков использовать для распознавания ячеек (каждый вызов
# pytesseract запускает отдельный процесс tesseract).
_WORKERS = max(1, min(8, (os.cpu_count() or 4)))

# Варианты предобработки одной ячейки. Единый порог работает не для всех
# ячеек из-за ClearType-сглаживания и разного цвета текста, поэтому берём
# несколько вариантов и оставляем самый уверенный результат.
_VARIANTS = (
    ("gray", cv2.INTER_LANCZOS4, 6, "rel", 0.35, 0),
    ("gray", cv2.INTER_LANCZOS4, 6, "rel", 0.45, 0),
    ("gray", cv2.INTER_LANCZOS4, 6, "fix", 120, 0),
    ("gray", cv2.INTER_LANCZOS4, 6, "fix", 150, 0),
    ("min", cv2.INTER_LANCZOS4, 8, "rel", 0.45, 0),
    ("min", cv2.INTER_LANCZOS4, 6, "fix", 140, 0),
    ("R", cv2.INTER_LANCZOS4, 4, "rel", 0.45, 0),
    ("G", cv2.INTER_LANCZOS4, 4, "rel", 0.35, 3),
    ("gray", cv2.INTER_CUBIC, 4, "fix", 120, 0),
    ("gray", cv2.INTER_LANCZOS4, 8, "rel", 0.35, 0),
    # запасные варианты для «капризных» ячеек с ClearType
    ("gray", cv2.INTER_LANCZOS4, 4, "rel", 0.35, 3),
    ("gray", cv2.INTER_LANCZOS4, 4, "rel", 0.45, 3),
    ("max", cv2.INTER_LANCZOS4, 4, "fix", 120, 3),
    ("B", cv2.INTER_LANCZOS4, 4, "rel", 0.45, 0),
    ("G", cv2.INTER_LANCZOS4, 4, "rel", 0.45, 0),
    ("gray", cv2.INTER_NEAREST, 8, "fix", 140, 0),
    ("min", cv2.INTER_LANCZOS4, 12, "fix", 140, 3),
    ("gray", cv2.INTER_LANCZOS4, 12, "fix", 140, 3),
    ("R", cv2.INTER_NEAREST, 4, "fix", 120, 3),
    ("B", cv2.INTER_LANCZOS4, 6, "fix", 120, 3),
)

_MONTH_ALIASES = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "мая": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}


class OcrError(Exception):
    """Не удалось распознать таблицу."""


class OcrCancelled(OcrError):
    """Распознавание отменено пользователем."""


def _check_cancel(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OcrCancelled("Распознавание отменено.")


@dataclass
class OcrEmployee:
    position: int
    name: str


@dataclass
class OcrCell:
    day: int
    position: int
    code: str | None
    type: str
    confidence: int = 0
    overtime: bool = False


@dataclass
class OcrResult:
    month: int | None
    year: int | None
    employees: list[OcrEmployee]
    days: list[int]
    cells: dict[tuple[int, int], OcrCell]
    weekend: dict[int, bool]
    counts: dict[int, tuple[int | None, int | None]]
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def shift(self, position: int, day: int) -> OcrCell:
        return self.cells.get((position, day)) or OcrCell(
            day=day, position=position, code=None, type=OFF
        )

    def counts_for_day(self, day: int, shift_type: str) -> int:
        return sum(
            1
            for emp in self.employees
            if self.shift(emp.position, day).type == shift_type
        )

    def totals(self) -> dict[str, int]:
        result = {
            DAY: 0,
            NIGHT: 0,
            SHORT_DAY: 0,
            VACATION: 0,
            OFF: 0,
            UNKNOWN: 0,
            "overtime": 0,
        }
        for cell in self.cells.values():
            result[cell.type] = result.get(cell.type, 0) + 1
            if cell.overtime:
                result["overtime"] += 1
        return result


def complete_month(result: OcrResult, year: int, month: int) -> list[int]:
    """Дополняет результат всеми датами месяца, отсутствующие — unknown."""

    limit = monthrange(year, month)[1]
    existing = set(result.days)
    missing = [day for day in range(1, limit + 1) if day not in existing]
    if not missing:
        return []

    positions = [employee.position for employee in result.employees]
    for day in missing:
        result.days.append(day)
        result.weekend[day] = date(year, month, day).weekday() >= 5
        for position in positions:
            result.cells[(position, day)] = OcrCell(
                day=day,
                position=position,
                code=None,
                type=UNKNOWN,
                confidence=0,
            )
    result.days.sort()
    return missing


# ---------------------------------------------------------------------------
# Работа с изображением
# ---------------------------------------------------------------------------


def _load_image(path: str | Path) -> np.ndarray:
    """Читает картинку в BGR, корректно работает с кириллическими путями."""

    path = str(path)
    try:
        data = np.fromfile(path, dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    except OSError:
        image = None
    if image is None:
        try:
            with Image.open(path) as pil:
                rgb = np.array(pil.convert("RGB"))
            image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        except Exception as exc:  # pragma: no cover - защита от битых файлов
            raise OcrError("Не удалось открыть картинку. Пришлите её как файл PNG/JPG.") from exc
    if image is None or image.size == 0:
        raise OcrError("Не удалось прочитать изображение.")
    return image


def _group_peaks(values: np.ndarray, fraction: float) -> list[int]:
    """Возвращает центры групп соседних пиков проекции."""

    if values.size == 0:
        return []
    maximum = float(values.max())
    if maximum <= 0:
        return []
    indexes = np.where(values > maximum * fraction)[0]
    if indexes.size == 0:
        return []
    groups: list[list[int]] = []
    for index in indexes:
        if groups and index - groups[-1][-1] <= 2:
            groups[-1].append(int(index))
        else:
            groups.append([int(index)])
    centers = [int(round(sum(group) / len(group))) for group in groups]

    # Артефакты JPEG иногда дают «двойную» линию в паре пикселей от настоящей.
    # Настоящие линии сетки отстоят друг от друга на десятки пикселей,
    # поэтому близкие пики безопасно склеиваем.
    merged: list[int] = []
    for center in centers:
        if merged and center - merged[-1] <= 8:
            merged[-1] = int(round((merged[-1] + center) / 2))
        else:
            merged.append(center)
    return merged


def _detect_lines(gray: np.ndarray) -> tuple[list[int], list[int]]:
    """Находит координаты вертикальных и горизонтальных линий сетки."""

    _, binary = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)
    height, width = binary.shape

    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(15, width // 50), 1)
    )
    vertical_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, max(15, height // 50))
    )
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel)
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel)

    xs = _group_peaks(vertical.sum(axis=0), 0.15)
    ys = _group_peaks(horizontal.sum(axis=1), 0.15)
    return xs, ys


def _crop(image: np.ndarray, x0: int, x1: int, y0: int, y1: int, inset: int = 3) -> np.ndarray:
    h, w = image.shape[:2]
    return image[
        max(0, y0 + inset) : min(h, y1 - inset),
        max(0, x0 + inset) : min(w, x1 - inset),
    ]


def _upscale_threshold(
    image: np.ndarray,
    scale: int = 4,
    threshold: int = 150,
    pad: int = 20,
) -> np.ndarray:
    resized = cv2.resize(
        image, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4
    )
    _, binary = cv2.threshold(resized, threshold, 255, cv2.THRESH_BINARY)
    return cv2.copyMakeBorder(
        binary, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255
    )


def _tesseract_text(image: np.ndarray, config: str, lang: str = "rus") -> str:
    try:
        return pytesseract.image_to_string(
            Image.fromarray(image), lang=lang, config=config
        ).strip()
    except pytesseract.TesseractNotFoundError as exc:  # pragma: no cover
        raise OcrError(
            "Tesseract не установлен. Установите его или запустите бота в Docker."
        ) from exc


def _tesseract_best(
    image: np.ndarray, config: str, lang: str = "rus"
) -> tuple[str, int]:
    """Возвращает самый уверенный фрагмент текста и его confidence."""

    try:
        data = pytesseract.image_to_data(
            Image.fromarray(image),
            lang=lang,
            config=config,
            output_type=pytesseract.Output.DICT,
        )
    except pytesseract.TesseractNotFoundError as exc:  # pragma: no cover
        raise OcrError(
            "Tesseract не установлен. Установите его или запустите бота в Docker."
        ) from exc

    best_text = ""
    best_conf = -1
    for text, conf in zip(data.get("text", []), data.get("conf", [])):
        text = (text or "").strip()
        if not text:
            continue
        try:
            value = int(float(conf))
        except (TypeError, ValueError):
            value = -1
        if value > best_conf:
            best_text, best_conf = text, value
    return best_text, best_conf


def _normalize_name(text: str) -> str:
    text = re.sub(r"[^\w\s\-ёЁ]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text, flags=re.UNICODE).strip()
    return text


def _ocr_digits(cell: np.ndarray) -> int | None:
    if cell.size == 0:
        return None
    gray = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY) if cell.ndim == 3 else cell
    candidates: list[tuple[int, int]] = []
    for scale, threshold in ((5, 120), (5, 140), (5, 160), (6, 150)):
        prepared = _upscale_threshold(gray, scale=scale, threshold=threshold)
        text, confidence = _tesseract_best(
            prepared,
            "--psm 10 -c tessedit_char_whitelist=0123456789",
        )
        match = re.search(r"\d{1,2}", text)
        if match:
            candidates.append((int(match.group()), max(confidence, 1)))
    if not candidates:
        return None
    scores: dict[int, int] = {}
    for value, confidence in candidates:
        scores[value] = scores.get(value, 0) + confidence
    return max(scores, key=lambda value: scores[value])


# ---------------------------------------------------------------------------
# Месяц и шапка
# ---------------------------------------------------------------------------


def _save_ratio(left: str, right: str) -> float:
    """Простое сходство строк по общим символам (аналог difflib без импорта)."""

    if not left or not right:
        return 0.0
    common = 0
    right_list = list(right)
    for char in left:
        if char in right_list:
            common += 1
            right_list.remove(char)
    return 2 * common / (len(left) + len(right))


def _match_month(text: str) -> int | None:
    cleaned = re.sub(r"[^а-яё]", "", text.lower(), flags=re.UNICODE)
    if not cleaned:
        return None
    best_month = None
    best_score = 0.0
    for index, name in enumerate(MONTH_NAMES, start=1):
        score = _save_ratio(cleaned, name)
        if cleaned.startswith(name[:3]) and len(cleaned) >= 3:
            score = max(score, 0.85)
        if score > best_score:
            best_month, best_score = index, score
    for prefix, index in _MONTH_ALIASES.items():
        if cleaned.startswith(prefix):
            return index
    return best_month if best_score >= 0.6 else None


def _ocr_month(gray: np.ndarray, left_border: int) -> tuple[int | None, int | None, str]:
    """Распознаёт повёрнутую подпись месяца слева от таблицы."""

    if left_border <= 5:
        return None, None, ""
    strip = gray[:, : max(0, left_border - 2)]
    if strip.size == 0:
        return None, None, ""
    ys, xs = np.where(strip < 120)
    if ys.size == 0:
        return None, None, ""
    y0, y1 = max(0, int(ys.min()) - 8), min(strip.shape[0], int(ys.max()) + 8)
    x0, x1 = max(0, int(xs.min()) - 8), min(strip.shape[1], int(xs.max()) + 8)
    crop = strip[y0:y1, x0:x1]
    if crop.size == 0:
        return None, None, ""

    best: tuple[int | None, int | None, str] = (None, None, "")
    best_ratio = 0.0
    for rotation in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
        rotated = cv2.rotate(crop, rotation)
        resized = cv2.resize(
            rotated, None, fx=3, fy=3, interpolation=cv2.INTER_LANCZOS4
        )
        _, binary = cv2.threshold(resized, 150, 255, cv2.THRESH_BINARY)
        prepared = cv2.copyMakeBorder(
            binary, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255
        )
        text = _tesseract_text(prepared, "--psm 7")
        month = _match_month(text)
        ratio = _save_ratio(
            re.sub(r"[^а-яё]", "", text.lower()), MONTH_NAMES[month - 1]
        ) if month else 0.0
        if month and ratio > best_ratio:
            year_match = re.search(r"20\d{2}", text)
            year = int(year_match.group()) if year_match else None
            best = (month, year, text)
            best_ratio = ratio
        if best[0]:
            break
    return best


def _ocr_employee_names(
    gray: np.ndarray, xs: list[int], y0: int, y1: int, columns: list[int]
) -> list[OcrEmployee]:
    employees: list[OcrEmployee] = []
    for position, column in enumerate(columns):
        cell = _crop(gray, xs[column], xs[column + 1], y0, y1, inset=3)
        if cell.size == 0:
            employees.append(OcrEmployee(position=position, name=""))
            continue
        prepared = _upscale_threshold(cell, scale=4, threshold=150)
        text = _normalize_name(_tesseract_text(prepared, "--psm 6"))
        employees.append(OcrEmployee(position=position, name=text))
    return employees


def _ocr_column_header(
    gray: np.ndarray, xs: list[int], y0: int, y1: int, column: int
) -> str:
    cell = _crop(gray, xs[column], xs[column + 1], y0, y1, inset=3)
    if cell.size == 0:
        return ""
    prepared = _upscale_threshold(cell, scale=4, threshold=150)
    return _normalize_name(_tesseract_text(prepared, "--psm 6"))


# ---------------------------------------------------------------------------
# Цвета и ячейки
# ---------------------------------------------------------------------------


def _has_ink(cell: np.ndarray) -> bool:
    gray = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY) if cell.ndim == 3 else cell
    if gray.size == 0:
        return False
    background = float(np.median(gray))
    return int((gray < background - 60).sum()) >= 15


def _is_yellow(cell: np.ndarray) -> bool:
    if cell.ndim != 3 or cell.size == 0:
        return False
    hsv = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    mask = (
        (hue >= 12)
        & (hue <= 38)
        & (saturation > 110)
        & (value > 150)
    )
    return bool(mask.mean() > 0.3)


def _is_blue(cell: np.ndarray) -> bool:
    if cell.ndim != 3 or cell.size == 0:
        return False
    hsv = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    mask = (
        (hue >= 88)
        & (hue <= 122)
        & (saturation > 30)
        & (saturation < 150)
        & (value > 140)
    )
    return bool(mask.mean() > 0.3)


def _prepare_variant(cell: np.ndarray, variant: tuple) -> np.ndarray | None:
    channel, interpolation, scale, mode, value, blur = variant
    if channel == "gray":
        gray = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY)
    elif channel == "min":
        gray = cell.min(axis=2)
    elif channel == "R":
        gray = cell[:, :, 2]
    elif channel == "G":
        gray = cell[:, :, 1]
    elif channel == "B":
        gray = cell[:, :, 0]
    elif channel == "max":
        gray = cell.max(axis=2)
    else:
        gray = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY)

    resized = cv2.resize(
        gray, None, fx=scale, fy=scale, interpolation=interpolation
    )
    if blur:
        resized = cv2.GaussianBlur(resized, (blur, blur), 0)
    if mode == "rel":
        background = float(np.percentile(resized, 95))
        ink = float(np.percentile(resized, 1))
        if background - ink < 20:
            return None
        threshold = ink + value * (background - ink)
    else:
        threshold = float(value)
    _, binary = cv2.threshold(resized, threshold, 255, cv2.THRESH_BINARY)
    return cv2.copyMakeBorder(
        binary, 25, 25, 25, 25, cv2.BORDER_CONSTANT, value=255
    )


def _ocr_shift(
    cell: np.ndarray, cancel_event: Event | None = None
) -> tuple[str, int]:
    best_text = ""
    best_conf = -1
    for variant in _VARIANTS:
        _check_cancel(cancel_event)
        prepared = _prepare_variant(cell, variant)
        if prepared is None:
            continue
        text, conf = _tesseract_best(
            prepared,
            "--psm 10 -c tessedit_char_whitelist=ДНднМмОоОПп",
        )
        if conf > best_conf:
            best_text, best_conf = text, conf
        if conf >= 85:
            break
    if not best_text:
        # Иногда многосимвольный код (ДД+П, НМ+П) не читается как один символ.
        for variant in _VARIANTS[:4]:
            _check_cancel(cancel_event)
            prepared = _prepare_variant(cell, variant)
            if prepared is None:
                continue
            text, conf = _tesseract_best(
                prepared,
                "--psm 7 -c tessedit_char_whitelist=ДНднМмОоОПп",
            )
            if conf > best_conf:
                best_text, best_conf = text, conf
            if conf >= 60:
                break
    return best_text, best_conf


def _shift_code(raw: str, shift_type: str, overtime: bool) -> str:
    letters = re.sub(r"[^а-яё]", "", raw.lower(), flags=re.UNICODE)
    base = letters.replace("п", "")
    if shift_type == SHORT_DAY and "дд" in base:
        code = "ДД"
    elif shift_type == NIGHT and "нм" in base:
        code = "НМ"
    elif shift_type == NIGHT and "н" in base:
        code = "Н"
    elif shift_type == SHORT_DAY:
        code = "ДД"
    elif shift_type == DAY:
        code = "Д"
    elif shift_type == VACATION:
        code = "О"
    else:
        code = raw.strip().upper()
    if overtime:
        code = code.replace("+П", "") + "+П"
    return code


def _classify_shift(
    text: str, confidence: int
) -> tuple[str | None, str, bool]:
    raw = text.strip()
    letters = re.sub(r"[^а-яё]", "", raw.lower(), flags=re.UNICODE)
    if "отпуск" in letters:
        overtime = "+" in raw
        base = letters
    else:
        overtime = "+" in raw or "п" in letters
        base = letters.replace("п", "")

    if not base:
        if not letters and not raw:
            return None, OFF, False
        return raw.upper() or None, UNKNOWN, overtime

    if "дд" in base:
        shift_type = SHORT_DAY
    elif "н" in base:
        shift_type = NIGHT
    elif "д" in base:
        shift_type = DAY
    elif "о" in base or "отпуск" in base:
        shift_type = VACATION
    else:
        return raw.upper() or None, UNKNOWN, overtime

    return _shift_code(raw, shift_type, overtime), shift_type, overtime


# ---------------------------------------------------------------------------
# Основной конвейер
# ---------------------------------------------------------------------------


def _find_data_start(
    gray: np.ndarray, xs: list[int], ys: list[int]
) -> int:
    """Индекс горизонтальной линии, с которой начинаются строки дней."""

    limit = min(4, len(ys) - 1)
    for index in range(limit):
        cell = _crop(gray, xs[0], xs[1], ys[index], ys[index + 1], inset=3)
        if _ocr_digits(cell) == 1:
            return index
    return 1 if len(ys) > 1 else 0


def _numeric_column(
    gray: np.ndarray, xs: list[int], ys: list[int], column: int, data_start: int
) -> bool:
    total = 0
    hits = 0
    rows = len(ys) - data_start - 1
    for offset in range(min(5, max(0, rows))):
        y0 = ys[data_start + offset]
        y1 = ys[data_start + offset + 1]
        cell = _crop(gray, xs[column], xs[column + 1], y0, y1, inset=3)
        if not _has_ink(cell):
            continue
        total += 1
        if _ocr_digits(cell) is not None:
            hits += 1
    return total > 0 and hits >= max(2, total * 0.6)


def parse_schedule_image(
    path: str | Path,
    tesseract_cmd: str | None = None,
    cancel_event: Event | None = None,
) -> OcrResult:
    """Распознаёт картинку и возвращает структуру расписания."""

    _check_cancel(cancel_event)
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    image = _load_image(path)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    _check_cancel(cancel_event)

    xs, ys = _detect_lines(gray)
    if len(xs) < 4 or len(ys) < 4:
        raise OcrError(
            "Не удалось найти таблицу. Пришлите скриншот целиком, лучше как файл PNG."
        )

    _check_cancel(cancel_event)
    data_start = _find_data_start(gray, xs, ys)
    if len(ys) - data_start - 1 < 5:
        raise OcrError("В таблице не найдено строк с днями месяца.")
    _check_cancel(cancel_event)

    # Месяц и год.
    month, year, month_text = _ocr_month(gray, xs[0])
    _check_cancel(cancel_event)

    # Определяем служебные колонки с контрольными суммами (последние 0-2).
    column_count = len(xs) - 1
    numeric: list[bool] = []
    for column in range(max(1, column_count - 2), column_count):
        _check_cancel(cancel_event)
        numeric.append(_numeric_column(gray, xs, ys, column, data_start))

    counts_columns: list[int] = []
    if len(numeric) >= 2 and numeric[-1] and numeric[-2]:
        counts_columns = [column_count - 2, column_count - 1]
    elif numeric and numeric[-1]:
        counts_columns = [column_count - 1]

    employee_columns = [
        column
        for column in range(1, column_count)
        if column not in counts_columns
    ]
    if not employee_columns:
        raise OcrError("В таблице не найдено ни одной колонки сотрудника.")
    if len(employee_columns) > 40:
        raise OcrError("Слишком много колонок — проверьте, что это таблица смен.")

    header_y0 = ys[0]
    header_y1 = ys[data_start]
    _check_cancel(cancel_event)
    employees = _ocr_employee_names(
        gray, xs, header_y0, header_y1, employee_columns
    )
    _check_cancel(cancel_event)

    # Индексы колонок «день/ночь».
    count_day: int | None = counts_columns[0] if counts_columns else None
    count_night: int | None = counts_columns[1] if len(counts_columns) > 1 else None
    if len(counts_columns) >= 2:
        headers = [
            _ocr_column_header(gray, xs, header_y0, header_y1, column).lower()
            for column in counts_columns
        ]
        night_index = next(
            (i for i, text in enumerate(headers) if "ноч" in text), None
        )
        day_index = next(
            (i for i, text in enumerate(headers) if "дн" in text), None
        )
        if night_index is not None and day_index is not None and night_index != day_index:
            count_day = counts_columns[day_index]
            count_night = counts_columns[night_index]

    days = list(range(1, len(ys) - data_start))
    rows = len(days)

    # Контрольные суммы.
    counts: dict[int, tuple[int | None, int | None]] = {}
    for day in days:
        _check_cancel(cancel_event)
        y0, y1 = ys[data_start + day - 1], ys[data_start + day]
        day_value = night_value = None
        if count_day is not None:
            cell = _crop(gray, xs[count_day], xs[count_day + 1], y0, y1, inset=3)
            day_value = _ocr_digits(cell)
        if count_night is not None:
            cell = _crop(gray, xs[count_night], xs[count_night + 1], y0, y1, inset=3)
            night_value = _ocr_digits(cell)
        counts[day] = (day_value, night_value)

    # Выходные/праздники — по цвету фона в колонке с номером дня.
    weekend: dict[int, bool] = {}
    for day in days:
        _check_cancel(cancel_event)
        y0, y1 = ys[data_start + day - 1], ys[data_start + day]
        cell = _crop(image, xs[0], xs[1], y0, y1, inset=5)
        weekend[day] = _is_blue(cell)

    # Ячейки смен.
    cells: dict[tuple[int, int], OcrCell] = {}
    issues: list[str] = []
    warnings: list[str] = []

    jobs: list[tuple[int, int, np.ndarray]] = []
    for position, column in enumerate(employee_columns):
        for day in days:
            _check_cancel(cancel_event)
            y0, y1 = ys[data_start + day - 1], ys[data_start + day]
            cell = image[y0 + 4 : y1 - 4, xs[column] + 5 : xs[column + 1] - 5]
            if cell.size == 0:
                cells[(position, day)] = OcrCell(
                    day=day, position=position, code=None, type=OFF
                )
                continue
            if _is_yellow(cell):
                cells[(position, day)] = OcrCell(
                    day=day, position=position, code="О", type=VACATION
                )
                continue
            if not _has_ink(cell):
                cells[(position, day)] = OcrCell(
                    day=day, position=position, code=None, type=OFF
                )
                continue
            jobs.append((position, day, cell))

    def _run(job: tuple[int, int, np.ndarray]) -> tuple[int, int, str, int]:
        position, day, cell = job
        _check_cancel(cancel_event)
        text, confidence = _ocr_shift(cell, cancel_event)
        return position, day, text, confidence

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for position, day, text, confidence in pool.map(_run, jobs):
            code, shift_type, overtime = _classify_shift(text, confidence)
            cells[(position, day)] = OcrCell(
                day=day,
                position=position,
                code=code,
                type=shift_type,
                confidence=confidence,
                overtime=overtime,
            )
            if shift_type == UNKNOWN:
                issues.append(
                    f"День {day}, {employees[position].name or 'без имени'}: "
                    f"не удалось распознать код смены «{text}»."
                )
    _check_cancel(cancel_event)

    # Сверка с контрольными суммами.
    for day in days:
        row_cells = [
            cells.get(
                (position, day),
                OcrCell(
                    day=day, position=position, code=None, type=OFF
                ),
            )
            for position in range(len(employees))
        ]
        calculated_day = sum(
            1 for cell in row_cells if cell.type in {DAY, SHORT_DAY}
        )
        calculated_night = sum(1 for cell in row_cells if cell.type == NIGHT)
        stated_day, stated_night = counts.get(day, (None, None))
        mismatch = False
        if count_day is not None and stated_day is not None and stated_day != calculated_day:
            mismatch = True
        if (
            count_night is not None
            and stated_night is not None
            and stated_night != calculated_night
        ):
            mismatch = True
        if mismatch:
            issues.append(
                f"День {day}: распознано {calculated_day} дн./{calculated_night} ноч., "
                f"в таблице {stated_day} дн./{stated_night} ноч."
            )

    if count_day is None:
        warnings.append("Контрольные суммы не найдены — данные не с чем сверить.")
    if month is None:
        warnings.append("Не удалось распознать месяц — проверьте вручную.")
        issues.append("Месяц не распознан.")
    empty_names = [emp for emp in employees if not emp.name]
    if empty_names:
        issues.append(
            f"Не распознаны имена сотрудников: {len(empty_names)}. "
            "Переименуйте их в режиме исправления."
        )
    if len(employees) < 1:
        raise OcrError("Не удалось распознать ни одного сотрудника.")

    # Дни без сотрудников (все ячейки пустые) — нормально, просто предупреждаем,
    # если контрольных сумм нет.
    if not cells:
        raise OcrError("Таблица найдена, но смены не распознаны.")
    _check_cancel(cancel_event)

    log.info(
        "OCR: месяц=%s, сотрудников=%d, дней=%d, проблем=%d",
        month,
        len(employees),
        rows,
        len(issues),
    )
    return OcrResult(
        month=month,
        year=year,
        employees=employees,
        days=days,
        cells=cells,
        weekend=weekend,
        counts=counts,
        issues=issues,
        warnings=warnings,
    )
