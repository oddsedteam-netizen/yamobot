"""Заходы и выходы в «чат админов».

Бот видит, кто входит в привязанный чат админов, и спрашивает владельца в
личку: добавлять нового участника в админы или нет. При выходе участника —
так же спрашивает, не убрать ли его из списка админов.

Тут же — список участников чата, которых нет в базе админов.
"""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
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
    get_admins_all,
    get_all_users_registry,
    get_bound_chat,
    get_owner_by_admin_chat,
    get_user_registry,
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


async def is_chat_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Спрашивает у Telegram, состоит ли человек в чате.

    Telegram отвечает ошибкой, если человека в чате нет — это и считаем
    «не участник». Если бот не администратор в чате, он всё равно видит
    участников (нужен лишь доступ к getChatMember).
    """
    try:
        await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest as e:
        logger.info("getChatMember %s в чате %s: %s", user_id, chat_id, e)
        return False
    except Exception as e:
        logger.warning("Не удалось проверить участника %s в чате %s: %s",
                       user_id, chat_id, e)
        return False
    return True


# Сколько кандидатов из реестра проверяем за одно нажатие: каждый — запрос
# в Telegram, а чат может быть большим, и список ради этого не должен
# подвисать на десятки секунд.
_MEMBERS_CHECK_LIMIT = 200


async def known_people(bot: Bot, owner_id: int, chat_id: int) -> tuple[list[dict], int, int]:
    """Люди из реестра, которые состоят в чате админов, но не в списке админов.

    Bot API не отдаёт список участников чата (есть только счётчик и
    администраторы), поэтому «Не в списке» строим так: берём людей, которые
    писали ботам (реестр платформы), и каждого проверяем через getChatMember.
    Показываем только тех, кто реально состоит в привязанном чате админов.

    Возвращает (люди, проверено кандидатов, всего кандидатов в реестре).
    """
    admins = {int(a["user_id"]) for a in get_admins_all(owner_id)}
    candidates: list[dict] = []
    for row in get_all_users_registry():
        user_id = int(row.get("user_id") or 0)
        if not user_id or user_id in admins:
            continue
        candidates.append({
            "id": user_id,
            "username": row.get("username") or "",
            "name": row.get("first_name") or "",
        })

    total = len(candidates)
    to_check = candidates[:_MEMBERS_CHECK_LIMIT]

    semaphore = asyncio.Semaphore(8)

    async def check(person: dict) -> dict | None:
        async with semaphore:
            inside = await is_chat_member(bot, chat_id, person["id"])
        return person if inside else None

    results = await asyncio.gather(*(check(p) for p in to_check))
    people = [p for p in results if p is not None]
    return people, len(to_check), total


async def chat_members(bot: Bot, chat_id: int) -> list[dict]:
    """Совместимость: раньше тут пытались читать участников чата напрямую."""
    return []


def _member_label(member: dict) -> str:
    if member.get("username"):
        return f"@{member['username']}"
    return f"ID:{member['id']}"


def not_in_admins_kb(people: list[dict], owner_id: int) -> InlineKeyboardMarkup:
    """Кнопки «🚫 Не в списке»: до 6 участников на кнопку."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for person in people[:12]:
        row.append(InlineKeyboardButton(
            text=_member_label(person)[:18],
            callback_data=f"adm_ask_add_{person['id']}",
            style="primary",
        ))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_notlist",
                                      style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm_notlist")
async def cb_not_in_list(callback: CallbackQuery) -> None:
    """Участники чата админов, которых нет в списке админов."""
    user_id = cb_uid(callback)
    chat_id = get_bound_chat(user_id, "admin")
    if not chat_id:
        await callback.answer("⚠️ Чат админов не привязан", show_alert=True)
        return

    bot = event_bot(callback)
    members = await chat_human_count(bot, chat_id)
    known = {a["user_id"] for a in get_admins_all(user_id)}
    people, checked, total = await known_people(bot, user_id, chat_id)

    text = (
        "🚫 <b>Не в списке админов</b>\n\n"
        f"👥 Участников в чате (без ботов): <b>{members if members is not None else '—'}</b>\n"
        f"📋 В списке админов: <b>{len(known)}</b>\n"
        f"🆕 В чате, но не админы: <b>{len(people)}</b>\n\n"
    )
    if members is None:
        text += ("⚠️ Telegram не отдал число участников: проверь, что чат "
                 "админов привязан и YamoBot — администратор в нём.\n\n")
    if people:
        text += ("Нажми на человека — бот спросит, добавлять ли его в админы.\n"
                 "<i>Показываем только тех, кто состоит в чате админов.</i>")
    else:
        text += "✅ В чате админов нет никого, кого ещё нет в списке."
    if checked < total:
        text += (f"\n\n<i>Проверено {checked} из {total} известных боту людей "
                 f"— остальные не проверены, список может быть неполным.</i>")

    await render_callback(callback, text,
                          not_in_admins_kb(people, user_id) if people else
                          InlineKeyboardMarkup(inline_keyboard=[
                              [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_notlist",
                                                    style="primary")],
                              [InlineKeyboardButton(text="⬅️ Меню админов", callback_data="gadmins",
                                                    style="primary")],
                          ]))


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


@router.callback_query(F.data.regexp(r"^adm_ask_add_\d+$"))
async def cb_ask_add_from_list(callback: CallbackQuery) -> None:
    """Из экрана «Не в списке» — тот же вопрос про добавление."""
    new_admin_id = int(cb_data(callback).rsplit("_", 1)[-1])
    owner_id = cb_uid(callback)
    chat_id = get_bound_chat(owner_id, "admin")
    if not chat_id:
        await callback.answer("⚠️ Чат админов не привязан", show_alert=True)
        return

    if get_admin_by_user_id(owner_id, new_admin_id):
        await callback.answer("⚠️ Уже в списке")
        return

    bot = event_bot(callback)
    if not await is_chat_member(bot, chat_id, new_admin_id):
        await callback.answer("❌ Он не состоит в чате админов", show_alert=True)
        return

    row = get_user_registry(new_admin_id)
    if row is None:
        await callback.answer("❌ Человек не найден среди пользователей бота", show_alert=True)
        return

    await _ask_add_admin(bot, owner_id, new_admin_id,
                         row.get("username") or "", row.get("first_name") or "",
                         "чат админов")
    await callback.answer("Вопрос отправлен в личку")


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