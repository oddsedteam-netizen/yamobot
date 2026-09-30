"""Хранилище: админы (глобальные — привязаны ко всем ботам).

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from services.db.connection import (
    _get_conn,
    _lock,
)
from services.db.helpers import _now_utc_str, _owner_bot_ids
import logging

logger = logging.getLogger("services")


def add_admin(owner_id: int, user_id: int, username: str, tag: str) -> bool:
    conn = _get_conn()
    with _lock:
        try:
            conn.execute(
                "INSERT INTO admins (owner_id, user_id, username, tag) VALUES (?, ?, ?, ?)",
                (owner_id, user_id, username, tag)
            )
            conn.execute(
                "INSERT INTO admin_tag_history (admin_user_id, old_tag, new_tag) VALUES (?, ?, ?)",
                (user_id, "", tag)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def remove_admin(owner_id: int, user_id: int) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM admins WHERE owner_id = ? AND user_id = ?", (owner_id, user_id))
        conn.commit()
        return cur.rowcount > 0

def ensure_admin(owner_id: int, user_id: int, username: str = "") -> bool:
    """Гарантирует, что юзер записан админом владельца (тег = username).

    Нужно после передачи прав: новый владелец автоматически становится админом
    со своим тегом, чтобы при взятии ПЗ название топика было админским тегом,
    а не личным именем/юзером. Существующую запись (в т.ч. легаси owner_id=0)
    не перезаписывает.
    """
    if get_admin_by_user_id(owner_id, user_id):
        return True
    existing_tag = ""
    if owner_id != 0:
        legacy = get_admin_by_user_id(0, user_id)
        if legacy and legacy.get("tag"):
            existing_tag = legacy["tag"]
    tag = existing_tag or username or f"id{user_id}"
    conn = _get_conn()
    with _lock:
        try:
            conn.execute(
                "INSERT INTO admins (owner_id, user_id, username, tag) VALUES (?, ?, ?, ?)",
                (owner_id, user_id, username or "", tag),
            )
            conn.execute(
                "INSERT INTO admin_tag_history (admin_user_id, old_tag, new_tag) VALUES (?, ?, ?)",
                (user_id, "", tag),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Запись уже появилась (гонка) — считаем успехом.
            logger.debug(
                "Исключение проглочено",
                exc_info=True,
            )
    return True

def get_admins_all(owner_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM admins WHERE owner_id = ?", (owner_id,)).fetchall()
    return [dict(r) for r in rows]

def get_admin_by_tag(owner_id: int, tag: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM admins WHERE owner_id = ? AND tag = ?", (owner_id, tag)).fetchone()
    if row:
        return dict(row)
    # Легаси-админы, добавленные до появления owner_id, хранятся с owner_id = 0.
    if owner_id != 0:
        row = conn.execute("SELECT * FROM admins WHERE owner_id = 0 AND tag = ?", (tag,)).fetchone()
        if row:
            return dict(row)
    return None

def get_admin_by_user_id(owner_id: int, user_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM admins WHERE owner_id = ? AND user_id = ?", (owner_id, user_id)).fetchone()
    if row:
        return dict(row)
    # Легаси-админы, добавленные до появления owner_id, хранятся с owner_id = 0.
    if owner_id != 0:
        row = conn.execute("SELECT * FROM admins WHERE owner_id = 0 AND user_id = ?", (user_id,)).fetchone()
        if row:
            return dict(row)
    return None

def update_admin_tag(owner_id: int, user_id: int, new_tag: str) -> bool:
    conn = _get_conn()
    with _lock:
        old = conn.execute(
            "SELECT tag FROM admins WHERE owner_id = ? AND user_id = ?", (owner_id, user_id)
        ).fetchone()
        if not old:
            return False
        old_tag = old[0]
        conn.execute(
            "UPDATE admins SET tag = ? WHERE owner_id = ? AND user_id = ?",
            (new_tag, owner_id, user_id)
        )
        conn.execute(
            "INSERT INTO admin_tag_history (admin_user_id, old_tag, new_tag) VALUES (?, ?, ?)",
            (user_id, old_tag, new_tag)
        )
        conn.commit()
        return True

def get_admin_tag_history(owner_id: int, user_id: int) -> list[dict]:
    conn = _get_conn()
    admin = conn.execute(
        "SELECT id FROM admins WHERE owner_id = ? AND user_id = ?", (owner_id, user_id)
    ).fetchone()
    if not admin:
        return []
    rows = conn.execute(
        "SELECT * FROM admin_tag_history WHERE admin_user_id = ? ORDER BY changed_at",
        (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]

def add_admin_message(bot_id: int, admin_user_id: int, direction: str = "out") -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO admin_messages (bot_id, admin_user_id, direction) VALUES (?, ?, ?)",
            (bot_id, admin_user_id, direction)
        )
        conn.commit()

def get_bot_owner(bot_id: int) -> int | None:
    conn = _get_conn()
    row = conn.execute("SELECT owner_id FROM bots WHERE id = ?", (bot_id,)).fetchone()
    return row[0] if row else None


def get_admin_message_stats(owner_id: int, admin_user_id: int) -> dict:
    conn = _get_conn()
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # наивный UTC (как CURRENT_TIMESTAMP)
    day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return {"total": 0, "day": 0, "week": 0, "month": 0}
    placeholders = ",".join("?" for _ in bot_ids)
    params = bot_ids

    def _count(since: str) -> int:
        row = conn.execute(
            f"SELECT COUNT(*) FROM admin_messages WHERE admin_user_id = ? AND created_at >= ? AND bot_id IN ({placeholders})",
            (admin_user_id, since, *params)
        ).fetchone()
        return row[0]

    total = conn.execute(
        f"SELECT COUNT(*) FROM admin_messages WHERE admin_user_id = ? AND bot_id IN ({placeholders})",
        (admin_user_id, *params)
    ).fetchone()[0]

    return {
        "total": total,
        "day": _count(day_ago),
        "week": _count(week_ago),
        "month": _count(month_ago),
    }

def get_admin_active_topics(owner_id: int, admin_user_id: int) -> int:
    conn = _get_conn()
    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return 0
    placeholders = ",".join("?" for _ in bot_ids)
    row = conn.execute(
        f"SELECT COUNT(*) FROM feedback_topics WHERE admin_user_id = ? AND status = 'assigned' AND bot_id IN ({placeholders})",
        (admin_user_id, *bot_ids)
    ).fetchone()
    return row[0]

def get_all_admins_stats(owner_id: int) -> list[dict]:
    admins = get_admins_all(owner_id)
    result = []
    for a in admins:
        stats = get_admin_message_stats(owner_id, a["user_id"])
        topics = get_admin_active_topics(owner_id, a["user_id"])
        result.append({
            "admin": a,
            "stats": stats,
            "active_topics": topics,
        })
    return result

def add_coowner(owner_id: int, coowner_id: int, username: str = "") -> bool:
    conn = _get_conn()
    with _lock:
        try:
            conn.execute(
                "INSERT INTO coowners (owner_id, coowner_id, username) VALUES (?, ?, ?)",
                (owner_id, coowner_id, username)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def remove_coowner(owner_id: int, coowner_id: int) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM coowners WHERE owner_id = ? AND coowner_id = ?", (owner_id, coowner_id))
        conn.commit()
        return cur.rowcount > 0

def get_coowners(owner_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM coowners WHERE owner_id = ?", (owner_id,)).fetchall()
    return [dict(r) for r in rows]

def is_coowner(owner_id: int, user_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id FROM coowners WHERE owner_id = ? AND coowner_id = ?",
        (owner_id, user_id)
    ).fetchone()
    return row is not None

def create_admin_invite(owner_id: int, max_uses: int = 1) -> str:
    """Создаёт токен-приглашение админа на `max_uses` человек (по умолчанию — 1)."""
    token = secrets.token_urlsafe(16)
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO admin_invites (token, owner_id, max_uses, used) VALUES (?, ?, ?, 0)",
            (token, owner_id, max_uses)
        )
        conn.commit()
    return token

def get_admin_invite(token: str) -> dict | None:
    """Возвращает инфо о приглашении: {'owner_id', 'max_uses', 'used'} или None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT owner_id, max_uses, used FROM admin_invites WHERE token = ?", (token,)
    ).fetchone()
    return dict(row) if row else None

