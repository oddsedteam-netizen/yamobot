"""Сохранение и применение конфигов ботов.

Конфиг — это «слепок» настроек бота: приветствие (текст / фото / статья),
тип бота, инлайн-кнопки-ссылки, антиспам, анонимность и reply-клавиатура.
Под коротким кодом его можно сохранить, перенести на другого бота или
восстановить у того же.

Что НЕ трогается при применении конфига:
  • статистика (сообщения, пользователи, рассылки);
  • токен, имя и username бота — сам бот остаётся тем же;
  • привязанные чаты, ПЗ, админы, напоминалки.
"""

import json
import logging

from services.storage import (
    get_bot_keyboard_by_bot,
    set_bot_keyboard,
    update_bot_field,
)

logger = logging.getLogger(__name__)

# Поля бота, которые входят в конфиг.
CONFIG_FIELDS = (
    "welcome_text", "welcome_photo", "welcome_rich",
    "links", "bot_type", "antispam_mode", "anonymous_mode",
)

_BOT_TYPES = {"standard", "anketa"}
_ANTISPAM_MODES = {"off", "auto", "manual"}


def snapshot_bot(bot: dict) -> dict:
    """Собирает конфиг-слепок по данным бота из БД."""
    data: dict = {}
    for field in CONFIG_FIELDS:
        value = bot.get(field)
        if value is None:
            value = "" if field != "links" else "[]"
        data[field] = value

    # Ссылки храним как в БД (JSON-строкой), чтобы конфиг был «сырым» слепком.
    links = data.get("links") or "[]"
    if isinstance(links, list):
        links = json.dumps(links, ensure_ascii=False)
    data["links"] = links

    data["keyboard"] = get_bot_keyboard_by_bot(int(bot["id"]))
    return data


def _config_dict(raw) -> dict:
    """Разбирает data конфига (строка JSON или уже словарь)."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _links_count(data: dict) -> int:
    try:
        links = json.loads(data.get("links") or "[]")
    except (json.JSONDecodeError, TypeError):
        return 0
    return len(links) if isinstance(links, list) else 0


def config_preview(data) -> list[str]:
    """Человекочитаемое описание конфига (для сообщений в панели)."""
    parsed = _config_dict(data)
    if not parsed:
        return ["• пусто"]

    welcome = str(parsed.get("welcome_text") or "").strip()
    if len(welcome) > 90:
        welcome = welcome[:90] + "…"
    media = []
    if str(parsed.get("welcome_rich") or "").strip():
        media.append("📰 статья")
    if str(parsed.get("welcome_photo") or "").strip():
        media.append("🖼 фото")
    media_line = f"  ({', '.join(media)})" if media else ""

    bot_type = parsed.get("bot_type") or "standard"
    antispam = {"off": "выключен", "auto": "авто", "manual": "ручной"}.get(
        str(parsed.get("antispam_mode") or "off"), "выключен"
    )
    anon = "включён" if int(parsed.get("anonymous_mode") or 0) else "выключен"
    keyboard = parsed.get("keyboard") or []

    return [
        f"• 💬 Приветствие: <code>{welcome or '— не задано —'}</code>{media_line}",
        f"• 🔗 Кнопок-ссылок: <b>{_links_count(parsed)}</b>",
        f"• ⌨️ Reply-кнопок: <b>{len(keyboard) if isinstance(keyboard, list) else 0}</b>",
        f"• 🗂 Тип бота: <b>{bot_type}</b>",
        f"• 🛡 Антиспам: <b>{antispam}</b>",
        f"• 🕶 Анонимность: <b>{anon}</b>",
    ]


def apply_bot_config(owner_id: int, bot_id: int, data) -> dict:
    """Применяет конфиг к боту, НЕ трогая статистику, имя и токен бота.

    Возвращает словарь с тем, что реально применено.
    """
    parsed = _config_dict(data)
    if not parsed:
        return {"applied": [], "links": 0, "keyboard": 0}

    applied: list[str] = []

    # ── Приветствие (текст + медиа) ──
    for field in ("welcome_text", "welcome_photo", "welcome_rich"):
        if field in parsed:
            value = parsed.get(field)
            value = "" if value is None else str(value)
            if update_bot_field(owner_id, bot_id, field, value):
                applied.append(field)

    # ── Ссылки-кнопки (инлайны) ──
    links_raw = parsed.get("links")
    if links_raw is not None:
        if isinstance(links_raw, list):
            links_raw = json.dumps(links_raw, ensure_ascii=False)
        try:
            parsed_links = json.loads(links_raw or "[]")
        except (json.JSONDecodeError, TypeError):
            parsed_links = []
        if isinstance(parsed_links, list):
            update_bot_field(owner_id, bot_id, "links",
                             json.dumps(parsed_links, ensure_ascii=False))
            applied.append("links")

    # ── Тип бота / антиспам / анонимность (с проверкой значений) ──
    bot_type = str(parsed.get("bot_type") or "").strip()
    if bot_type in _BOT_TYPES:
        update_bot_field(owner_id, bot_id, "bot_type", bot_type)
        applied.append("bot_type")

    antispam = str(parsed.get("antispam_mode") or "").strip()
    if antispam in _ANTISPAM_MODES:
        update_bot_field(owner_id, bot_id, "antispam_mode", antispam)
        applied.append("antispam_mode")

    if "anonymous_mode" in parsed:
        anon = 1 if int(parsed.get("anonymous_mode") or 0) else 0
        update_bot_field(owner_id, bot_id, "anonymous_mode", anon)
        applied.append("anonymous_mode")

    # ── Reply-клавиатура ──
    keyboard = parsed.get("keyboard")
    if isinstance(keyboard, list):
        buttons = [b for b in keyboard if isinstance(b, dict)]
        set_bot_keyboard(owner_id, bot_id, buttons)
        applied.append("keyboard")

    links_count = _links_count(parsed)
    keyboard_count = len(keyboard) if isinstance(keyboard, list) else 0
    logger.info(
        "Бот %s: применён конфиг (полей: %s, ссылок: %s, reply-кнопок: %s)",
        bot_id, len(applied), links_count, keyboard_count,
    )
    return {"applied": applied, "links": links_count, "keyboard": keyboard_count}