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
    get_norm_settings,
    get_work_hours,
    set_bot_keyboard,
    set_work_hours_enabled,
    set_work_hours_message,
    set_work_hours_time,
    set_norm_field,
    update_bot_field,
)

logger = logging.getLogger(__name__)

# Поля бота, которые входят в конфиг.
# Категория бота (bot_type) здесь НЕТ намеренно: через конфиг её менять нельзя,
# для этого есть выбор категории при добавлении бота.
CONFIG_FIELDS = (
    "welcome_text", "welcome_photo", "welcome_rich",
    "links", "antispam_mode", "anonymous_mode",
    "cat_ask_enabled", "cat_ask_categories", "cat_ask_custom",
    "admin_change_enabled", "admin_change_limit",
)

_ANTISPAM_MODES = {"off", "auto", "manual"}

# ── Разделы конфига ───────────────────────────────────────────────────
# Конфиг разбит на независимые разделы: при переносе можно включить только
# нужные, чтобы случайно не затереть лимиты смены админа или режим работы.
# Reply-клавиатура сюда НЕ входит: она привязана к категории «Стандарт»
# и переносится вместе с режимами автоматически.
CONFIG_SECTIONS: tuple[tuple[str, str], ...] = (
    ("welcome", "💬 Приветствие"),
    ("links", "🔗 Кнопки-ссылки"),
    ("modes", "🗂 Тип, антиспам, анонимность"),
    ("catask", "🏷 Уточнение категории"),
    ("admchange", "🔄 Смена админа"),
    ("work", "🕐 Время работы"),
    ("norm", "📊 Норма админов"),
)

_SECTION_IDS = {key for key, _title in CONFIG_SECTIONS}


def snapshot_bot(bot: dict, owner_id: int = 0) -> dict:
    """Собирает конфиг-слепок по данным бота из БД.

    owner_id нужен для настроек владельца (время работы и норма админов):
    они общие для всех его ботов, но хранятся рядом с конфигом, чтобы
    переносить их одним куском.
    """
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

    if owner_id:
        work = get_work_hours(owner_id)
        data["work_hours"] = {
            "enabled": int(work.get("enabled") or 0),
            "start": work.get("start") or "09:00",
            "end": work.get("end") or "21:00",
            "msg_text": work.get("msg_text") or "",
            "msg_photo": work.get("msg_photo") or "",
        }
        norms = get_norm_settings(owner_id)
        data["norms"] = {
            # У нормы нет отдельного флага: norm = 0 значит «выключено».
            "norm": int(norms.get("norm") or 0),
            "start_day": int(norms.get("start_day") or 0),
            "end_day": int(norms.get("end_day") or 4),
            "notify_enabled": int(norms.get("notify_enabled") or 0),
        }
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

    antispam = {"off": "выключен", "auto": "авто", "manual": "ручной"}.get(
        str(parsed.get("antispam_mode") or "off"), "выключен"
    )
    anon = "включён" if int(parsed.get("anonymous_mode") or 0) else "выключен"
    keyboard = parsed.get("keyboard") or []

    lines = [
        f"• 💬 Приветствие: <code>{welcome or '— не задано —'}</code>{media_line}",
        f"• 🔗 Кнопок-ссылок: <b>{_links_count(parsed)}</b>",
        f"• ⌨️ Reply-кнопок: <b>{len(keyboard) if isinstance(keyboard, list) else 0}</b> "
        "(переносятся вместе с режимами)\n",
        f"• 🛡 Антиспам: <b>{antispam}</b>",
        f"• 🕶 Анонимность: <b>{anon}</b>",
        "• 🗂 Категория бота: <b>не переносится</b>",
    ]

    # Уточнение категории (вкл/выкл, стандартные и свои категории)
    ask_on = "включено" if int(parsed.get("cat_ask_enabled") or 0) else "выключено"
    raw_cats = str(parsed.get("cat_ask_categories") or "").strip()
    cats = [c.strip() for c in raw_cats.split(",") if c.strip()] if raw_cats else []
    raw_own = str(parsed.get("cat_ask_custom") or "").strip()
    own = [c.strip() for c in raw_own.split(",") if c.strip()] if raw_own else []
    lines.append(
        f"• 🏷 Уточнение категории: <b>{ask_on}</b> — "
        f"категорий: <b>{len(cats)}</b>, своих: <b>{len(own)}</b>"
    )

    # Смена админа
    chg_on = "включено" if int(parsed.get("admin_change_enabled") or 0) else "выключено"
    try:
        chg_limit = int(parsed.get("admin_change_limit") or 3)
    except (TypeError, ValueError):
        chg_limit = 3
    lines.append(f"• 🔄 Смена админа: <b>{chg_on}</b>, лимит: <b>{chg_limit}</b> в сутки")

    # Время работы (настройка владельца)
    work = parsed.get("work_hours")
    if isinstance(work, dict):
        work_state = "включено" if int(work.get("enabled") or 0) else "выключено"
        lines.append(
            f"• 🕐 Время работы: <b>{work_state}</b>, "
            f"<b>{work.get('start', '09:00')}–{work.get('end', '21:00')}</b>"
        )

    # Норма админов (настройка владельца). У нормы нет флага «включено»:
    # norm = 0 означает выключенную.
    norms = parsed.get("norms")
    if isinstance(norms, dict):
        try:
            norm_value = max(0, int(norms.get("norm") or 0))
        except (TypeError, ValueError):
            norm_value = 0
        norm_state = "включена" if norm_value else "выключена"
        lines.append(
            f"• 📊 Норма админов: <b>{norm_state}</b>, норма: <b>{norm_value}</b>"
        )

    return lines


