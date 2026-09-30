"""Хранилище: норма админов (раздел «📊 Норма» в профиле).

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""


from services.db.connection import (
    _get_conn,
    _lock,
)
from services.db.helpers import _owner_bot_ids
from services.db.admins import get_admin_active_topics, get_admin_message_stats, get_admins_all


_NORM_DEFAULTS = {
    "norm": 0,             # 0 — норма выключена
    "start_day": 0,
    "end_day": 4,
    "notify_enabled": 1,
    "last_notified": "",
}

def get_norm_settings(owner_id: int) -> dict:
    """Настройки нормы владельца (со значениями по умолчанию)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM admin_norms WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    settings = dict(_NORM_DEFAULTS)
    if row:
        for k in settings:
            if k in row.keys():
                settings[k] = row[k]
    settings["norm"] = max(0, int(settings.get("norm") or 0))
    settings["start_day"] = min(6, max(0, int(settings.get("start_day") or 0)))
    settings["end_day"] = min(6, max(0, int(settings.get("end_day") or 0)))
    settings["notify_enabled"] = 1 if settings.get("notify_enabled") is None \
        else int(settings["notify_enabled"])
    settings["last_notified"] = str(settings.get("last_notified") or "")
    return settings

def set_norm_field(owner_id: int, field: str, value) -> bool:
    """Обновляет одно поле настроек нормы владельца."""
    if field not in _NORM_DEFAULTS:
        return False
    conn = _get_conn()
    with _lock:
        conn.execute(
            f"INSERT INTO admin_norms (owner_id, {field}) VALUES (?, ?) "
            f"ON CONFLICT(owner_id) DO UPDATE SET {field}=excluded.{field}, "
            "updated_at=CURRENT_TIMESTAMP",
            (owner_id, value),
        )
        conn.commit()
    return True

def get_all_norm_settings() -> list[dict]:
    """Настройки нормы всех владельцев (для фоновой проверки недобора).

    В каждую запись добавляется ``owner_id`` — по нему отправляется уведомление.
    """
    conn = _get_conn()
    rows = conn.execute("SELECT owner_id FROM admin_norms").fetchall()
    result: list[dict] = []
    for row in rows:
        owner_id = int(row["owner_id"])
        settings = get_norm_settings(owner_id)
        settings["owner_id"] = owner_id
        result.append(settings)
    return result

def get_admin_period_messages(owner_id: int, admin_user_id: int,
                              since: str, until: str) -> int:
    """Сколько сообщений админ отправил за период [since, until) (UTC).

    Считаем все записи admin_messages: и ответы админа в топиках, и его
    действия по кнопкам — это и есть «активность» админа за период.
    """
    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return 0
    placeholders = ",".join("?" for _ in bot_ids)
    conn = _get_conn()
    row = conn.execute(
        f"SELECT COUNT(*) FROM admin_messages WHERE admin_user_id = ? "
        f"AND created_at >= ? AND created_at < ? AND bot_id IN ({placeholders})",
        (admin_user_id, since, until, *bot_ids),
    ).fetchone()
    return int(row[0]) if row else 0

def get_norm_period_stats(owner_id: int, since: str, until: str) -> list[dict]:
    """Активность всех админов владельца за период, отсортированная по убыванию.

    Возвращает список словарей: admin, period (сообщений за период),
    stats (день/неделя/месяц/всего), active_topics, reached (набрана ли норма).
    """
    norm = get_norm_settings(owner_id)["norm"]
    result: list[dict] = []
    for a in get_admins_all(owner_id):
        period = get_admin_period_messages(owner_id, a["user_id"], since, until)
        result.append({
            "admin": a,
            "period": period,
            "stats": get_admin_message_stats(owner_id, a["user_id"]),
            "active_topics": get_admin_active_topics(owner_id, a["user_id"]),
            "reached": bool(norm) and period >= norm,
        })
    result.sort(key=lambda item: (-item["period"], str(item["admin"].get("tag") or "")))
    return result

def clear_antinakrutka_snapshot(owner_id: int) -> None:
    """Убирает снимок статистики (решение по наплыву уже принято).

    Флаг ``triggered`` при этом НЕ трогаем: защита снимается отдельно —
    кнопкой «Снять защиту»/«Сброс защиты».
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE antinakrutka_settings SET snapshot = '' WHERE owner_id = ?",
            (owner_id,),
        )
        conn.commit()
