"""Хранилище: жалобы.

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""

import json

from services.db.connection import (
    _get_conn,
    _lock,
)


def create_complaint(user_id: int, username: str, category: str,
                     screenshot_id: str, comment: str) -> int:
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO complaints (user_id, user_username, category, screenshot_id, comment) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, category, screenshot_id, comment)
        )
        conn.commit()
        return cur.lastrowid or 0

def get_complaints(status: str | None = None) -> list[dict]:
    conn = _get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM complaints WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM complaints ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]

def get_complaint(complaint_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM complaints WHERE id = ?", (complaint_id,)).fetchone()
    return dict(row) if row else None

def set_complaint_status(complaint_id: int, status: str) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE complaints SET status = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, complaint_id)
        )
        conn.commit()
        return cur.rowcount > 0

def complaints_count(status: str | None = None) -> int:
    conn = _get_conn()
    if status:
        row = conn.execute("SELECT COUNT(*) FROM complaints WHERE status = ?", (status,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) FROM complaints").fetchone()
    return row[0]

TICKET_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("tech", "❓ Тех. вопрос"),
    ("complaint", "⚠️ Жалоба"),
    ("review", "⭐ Отзыв"),
    ("other", "📦 Другое"),
)

TICKET_CATEGORY_TITLES: dict[str, str] = dict(TICKET_CATEGORIES)

def create_ticket(user_id: int, category: str, text: str,
                  photos: list[str] | None = None, has_logs: bool = False,
                  username: str = "", first_name: str = "") -> int:
    """Создаёт тикет и возвращает его ID."""
    conn = _get_conn()
    with _lock:
        cursor = conn.execute(
            "INSERT INTO tickets (user_id, username, first_name, category, text, "
            "photos, has_logs, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (user_id, username or "", first_name or "", category or "other",
             text or "", json.dumps(photos or [], ensure_ascii=False),
             1 if has_logs else 0),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)

def get_ticket(ticket_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return dict(row) if row else None

def ticket_photos(ticket: dict) -> list[str]:
    """Фото тикета (могут прийти как file_id — их бот видит в своей БД)."""
    try:
        data = json.loads(ticket.get("photos") or "[]")
    except (TypeError, ValueError):
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []

def get_user_tickets(user_id: int, status: str = "open") -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tickets WHERE user_id = ? AND status = ? ORDER BY id DESC LIMIT 50",
        (user_id, status),
    ).fetchall()
    return [dict(r) for r in rows]

def get_all_tickets(status: str = "open", limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tickets WHERE status = ? ORDER BY id DESC LIMIT ?",
        (status, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]

def close_ticket(ticket_id: int, answer: str = "") -> bool:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE tickets SET status = 'closed', answer = ?, "
            "closed_at = datetime('now') WHERE id = ?",
            (answer or "", ticket_id),
        )
        conn.commit()
    return True

def ticket_counts() -> dict[str, int]:
    conn = _get_conn()
    rows = conn.execute("SELECT status, COUNT(*) FROM tickets GROUP BY status").fetchall()
    counts = {"open": 0, "closed": 0}
    for status, total in rows:
        counts[str(status)] = int(total)
    return counts
