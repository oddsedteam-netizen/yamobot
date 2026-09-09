"""Команда /стата в «чате админов» (главный бот YamoBot).

Здесь только статистика админов за период. Команды банов/мутов/предов и
модераторов вынесены (упрощены), потому что мешали стабильной работе.
Простая сводка «/.стата» и «/стата» (без аргумента) обрабатывается в
handlers/start.py, а /perezap и /perestart — в handlers/restart.py.

Доступные команды:
  /стата неделя|день|месяц — статистика админов за период.
"""

import logging
import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from handlers._common import _retry_send
from services.storage import (
    get_owner_by_admin_chat,
    get_admins_all,
    get_admin_message_stats,
    get_admin_active_topics,
)

logger = logging.getLogger(__name__)

router = Router()

_MARK = r"[/\.!]"
_STATA_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*стата\b\s*(.+)$")


_PERIOD_KEYS = {
    "день": "day", "дн": "day", "day": "day",
    "неделя": "week", "нед": "week", "week": "week",
    "месяц": "month", "мес": "month", "month": "month",
    "всего": "total", "все": "total", "всё": "total", "total": "total",
}

_PERIOD_LABELS = {"day": "день", "week": "неделю", "month": "месяц", "total": "всё время"}


def _resolve_owner(message: Message) -> int | None:
    if message.chat and message.chat.type != ChatType.PRIVATE:
        return get_owner_by_admin_chat(message.chat.id)
    return message.from_user.id


def _usage(msg: str) -> str:
    return f"⚠️ <b>Неверное использование</b>\n{msg}"


# ═══════════════════════════════════════════════════════════
#  /стата <период> — статистика админов
# ═══════════════════════════════════════════════════════════

@router.message(F.text.regexp(_STATA_RE))
async def cmd_stata_period(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None:
        await message.answer("❌ Команда работает в привязанном «чате админов».")
        return

    arg = (_STATA_RE.match(message.text or "").group(1) or "").strip().lower()
    period = _PERIOD_KEYS.get(arg)
    if not period:
        await message.answer(
            _usage("Доступные периоды: <code>неделя</code>, <code>день</code>, <code>месяц</code>.")
        )
        return

    admins = get_admins_all(owner_id)
    if not admins:
        await message.answer("👥 Нет админов.")
        return

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

    # Flood-wait в группе (медленный режим) обрабатываем ретраем, чтобы бот
    # ВСЕГДА отвечал на /стата, а не «молча пропадал».
    try:
        await _retry_send(lambda: message.answer("\n".join(lines)), attempts=6)
    except Exception as e:
        logger.warning("Не удалось отправить /стата: %s", e)
        try:
            await _retry_send(
                lambda: message.answer(f"❌ Не удалось сформировать статистику: {e}"),
                attempts=2,
            )
        except Exception:
            pass