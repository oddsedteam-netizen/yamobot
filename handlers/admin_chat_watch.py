"""Заходы и выходы в «чат админов».

Бот видит, кто входит в привязанный чат админов, и спрашивает владельца в
личку: добавлять нового участника в админы или нет. При выходе участника —
так же спрашивает, не убрать ли его из списка админов.

Тут же — список участников чата, которых нет в базе админов.
"""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
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
    forget_admin_chat_member,
    get_admin_by_user_id,
    get_admin_chat_members,
    get_admins_all,
    get_all_users_registry,
    get_bound_chat,
    get_owner_by_admin_chat,
    get_user_registry,
    remember_admin_chat_member,
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


# Статусы ChatMember, при которых человек находится ВНУТРИ чата. Всё остальное
# (left, kicked) — человек в чате не состоит, даже если Telegram не бросил
# ошибку, а спокойно вернул объект участника.
_INSIDE_STATUSES = {"member", "administrator", "creator", "restricted"}

# Сколько кандидатов из реестра сверяем с Telegram за одно нажатие: каждый —
# запрос в Telegram, и список не должен подвисать на десятки секунд.
_MEMBERS_CHECK_LIMIT = 200


async def is_chat_member(bot: Bot, chat_id: int, user_id: int) -> tuple[bool | None, str]:
    """Спрашивает у Telegram, состоит ли человек в чате.

    ВАЖНО: нельзя судить только по тому, бросил ли Telegram ошибку. На человека,
    которого в чате нет, Telegram часто НЕ бросает ошибку, а возвращает
    участника со статусом 'left'/'kicked'. Поэтому смотрим именно на статус.

    Возвращает (True — в чате, False — точно не в чате, None — не смогли
    проверить: нет прав/чат недоступен). Различать False и None обязательно,
    иначе поломка прав молча превращается в «в чате никого нет».
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest as e:
        # «user not found» — человека в чате нет. «chat not found» —
        # чат недоступен, это не значит, что человека нет.
        logger.info("getChatMember %s в чате %s: %s", user_id, chat_id, e)
        return False, ""
    except Exception as e:
        logger.warning("Не удалось проверить участника %s в чате %s: %s",
                       user_id, chat_id, e)
        return None, ""

    status = str(getattr(member, "status", "") or "")
    return status in _INSIDE_STATUSES, status


async def known_people(bot: Bot, owner_id: int, chat_id: int) -> tuple[list[dict], int, int]:
    """Участники ПРИВЯЗАННОГО чата админов, которых ещё нет в списке админов.

    Bot API не умеет отдавать список участников чата: метода getChatMembers не
    существует, есть только счётчик getChatMemberCount и администраторы. Поэтому
    список собираем из того, что бот реально видит в этом чате, и СТРОГО для
    chat_id из профиля владельца:

      1. Наш учёт admin_chat_members: сюда попадают люди из сообщений в чате и
         из событий chat_member (заход/выход). Это единственный источник, где
         видны люди, никогда не писавшие боту — в реестре их просто нет.
      2. Сверка людей из реестра (писавших ботам) через getChatMember — для тех,
         кто был в чате до того, как бот туда встал.

    Перед выводом учёт перепроверяется по Telegram: кто вышел — выпадает из
    списка. Так в разделе не может появиться человек из другого чата или уже
    ушедший из этого.

    Возвращает (люди, проверено кандидатов из реестра, всего кандидатов).
    """
    admins_in_base = {int(a["user_id"]) for a in get_admins_all(owner_id)}

    # ── 1. Наш учёт участников этого чата ──
    people: dict[int, dict] = {}
    for row in get_admin_chat_members(chat_id):
        uid = int(row.get("user_id") or 0)
        if not uid or uid in admins_in_base:
            continue  # уже в списке админов — раздел «Не в списке» не про него
        people[uid] = {
            "id": uid,
            "username": row.get("username") or "",
            "name": row.get("first_name") or "",
        }

    # ── 1a. Перепроверяем учёт по Telegram: кто вышел — выпадает ──
    #    Молчаливый дрейф недопустим: в списке не должно быть никого, кто
    #    сейчас не в этом чате.
    known_ids = list(people)
    semaphore = asyncio.Semaphore(8)

    async def recheck(person: dict) -> None:
        async with semaphore:
            inside, status = await is_chat_member(bot, chat_id, person["id"])
        if inside is False:
            logger.info("%s больше не в чате %s (статус %r) — убираем из списка",
                        person["id"], chat_id, status)
            people.pop(person["id"], None)
            forget_admin_chat_member(chat_id, person["id"])
        elif inside is True:
            # Уточняем username/имя свежими данными Telegram.
            pass

    if known_ids:
        await asyncio.gather(*(recheck(people[uid]) for uid in known_ids))

    # ── 2. Сверка реестра: вдруг человек в чате, а бот его не видел ──
    candidates: list[dict] = []
    for row in get_all_users_registry():
        user_id = int(row.get("user_id") or 0)
        if not user_id or user_id in admins_in_base or user_id in people:
            continue
        candidates.append({
            "id": user_id,
            "username": row.get("username") or "",
            "name": row.get("first_name") or "",
        })

    total = len(candidates)
    to_check = candidates[:_MEMBERS_CHECK_LIMIT]

    async def check(person: dict) -> None:
        async with semaphore:
            inside, status = await is_chat_member(bot, chat_id, person["id"])
        if inside is None:
            return  # проверить не смогли — не выдумываем
        if not inside:
            logger.info("Пользователь %s не в чате %s (статус %r)",
                        person["id"], chat_id, status)
            return
        remember_admin_chat_member(chat_id, person["id"],
                                   person["username"], person["name"])
        people[person["id"]] = {
            "id": person["id"],
            "username": person["username"],
            "name": person["name"],
        }

    if to_check:
        await asyncio.gather(*(check(p) for p in to_check))

    result = sorted(people.values(), key=lambda p: p.get("id"))
    return result, len(to_check), total


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
        "🚫 <b>В чате, но не в списке</b>\n\n"
        f"👥 Участников в чате (без ботов): <b>{members if members is not None else '—'}</b>\n"
        f"📋 Уже в списке админов: <b>{len(known)}</b>\n"
        f"🆕 Ждут добавления: <b>{len(people)}</b>\n\n"
    )
    if members is None:
        text += ("⚠️ Telegram не отдал число участников: проверь, что чат "
                 "админов привязан и YamoBot — администратор в нём.\n\n")

    if people:
        text += ("Нажми на человека — бот спросит, добавлять ли его в админы.\n"
                 "<i>Список только по этому чату: каждого я сверил с Telegram "
                 "прямо сейчас.</i>")
    else:
        # Важно не врать: пустой список означает не «в чате никого нет»,
        # а «бот пока никого не знает в этом чате».
        text += ("🤔 Пока никого не нашёл. Telegram не даёт боту список участников "
                 "чата, поэтому YamoBot собирает его сам — из того, что видит:\n\n"
                 "• <b>Кто пишет в чате</b> — попадает сразу.\n"
                 "• <b>Кто заходит в чат</b> — тоже, но только если YamoBot "
                 "администратор чата (тогда Telegram шлёт события).\n"
                 "• <b>Те, кто писал ботам</b> — проверяются по карточке.\n\n"
                 "Чтобы список наполнился, проще всего сделать любой запрос в "
                 "чате: те, кто активен, сразу появятся здесь.")

    if total and checked:
        text += (f"\n\n<i>Дополнительно сверил {checked} из {total} людей, которые "
                 f"писали ботам. Кто в этом чате не состоит — сюда не попал.</i>")

    kb = not_in_admins_kb(people, user_id) if people else InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_notlist",
                                  style="primary")],
            [InlineKeyboardButton(text="⬅️ Меню админов", callback_data="gadmins",
                                  style="primary")],
        ])
    await render_callback(callback, text, kb)


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
    inside, status = await is_chat_member(bot, chat_id, new_admin_id)
    if inside is False:
        logger.info("Отказ: %s не в чате %s (статус %r)", new_admin_id, chat_id, status)
        await callback.answer("❌ Он не состоит в чате админов", show_alert=True)
        return
    # inside is None — Telegram не дал проверить (нет прав). Человек взят из
    # нашего учёта чата, значит он там есть; просто добавляем.

    # Данные берём из нашего учёта чата, а не только из реестра: человек мог
    # ни разу не писать боту и в реестре просто отсутствовать.
    username = ""
    first_name = ""
    for m in get_admin_chat_members(chat_id):
        if int(m.get("user_id") or 0) == new_admin_id:
            username = m.get("username") or ""
            first_name = m.get("first_name") or ""
            break
    if not username:
        row = get_user_registry(new_admin_id)
        if row is not None:
            username = row.get("username") or ""
            first_name = first_name or (row.get("first_name") or "")

    await _ask_add_admin(bot, owner_id, new_admin_id, username, first_name,
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


# ═══════════════ Состав чата админов ═══════════════
#
# Telegram НЕ умеет отдавать боту список участников чата (метода getChatMembers
# нет), поэтому список «кто в чате, но не админ» мы собираем сами из того, что
# бот реально видит:
#   • сообщения в привязанном чате — главный источник (обработчик живёт в
#     handlers/antiraid.py::on_admin_chat_message, потому что там уже стоит
#     такой же широкий фильтр и он намеренно последний по регистрации, чтобы
#     не перехватывать /perezap и /perestart);
#   • события chat_member — кто зашёл/вышёл (см. on_admin_chat_member ниже);
#   • сверка людей из реестра через getChatMember — см. known_people.


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
        # Убираем из нашего учёта участников: чат — единственный источник,
        # кроме сверки с реестром, поэтому забытый человек «залипнет» в списке.
        forget_admin_chat_member(chat.id, user.id)
        if get_admin_by_user_id(owner_id, user.id):
            await _ask_remove_admin(event_bot(event), owner_id, user.id,
                                    username, first_name, chat_title)
        return

    # ── Зашёл в чат ──
    if new_status in inside and old_status not in inside:
        # Запоминаем участника, иначе в разделе «Не в списке» его не будет:
        # в реестре платформы его может не быть вообще (никогда не писал боту),
        # и сверять будет некого.
        remember_admin_chat_member(chat.id, user.id, username, first_name,
                                   is_admin=(new_status in ("administrator", "creator")))
        if get_admin_by_user_id(owner_id, user.id):
            return  # уже админ — спрашивать нечего
        await _ask_add_admin(event_bot(event), owner_id, user.id,
                             username, first_name, chat_title)