def apply_bot_config(owner_id: int, bot_id: int, data,
                     sections: set[str] | None = None) -> dict:
    """Применяет конфиг к боту, НЕ трогая статистику, имя и токен бота.

    ``sections`` — какие разделы переносить (None = все). Настройки владельца
    (время работы, норма админов) общие для всех его ботов: они тоже
    применяются, но помечаются отдельно в отчёте.

    Возвращает словарь с тем, что реально применено.
    """
    parsed = _config_dict(data)
    chosen = set(_SECTION_IDS) if sections is None else {
        s for s in sections if s in _SECTION_IDS
    }
    empty = {"applied": [], "links": 0, "keyboard": 0, "owner": []}
    if not parsed or not chosen:
        return empty

    applied: list[str] = []
    owner_applied: list[str] = []

    # ── Приветствие (текст + медиа) ──
    if "welcome" in chosen:
        for field in ("welcome_text", "welcome_photo", "welcome_rich"):
            if field in parsed:
                value = parsed.get(field)
                value = "" if value is None else str(value)
                if update_bot_field(owner_id, bot_id, field, value):
                    applied.append(field)

    # ── Ссылки-кнопки (инлайны) ──
    links_raw = parsed.get("links")
    if "links" in chosen and links_raw is not None:
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

    # ── Антиспам / анонимность (с проверкой значений) ──
    # Категорию бота (bot_type) конфиг НЕ переносит: её меняют только вручную
    # при добавлении бота.
    if "modes" in chosen:
        antispam = str(parsed.get("antispam_mode") or "").strip()
        if antispam in _ANTISPAM_MODES:
            update_bot_field(owner_id, bot_id, "antispam_mode", antispam)
            applied.append("antispam_mode")

        if "anonymous_mode" in parsed:
            anon = 1 if int(parsed.get("anonymous_mode") or 0) else 0
            update_bot_field(owner_id, bot_id, "anonymous_mode", anon)
            applied.append("anonymous_mode")

    # ── Уточнение категории ──
    if "catask" in chosen:
        if "cat_ask_enabled" in parsed:
            update_bot_field(owner_id, bot_id, "cat_ask_enabled",
                             1 if int(parsed.get("cat_ask_enabled") or 0) else 0)
            applied.append("cat_ask_enabled")
        for field in ("cat_ask_categories", "cat_ask_custom"):
            if field in parsed:
                update_bot_field(owner_id, bot_id, field, str(parsed.get(field) or ""))
                applied.append(field)

    # ── Смена админа ──
    if "admchange" in chosen:
        if "admin_change_enabled" in parsed:
            update_bot_field(owner_id, bot_id, "admin_change_enabled",
                             1 if int(parsed.get("admin_change_enabled") or 0) else 0)
            applied.append("admin_change_enabled")
        if "admin_change_limit" in parsed:
            try:
                limit = max(1, min(int(parsed.get("admin_change_limit") or 3), 20))
            except (TypeError, ValueError):
                limit = 3
            update_bot_field(owner_id, bot_id, "admin_change_limit", limit)
            applied.append("admin_change_limit")

    # ── Время работы (общее для всех ботов владельца) ──
    work = parsed.get("work_hours")
    if "work" in chosen and isinstance(work, dict):
        set_work_hours_enabled(owner_id, bool(int(work.get("enabled") or 0)))
        set_work_hours_time(owner_id, str(work.get("start") or "09:00"),
                            str(work.get("end") or "21:00"))
        set_work_hours_message(owner_id, str(work.get("msg_text") or ""),
                               str(work.get("msg_photo") or ""), "[]")
        owner_applied.append("🕐 Время работы")

    # ── Норма админов (общая для всех ботов владельца) ──
    norms = parsed.get("norms")
    if "norm" in chosen and isinstance(norms, dict):
        try:
            set_norm_field(owner_id, "norm", max(0, int(norms.get("norm") or 0)))
            set_norm_field(owner_id, "start_day", min(6, max(0, int(norms.get("start_day") or 0))))
            set_norm_field(owner_id, "end_day", min(6, max(0, int(norms.get("end_day") or 4))))
            set_norm_field(owner_id, "notify_enabled",
                           1 if int(norms.get("notify_enabled") or 0) else 0)
        except (TypeError, ValueError):
            logger.warning("Некорректные данные нормы в конфиге — пропускаем")
        else:
            owner_applied.append("📊 Норма админов")

    # ── Reply-клавиатура ──
    # Отдельного раздела у неё нет: она переносится вместе с режимами
    # (категорией бота), потому что reply-кнопки работают в «Стандарте».
    keyboard = parsed.get("keyboard")
    if "modes" in chosen and isinstance(keyboard, list):
        set_bot_keyboard(owner_id, bot_id, [b for b in keyboard if isinstance(b, dict)])
        applied.append("keyboard")

    links_count = _links_count(parsed)
    keyboard_count = len(keyboard) if isinstance(keyboard, list) else 0
    logger.info(
        "Бот %s: применён конфиг (разделы: %s, ссылок: %s, reply-кнопок: %s)",
        bot_id, sorted(chosen), links_count, keyboard_count,
    )
    return {"applied": applied, "links": links_count, "keyboard": keyboard_count,
            "owner": owner_applied}