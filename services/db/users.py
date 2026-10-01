"""Хранилище: пользователи дочерних ботов.

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from services.db.connection import (
    _get_conn,
    _lock,
)
from services.db.helpers import _now_utc_str, _owner_bot_ids


def add_child_user(bot_id: int, chat_id: int, username: str = "", first_name: str = "") -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO users (bot_id, chat_id, username, first_name) VALUES (?, ?, ?, ?)",
            (bot_id, chat_id, username, first_name)
        )
        conn.commit()

def mark_user_blocked(bot_id: int, chat_id: int) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute("UPDATE users SET blocked = 1 WHERE bot_id = ? AND chat_id = ?", (bot_id, chat_id))
        conn.commit()

def get_child_users(bot_id: int, only_active: bool = True) -> list[dict]:
    conn = _get_conn()
    if only_active:
        rows = conn.execute("SELECT * FROM users WHERE bot_id = ? AND blocked = 0", (bot_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM users WHERE bot_id = ?", (bot_id,)).fetchall()
    return [dict(r) for r in rows]

def get_child_users_count(bot_id: int) -> dict:
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM users WHERE bot_id = ?", (bot_id,)).fetchone()[0]
    blocked = conn.execute("SELECT COUNT(*) FROM users WHERE bot_id = ? AND blocked = 1", (bot_id,)).fetchone()[0]
    return {"total": total, "blocked": blocked, "active": total - blocked}


def _expire_time(minutes: int | None) -> str | None:
    """Время истечения в ISO-строках БД; None означает вечный бан."""
    if not minutes:
        return None
    return (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# Колонки user_restrictions, которые разрешено менять: имя колонки
# подставляется в SQL напрямую, поэтому оно обязано быть из белого списка.
_RESTRICTION_COLUMNS = {"ban_until", "mute_until", "warns"}

def _restriction_upsert(conn: sqlite3.Connection, bot_id: int, chat_id: int, col: str, value) -> None:
    if col not in _RESTRICTION_COLUMNS:
        # Колонка подставляется в SQL напрямую, поэтому имя обязано быть из
        # белого списка: так правка не сможет случайно протащить сюда
        # произвольную строку.
        raise ValueError(f"Недопустимая колонка user_restrictions: {col!r}")
    conn.execute(
        f"INSERT INTO user_restrictions (bot_id, user_chat_id, {col}) VALUES (?, ?, ?) "
        f"ON CONFLICT(bot_id, user_chat_id) DO UPDATE SET {col}=excluded.{col}",
        (bot_id, chat_id, value)
    )

def get_user_restriction(bot_id: int, chat_id: int) -> dict:
    """Текущее состояние ограничений юзера (бан/мут/преды)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT ban_until, mute_until, warns FROM user_restrictions "
        "WHERE bot_id = ? AND user_chat_id = ?",
        (bot_id, chat_id)
    ).fetchone()
    if not row:
        return {"ban_until": None, "mute_until": None, "warns": 0}
    return {"ban_until": row[0], "mute_until": row[1], "warns": row[2]}

