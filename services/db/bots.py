"""Хранилище: боты.

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""

import json

from services.db.connection import (
    _get_conn,
    _lock,
    logger,
)


def get_user_bots(user_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM bots WHERE owner_id = ?", (user_id,)).fetchall()
    return [dict(r) for r in rows]

def get_accessible_bots(user_id: int) -> list[dict]:
    conn = _get_conn()
    own = conn.execute("SELECT * FROM bots WHERE owner_id = ?", (user_id,)).fetchall()
    co_owners = conn.execute("SELECT owner_id FROM coowners WHERE coowner_id = ?", (user_id,)).fetchall()
    result = [dict(r) for r in own]
    for co in co_owners:
        co_bots = conn.execute("SELECT * FROM bots WHERE owner_id = ?", (co["owner_id"],)).fetchall()
        result.extend([dict(r) for r in co_bots])
    seen = set()
    unique = []
    for b in result:
        if b["id"] not in seen:
            seen.add(b["id"])
            unique.append(b)
    return unique

def add_user_bot(user_id: int, bot_info: dict) -> bool:
    """Привязывает бота к владельцу. False — если бот занят другим владельцем."""
    conn = _get_conn()
    existing = conn.execute("SELECT owner_id FROM bots WHERE id = ?", (bot_info["id"],)).fetchone()
    if existing and existing["owner_id"] != user_id:
        # Токен даёт полный доступ к боту, но привязка уже занята другим владельцем
        # панели: не даём «увести» бота к себе. Передавать права нужно через
        # «👑 Передать права» (или сначала удалить бота у текущего владельца).
        logger.warning(
            "Отклонена привязка бота %s к %s: бот уже привязан к %s",
            bot_info["id"], user_id, existing["owner_id"],
        )
        return False

    with _lock:
        if existing:
            conn.execute(
                "UPDATE bots SET token=?, username=?, first_name=?, owner_id=? WHERE id=?",
                (bot_info["token"], bot_info.get("username", ""),
                 bot_info.get("first_name", ""), user_id, bot_info["id"])
            )
        else:
            conn.execute(
                "INSERT INTO bots (id, owner_id, token, username, first_name, welcome_text, links, stopped) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bot_info["id"], user_id, bot_info["token"],
                 bot_info.get("username", ""), bot_info.get("first_name", ""),
                 bot_info.get("welcome_text", ""), json.dumps(bot_info.get("links", [])), 0)
            )
        conn.commit()
    return True

def remove_user_bot(user_id: int, bot_id: int) -> bool:
    """Удаляет бота владельца вместе со всеми данными по нему."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM bots WHERE id = ? AND owner_id = ?", (bot_id, user_id))
        conn.execute("DELETE FROM users WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM stats WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM mailings WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM admin_messages WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM feedback_chats WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM feedback_topics WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM feedback_messages WHERE bot_id = ?", (bot_id,))
        # Удалённый бот не должен «висеть» в списке мёртвых.
        conn.execute("DELETE FROM dead_bots WHERE bot_id = ?", (bot_id,))
        conn.commit()
        return cur.rowcount > 0

