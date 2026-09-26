"""Заходы и выходы в «чат админов».

Бот видит, кто входит в привязанный чат админов, и спрашивает владельца в
личку: добавлять нового участника в админы или нет. При выходе участника —
так же спрашивает, не убрать ли его из списка админов.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import (cb_data, cb_uid, event_bot, msg_uid, render_callback,
                              safe_edit)
from services.storage import (
    add_admin,
    get_admin_by_user_id,
    get_bound_chat,
    get_owner_by_admin_chat,
    remove_admin,
)

logger = logging.getLogger(__name__)

router = Router()

# Сколько участников чата просматриваем максимум за один раз (Telegram
# отдаёт участников постранично, а чат может быть большим).
_MEMBERS_PAGE = 200
_MEMBERS_LIMIT = 500


class AdminJoinFSM(StatesGroup):
    waiting_tag = State()      # владельцу просим тег для нового админа
    waiting_leave_id = State()  # ждём ответа «удалить из админов» по ID


async def chat_human_count(bot: Bot, chat_id: int | None) -> int | None:
    """Сколько людей (без ботов) в чате админов.

    None, если чат не привязан или Telegram не дал число (например, бот не
    администратор) — тогда в интерфейсе показываем прочерк, а не ноль.
    """
    if not chat_id:
        return None

    # В aiogram 3.31 это get_chat_member_count / get_chat_administrators
    # (а не get_chat_members_count / get_administrators из Bot API).
    try:
        total = await bot.get_chat_member_count(chat_id)
    except Exception as e:
        logger.warning("Число участников чата %s недоступно: %s", chat_id, e)
        return None

    bots_here = 0
    try:
        for member in await bot.get_chat_administrators(chat_id):
            user = getattr(member, "user", None)
            if user is not None and getattr(user, "is_bot", False):
                bots_here += 1
    except Exception as e:
        # Нет прав на список админов — считаем всех, но предупреждаем в лог.
        logger.info("Список админов чата %s недоступен: %s", chat_id, e)

    return max(0, int(total) - bots_here)


# ═══════════════ Вопрос «добавить в админы?» ═══════════════

async def _ask_add_admin(bot: Bot, owner_id: int, user_id: int,
                         username: str, first_name: str, chat_title: str) -> None:
    """Спрашивает в ЛС владельца: добавлять ли нового участника в админы."""
    who = f"@{username}" if username else f"ID:{user_id}"
    name = f" ({first_name})" if first_name else ""
    text = (
        "🆕 <b>Новый участник в чате админов</b>\n\n"
        f"👤 {who}{name}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"💬 Чат: <b>{chat_title}</b>\n\n"
        "Добавить его в список админов?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, добавить", callback_data=f"adm_join_yes_{user_id}",
                                 style="success"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"adm_join_no_{user_id}",
                                 style="danger"),
        ]
    ])
    try:
        await bot.send_message(owner_id, text, reply_markup=kb)
    except Exception as e:
        logger.info("Не удалось спросить про нового админа: %s", e)


async def _ask_remove_admin(bot: Bot, owner_id: int, user_id: int,
                            username: str, first_name: str, chat_title: str) -> None:
    """Спрашивает в ЛС: убрать ли участника из списка админов (он вышел)."""
    who = f"@{username}" if username else f"ID:{user_id}"
    name = f" ({first_name})" if first_name else ""
    text = (
        "🚪 <b>Админ вышел из чата</b>\n\n"
        f"👤 {who}{name}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"💬 Чат: <b>{chat_title}</b>\n\n"
        "Он был в списке админов. Убрать его из списка?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Убрать из админов",
                                 callback_data=f"adm_leave_yes_{user_id}", style="danger"),
            InlineKeyboardButton(text="❌ Оставить", callback_data=f"adm_leave_no_{user_id}",
                                 style="primary"),
        ]
    ])
    try:
        await bot.send_message(owner_id, text, reply_markup=kb)
    except Exception as e:
        logger.info("Не удалось спросить про вышедшего админа: %s", e)


# ═══════════════ Ответы владельца: «добавить в админы?» ═══════════════

@router.callback_query(F.data.regexp(r"^adm_join_no_\d+$"))
async def cb_join_no(callback: CallbackQuery) -> None:
    """Владелец отказался — просто отменяем, ничего не меняем."""
    await callback.answer("👌 Не добавляю")
    if callback.message:
        await safe_edit(callback.message,
                        "👌 <b>Не добавляю.</b>\n\n"
                        "Участник останется без прав админа.",
                        InlineKeyboardMarkup(inline_keyboard=[]))


@router.callback_query(F.data.regexp(r"^adm_join_yes_\d+$"))
async def cb_join_yes(callback: CallbackQuery, state: FSMContext) -> None:
    """Владелец согласился — спрашиваем тег и добавляем в список админов."""
    new_admin_id = int(cb_data(callback).rsplit("_", 1)[-1])
    owner_id = cb_uid(callback)

    if get_admin_by_user_id(owner_id, new_admin_id):
        await callback.answer("⚠️ Уже в списке")
        if callback.message:
            await safe_edit(callback.message, "⚠️ Этот человек уже в списке админов.",
                            InlineKeyboardMarkup(inline_keyboard=[]))
        return

    await state.set_state(AdminJoinFSM.waiting_tag)
    await state.update_data(adm_new_id=new_admin_id)
    await safe_edit(
        callback.message,
        "🏷 <b>Придумай тег для этого админа</b>\n\n"
        "Это короткое имя, по которому админа узнают в топиках — по его роли "
        "в команде.\n\n"
        "Напиши тег одним словом 👇",
        InlineKeyboardMarkup(inline_keyboard=[]),
    )
    await callback.answer()


@router.message(AdminJoinFSM.waiting_tag)
async def fsm_join_tag(message: Message, state: FSMContext) -> None:
    """Сохраняем тег и добавляем нового админа."""
    data = await state.get_data()
    owner_id = msg_uid(message)
    new_admin_id = int(data.get("adm_new_id") or 0)
    tag = (message.text or "").strip().lstrip("#")[:16]

    if not new_admin_id or not tag:
        await message.answer("❌ Нужен тег одним словом. Напиши его ещё раз.")
        return

    # Username забираем у Telegram: у многих он есть, но вводить его вручную
    # больше не нужно.
    username = ""
    chat_id = get_bound_chat(owner_id, "admin")
    if chat_id:
        try:
            member = await event_bot(message).get_chat_member(chat_id, new_admin_id)
            username = getattr(member.user, "username", "") or ""
        except Exception:
            pass

    add_admin(owner_id, new_admin_id, username, tag)
    await state.clear()
    await message.answer(
        "✅ <b>Добавлен в админы</b>\n\n"
        f"🆔 <code>{new_admin_id}</code>\n"
        + (f"👤 @{username}\n" if username else "")
        + f"🏷 #{tag}\n\n"
        "Он привязан ко всем твоим ботам.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список админов", callback_data="gadmins_list",
                                  style="primary")]
        ]),
    )


# ═══════════════ Ответы: убрать админа, который вышел ═══════════════

@router.callback_query(F.data.regexp(r"^adm_leave_no_\d+$"))
async def cb_leave_no(callback: CallbackQuery) -> None:
    """Оставляем админа в списке."""
    await callback.answer("👌 Оставляю")
    if callback.message:
        await safe_edit(callback.message,
                        "👌 <b>Оставляю в списке админов.</b>\n\n"
                        "Он по-прежнему сможет брать ПЗ.",
                        InlineKeyboardMarkup(inline_keyboard=[]))


@router.callback_query(F.data.regexp(r"^adm_leave_yes_\d+$"))
async def cb_leave_yes(callback: CallbackQuery) -> None:
    """Убираем админа из списка."""
    user_id = int(cb_data(callback).rsplit("_", 1)[-1])
    removed = remove_admin(cb_uid(callback), user_id)
    await callback.answer("🗑 Убрал" if removed else "Не найден")
    if callback.message:
        if removed:
            await safe_edit(
                callback.message,
                "🗑 <b>Убран из списка админов.</b>",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Список админов", callback_data="gadmins_list",
                                          style="primary")]
                ]),
            )
        else:
            await safe_edit(callback.message, "⚠️ Его не было в списке админов.",
                            InlineKeyboardMarkup(inline_keyboard=[]))


# ═══════════════ Заходы и выходы в чат админов ═══════════════

@router.chat_member()
async def on_admin_chat_member(event: ChatMemberUpdated) -> None:
    """Новый участник чата админов → вопрос владельцу; выход → предложение убрать.

    Бот должен быть администратором чата, иначе Telegram не присылает эти
    события — это уже написано в подсказке при привязке чата админов.
    """
    chat = event.chat
    if chat is None or str(chat.type) not in ("group", "supergroup"):
        return

    owner_id = get_owner_by_admin_chat(chat.id)
    if not owner_id:
        return  # это не привязанный чат админов

    member = getattr(event, "new_chat_member", None)
    old = getattr(event, "old_chat_member", None)
    if member is None or old is None:
        return

    user = getattr(member, "user", None)
    if user is None or getattr(user, "is_bot", False):
        return

    username = getattr(user, "username", "") or ""
    first_name = getattr(user, "first_name", "") or ""
    chat_title = chat.title or "чат админов"
    new_status = str(getattr(member, "status", ""))
    old_status = str(getattr(old, "status", ""))
    inside = {"member", "administrator", "creator", "restricted"}

    # ── Вышел или был исключён ──
    if new_status in ("left", "kicked"):
        if get_admin_by_user_id(owner_id, user.id):
            await _ask_remove_admin(event_bot(event), owner_id, user.id,
                                    username, first_name, chat_title)
        return

    # ── Зашёл в чат ──
    if new_status in inside and old_status not in inside:
        if get_admin_by_user_id(owner_id, user.id):
            return  # уже админ — спрашивать нечего
        await _ask_add_admin(event_bot(event), owner_id, user.id,
                             username, first_name, chat_title)