def set_user_ban(bot_id: int, chat_id: int, until_iso: str | None = None) -> None:
    """Устанавливает бан. None — навсегда (постоянный флаг), строка — до даты."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO users (bot_id, chat_id, username, first_name) "
            "VALUES (?, ?, '', '')",
            (bot_id, chat_id)
        )
        if until_iso is None:
            # Вечный бан — постоянный флаг, чтобы is_user_banned ловил его всегда.
            conn.execute(
                "UPDATE users SET blocked = 1 WHERE bot_id = ? AND chat_id = ?",
                (bot_id, chat_id)
            )
            _restriction_upsert(conn, bot_id, chat_id, "ban_until", None)
        else:
            conn.execute(
                "UPDATE users SET blocked = 0 WHERE bot_id = ? AND chat_id = ?",
                (bot_id, chat_id)
            )
            _restriction_upsert(conn, bot_id, chat_id, "ban_until", until_iso)
        conn.commit()

def set_user_mute(bot_id: int, chat_id: int, until_iso: str | None) -> None:
    """Устанавливает мут до until_iso."""
    conn = _get_conn()
    with _lock:
        _restriction_upsert(conn, bot_id, chat_id, "mute_until", until_iso)
        conn.execute(
            "INSERT OR IGNORE INTO users (bot_id, chat_id, username, first_name) "
            "VALUES (?, ?, '', '')",
            (bot_id, chat_id)
        )
        conn.commit()

def clear_user_restriction(bot_id: int, chat_id: int) -> None:
    """Снимает бан, мут и преды."""
    conn = _get_conn()
    with _lock:
        _restriction_upsert(conn, bot_id, chat_id, "ban_until", None)
        _restriction_upsert(conn, bot_id, chat_id, "mute_until", None)
        _restriction_upsert(conn, bot_id, chat_id, "warns", 0)
        conn.execute(
            "UPDATE users SET blocked = 0 WHERE bot_id = ? AND chat_id = ?",
            (bot_id, chat_id)
        )
        conn.commit()

def add_user_warn(bot_id: int, chat_id: int) -> int:
    """Записывает один пред. Возвращает новое количество предов юзера."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO user_restrictions (bot_id, user_chat_id, warns) VALUES (?, ?, 1) "
            "ON CONFLICT(bot_id, user_chat_id) DO UPDATE SET warns=warns+1",
            (bot_id, chat_id)
        )
        conn.commit()
        row = conn.execute(
            "SELECT warns FROM user_restrictions WHERE bot_id = ? AND user_chat_id = ?",
            (bot_id, chat_id)
        ).fetchone()
        return row[0] if row else 1

def reset_user_warns(bot_id: int, chat_id: int) -> None:
    conn = _get_conn()
    with _lock:
        _restriction_upsert(conn, bot_id, chat_id, "warns", 0)
        conn.commit()

def get_warn_settings(owner_id: int) -> dict:
    """Порог предов до наказания и само наказание для владельца."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT max_warns, punish_type, punish_duration FROM warn_settings WHERE owner_id = ?",
        (owner_id,)
    ).fetchone()
    if not row:
        return {"max_warns": 5, "punish_type": "mute", "punish_duration": 60}
    return {"max_warns": row[0], "punish_type": row[1], "punish_duration": row[2]}

def set_warn_settings(owner_id: int, max_warns: int, punish_type: str, punish_duration: int) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO warn_settings (owner_id, max_warns, punish_type, punish_duration) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET "
            "max_warns=excluded.max_warns, punish_type=excluded.punish_type, "
            "punish_duration=excluded.punish_duration",
            (owner_id, max_warns, punish_type, punish_duration)
        )
        conn.commit()

def is_user_muted(bot_id: int, chat_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT mute_until FROM user_restrictions WHERE bot_id = ? AND user_chat_id = ?",
        (bot_id, chat_id)
    ).fetchone()
    if row and row[0]:
        return row[0] > _now_utc_str()
    return False

def get_owner_users(owner_id: int) -> list[dict]:
    """Все пользователи дочерних ботов владельца (bot_id, chat_id, username, first_name)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT u.bot_id AS bot_id, u.chat_id AS chat_id, "
        "u.username AS username, u.first_name AS first_name "
        "FROM users u JOIN bots b ON b.id = u.bot_id "
        "WHERE b.owner_id = ?",
        (owner_id,)
    ).fetchall()
    return [dict(r) for r in rows]

def clear_user_restriction_for_owner(owner_id: int, chat_id: int) -> None:
    """Снимает бан, мут и преды у юзера по всем ботам владельца."""
    for bot_id in _owner_bot_ids(owner_id):
        clear_user_restriction(bot_id, chat_id)

def clear_user_mute_for_owner(owner_id: int, chat_id: int) -> None:
    """Снимает только мут у юзера по всем ботам владельца."""
    conn = _get_conn()
    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return
    with _lock:
        for bot_id in bot_ids:
            _restriction_upsert(conn, bot_id, chat_id, "mute_until", None)
        conn.commit()

def reset_user_warns_for_owner(owner_id: int, chat_id: int) -> None:
    """Сбрасывает преды у юзера по всем ботам владельца."""
    conn = _get_conn()
    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return
    with _lock:
        for bot_id in bot_ids:
            _restriction_upsert(conn, bot_id, chat_id, "warns", 0)
        conn.commit()

