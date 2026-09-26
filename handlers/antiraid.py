"""Антирейд «чата админов» (и защита от спама — сообщениями и стикерами).

Суть: бот защищает привязанный «чат админов» владельца двумя независимыми
путями:

  1) Заходы. Если в чат за короткое время заходит слишком много человек подряд
     (порог настраивается) — это похоже на рейд, и бот:
       • выключает возможность писать в чат (права членов понижаются);
       • уведомляет владельца в личку и пишет тревогу в сам чат;
       • делает ссылки на заход неактивными (отзывает основную ссылку-приглашение);
       • по настройке удаляет последних зашедших (кикает их).

  2) Спам внутри чата. Если кто-то флудит сообщениями или стикерами, бот:
       • предупреждает нарушителя и удаляет его сообщения;
       • при повторном флуде навсегда убирает его из чата и зовёт владельца.
     Анти-спам работает только когда антирейд включён командой /вкланти.

ВАЖНО: без прав администратора Telegram не отдаёт боту события о заходах и сами
сообщения в супергруппе — защита и наказания физически невозможны. Поэтому
/вкланти проверяет права бота и отказывает с понятным текстом, пока права
не выданы (а не «тихо ничего не делает»).

Команды (в «чате админов»):
  /вкланти   — включить антирейд для этого чата (нужны права администратора);
  /вклчат    — вернуть чату настройки после срабатывания антирейда
               (антирейд остаётся включённым и продолжает защиту);
  /выкланти  — выключить антирейд и полностью восстановить права чата.
"""

import logging
import re
import time
from collections import defaultdict

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import render_callback, cb_data, cb_uid, try_edit_answer
from services.config import is_super_admin
from services.storage import (
    get_owner_by_admin_chat,
    get_bound_chat,
    get_antiraid_settings,
    set_antiraid_enabled,
    set_antiraid_threshold,
    set_antiraid_del_links,
    set_antiraid_del_members,
    set_antiraid_triggered,
    is_admin_chat_moderator,
    remember_admin_chat_member,
)

logger = logging.getLogger(__name__)

router = Router()

_MARK = r"[/\.!]"
_VKLASTI_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*(?:вкланти)\b\s*$")
_VYLASTI_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*(?:выкланти)\b\s*$")
_VKLCHAT_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*(?:вклчат)\b\s*$")

# Последние заходы в чат админов: chat_id -> [record, ...]
_JOINS: dict[int, list[dict]] = defaultdict(list)
# Чаты, где антирейд уже сработал (пока не выключим через /выкланти).
_ARMED: set[int] = set()

# Окно, в течение которого заходы считаются «рейдом» (секунды).
_JOIN_WINDOW = 3600.0

# Дедупликация одинаковых заходов из двух источников (new_chat_members и
# chat_member) — не считаем один и тот же заход дважды.
_JOIN_DEDUP_SEC = 8.0
_last_join_ts: dict[tuple[int, int], float] = {}

# Спам в чате админов (сообщения/стикеры): (chat_id, user_id) -> список записей.
# Каждая запись: {"ts": float, "message_id": int|None, "kind": "text"|"sticker"}.
_SPAM_LOG: dict[tuple[int, int], list[dict]] = defaultdict(list)
# Кто уже предупреждён: (chat_id, user_id, kind) -> время предупреждения.
_SPAM_WARNED: dict[tuple[int, int, str], float] = {}
# Окна и лимиты флуда в чате админов.
_MSG_WINDOW = 12.0
_MSG_LIMIT = 10          # 10+ сообщений за 12 секунд.
_STICKER_WINDOW = 25.0
_STICKER_LIMIT = 5       # 5+ стикеров за 25 секунд.
# Повторное предупреждение в течение 5 минут сразу банит (без «второго шанса»).
_SPAM_WARN_COOLDOWN = 300.0

ANTIRAID_TEXTS = {
    "on": "🟢 включён",
    "off": "🔴 выключен",
    "yes": "✅ да",
    "no": "❌ нет",
}


class AntiraidFSM(StatesGroup):
    waiting_threshold = State()


