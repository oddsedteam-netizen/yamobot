"""Хранилище: время работы бота (профиль владельца).

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""

from datetime import datetime

from services.db.connection import (
    _get_conn,
    _lock,
)


DEFAULT_WORK_START = "09:00"

DEFAULT_WORK_END = "21:00"

DEFAULT_WORK_MESSAGE = (
    "Извините, наш бот работает с {start} до {end}! "
    "Многие админы заняты или уже спят, но если кто-то будет свободен — "
    "обязательно вам напишет 🤍"
)

# Колонки work_hours, которые разрешено менять. Имя колонки подставляется в
# SQL напрямую (_save_work_hours), поэтому оно обязано быть из белого списка.
_WORK_HOURS_COLUMNS = {"enabled", "start", "end", "msg_text", "msg_photo", "msg_entities"}

def get_work_hours(owner_id: int) -> dict:
    """Настройки времени работы: включено, начало, конец, текст/фото ответа."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM work_hours WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    if not row:
        return {
            "enabled": 0,
            "start": DEFAULT_WORK_START,
            "end": DEFAULT_WORK_END,
            "msg_text": DEFAULT_WORK_MESSAGE,
            "msg_photo": "",
            "msg_entities": "[]",
        }
    return dict(row)

def set_work_hours_enabled(owner_id: int, enabled: bool) -> None:
    _save_work_hours(owner_id, {"enabled": 1 if enabled else 0})

def set_work_hours_time(owner_id: int, start: str, end: str) -> None:
    _save_work_hours(owner_id, {"start": start, "end": end})

def set_work_hours_message(owner_id: int, text: str, photo: str = "",
                            entities: str = "[]") -> None:
    _save_work_hours(owner_id, {"msg_text": text, "msg_photo": photo, "msg_entities": entities})

def _save_work_hours(owner_id: int, values: dict) -> None:
    """Обновляет только переданные поля записи времени работы."""
    if not values:
        return
    # Имена колонок подставляются в SQL напрямую — принимаем только известные.
    unknown = set(values) - _WORK_HOURS_COLUMNS
    if unknown:
        raise ValueError(f"Недопустимые поля work_hours: {sorted(unknown)!r}")
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO work_hours (owner_id) VALUES (?) "
            "ON CONFLICT(owner_id) DO NOTHING",
            (owner_id,),
        )
        assignments = ", ".join(f"{key} = ?" for key in values)
        conn.execute(
            f"UPDATE work_hours SET {assignments} WHERE owner_id = ?",
            (*values.values(), owner_id),
        )
        conn.commit()

def is_within_work_hours(owner_id: int, now: datetime | None = None) -> bool:
    """Сейчас «рабочее» время бота? Поддерживается интервал через полночь."""
    settings = get_work_hours(owner_id)
    if not settings.get("enabled"):
        return True

    def _parse(value: str) -> tuple[int, int] | None:
        try:
            hh, mm = str(value or "").strip().split(":")
            return int(hh), int(mm)
        except (ValueError, AttributeError):
            return None

    start = _parse(settings.get("start", ""))
    end = _parse(settings.get("end", ""))
    if start is None or end is None:
        return True  # кривые настройки — работаем всегда

    current = (now or datetime.now()).hour * 60 + (now or datetime.now()).minute
    start_min = start[0] * 60 + start[1]
    end_min = end[0] * 60 + end[1]

    if start_min == end_min:
        return True  # круглосуточно
    if start_min < end_min:
        return start_min <= current < end_min
    # Интервал через полночь, например 22:00–08:00.
    return current >= start_min or current < end_min

def get_app_setting(key: str, default: str = "") -> str:
    conn = _get_conn()
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row and row[0] is not None:
        return str(row[0]).strip()
    return default

def set_app_setting(key: str, value: str) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (key, str(value).strip()),
        )
        conn.commit()
