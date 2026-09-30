"""Хранилище: тГК (Telegram-каналы) и посты канала.

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""


from services.db.connection import (
    _get_conn,
    _lock,
)
from services.db.migrations import _drop_channel_binding


def bind_channel(owner_id: int, channel_id: int, title: str = "",
                 username: str = "", *, takeover: bool = False) -> bool:
    """Привязывает канал к пользователю. ``True`` — привязка сделана.

    Права человека («владелец или админ канала») проверяются в обработчиках —
    там есть доступ к Telegram. Здесь страхуем второе правило: у одного ТГК
    не может быть двух хозяев.

    * ``False`` — канал уже привязан к другому владельцу YamoBot: чужой ТГК
      не отдаём (обработчик объясняет это пользователю);
    * ``takeover=True`` — разрешено забрать канал у прежнего владельца. Так
      делает только владелец (создатель) канала в Telegram: настоящий хозяин
      канала не должен остаться без доступа из-за чужой привязки. Прежний
      владелец теряет привязку и отложенные посты этого канала (как при
      «🔴 Отвязать ТГК»), обработчик уведомляет его об этом.
    """
    conn = _get_conn()
    with _lock:
        others = [
            int(r[0]) for r in conn.execute(
                "SELECT owner_id FROM tg_channels WHERE channel_id = ? "
                "AND owner_id != ?",
                (channel_id, owner_id),
            ).fetchall()
        ]
        if others:
            # Канал уже чей-то: чужой ТГК не отдаём. Исключение — takeover:
            # владелец канала в Telegram возвращает привязку себе.
            if not takeover:
                return False
            for other_owner in others:
                _drop_channel_binding(conn, other_owner)
        conn.execute(
            "INSERT INTO tg_channels (owner_id, channel_id, title, username) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET channel_id=excluded.channel_id, "
            "title=excluded.title, username=excluded.username",
            (owner_id, channel_id, title or "", username or ""),
        )
        conn.commit()
        return True

def get_bound_channel(owner_id: int) -> dict | None:
    """Привязанный канал пользователя (или None)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM tg_channels WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    return dict(row) if row else None

def get_channel_owner(channel_id: int) -> int | None:
    """Чей канал привязан (owner_id) — по ID канала."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT owner_id FROM tg_channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    return int(row[0]) if row else None

def update_channel_info(channel_id: int, title: str = "", username: str = "") -> None:
    """Обновляет название/username канала (например, после переименования)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE tg_channels SET title = ?, username = ? WHERE channel_id = ?",
            (title or "", username or "", channel_id),
        )
        conn.commit()

def unbind_channel(owner_id: int) -> bool:
    """Отвязывает канал пользователя (вместе с его отложенными постами)."""
    conn = _get_conn()
    with _lock:
        removed = _drop_channel_binding(conn, owner_id)
        conn.commit()
        return removed > 0

def add_channel_post(owner_id: int, channel_id: int, text: str = "",
                     photo: str = "", buttons: str = "[]",
                     status: str = "scheduled", publish_at: str = "") -> int:
    """Добавляет пост канала. Возвращает его id."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO channel_posts "
            "(owner_id, channel_id, text, photo, buttons, status, publish_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_id, channel_id, text or "", photo or "", buttons or "[]",
             status, publish_at or ""),
        )
        conn.commit()
        return int(cur.lastrowid or 0)

def get_channel_post(post_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM channel_posts WHERE id = ?", (post_id,)
    ).fetchone()
    return dict(row) if row else None

def get_channel_posts(owner_id: int, status: str | None = None) -> list[dict]:
    """Посты канала пользователя (свежие сверху). status=None — все."""
    conn = _get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM channel_posts WHERE owner_id = ? AND status = ? "
            "ORDER BY id DESC",
            (owner_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM channel_posts WHERE owner_id = ? ORDER BY id DESC",
            (owner_id,),
        ).fetchall()
    return [dict(r) for r in rows]

def count_channel_posts(owner_id: int, status: str) -> int:
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM channel_posts WHERE owner_id = ? AND status = ?",
        (owner_id, status),
    ).fetchone()
    return int(row[0]) if row else 0

_CHANNEL_POST_FIELDS = {"text", "photo", "buttons", "status", "publish_at", "message_id"}

def update_channel_post(post_id: int, **fields) -> bool:
    """Обновляет поля поста (только из белого списка)."""
    data = {k: v for k, v in fields.items() if k in _CHANNEL_POST_FIELDS}
    if not data:
        return False
    conn = _get_conn()
    with _lock:
        columns = ", ".join(f"{k} = ?" for k in data)
        cur = conn.execute(
            f"UPDATE channel_posts SET {columns} WHERE id = ?",
            (*data.values(), post_id),
        )
        conn.commit()
        return cur.rowcount > 0

def delete_channel_post(post_id: int) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM channel_posts WHERE id = ?", (post_id,))
        conn.commit()
        return cur.rowcount > 0

def get_due_channel_posts(now_utc: str) -> list[dict]:
    """Отложенные посты, время которых уже наступило (UTC-строка)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM channel_posts "
        "WHERE status = 'scheduled' AND publish_at != '' AND publish_at <= ? "
        "ORDER BY publish_at ASC",
        (now_utc,),
    ).fetchall()
    return [dict(r) for r in rows]

def add_channel_giveaway(owner_id: int, channel_id: int, title: str = "",
                         winners: str = "", status: str = "finished") -> int:
    """Добавляет запись о розыгрыше канала (для сводки в «Мой ТГК»)."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO channel_giveaways (owner_id, channel_id, title, winners, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_id, channel_id, title or "", winners or "", status),
        )
        conn.commit()
        return int(cur.lastrowid or 0)

def get_channel_giveaways(owner_id: int) -> list[dict]:
    """Розыгрыши канала пользователя (свежие сверху)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM channel_giveaways WHERE owner_id = ? ORDER BY id DESC",
        (owner_id,),
    ).fetchall()
    return [dict(r) for r in rows]

def set_channel_bind_request(owner_id: int) -> None:
    """Отмечает, что пользователь просит привязать ТГК.

    Нужно, когда Telegram не сообщает инициатора добавления бота в канал
    (анонимный админ): тогда привязываем канал владельцу со свежей заявкой.
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO channel_bind_requests (owner_id, created_at) "
            "VALUES (?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(owner_id) DO UPDATE SET created_at=CURRENT_TIMESTAMP",
            (owner_id,),
        )
        conn.commit()

def clear_channel_bind_request(owner_id: int) -> None:
    """Снимает заявку на привязку ТГК (привязали или отменили)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM channel_bind_requests WHERE owner_id = ?", (owner_id,)
        )
        conn.commit()

def get_channel_bind_request(max_age_seconds: int = 900) -> int | None:
    """Владелец со свежей заявкой «привяжи ТГК» (или None).

    Берём только заявки не старше ``max_age_seconds`` (по умолчанию 15 минут):
    так старые ожидания не перехватят чужой канал.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT owner_id FROM channel_bind_requests "
        "WHERE created_at >= datetime('now', ?) "
        "ORDER BY created_at DESC LIMIT 1",
        (f"-{int(max_age_seconds)} seconds",),
    ).fetchone()
    return int(row[0]) if row else None