def antiraid_kb(owner_id: int | None = None) -> InlineKeyboardMarkup:
    """Клавиатура настроек антирейда из профиля.

    Первой строкой — переключатель «🟢 Включить» / «🔴 Выключить»: кнопки просто
    меняются местами, команды в чате писать не нужно.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if owner_id:
        enabled = bool(get_antiraid_settings(owner_id)["enabled"])
        rows.append([InlineKeyboardButton(
            text="🔴 Выключить антирейд" if enabled else "🟢 Включить антирейд",
            callback_data="antiraid_off" if enabled else "antiraid_on",
            style="danger" if enabled else "success",
        )])
    rows += [
        [InlineKeyboardButton(text="🔢 Количество заходов", callback_data="antiraid_threshold", style="primary")],
        [InlineKeyboardButton(text="🔗 Удаление ссылок", callback_data="antiraid_links_ask", style="primary")],
        [InlineKeyboardButton(text="👥 Удаление зашедших", callback_data="antiraid_members_ask", style="primary")],
        [InlineKeyboardButton(text="⬅️ Защита", callback_data="profile_protection")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def antiraid_text(owner_id: int) -> str:
    s = get_antiraid_settings(owner_id)
    status = ANTIRAID_TEXTS["on"] if s["enabled"] else ANTIRAID_TEXTS["off"]
    links = ANTIRAID_TEXTS["yes"] if s["del_links"] else ANTIRAID_TEXTS["no"]
    members = ANTIRAID_TEXTS["yes"] if s["del_members"] else ANTIRAID_TEXTS["no"]
    return (
        "🛡 <b>Антирейд</b>\n\n"
        "Бот следит за <b>чатом админов</b> и реагирует на:\n\n"
        "👥 <b>Частые заходы</b> — если за короткое время заходит "
        "много людей (порог настраивается), чат блокируется, "
        "владелец зовётся, ссылки отзываются, зашедшие удаляются;\n"
        "💬 <b>Спам в чате</b> — флуд сообщениями или стикерами: "
        "предупреждение, а при повторе нарушитель убирается из чата.\n\n"
        "🔒 <b>выключает возможность писать в чат</b>;\n"
        "📢 <b>зовёт владельца</b> и сообщает об атаке;\n"
        "🔗 <b>делает все ссылки на заход неактивными</b>;\n"
        "👥 по настройке <b>удаляет последних зашедших</b>.\n\n"
        "🔹 Включение и выключение — кнопкой ниже, писать команды в чате "
        "не нужно: бот сам работает в привязанном «чате админов» "
        "(ему нужны <b>права администратора</b>).\n"
        "🔹 Если чат уже заблокирован после срабатывания — нажми "
        "<b>«🟢 Включить антирейд»</b> ещё раз: права чата восстановятся, "
        "а защита продолжит следить за чатом.\n"
        "🔹 Команды <code>/вкланти</code>, <code>/вклчат</code> и "
        "<code>/выкланти</code> тоже работают — как запасной вариант.\n\n"
        "📊 <u>Текущие настройки:</u>\n"
        f"  • Статус: {status}\n"
        f"  • Порог заходов: <b>{s['threshold']}</b>\n"
        f"  • Удаление ссылок: {links}\n"
        f"  • Удаление зашедших: {members}\n\n"
        "⚠️ Если статус «включён», но YamoBot <b>не администратор</b> "
        "чата админов — защита не может работать. Выдай права и включи "
        "антирейд ещё раз."
    )


# ═══════════════════════════════════════════════════════════════
#  Открытие настроек антирейда из профиля
# ═══════════════════════════════════════════════════════════════

@router.callback_query(F.data == "antiraid")
async def cb_antiraid(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    await render_callback(callback, antiraid_text(user_id), antiraid_kb(user_id))


# ═══════════════ Включение и выключение антирейда ═══════════════

async def _set_antiraid_from_profile(callback: CallbackQuery, enabled: bool) -> None:
    """Включает/выключает антирейд кнопкой из профиля.

    Раньше для этого нужно было писать команды в чате. Логика та же:
    при включении проверяем права бота в «чате админов», снимаем прошлое
    срабатывание и очищаем журналы; при выключении возвращаем чату права.
    """
    owner_id = cb_uid(callback)
    admin_chat = get_bound_chat(owner_id, "admin")
    bot = getattr(callback, "bot", None)

    if enabled:
        if not admin_chat:
            await callback.answer("⚠️ Сначала привяжи «чат админов» в профиле.",
                                  show_alert=True)
            return
        if bot is not None and not await _bot_admin_status(bot, int(admin_chat)):
            await callback.answer(
                "🚫 YamoBot не администратор чата админов — выдай права и попробуй снова.",
                show_alert=True,
            )
            return

        set_antiraid_enabled(owner_id, True)
        set_antiraid_triggered(owner_id, False)
        _ARMED.discard(int(admin_chat))
        _JOINS.pop(int(admin_chat), None)
        for key in [k for k in _SPAM_LOG if k[0] == int(admin_chat)]:
            _SPAM_LOG.pop(key, None)
        for key in [k for k in _SPAM_WARNED if k[0] == int(admin_chat)]:
            _SPAM_WARNED.pop(key, None)
        # Возвращаем чату права: после срабатывания он мог остаться закрытым.
        if bot is not None:
            try:
                await _restore_chat(bot, int(admin_chat))
            except Exception as e:
                logger.warning("Не удалось восстановить права чата админов: %s", e)
        await callback.answer("🛡 Антирейд включён")
    else:
        set_antiraid_enabled(owner_id, False)
        set_antiraid_triggered(owner_id, False)
        if admin_chat:
            _ARMED.discard(int(admin_chat))
            _JOINS.pop(int(admin_chat), None)
            if bot is not None:
                try:
                    await _restore_chat(bot, int(admin_chat))
                except Exception as e:
                    logger.warning("Не удалось восстановить права чата админов: %s", e)
        await callback.answer("🔻 Антирейд выключен")

    await render_callback(callback, antiraid_text(owner_id), antiraid_kb(owner_id))


@router.callback_query(F.data == "antiraid_on")
async def cb_antiraid_on(callback: CallbackQuery) -> None:
    await _set_antiraid_from_profile(callback, True)


@router.callback_query(F.data == "antiraid_off")
async def cb_antiraid_off(callback: CallbackQuery) -> None:
    await _set_antiraid_from_profile(callback, False)


# ═══════════════ Количество заходов (порог) ═══════════════════

@router.callback_query(F.data == "antiraid_threshold")
async def cb_antiraid_threshold(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AntiraidFSM.waiting_threshold)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="antiraid", style="primary")]
    ])
    if callback.message:
        await try_edit_answer(
            callback.message,
            "🔢 <b>Количество заходов</b>\n\n"
            "Напиши, сколько человек должно зайти в чат админов подряд, "
            "чтобы сработал антирейд (от 1 до 100).",
            reply_markup=kb,
        )
    await callback.answer()


@router.message(AntiraidFSM.waiting_threshold, F.chat.type == ChatType.PRIVATE)
async def fsm_antiraid_threshold(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("❌ Нужно отправить число. Например: <code>10</code>")
        return
    if value < 1 or value > 100:
        await message.answer("❌ Порог должен быть числом от 1 до 100.")
        return

    user_id = message.from_user.id if message.from_user else 0
    set_antiraid_threshold(user_id, value)
    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К настройкам антирейда", callback_data="antiraid", style="primary")]
    ])
    await message.answer(
        f"✅ Порог заходов установлен: <b>{value}</b>.\n"
        f"Антирейд сработает, когда в чат админов подряд зайдёт {value}+ человек.",
        reply_markup=kb,
    )


# ═══════════════ Удаление ссылок на заход ═════════════════════

@router.callback_query(F.data == "antiraid_links_ask")
async def cb_antiraid_links_ask(callback: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалять ссылки", callback_data="antiraid_links_set_1", style="success")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="antiraid_links_set_0", style="danger")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="antiraid", style="primary")],
    ])
    if callback.message:
        await try_edit_answer(
            callback.message,
            "🔗 <b>Удаление ссылок</b>\n\n"
            "Будет ли бот при срабатывании антирейда делать все доступные "
            "ссылки для захода в чат неактивными?",
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("antiraid_links_set_"))
async def cb_antiraid_links_set(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    value = cb_data(callback) == "antiraid_links_set_1"
    set_antiraid_del_links(user_id, value)
    await callback.answer("✅ Включено" if value else "❌ Выключено")
    await render_callback(callback, antiraid_text(user_id), antiraid_kb(user_id))


# ═══════════════ Удаление последних зашедших ══════════════════

@router.callback_query(F.data == "antiraid_members_ask")
async def cb_antiraid_members_ask(callback: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалять зашедших", callback_data="antiraid_members_set_1", style="success")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="antiraid_members_set_0", style="danger")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="antiraid", style="primary")],
    ])
    if callback.message:
        await try_edit_answer(
            callback.message,
            "👥 <b>Удаление зашедших</b>\n\n"
            "Будет ли бот при срабатывании антирейда удалять из чата "
            "последних зашедших?",
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("antiraid_members_set_"))
async def cb_antiraid_members_set(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    value = cb_data(callback) == "antiraid_members_set_1"
    set_antiraid_del_members(user_id, value)
    await callback.answer("✅ Включено" if value else "❌ Выключено")
    await render_callback(callback, antiraid_text(user_id), antiraid_kb(user_id))
# ═══════════════════════════════════════════════════════════════
#  /вкланти и /выкланти в «чате админов»
# ═══════════════════════════════════════════════════════════════

def _resolve_owner(chat_id: int, user_id: int) -> int | None:
    """Владелец чата админов, если чат привязан и юзер имеет права."""
    owner = get_owner_by_admin_chat(chat_id)
    if owner is None:
        return None
    if owner == user_id or is_super_admin(user_id):
        return owner
    if is_admin_chat_moderator(owner, user_id):
        return owner
    return None


@router.message(
    F.text.regexp(_VKLASTI_RE) & F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
)
async def cmd_vklasti(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id if message.chat else 0
    if not chat_id:
        return

    owner = _resolve_owner(chat_id, user_id)
    if owner is None:
        await message.answer("❌ <code>/вкланти</code>: нет прав или чат не привязан как «чат админов».")
        return

    # Без прав администратора бот НЕ видит заходы и сообщения в супергруппе
    # и не может никого наказать — включение «вслепую» бесполезно (пользователи
    # потом жалуются «антирейд не сработал»). Поэтому сначала проверяем права.
    me = await _bot_admin_status(message.bot, chat_id)
    if not me:
        await message.answer(
            "🚫 <b>Антирейд не включён: YamoBot не администратор этого чата.</b>\n\n"
            "Без прав администратора Telegram не даёт боту видеть заходы и "
            "сообщения — защита работать не будет.\n\n"
            "Как включить:\n"
            "1. Открой <b>«Управление чатом» → «Администраторы»</b>;\n"
            "2. Нажми на YamoBot и выбери <b>«Назначить администратором»</b>;\n"
            "3. Вернись сюда и напиши <code>/вкланти</code> ещё раз."
        )
        return

    set_antiraid_enabled(owner, True)
    set_antiraid_triggered(owner, False)
    _ARMED.discard(chat_id)
    _JOINS.pop(chat_id, None)
    for key in [k for k in _SPAM_LOG if k[0] == chat_id]:
        _SPAM_LOG.pop(key, None)
    for key in [k for k in _SPAM_WARNED if k[0] == chat_id]:
        _SPAM_WARNED.pop(key, None)

    await message.answer(
        "🛡 <b>Антирейд включён.</b>\n"
        "Бот следит за заходами и за спамом (сообщения/стикеры) в этот чат. "
        "Отключить можно командой <code>/выкланти</code>."
    )

    bot = message.bot
    if bot is not None:
        try:
            await bot.send_message(
                owner,
                "🛡 <b>Антирейд включён</b> для твоего чата админов.\n"
                "Бот будет следить за заходами и спамом; при подозрении на рейд "
                "заблокирует чат и позовёт тебя.",
            )
        except Exception as e:
            logger.warning("Не удалось уведомить владельца об антирейде: %s", e)


@router.message(
    F.text.regexp(_VYLASTI_RE) & F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
)
async def cmd_vylasti(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id if message.chat else 0
    if not chat_id:
        return

    owner = _resolve_owner(chat_id, user_id)
    if owner is None:
        await message.answer("❌ <code>/выкланти</code>: нет прав или чат не привязан как «чат админов».")
        return

    set_antiraid_enabled(owner, False)
    set_antiraid_triggered(owner, False)
    _ARMED.discard(chat_id)
    _JOINS.pop(chat_id, None)
    # Очищаем журналы спама по этому чату.
    for key in [k for k in _SPAM_LOG if k[0] == chat_id]:
        _SPAM_LOG.pop(key, None)
    for key in [k for k in _SPAM_WARNED if k[0] == chat_id]:
        _SPAM_WARNED.pop(key, None)

    # Полностью восстанавливаем права чата.
    restored = await _restore_chat(message.bot, chat_id)

    state_line = "Права чата полностью восстановлены." if restored else \
        "Не удалось восстановить права — выдай боту права администратора."
    await message.answer(
        "🔻 <b>Антирейд выключен.</b>\n"
        f"{state_line}\n"
        "Снова включить можно командой <code>/вкланти</code>."
    )

    bot = message.bot
    if bot is not None:
        try:
            await bot.send_message(
                owner,
                "🔻 <b>Антирейд выключен</b> для твоего чата админов.",
            )
        except Exception as e:
            logger.warning("Не удалось уведомить владельца об отключении антирейда: %s", e)


# ═══════════════════════════════════════════════════════════════
#  /вклчат — вернуть чату настройки после срабатывания антирейда
# ═══════════════════════════════════════════════════════════════

@router.message(
    F.text.regexp(_VKLCHAT_RE) & F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
)
async def cmd_vklchat(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id if message.chat else 0
    if not chat_id:
        return

    owner = _resolve_owner(chat_id, user_id)
    if owner is None:
        await message.answer("❌ <code>/вклчат</code>: нет прав или чат не привязан как «чат админов».")
        return

    # Возвращаем чату обычные права (можно снова писать).
    restored = await _restore_chat(message.bot, chat_id)

    # Снимаем флаг срабатывания и «взводим» защиту заново: чат снова под
    # наблюдением — антирейд продолжает следить за заходами и спамом.
    set_antiraid_triggered(owner, False)
    _ARMED.discard(chat_id)
    _JOINS.pop(chat_id, None)
    for key in [k for k in _SPAM_LOG if k[0] == chat_id]:
        _SPAM_LOG.pop(key, None)
    for key in [k for k in _SPAM_WARNED if k[0] == chat_id]:
        _SPAM_WARNED.pop(key, None)

    settings = get_antiraid_settings(owner)
    if not settings["enabled"]:
        enabled_line = ""
    else:
        enabled_line = "🛡 Антирейд остаётся <b>включённым</b> и снова следит за чатом."

    state_line = "✅ Настройки чата восстановлены, можно снова писать." if restored else \
        "⚠️ Не удалось восстановить права чата — выдай боту права администратора."
    await message.answer(
        f"🔓 <b>Чат разблокирован.</b>\n\n"
        f"{state_line}\n"
        + (f"{enabled_line}\n" if enabled_line else "")
        + "Полностью выключить защиту можно командой <code>/выкланти</code>."
    )

    bot = message.bot
    if bot is not None:
        try:
            await bot.send_message(
                owner,
                f"🔓 <b>Чат админов разблокирован после антирейда.</b>\n\n"
                f"{state_line}\n"
                + (f"{enabled_line}\n" if enabled_line else ""),
            )
        except Exception as e:
            logger.warning("Не удалось уведомить владельца о разблокировке: %s", e)
# ═══════════════════════════════════════════════════════════════
#  Следим за заходами в «чат админов»
# ═══════════════════════════════════════════════════════════════

async def _bot_admin_status(bot, chat_id: int) -> bool:
    """True, если YamoBot является администратором (или создателем) чата.

    Без прав администратора в супергруппе бот не получает события о заходах
    и обычные сообщения — антирейд физически не может работать.
    """
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        return getattr(member, "status", "") in ("administrator", "creator")
    except Exception as e:
        logger.warning("Не удалось проверить права бота в чате %s: %s", chat_id, e)
        return False


def _prune_joins(chat_id: int, now: float) -> None:
    """Оставляет только свежие заходы, чтобы не копить мусор."""
    _JOINS[chat_id] = [j for j in _JOINS[chat_id] if now - j["ts"] < _JOIN_WINDOW]


def _record_join(chat_id: int, uid: int, name: str,
                 message_id: int | None, now: float) -> bool:
    """Фиксирует заход пользователя в чат админов (с дедупликацией).

    Один и тот же заход может прийти двумя способами — сервисным сообщением
    (new_chat_members) и апдейтом chat_member. Чтобы не задваивать счёт,
    одинаковые заходы в течение короткого окна игнорируются.

    Возвращает True, если заход реально записан.
    """
    key = (chat_id, uid)
    last = _last_join_ts.get(key, 0.0)
    if now - last < _JOIN_DEDUP_SEC:
        return False
    _last_join_ts[key] = now
    _JOINS[chat_id].append({
        "user_id": uid,
        "name": name or f"ID {uid}",
        "ts": now,
        "message_id": message_id,
    })
    return True


async def _maybe_trigger(chat_id: int, settings: dict, bot,
                         chat_title: str, owner_id: int) -> None:
    """Проверяет порог заходов и запускает антирейд, если пора."""
    if settings["triggered"] or chat_id in _ARMED:
        return
    _prune_joins(chat_id, time.monotonic())
    joins = _JOINS[chat_id]
    if len(joins) >= settings["threshold"]:
        await _trigger_antiraid(bot, chat_id, chat_title, owner_id, settings, joins)


def _lock_chat_permissions() -> ChatPermissions:
    """Права для «выключения» чата: всем запрещено писать и приглашать."""
    return ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
    )


def _unlock_chat_permissions() -> ChatPermissions:
    """Полностью открытые права (используются при /выкланти)."""
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=True,
        can_invite_users=True,
        can_pin_messages=True,
    )


async def _restore_chat(bot, chat_id: int) -> bool:
    """Возвращает чату обычные права. True, если получилось."""
    try:
        await bot.set_chat_permissions(chat_id, _unlock_chat_permissions())
        return True
    except Exception as e:
        logger.warning("Не удалось восстановить права чата %s: %s", chat_id, e)
        return False


async def _deactivate_links(bot, chat_id: int) -> None:
    """Старается сделать ссылки на заход неактивными.

    Напрямую Telegram не даёт отозвать все ссылки сразу, поэтому создаём
    новую основную ссылку (старая основная автоматически отзывается) и
    тут же отзываем её.
    """
    try:
        new_link = await bot.export_chat_invite_link(chat_id)
        await bot.revoke_chat_invite_link(chat_id, new_link)
    except Exception as e:
        logger.warning("Не удалось отозвать ссылки-приглашения чата %s: %s", chat_id, e)


async def _remove_recent_joiners(bot, chat_id: int, joins: list[dict]) -> None:
    """Кикает последних зашедших (бан + разбан) и удаляет их сообщения о заходе."""
    for rec in joins:
        uid = rec.get("user_id")
        if not uid:
            continue
        # Сообщение-уведомление о заходе.
        mid = rec.get("message_id")
        if mid:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
        # Кик: баним и сразу разбаниваем (удаляет участника).
        try:
            await bot.ban_chat_member(chat_id, uid)
        except Exception:
            pass
        try:
            await bot.unban_chat_member(chat_id, uid)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  Анти-спам (флуд сообщениями и стикерами в чате админов)
# ═══════════════════════════════════════════════════════════════

def _prune_spam(chat_id: int, uid: int, kind: str, now: float) -> list[dict]:
    """Обрезает журнал сообщений юзера до окон и возвращает свежие записи kind.

    Журнал хранит оба типа (текст и стикеры) с раздельными окнами, поэтому
    чистка одного типа не выбрасывает записи другого.
    """
    key = (chat_id, uid)
    logs = _SPAM_LOG.get(key, [])
    fresh_text = [r for r in logs
                  if r["kind"] == "text" and now - r["ts"] < _MSG_WINDOW]
    fresh_sticker = [r for r in logs
                     if r["kind"] == "sticker" and now - r["ts"] < _STICKER_WINDOW]
    _SPAM_LOG[key] = fresh_text + fresh_sticker
    return [r for r in _SPAM_LOG[key] if r["kind"] == kind]


async def _warn_spammer(bot, chat_id: int, uid: int, kind: str, name: str) -> None:
    """Первая стадия: предупреждение + зачистка сообщений нарушителя."""
    logs = _SPAM_LOG.get((chat_id, uid), [])
    for rec in logs:
        mid = rec.get("message_id")
        if mid:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
    label = "стикерами" if kind == "sticker" else "сообщениями"
    try:
        await bot.send_message(
            chat_id,
            f"⚠️ <b>Антиспам чата админов!</b>\n\n"
            f"<code>{name}</code> флудит {label}. Сообщения удалены.\n"
            f"Повторный флуд — удаление из чата.",
        )
    except Exception as e:
        logger.warning("Не удалось отправить предупреждение о спаме: %s", e)


async def _ban_spammer(bot, chat_id: int, uid: int, kind: str,
                       name: str, owner_id: int, chat_title: str) -> None:
    """Вторая стадия: навсегда убираем спамера из чата админов и зовём владельца."""
    logs = _SPAM_LOG.get((chat_id, uid), [])
    for rec in logs:
        mid = rec.get("message_id")
        if mid:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
    try:
        await bot.ban_chat_member(chat_id, uid)
        banned = True
    except Exception as e:
        logger.warning("Не удалось забанить спамера %s в чате %s: %s",
                       uid, chat_id, e)
        banned = False

    # Если «отзыв ссылок» включён (по умолчанию включён) — отзываем основную
    # ссылку-приглашение, чтобы рейдеры не вернулись по ней же после спама.
    links_revoked = False
    if get_antiraid_settings(owner_id).get("del_links"):
        await _deactivate_links(bot, chat_id)
        links_revoked = True

    # Чистим журнал спама по этому юзеру.
    _SPAM_LOG.pop((chat_id, uid), None)
    for key in [k for k in _SPAM_WARNED if k[0] == chat_id and k[1] == uid]:
        _SPAM_WARNED.pop(key, None)

    title = chat_title or f"чат <code>{chat_id}</code>"
    label = "стикерами" if kind == "sticker" else "сообщениями"
    links_line = "🔗 Ссылка-приглашение отозвана — по ней больше не зайти." if links_revoked else ""
    notice = (
        f"🚫 <b>Спамер удалён из чата.</b>\n\n"
        f"👤 <code>{name}</code> флудил {label} в чате админов.\n"
        f"Его сообщения удалены, аккаунт забанен в чате."
        + ("" if banned else
           "\n\n⚠️ Не удалось забанить — проверь права администратора у бота.")
        + (f"\n{links_line}" if links_line else "")
    )
    try:
        await bot.send_message(chat_id, notice)
    except Exception as e:
        logger.warning("Не удалось отправить уведомление о бане спамера: %s", e)

    try:
        await bot.send_message(
            owner_id,
            f"🚨 <b>Антирейд: спам в чате админов!</b>\n\n"
            f"Чат: <b>{title}</b>\n"
            f"Нарушитель: <code>{name}</code>\n"
            f"Тип: {label}\n\n"
            + ("✅ Спамер удалён из чата." if banned else
               "⚠️ Не удалось удалить нарушителя — проверь права администратора у бота.")
            + (f"\n{links_line}" if links_line else ""),
        )
    except Exception as e:
        logger.warning("Не удалось уведомить владельца о спамере: %s", e)


async def notify_antiraid_promoted_if_bound(event: ChatMemberUpdated) -> None:
    """Вызывается из общего обработчика my_chat_member (profile.py).

    Если бота повысили до администратора в привязанном «чате админов» и там
    включён антирейд — сообщаем владельцу, что защита теперь полностью работает.
    """
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    new_status = getattr(event.new_chat_member, "status", "")
    if new_status not in ("administrator", "creator"):
        return
    owner_id = get_owner_by_admin_chat(chat.id)
    if not owner_id:
        return
    settings = get_antiraid_settings(owner_id)
    if not settings["enabled"] or settings["triggered"]:
        return
    bot = event.bot
    if bot is not None:
        try:
            await bot.send_message(
                owner_id,
                "🛡 <b>Антирейд теперь полностью активен!</b>\n\n"
                "YamoBot получил права администратора в чате админов — "
                "видит заходы и сообщения, может блокировать нарушителей.\n\n"
                "Если защита была отключена с пометкой «не хватает прав» — "
                "напиши в чате <code>/вкланти</code>, чтобы включить её.",
            )
        except Exception as e:
            logger.warning("Не удалось уведомить владельца о повышении прав: %s", e)


async def _trigger_antiraid(bot, chat_id: int, chat_title: str,
                            owner_id: int, settings: dict, joins: list[dict]) -> None:
    """Антирейд сработал: блокируем чат, зовём владельца, гасим ссылки и заходы."""
    _ARMED.add(chat_id)
    set_antiraid_triggered(owner_id, True)
    for key in [k for k in _SPAM_LOG if k[0] == chat_id]:
        _SPAM_LOG.pop(key, None)
    for key in [k for k in _SPAM_WARNED if k[0] == chat_id]:
        _SPAM_WARNED.pop(key, None)

    # 1) Выключаем возможность писать в чате.
    try:
        await bot.set_chat_permissions(chat_id, _lock_chat_permissions())
    except Exception as e:
        logger.warning("Не удалось понизить права чата %s: %s", chat_id, e)
        try:
            await bot.send_message(
                owner_id,
                "🚨 <b>Не удалось заблокировать чат админов!</b>\n\n"
                f"Чат: <b>{chat_title or chat_id}</b>\n\n"
                "Похоже, у YamoBot нет прав администратора — без них Telegram "
                "не даёт менять права чата и убирать нарушителей.\n"
                "Выдай права: «Управление чатом → Администраторы → YamoBot → "
                "Назначить администратором», затем выключи и включи антирейд "
                "командой <code>/выкланти</code> / <code>/вкланти</code>.",
            )
        except Exception as e2:
            logger.warning("Не удалось уведомить владельца об ошибке блокировки: %s", e2)

    # 2) Делаем ссылки на заход неактивными.
    if settings.get("del_links"):
        await _deactivate_links(bot, chat_id)

    # 3) Удаляем последних зашедших.
    if settings.get("del_members"):
        await _remove_recent_joiners(bot, chat_id, joins)

    title = chat_title or f"чат <code>{chat_id}</code>"
    alert = (
        "🚨 <b>Антирейд!</b>\n\n"
        f"В чат <b>{title}</b> зашло за короткое время "
        f"<b>{len(joins)}</b> человек. Это похоже на рейд.\n\n"
        "🔒 Возможность писать в чат выключена.\n"
        "👑 Владелец уведомлён.\n\n"
        "Чтобы вернуть чат в норму:\n"
        "• <code>/вклчат</code> — разблокировать чат (защита останется);\n"
        "• <code>/выкланти</code> — выключить антирейд полностью."
    )
    try:
        await bot.send_message(chat_id, alert)
    except Exception as e:
        logger.warning("Не удалось отправить тревогу в чат %s: %s", chat_id, e)

    try:
        names = ", ".join(rec.get("name") or f"ID {rec.get('user_id')}" for rec in joins[-10:])
        revoked_line = (
            "🔗 Ссылки на заход <b>отозваны</b>."
            if settings.get("del_links")
            else "🔗 Ссылки на заход не отзывались (настройка выключена)."
        )
        await bot.send_message(
            owner_id,
            "🚨 <b>Антирейд сработал!</b>\n\n"
            f"В твой чат админов <b>{title}</b> зашло <b>{len(joins)}</b> человек — "
            "это похоже на рейд.\n\n"
            "✅ Возможность писать в чат <b>выключена</b>.\n"
            f"{revoked_line}\n\n"
            f"<u>Последние зашедшие:</u>\n{names}\n\n"
            "Вернись в чат админов и напиши:\n"
            "• <code>/вклчат</code> — разблокировать чат, оставив антирейд "
            "включённым;\n"
            "• <code>/выкланти</code> — выключить антирейд полностью.",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить владельца %s: %s", owner_id, e)


@router.message(
    F.new_chat_members,
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
)
async def on_new_chat_members(message: Message) -> None:
    """Источник 1 заходов: сервисное сообщение о вступлении."""
    chat_id = message.chat.id
    owner_id = get_owner_by_admin_chat(chat_id)
    if not owner_id:
        return

    settings = get_antiraid_settings(owner_id)
    if not settings["enabled"] or settings["triggered"] or chat_id in _ARMED:
        return

    now = time.monotonic()
    added = 0
    for member in message.new_chat_members or []:
        uid = getattr(member, "id", None)
        if not uid or getattr(member, "is_bot", False):
            continue
        name = getattr(member, "first_name", "") or ""
        if getattr(member, "last_name", None):
            name += f" {member.last_name}"
        if _record_join(chat_id, uid, name.strip(), message.message_id, now):
            added += 1
    if not added:
        return

    chat_title = getattr(message.chat, "title", "") or ""
    await _maybe_trigger(chat_id, settings, message.bot, chat_title, owner_id)


@router.chat_member(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_chat_member_update(event: ChatMemberUpdated) -> None:
    """Источник 2 заходов: апдейт chat_member (надёжный для админов чата).

    Дублирующий источник на случай, если сервисное сообщение недоступно —
    счётчики защищены дедупликацией в _record_join.
    """
    chat_id = event.chat.id
    owner_id = get_owner_by_admin_chat(chat_id)
    if not owner_id:
        return

    settings = get_antiraid_settings(owner_id)
    if not settings["enabled"] or settings["triggered"] or chat_id in _ARMED:
        return

    new_member = getattr(event, "new_chat_member", None)
    if not new_member:
        return
    new_status = getattr(new_member, "status", "")
    if new_status not in ("member", "administrator"):
        return
    old_member = getattr(event, "old_chat_member", None)
    old_status = getattr(old_member, "status", "") if old_member else ""
    was_member = old_status in ("member", "administrator")
    if was_member:
        return

    user = getattr(new_member, "user", None)
    if not user:
        return
    uid = getattr(user, "id", None)
    if not uid or getattr(user, "is_bot", False):
        return
    name = f"{getattr(user, 'first_name', '') or ''}"
    if getattr(user, "last_name", None):
        name += f" {user.last_name}"

    now = time.monotonic()
    if not _record_join(chat_id, uid, name.strip(), None, now):
        return  # Заход уже учтён из сервисного сообщения.

    chat_title = getattr(event.chat, "title", "") or ""
    await _maybe_trigger(chat_id, settings, event.bot, chat_title, owner_id)
# ═══════════════════════════════════════════════════════════════
#  Спам-мониторинг сообщений в чате админов (флуд текстом/стикерами)
# ═══════════════════════════════════════════════════════════════
#
# ВАЖНО про порядок обработчиков: этот обработчик намеренно самый последний
# в файле. Все команды (/вкланти, /вклчат, /выкланти, /стата, /perezap,
# /perestart) зарегистрированы РАНЬШЕ (в этом и предыдущем роутерах) и
# получают апдейты первыми — широкий фильтр ниже их не перехватывает.

@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
)
async def on_admin_chat_message(message: Message) -> None:
    """Реагирует на флуд (текст/стикеры) в привязанном «чате админов».

    Двухстадийная схема:
      1) предупреждение + удаление сообщений нарушителя;
      2) при повторном флуде (или флуде после недавнего предупреждения) —
         юзер навсегда убирается из чата, владелец уведомляется.

    Заодно ЗАПОМИНАЕТ участника чата: Telegram не умеет отдавать боту список
    участников (метода getChatMembers нет), а раздел «В чате, но не в списке»
    строить надо по составу чата. Сюда попадает каждый, кто пишет в чате, даже
    если он никогда не писал боту и его нет в реестре платформы. Учёт сделан
    ДО проверки антирейда, чтобы собирать состав чата независимо от того,
    включён ли антирейд.

    Этот обработчик намеренно последний: команды (/perezap, /perestart и др.)
    зарегистрированы раньше и обрабатываются первыми.
    """
    sender = message.from_user
    if not sender or getattr(sender, "is_bot", False):
        return

    chat_id = message.chat.id
    owner_id = get_owner_by_admin_chat(chat_id)
    if not owner_id:
        return

    # Состав чата собираем всегда, независимо от настроек антирейда.
    remember_admin_chat_member(
        chat_id,
        sender.id,
        getattr(sender, "username", "") or "",
        getattr(sender, "first_name", "") or "",
    )

    settings = get_antiraid_settings(owner_id)
    if not settings["enabled"] or settings["triggered"] or chat_id in _ARMED:
        return

    uid = sender.id
    if uid == owner_id or uid <= 0:
        return  # не следим за самим владельцем.

    if not message.text and not message.sticker:
        return  # нас интересует только текст и стикеры.

    kind = "sticker" if message.sticker else "text"
    now = time.monotonic()
    name = f"{sender.first_name or ''}"
    if getattr(sender, "last_name", None):
        name += f" {sender.last_name}"
    name = name.strip() or f"ID {uid}"

    # Запоминаем свежее сообщение; журнал обрежется до окон при подсчёте.
    _SPAM_LOG[(chat_id, uid)].append({
        "ts": now,
        "message_id": getattr(message, "message_id", None),
        "kind": kind,
    })

    limit = _STICKER_LIMIT if kind == "sticker" else _MSG_LIMIT
    recent = _prune_spam(chat_id, uid, kind, now)
    if len(recent) < limit:
        return

    warn_key = (chat_id, uid, kind)
    last_warn = _SPAM_WARNED.get(warn_key, 0.0)
    chat_title = getattr(message.chat, "title", "") or ""

    if now - last_warn > _SPAM_WARN_COOLDOWN:
        # Ещё не предупреждали (или предупреждали давно) — предупреждаем.
        _SPAM_WARNED[warn_key] = now
        await _warn_spammer(message.bot, chat_id, uid, kind, name)
        return

    # Повторный флуд после свежего предупреждения — бан из чата.
    await _ban_spammer(message.bot, chat_id, uid, kind, name, owner_id, chat_title)