def get_admin_invite_owner(token: str) -> int | None:
    """Возвращает владельца приглашения (или None). Совместимость со старым кодом."""
    invite = get_admin_invite(token)
    return invite["owner_id"] if invite else None

def consume_admin_invite(token: str) -> int | None:
    """Использует один «слот» приглашения.

    Возвращает количество оставшихся мест (0 — ссылка исчерпана и удалена),
    либо None, если приглашения не существует.
    """
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT max_uses, used FROM admin_invites WHERE token = ?", (token,)
        ).fetchone()
        if not row:
            return None
        new_used = row["used"] + 1
        if new_used >= row["max_uses"]:
            conn.execute("DELETE FROM admin_invites WHERE token = ?", (token,))
            conn.commit()
            return 0
        conn.execute(
            "UPDATE admin_invites SET used = ? WHERE token = ?", (new_used, token)
        )
        conn.commit()
        return row["max_uses"] - new_used

def get_owner_admin_invites(owner_id: int) -> list[dict]:
    """Все действующие ссылки-приглашения админов владельца (новые сверху)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT token, max_uses, used, created_at FROM admin_invites "
        "WHERE owner_id = ? ORDER BY created_at DESC",
        (owner_id,),
    ).fetchall()
    return [dict(r) for r in rows]

def delete_admin_invite(token: str) -> bool:
    """Аннулирует ссылку-приглашение (после этого она не действует)."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM admin_invites WHERE token = ?", (token,))
        conn.commit()
        return cur.rowcount > 0

