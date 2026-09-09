"""Команды перезапуска бота YamoBot в групповом «чате админов».

  /perezap   (или /перезап, /перезапуск) — перезапуск бота с перепривязкой
             этого чата к владельцу. Только владелец/модератор: не-владелец
             получает «нет прав» и перепривязки НЕ происходит.
  /perestart (или /перестарт, /рестарт, /restart) — простой перезапуск бота
             в чате (пере-приветствие), привязку не трогает.

Обе команды имеют локальный кулдаун на чат, чтобы в группе с включённым
«медленным режимом»/антиспамом не упираться в flood-wait (TelegramRetryAfter).
"""

import logging
import re
import time

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from handlers._common import _retry_send, ADMIN_CHAT_WELCOME
from services.config import is_super_admin
from services.storage import (
    get_owner_by_admin_chat,
    get_bound_chat,
    set_bound_chat,
    get_user_bots,
    is_admin_chat_moderator,
)

logger = logging.getLogger(__name__)

router = Router()

_MARK = r"[/\.!]"
_PEREZAP_RE = re.compile(
    rf"(?i)^\s*{_MARK}+\s*(?:perezap|перезап|перезапуск)\b\s*$"
)
_PERESTART_RE = re.compile(
    rf"(?i)^\s*{_MARK}+\s*(?:perestart|перестарт|рестарт|restart)\b\s*$"
)

# Антиспам: не чаще одного перезапуска на чат за этот интервал (в секундах).
_RESTART_COOLDOWN = 30.0
_last_restart: dict[int, float] = {}


def _resolve_owner_for_chat(chat_id: int, user_id: int) -> int | None:
    """Настоящий владелец чата.

    Если чат уже привязан — это его владелец. Если чат непривязанный
    («осиротевший») — владельцем считаем того, у кого есть боты. Никогда
    не «забираем» уже привязанный чат у чужого владельца.
    """
    owner = get_owner_by_admin_chat(chat_id)
    if owner is not None:
        return owner
    if get_user_bots(user_id):
        return user_id
    return None


def _is_moderator(owner_id: int, user_id: int) -> bool:
    """Может ли юзер перезапускать: владелец, супер-админ или модератор чата."""
    if owner_id == user_id or is_super_admin(user_id):
        return True
    if is_admin_chat_moderator(owner_id, user_id):
        return True
    return False


def _cooldown_allowed(chat_id: int) -> bool:
    """Пропускает запрос, если для этого чата прошёл кулдаун, иначе False."""
    now = time.monotonic()
    last = _last_restart.get(chat_id, 0.0)
    if now - last < _RESTART_COOLDOWN:
        return False
    _last_restart[chat_id] = now
    return True


async def _deny(message: Message, cmd: str) -> None:
    await message.answer(f"❌ <code>/{cmd}</code>: нет прав.")


async def _not_yet(message: Message) -> None:
    await message.answer(
        f"⏳ Подожди ~{int(_RESTART_COOLDOWN)}с перед повторным перезапуском."
    )


def _guess_kind(owner_id: int, chat_id: int) -> str:
    """Тип чата: «work», если уже привязан как рабочий, иначе «admin»."""
    if get_bound_chat(owner_id, "work") == chat_id:
        return "work"
    return "admin"


# ═══════════════════════════════════════════════════════════════
#  /perezap — перезапуск с перепривязкой (только владелец/модератор)
# ═══════════════════════════════════════════════════════════════

@router.message(
    F.text.regexp(_PEREZAP_RE) & F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
)
async def cmd_perezap(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id if message.chat else 0
    if not chat_id:
        return

    # Всегда реагируем. Если это не владелец/модератор — «нет прав» и НЕ
    # перепривязываем бота (без перепривязки для чужих).
    owner = _resolve_owner_for_chat(chat_id, user_id)
    if owner is None or not _is_moderator(owner, user_id):
        await _deny(message, "perezap")
        return

    if not _cooldown_allowed(chat_id):
        await _not_yet(message)
        return

    kind = _guess_kind(owner, chat_id)
    set_bound_chat(owner, kind, chat_id)

    if kind == "work":
        await _retry_send(
            lambda: message.answer("💼 <b>Чат работы привязан к YamoBot.</b> Бот активен. ✅"),
            attempts=6,
        )
    else:
        await _retry_send(lambda: message.answer(ADMIN_CHAT_WELCOME), attempts=6)

    try:
        await _retry_send(
            lambda: message.answer(
                f"✅ <b>YamoBot перезапущен в этом чате.</b>\n"
                f"📎 Чат: <code>{chat_id}</code> (тип: {kind})\n"
                f"👑 Владелец: <code>{owner}</code>\n\n"
                "Теперь команды (<code>/стата</code>, <code>/.стата</code> и др.) "
                "должны работать."
            ),
            attempts=3,
        )
    except Exception as e:  # best-effort
        logger.warning("Не удалось отправить подтверждение /perezap: %s", e)


# ═══════════════════════════════════════════════════════════════
#  /perestart — простой перезапуск без изменения привязки
# ═══════════════════════════════════════════════════════════════

@router.message(
    F.text.regexp(_PERESTART_RE) & F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
)
async def cmd_perestart(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id if message.chat else 0
    if not chat_id:
        return

    owner = _resolve_owner_for_chat(chat_id, user_id)
    if owner is None or not _is_moderator(owner, user_id):
        await _deny(message, "perestart")
        return

    if not _cooldown_allowed(chat_id):
        await _not_yet(message)
        return

    # Никакой перепривязки — просто «просыпаемся» в чате.
    try:
        await _retry_send(lambda: message.answer(ADMIN_CHAT_WELCOME), attempts=6)
        await _retry_send(
            lambda: message.answer(
                "🔄 <b>YamoBot перезапущен.</b>\n"
                "Привязка чата не изменена — бот снова на связи."
            ),
            attempts=3,
        )
    except Exception as e:  # best-effort
        logger.warning("Не удалось отправить подтверждение /perestart: %s", e)