def add_admin_chat_moderator(owner_id: int, user_id: int, username: str = "", first_name: str = "") -> bool:
    conn = _get_conn()
    with _lock:
        try:
            conn.execute(
                "INSERT INTO admin_chat_moderators (owner_id, user_id, username, first_name) "
                "VALUES (?, ?, ?, ?)",
                (owner_id, user_id, username, first_name)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def remove_admin_chat_moderator(owner_id: int, user_id: int) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "DELETE FROM admin_chat_moderators WHERE owner_id = ? AND user_id = ?",
            (owner_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0

def get_admin_chat_moderators(owner_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM admin_chat_moderators WHERE owner_id = ? ORDER BY added_at ASC",
        (owner_id,)
    ).fetchall()
    return [dict(r) for r in rows]

def is_admin_chat_moderator(owner_id: int, user_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT user_id FROM admin_chat_moderators WHERE owner_id = ? AND user_id = ?",
        (owner_id, user_id)
    ).fetchone()
    return row is not None

def register_user(user_id: int, username: str = "", first_name: str = "") -> None:
    """Регистрирует/обновляет пользователя мастер-бота."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO users_registry (user_id, username, first_name) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
            "first_name=excluded.first_name",
            (user_id, username, first_name)
        )
        conn.commit()

def get_user_registry(user_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users_registry WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_yid(yid: int) -> dict | None:
    """Человек по внутреннему номеру YID (без префикса «Y»).

    Нужен админ-панели: владелец ищет человека то по номеру («Y104»), то по
    настоящему Telegram ID. Раньше номер находился только перебором всего
    реестра внутри хендлера — здесь поиск делает сама база.
    """
    if not yid:
        return None
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users_registry WHERE yid = ?", (int(yid),),
    ).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    """Человек по ``@username`` (регистр не важен).

    Юзернейм в реестре может быть пустым или с ведущим ``@`` — сравниваем
    по очищенному значению, иначе поиск «@ivan» не находил «ivan».
    """
    name = (username or "").strip().lstrip("@").lower()
    if not name:
        return None
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users_registry "
        "WHERE LOWER(TRIM(username, '@')) = ? LIMIT 1",
        (name,),
    ).fetchone()
    return dict(row) if row else None

_CHAT_BIND_COLUMNS = {"work": "work_chat_id", "admin": "admin_chat_id"}

def get_bound_chat(user_id: int, kind: str) -> int | None:
    """Возвращает ID привязанного чата ('work' или 'admin'), либо None."""
    col = _CHAT_BIND_COLUMNS.get(kind)
    if not col:
        return None
    row = get_user_registry(user_id)
    if not row:
        return None
    chat_id = row.get(col) or 0
    return int(chat_id) if chat_id else None

def set_bound_chat(user_id: int, kind: str, chat_id: int | None) -> bool:
    """Привязывает/отвязывает чат ('work' или 'admin') для пользователя."""
    col = _CHAT_BIND_COLUMNS.get(kind)
    if not col:
        return False
    conn = _get_conn()
    with _lock:
        conn.execute(
            f"INSERT INTO users_registry (user_id, {col}) VALUES (?, ?) "
            f"ON CONFLICT(user_id) DO UPDATE SET {col}=excluded.{col}",
            (user_id, chat_id or 0)
        )
        conn.commit()
    return True

def get_all_users_registry() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM users_registry ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]

def get_owner_by_admin_chat(chat_id: int) -> int | None:
    """Возвращает владельца, к которому привязан данный «чат админов» (или None)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT user_id FROM users_registry WHERE admin_chat_id = ?",
        (chat_id,)
    ).fetchone()
    return row[0] if row else None

def set_pending_bind(user_id: int, kind: str | None) -> bool:
    """Отмечает, какой чат пользователь сейчас привязывает ('work'/'admin').

    Хранится в БД, чтобы привязка переживала перезапуск бота и не терялась
    (раньше это был in-memory словарь, из-за чего бот со временем «не видел»
    добавление и привязка ломалась).
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO users_registry (user_id, pending_bind_kind) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET pending_bind_kind=excluded.pending_bind_kind",
            (user_id, kind or ""),
        )
        conn.commit()
    return True

def get_pending_bind(user_id: int) -> str | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT pending_bind_kind FROM users_registry WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row:
        return None
    val = (row[0] or "").strip()
    return val if val else None