def get_bot_by_id(user_id: int, bot_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM bots WHERE id = ? AND owner_id = ?", (bot_id, user_id)).fetchone()
    if row:
        return dict(row)
    co = conn.execute("SELECT owner_id FROM coowners WHERE coowner_id = ?", (user_id,)).fetchall()
    for c in co:
        row = conn.execute("SELECT * FROM bots WHERE id = ? AND owner_id = ?", (bot_id, c["owner_id"])).fetchone()
        if row:
            return dict(row)
    return None

def get_bot_by_id_any_owner(bot_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    return dict(row) if row else None

_BOT_FIELDS_WHITELIST = {
    "token", "username", "first_name", "welcome_text",
    "links", "stopped", "antispam_mode", "bot_type", "anonymous_mode",
    "welcome_photo", "welcome_rich", "cat_ask_enabled", "cat_ask_categories",
    "cat_ask_custom", "admin_change_enabled", "admin_change_limit",
}

def update_bot_field(user_id: int, bot_id: int, field: str, value) -> bool:
    if field not in _BOT_FIELDS_WHITELIST:
        raise ValueError(f"Недопустимое поле бота: {field!r}")
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            f"UPDATE bots SET {field} = ? WHERE id = ?",
            (value, bot_id),
        )
        conn.commit()
        return cur.rowcount > 0

def get_all_bots_flat() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM bots").fetchall()
    return [dict(r) for r in rows]

def bot_display_name(b: dict) -> str:
    name = b.get("first_name") or b.get("username") or f"bot_{b['id']}"
    username = f" (@{b['username']})" if b.get("username") else ""
    return f"{name}{username}"

def is_bot_anonymous(bot_id: int) -> bool:
    """Включён ли анонимный режим для бота (по данным любого владельца)."""
    bot = get_bot_by_id_any_owner(bot_id)
    if not bot:
        return False
    return bool(bot.get("anonymous_mode", 0))

def set_bot_anonymous(user_id: int, bot_id: int, enabled: bool) -> bool:
    """Включает/выключает анонимный режим бота."""
    return update_bot_field(user_id, bot_id, "anonymous_mode", 1 if enabled else 0)

DEFAULT_PZ_CATEGORIES: tuple[str, ...] = ("поддержка", "универсал", "общение")

def get_cat_ask_settings(bot_id: int) -> tuple[bool, list[str]]:
    """Настройки «уточнения категории»: (включено, список категорий).

    Категории хранятся одной строкой через запятую; пусто — набор по умолчанию.
    Список никогда не бывает пустым: без категорий вопрос ПЗ не имеет смысла.
    """
    bot = get_bot_by_id_any_owner(bot_id)
    if not bot:
        return False, list(DEFAULT_PZ_CATEGORIES)

    enabled = bool(bot.get("cat_ask_enabled", 0))
    raw = str(bot.get("cat_ask_categories") or "").strip()
    if raw:
        categories = [c.strip().lower() for c in raw.split(",") if c.strip()]
    else:
        categories = []
    return enabled, categories or list(DEFAULT_PZ_CATEGORIES)

def set_cat_ask_enabled(user_id: int, bot_id: int, enabled: bool) -> bool:
    """Включает/выключает «уточнение категории» у бота."""
    return update_bot_field(user_id, bot_id, "cat_ask_enabled", 1 if enabled else 0)

def set_cat_ask_categories(user_id: int, bot_id: int, categories: list[str]) -> bool:
    """Сохраняет список категорий, которые предлагать ПЗ (не пустой)."""
    clean: list[str] = []
    for raw in categories:
        name = str(raw or "").strip().lower().lstrip("#")
        if name and name not in clean:
            clean.append(name)
    if not clean:
        clean = list(DEFAULT_PZ_CATEGORIES)
    return update_bot_field(user_id, bot_id, "cat_ask_categories", ",".join(clean))

MAX_CUSTOM_CATEGORIES = 3

def get_cat_custom(user_id: int, bot_id: int) -> list[dict]:
    """Свои категории ПЗ: ``[{"name": ..., "active": bool}, ...]``.

    Храним в колонке ``cat_ask_custom`` списком через запятую; выключенные
    помечаем префиксом ``-`` (например ``-реклама, жалоба``).
    """
    bot = get_bot_by_id(user_id, bot_id)
    if not bot:
        return []
    raw = str(bot.get("cat_ask_custom") or "").strip()
    result: list[dict] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        active = True
        if name.startswith("-"):
            active, name = False, name[1:].strip()
        if name:
            result.append({"name": name.lower(), "active": active})
    return result

def set_cat_custom(user_id: int, bot_id: int, items: list[dict]) -> bool:
    """Сохраняет список своих категорий с их состоянием (вкл/выкл)."""
    parts: list[str] = []
    for item in items:
        name = str(item.get("name") or "").strip().lower().lstrip("#")
        if not name:
            continue
        parts.append(name if item.get("active", True) else f"-{name}")
    return update_bot_field(
        user_id, bot_id, "cat_ask_custom", ",".join(parts[:MAX_CUSTOM_CATEGORIES])
    )

def add_custom_category(user_id: int, bot_id: int, name: str) -> tuple[bool, str]:
    """Добавляет свою категорию. Возвращает ``(получилось, текст ответа)``."""
    clean = str(name or "").strip().lstrip("#").lower()
    if not clean:
        return False, "❌ Название не может быть пустым."
    if len(clean) > 24:
        clean = clean[:24]

    current = get_cat_custom(user_id, bot_id)
    if any(item["name"] == clean for item in current):
        return False, f"⚠️ Категория «{clean}» уже есть."
    if len(current) >= MAX_CUSTOM_CATEGORIES:
        return False, (
            f"⚠️ Больше {MAX_CUSTOM_CATEGORIES} своих категорий добавить нельзя.\n"
            "Удали или переименуй одну из текущих."
        )

    current.append({"name": clean, "active": True})
    set_cat_custom(user_id, bot_id, current)
    return True, f"✅ Категория «{clean}» добавлена."

def remove_custom_category(user_id: int, bot_id: int, name: str) -> bool:
    """Удаляет свою категорию по имени."""
    target = str(name or "").strip().lstrip("#").lower()
    current = get_cat_custom(user_id, bot_id)
    left = [item for item in current if item["name"] != target]
    if len(left) == len(current):
        return False
    set_cat_custom(user_id, bot_id, left)
    return True

def toggle_custom_category(user_id: int, bot_id: int, name: str) -> tuple[bool, bool]:
    """Включает/выключает свою категорию. Возвращает ``(нашлась, активна)``."""
    target = str(name or "").strip().lstrip("#").lower()
    current = get_cat_custom(user_id, bot_id)
    if not any(item["name"] == target for item in current):
        return False, False
    for item in current:
        if item["name"] == target:
            item["active"] = not item["active"]
            break
    set_cat_custom(user_id, bot_id, current)
    return True, bool(next(i["active"] for i in current if i["name"] == target))

def get_categories_for_pz(user_id: int, bot_id: int) -> list[str]:
    """Итоговый список категорий, которые бот предложит ПЗ."""
    _enabled, base = get_cat_ask_settings(bot_id)
    result = list(base)
    for item in get_cat_custom(user_id, bot_id):
        if item["active"] and item["name"] not in result:
            result.append(item["name"])
    return result

DEFAULT_ADMIN_CHANGE_LIMIT = 3

MAX_ADMIN_CHANGE_LIMIT = 20

def get_admin_change_settings(bot_id: int) -> tuple[bool, int]:
    """Настройки лимита смен админа: ``(включено, сколько смен в сутки)``."""
    bot = get_bot_by_id_any_owner(bot_id)
    if not bot:
        return False, DEFAULT_ADMIN_CHANGE_LIMIT
    enabled = bool(bot.get("admin_change_enabled", 0))
    try:
        limit = int(bot.get("admin_change_limit") or DEFAULT_ADMIN_CHANGE_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_ADMIN_CHANGE_LIMIT
    return enabled, max(1, min(limit, MAX_ADMIN_CHANGE_LIMIT))

def set_admin_change_enabled(user_id: int, bot_id: int, enabled: bool) -> bool:
    return update_bot_field(user_id, bot_id, "admin_change_enabled", 1 if enabled else 0)

def set_admin_change_limit(user_id: int, bot_id: int, limit: int) -> bool:
    value = max(1, min(int(limit), MAX_ADMIN_CHANGE_LIMIT))
    return update_bot_field(user_id, bot_id, "admin_change_limit", value)

def count_admin_changes_today(bot_id: int, user_chat_id: int) -> int:
    """Сколько раз ПЗ менял админа за последние сутки."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM admin_change_log "
        "WHERE bot_id = ? AND user_chat_id = ? "
        "AND changed_at >= datetime('now', '-1 day')",
        (bot_id, user_chat_id),
    ).fetchone()
    return int(row[0]) if row else 0

def log_admin_change(bot_id: int, user_chat_id: int) -> None:
    """Записывает смену админа (для подсчёта суточного лимита)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO admin_change_log (bot_id, user_chat_id) VALUES (?, ?)",
            (bot_id, user_chat_id),
        )
        # Подчищаем старые записи, чтобы таблица не росла бесконечно.
        conn.execute("DELETE FROM admin_change_log WHERE changed_at < datetime('now', '-7 day')")
        conn.commit()

def admin_changes_left(bot_id: int, user_chat_id: int) -> int:
    """Сколько смен осталось ПЗ сегодня; -1 — ограничение выключено."""
    enabled, limit = get_admin_change_settings(bot_id)
    if not enabled:
        return -1
    return max(0, limit - count_admin_changes_today(bot_id, user_chat_id))

def get_bot_links(user_id: int, bot_id: int) -> list[dict]:
    bot = get_bot_by_id(user_id, bot_id)
    if not bot:
        return []
    try:
        return json.loads(bot.get("links", "[]"))
    except (json.JSONDecodeError, TypeError):
        return []

def set_bot_links(user_id: int, bot_id: int, links: list[dict]) -> bool:
    return update_bot_field(user_id, bot_id, "links", json.dumps(links, ensure_ascii=False))

def get_antispam_mode(bot_id: int) -> str:
    conn = _get_conn()
    row = conn.execute("SELECT antispam_mode FROM bots WHERE id = ?", (bot_id,)).fetchone()
    return row[0] if row else "off"

def set_antispam_mode(user_id: int, bot_id: int, mode: str) -> bool:
    return update_bot_field(user_id, bot_id, "antispam_mode", mode)

ACTION_ADMIN = "admin"

def default_bot_keyboard() -> list[dict]:
    """Клавиатура по умолчанию: только «сменить админа»."""
    return [{"kind": "admin", "text": "сменить админа"}]

def get_bot_keyboard_raw(bot_id: int) -> str:
    conn = _get_conn()
    row = conn.execute("SELECT buttons FROM bot_keyboards WHERE bot_id = ?", (bot_id,)).fetchone()
    return row[0] if row else ""

def get_bot_keyboard(owner_id: int, bot_id: int) -> list[dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT buttons FROM bot_keyboards WHERE bot_id = ? AND owner_id = ?", (bot_id, owner_id)
    ).fetchone()
    if not row:
        return default_bot_keyboard()
    try:
        buttons = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return default_bot_keyboard()
    return buttons if isinstance(buttons, list) else default_bot_keyboard()

def get_bot_keyboard_by_bot(bot_id: int) -> list[dict]:
    """Для дочернего бота: клавиатура вне зависимости от того, кто владелец."""
    conn = _get_conn()
    row = conn.execute("SELECT buttons FROM bot_keyboards WHERE bot_id = ?", (bot_id,)).fetchone()
    if not row:
        return default_bot_keyboard()
    try:
        buttons = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return default_bot_keyboard()
    return buttons if isinstance(buttons, list) else default_bot_keyboard()

def set_welcome_for_all(user_id: int, welcome_text: str) -> int:
    """Устанавливает приветствие для всех ботов юзера."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE bots SET welcome_text = ? WHERE owner_id = ?",
            (welcome_text, user_id)
        )
        conn.commit()
        return cur.rowcount

def set_welcome_bundle_for_all(user_id: int, welcome_text: str,
                               welcome_photo: str = "",
                               welcome_rich: str = "") -> int:
    """Устанавливает текст + медиа-приветствие (фото/статью) всем ботам юзера."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE bots SET welcome_text = ?, welcome_photo = ?, welcome_rich = ? "
            "WHERE owner_id = ?",
            (welcome_text, welcome_photo or "", welcome_rich or "", user_id)
        )
        conn.commit()
        return cur.rowcount

def set_links_for_all(user_id: int, links: list[dict]) -> int:
    """Устанавливает линки для всех ботов юзера."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE bots SET links = ? WHERE owner_id = ?",
            (json.dumps(links, ensure_ascii=False), user_id)
        )
        conn.commit()
        return cur.rowcount

def set_bot_type(user_id: int, bot_id: int, bot_type: str) -> bool:
    """Устанавливает тип бота: 'standard' или 'anketa'."""
    if bot_type not in ("standard", "anketa"):
        return False
    return update_bot_field(user_id, bot_id, "bot_type", bot_type)

def get_bot_type(bot_id: int) -> str:
    conn = _get_conn()
    row = conn.execute("SELECT bot_type FROM bots WHERE id = ?", (bot_id,)).fetchone()
    return row[0] if row else "standard"

def get_user_bot_types(user_id: int) -> list[str]:
    """Список типов ботов, которые есть у юзера (например, ['standard', 'anketa'])."""
    conn = _get_conn()
    rows = conn.execute("SELECT DISTINCT bot_type FROM bots WHERE owner_id = ?", (user_id,)).fetchall()
    return [r[0] for r in rows if r[0]]
