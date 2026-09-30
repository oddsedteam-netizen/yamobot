"""Хранилище: антирейд «чата админов».

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""


from services.db.connection import (
    _get_conn,
    _lock,
)
from services.db.helpers import _owner_bot_ids


_ANTIRAID_DEFAULTS = {
    "enabled": 0,
    "threshold": 10,
    # «Удаление ссылок» включено по умолчанию: при рейде ссылку-приглашение
    # надо отзывать, чтобы рейдеры не вернулись по ней же.
    "del_links": 1,
    "del_members": 0,
    "triggered": 0,
}

def get_antiraid_settings(owner_id: int) -> dict:
    """Настройки антирейда владельца (с значениями по умолчанию)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM antiraid_settings WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    settings = dict(_ANTIRAID_DEFAULTS)
    if row:
        for k in settings:
            if k in row.keys():
                settings[k] = row[k]
    settings["enabled"] = int(settings.get("enabled") or 0)
    settings["threshold"] = max(1, int(settings.get("threshold") or 10))
    settings["del_links"] = int(settings.get("del_links") or 0)
    settings["del_members"] = int(settings.get("del_members") or 0)
    settings["triggered"] = int(settings.get("triggered") or 0)
    return settings

def set_antiraid_field(owner_id: int, field: str, value) -> bool:
    """Обновляет одно поле настроек антирейда владельца."""
    if field not in _ANTIRAID_DEFAULTS:
        return False
    conn = _get_conn()
    with _lock:
        conn.execute(
            f"INSERT INTO antiraid_settings (owner_id, {field}) VALUES (?, ?) "
            f"ON CONFLICT(owner_id) DO UPDATE SET {field}=excluded.{field}, "
            "updated_at=CURRENT_TIMESTAMP",
            (owner_id, int(value)),
        )
        conn.commit()
    return True

def set_antiraid_enabled(owner_id: int, enabled: bool) -> bool:
    return set_antiraid_field(owner_id, "enabled", 1 if enabled else 0)

def set_antiraid_threshold(owner_id: int, threshold: int) -> bool:
    return set_antiraid_field(owner_id, "threshold", max(1, int(threshold)))

def set_antiraid_del_links(owner_id: int, value: bool) -> bool:
    return set_antiraid_field(owner_id, "del_links", 1 if value else 0)

def set_antiraid_del_members(owner_id: int, value: bool) -> bool:
    return set_antiraid_field(owner_id, "del_members", 1 if value else 0)

def set_antiraid_triggered(owner_id: int, value: bool) -> bool:
    return set_antiraid_field(owner_id, "triggered", 1 if value else 0)

def reset_all_antiraid_triggered() -> None:
    """Сбрасывает флаг сработавшего антирейда у всех владельцев (при старте бота)."""
    conn = _get_conn()
    with _lock:
        conn.execute("UPDATE antiraid_settings SET triggered = 0")
        conn.commit()

def get_admin_active_topics_list(owner_id: int, admin_user_id: int) -> list[dict]:
    """Активные топики (ПЗ), закреплённые за конкретным админом."""
    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return []
    placeholders = ",".join("?" for _ in bot_ids)
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT * FROM feedback_topics "
        f"WHERE admin_user_id = ? AND status = 'assigned' AND bot_id IN ({placeholders})",
        (admin_user_id, *bot_ids)
    ).fetchall()
    return [dict(r) for r in rows]

def is_registry_user_banned(user_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT blocked FROM users_registry WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row[0])

def set_registry_user_blocked(user_id: int, blocked: bool) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO users_registry (user_id, blocked) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET blocked=excluded.blocked",
            (user_id, 1 if blocked else 0)
        )
        conn.commit()

_ANTINAKRUTKA_DEFAULTS = {
    # Включена ли защита (переключатель «🟢 Включить» / «🔴 Выключить»).
    # Когда выключена — бот не следит за наплывом и не присылает уведомлений.
    "enabled": 1,
    "count": 10,
    "window_minutes": 5,
    # «Пропускать ли топики ПЗ при защите»: 1 — да (топики создаются,
    # уведомления приостанавливаются), 0 — нет (бот не создаёт топики и пишет
    # пользователю, что временно не может принять обращение).
    "block_topics": 1,
    "triggered": 0,
    "snapshot": "",
    "triggered_at": None,
}

def get_antinakrutka_settings(owner_id: int) -> dict:
    """Настройки антинакрутки владельца (со значениями по умолчанию)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM antinakrutka_settings WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    settings = dict(_ANTINAKRUTKA_DEFAULTS)
    if row:
        for k in settings:
            if k in row.keys():
                settings[k] = row[k]
    settings["count"] = max(1, int(settings.get("count") or 10))
    settings["window_minutes"] = max(1, int(settings.get("window_minutes") or 5))
    settings["enabled"] = 1 if settings.get("enabled") is None else int(settings["enabled"])
    settings["block_topics"] = int(settings.get("block_topics") or 0)
    settings["triggered"] = int(settings.get("triggered") or 0)
    settings["snapshot"] = settings.get("snapshot") or ""
    return settings

def set_antinakrutka_field(owner_id: int, field: str, value) -> bool:
    """Обновляет одно поле настроек антинакрутки владельца."""
    if field not in _ANTINAKRUTKA_DEFAULTS:
        return False
    conn = _get_conn()
    with _lock:
        conn.execute(
            f"INSERT INTO antinakrutka_settings (owner_id, {field}) VALUES (?, ?) "
            f"ON CONFLICT(owner_id) DO UPDATE SET {field}=excluded.{field}, "
            "updated_at=CURRENT_TIMESTAMP",
            (owner_id, value),
        )
        conn.commit()
    return True

def set_antinakrutka_triggered(owner_id: int, value: bool,
                               snapshot: str | None = None) -> bool:
    """Переключает состояние тревоги антинакрутки (с опциональным снимком статы)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO antinakrutka_settings (owner_id, triggered) VALUES (?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET triggered=excluded.triggered, "
            "updated_at=CURRENT_TIMESTAMP",
            (owner_id, 1 if value else 0),
        )
        if value:
            conn.execute(
                "UPDATE antinakrutka_settings SET snapshot = ?, "
                "triggered_at = CURRENT_TIMESTAMP WHERE owner_id = ?",
                (snapshot or "", owner_id),
            )
        else:
            conn.execute(
                "UPDATE antinakrutka_settings SET snapshot = '', triggered_at = NULL "
                "WHERE owner_id = ?",
                (owner_id,),
            )
        conn.commit()
    return True

