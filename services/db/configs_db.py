"""Хранилище: конфиги ботов (сохранение/перенос настроек под кодом).

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""

import json
import secrets
import sqlite3

from services.db.connection import (
    _get_conn,
    _lock,
)


def _make_config_code() -> str:
    """Короткий код конфига вида ``YM-7F3A-91C2``."""
    raw = secrets.token_hex(4).upper()
    return f"YM-{raw[:4]}-{raw[4:]}"

def normalize_config_code(raw: str) -> str:
    """Приводит введённый код к каноническому виду ``YM-XXXX-XXXX``."""
    text = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if text.startswith("YM"):
        text = text[2:]
    if len(text) == 8:
        return f"YM-{text[:4]}-{text[4:]}"
    return (raw or "").strip().upper()

def create_bot_config(owner_id: int, bot_id: int, bot_name: str,
                      data: dict) -> str:
    """Сохраняет конфиг бота и возвращает его код."""
    conn = _get_conn()
    payload = json.dumps(data, ensure_ascii=False)
    for _ in range(10):
        code = _make_config_code()
        try:
            with _lock:
                conn.execute(
                    "INSERT INTO bot_configs (code, owner_id, bot_id, bot_name, data) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (code, owner_id, bot_id, bot_name, payload),
                )
                conn.commit()
            return code
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Не удалось создать уникальный код конфига")

def get_bot_config(code: str) -> dict | None:
    """Конфиг по коду (или None). Код можно писать без дефисов/в нижнем регистре."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM bot_configs WHERE code = ?", (normalize_config_code(code),)
    ).fetchone()
    return dict(row) if row else None

def get_user_bot_configs(owner_id: int) -> list[dict]:
    """Все конфиги владельца (новые сверху)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM bot_configs WHERE owner_id = ? ORDER BY created_at DESC",
        (owner_id,),
    ).fetchall()
    return [dict(r) for r in rows]

def get_bot_config_by_bot(bot_id: int) -> dict | None:
    """Последний конфиг, сохранённый именно для этого бота."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM bot_configs WHERE bot_id = ? ORDER BY created_at DESC LIMIT 1",
        (bot_id,),
    ).fetchone()
    return dict(row) if row else None

def update_bot_config(code: str, owner_id: int, bot_id: int, bot_name: str,
                      data: dict) -> bool:
    """Перезаписывает сохранённый конфиг (кнопка «Сохранить» повторно)."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE bot_configs SET owner_id = ?, bot_id = ?, bot_name = ?, data = ? "
            "WHERE code = ?",
            (owner_id, bot_id, bot_name, json.dumps(data, ensure_ascii=False),
             normalize_config_code(code)),
        )
        conn.commit()
        return cur.rowcount > 0

def delete_bot_config(code: str, owner_id: int | None = None) -> bool:
    """Удаляет конфиг («очистить конфиг»)."""
    conn = _get_conn()
    with _lock:
        if owner_id is None:
            cur = conn.execute("DELETE FROM bot_configs WHERE code = ?",
                               (normalize_config_code(code),))
        else:
            cur = conn.execute(
                "DELETE FROM bot_configs WHERE code = ? AND owner_id = ?",
                (normalize_config_code(code), owner_id),
            )
        conn.commit()
        return cur.rowcount > 0
