"""Команда /стата — единый и надёжный обработчик статистики в YamoBot.

Принимает все популярные формы:
  /стата               — сводка по ПЗ всех ботов владельца (с кнопкой).
  /стата неделя|день|месяц — статистика админов за период.
  .стата, /stata        — то же самое (без слеша/с латиницей).

Вся статистика обёрнута в try/except + ретраи при flood-wait, чтобы бот ВСЕГДА
отвечал на команду, а не «молча пропадал» (как бывает в медленном режиме групп).
"""

import logging
import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import InlineKeyboardMarkup, Message

from handlers._common import _retry_send
from handlers.start import _stats_payload, _resolve_stats_owner
from services.storage import (
    get_owner_by_admin_chat,
    get_admins_all,
    get_admin_message_stats,
    get_admin_active_topics,
)

logger = logging.getLogger(__name__)

router = Router()

_MARK = r"[/\.!]"
_STATA_RE = re.compile(rf"(?i)^\s*{_MARK}+\.?\s*стата\b\s*(.*)$")

_PERIOD_KEYS = {
    "день": "day", "дн": "day", "day": "day",
    "неделя": "week", "нед": "week", "week": "week",
    "месяц": "month", "мес": "month", "month": "month",
    "всего": "total", "все": "total", "всё": "total", "total": "total",
}

_PERIOD_LABELS = {"day": "день", "week": "неделю", "month": "месяц", "total": "всё время"}


def _usage(msg: str) -> str:
    return f"⚠️ <b>Неверное использование</b>\n{msg}"


async def _reply(message: Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Отправляет текст с ретраями при flood-wait и логирует любую ошибку."""
    try:
        await _retry_send(lambda: message.answer(text, reply_markup=kb), attempts=6)
        return
    except Exception as exc:
        err_text = str(exc)
        logger.warning("Не удалось отправить /стата: %s", err_text)
    # Последняя попытка — простой текст ошибки, чтобы команда НЕ молчала.
    try:
        await _retry_send(
            lambda: message.answer(f"❌ Не удалось сформировать статистику: {err_text}"),
            attempts=2,
        )
    except Exception:
        pass


async def _admins_stats(owner_id: int, period: str) -> str:
    """Текст статистики админов за период."""
    admins = get_admins_all(owner_id)
    if not admins:
        return "👥 Нет админов."

    lines = [
        f"📊 <b>Статистика админов за {_PERIOD_LABELS[period]}</b>",
        f"👤 Админов: <b>{len(admins)}</b>",
        "",
    ]
    for a in admins:
        stats = get_admin_message_stats(owner_id, a["user_id"])
        topics = get_admin_active_topics(owner_id, a["user_id"])
        msgs = stats.get(period, 0)
        uname = f"@{a['username']}" if a.get("username") else f"ID:{a['user_id']}"
        lines.append(
            f"• <b>#{a['tag']}</b> ({uname})\n"
            f"    ✉️ Сообщений: <b>{msgs}</b>\n"
            f"    📋 ПЗ за админом: <b>{topics}</b>"
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  /стата — единый хендлер (сводка или статистика за период)
# ═══════════════════════════════════════════════════════════

@router.message(F.text.regexp(_STATA_RE))
async def cmd_stata(message: Message) -> None:
    logger.info(
        "Команда /стата получена в чате %s от %s",
        message.chat.id if message.chat else None,
        getattr(message.from_user, "id", None),
    )

    match = _STATA_RE.match(message.text or "")
    arg = (match.group(1) if match else "").strip().lower()
    period = _PERIOD_KEYS.get(arg)

    if period:
        # Статистика админов за период.
        if message.chat and message.chat.type != ChatType.PRIVATE:
            owner_id = get_owner_by_admin_chat(message.chat.id)
        else:
            owner_id = message.from_user.id if message.from_user else None
        if owner_id is None:
            await _reply(message, "❌ Команда работает в привязанном «чате админов».")
            return
        try:
            text = await _admins_stats(owner_id, period)
        except Exception as exc:
            logger.warning("Ошибка расчёта /стата за период: %s", exc)
            text = f"❌ Не удалось сформировать статистику: {exc}"
        await _reply(message, text)
        return

    if arg and arg not in _PERIOD_KEYS:
        # Пользователь указал неверный период.
        await _reply(
            message,
            _usage("Доступные периоды: <code>неделя</code>, <code>день</code>, <code>месяц</code>.")
        )
        return

    # Сводка по ПЗ (без периода).
    fallback = message.from_user.id if message.from_user else 0
    owner_id = _resolve_stats_owner(message.chat, fallback)
    try:
        text, kb = _stats_payload(owner_id)
    except Exception as exc:
        logger.warning("Ошибка формирования сводки /стата: %s", exc)
        text = f"❌ Не удалось сформировать сводку: {exc}"
        kb = None
    await _reply(message, text, kb)