def update_admin_invite_uses(token: str, max_uses: int) -> bool:
    """Меняет лимит приглашения (для кнопки «Пересоздать»)."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE admin_invites SET max_uses = ?, used = 0 WHERE token = ?",
            (max(1, int(max_uses)), token),
        )
        conn.commit()
        return cur.rowcount > 0

def set_bot_keyboard(owner_id: int, bot_id: int, buttons: list[dict]) -> bool:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO bot_keyboards (bot_id, owner_id, buttons) VALUES (?, ?, ?)",
            (bot_id, owner_id, json.dumps(buttons, ensure_ascii=False))
        )
        conn.commit()
        return True

def import_users_bulk(bot_id: int, users_list: list[dict]) -> int:
    """
    Массовый импорт пользователей.
    users_list: [{"chat_id": 123, "username": "x", "first_name": "Y"}, ...]
    Возвращает количество добавленных.
    """
    conn = _get_conn()
    added = 0
    with _lock:
        for u in users_list:
            try:
                chat_id = int(u.get("chat_id", 0))
                if not chat_id:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO users (bot_id, chat_id, username, first_name) VALUES (?, ?, ?, ?)",
                    (bot_id, chat_id, u.get("username", ""), u.get("first_name", ""))
                )
                if cur.rowcount > 0:
                    added += 1
            except (ValueError, TypeError):
                continue
        conn.commit()
    return added

def ban_user(bot_id: int, chat_id: int) -> None:
    """Помечает юзера как заблокированного (не будет получать рассылки)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO users (bot_id, chat_id, username, first_name, blocked) VALUES (?, ?, '', '', 1)",
            (bot_id, chat_id)
        )
        conn.execute(
            "UPDATE users SET blocked = 1 WHERE bot_id = ? AND chat_id = ?",
            (bot_id, chat_id)
        )
        conn.commit()

def is_user_banned(bot_id: int, chat_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT blocked FROM users WHERE bot_id = ? AND chat_id = ?",
        (bot_id, chat_id)
    ).fetchone()
    if row and row[0]:
        return True
    # Временный бан из «чата админов».
    r = conn.execute(
        "SELECT ban_until FROM user_restrictions WHERE bot_id = ? AND user_chat_id = ?",
        (bot_id, chat_id)
    ).fetchone()
    if r and r[0]:
        return r[0] > _now_utc_str()
    return False

def unban_user(bot_id: int, chat_id: int) -> bool:
    """Снимает бан с юзера."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE users SET blocked = 0 WHERE bot_id = ? AND chat_id = ?",
            (bot_id, chat_id)
        )
        conn.commit()
        return cur.rowcount > 0
