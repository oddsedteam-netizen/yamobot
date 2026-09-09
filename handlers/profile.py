from aiogram import Router, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import (render_callback, ADMIN_CHAT_WELCOME, cb_data,
                              cb_uid, cb_username, cb_firstname, msg_uid,
                              msg_username, msg_firstname, try_edit_answer)
from services.config import is_super_admin
from services.constants import BOT_VERSION
from services.storage import (
    get_all_users_registry,
    get_user_bots,
    get_admins_all,
    get_all_topics_for_bot,
    get_stats,
    bot_display_name,
    is_registry_user_banned,
    set_registry_user_blocked,
    remove_user_bot,
    get_bound_chat,
    set_bound_chat,
    get_pending_bind,
    set_pending_bind,
    get_user_registry,
    get_bot_by_id_any_owner,
    create_transfer,
    get_transfer,
    delete_transfer,
    transfer_all_rights,
    transfer_bot,
)

router = Router()

# Ожидание привязки чатов: user_id -> kind ("work"|"admin").
_PENDING_BINDS: dict[int, str] = {}
# Последний добавленный чат для юзера: user_id -> chat_id (для кнопки «я добавил бота»).
_LAST_ADDED: dict[int, int] = {}



def _user_line(u: dict) -> str:
    name = u.get("username") or u.get("first_name") or str(u["user_id"])
    status = "🚫" if u.get("blocked") else "🟢"
    return f"{status} {name}"


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Список жалоб", callback_data="complaints_admin")],
        [InlineKeyboardButton(text="👥 Профили пользователей", callback_data="profiles_list")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")],
    ])


def profiles_kb(users: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for u in users:
        rows.append([InlineKeyboardButton(
            text=_user_line(u), callback_data=f"profile_view_{u['user_id']}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="profile_admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_admin_kb(user_id: int) -> InlineKeyboardMarkup:
    banned = is_registry_user_banned(user_id)
    ban_btn = "🚫 Забанить" if not banned else "✅ Разбанить"
    ban_data = f"profile_ban_{user_id}" if not banned else f"profile_unban_{user_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ban_btn, callback_data=ban_data)],
        [InlineKeyboardButton(text="📊 Статистика", callback_data=f"profile_stats_{user_id}")],
        [InlineKeyboardButton(text="🗑 Удалить ботов", callback_data=f"profile_del_bots_{user_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="profiles_list")],
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="profile_admin")],
    ])


def _profile_payload(user_id: int, first_name: str) -> tuple[str, InlineKeyboardMarkup]:
    """Собирает текст и клавиатуру профиля (используется и для message, и для callback)."""
    bots = get_user_bots(user_id)
    admins = get_admins_all(user_id)

    lines = []
    total_pz = 0
    for b in bots:
        topics = get_all_topics_for_bot(b["id"])
        total_pz += len(topics)
        lines.append(f"  • {bot_display_name(b)} — 📋 ПЗ: <b>{len(topics)}</b>")
    bots_list = "\n".join(lines) if lines else "  — нет ботов —"

    work_chat = get_bound_chat(user_id, "work")
    admin_chat = get_bound_chat(user_id, "admin")
    work_line = f"<code>{work_chat}</code>" if work_chat else "не привязан"
    admin_line = f"<code>{admin_chat}</code>" if admin_chat else "не привязан"

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"📛 Имя: <b>{first_name}</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"
        f"🤖 Ботов: <b>{len(bots)}</b>\n"
        f"👥 Админов: <b>{len(admins)}</b>\n"
        f"📋 Всего ПЗ: <b>{total_pz}</b>\n\n"
        f"💼 Чат работы: {work_line}\n"
        f"🛡 Чат админов: {admin_line}\n\n"
        f"<b>По ботам:</b>\n{bots_list}\n\n"
        f"⚙️ Версия бота: <b>{BOT_VERSION}</b>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Боты", callback_data="my_bots")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="gadmins")],
        [InlineKeyboardButton(text="📋 ПЗ", callback_data="gpz")],
        [InlineKeyboardButton(text="💼 Чат работы", callback_data="bind_work")],
        [InlineKeyboardButton(text="🛡 Чат админов", callback_data="bind_admin")],
    ])
    if work_chat:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="❌ Отвязать чат работы", callback_data="unbind_work")
        ])
    if admin_chat:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="❌ Отвязать чат админов", callback_data="unbind_admin")
        ])
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="👑 Передать права", callback_data="transfer")
    ])
    if is_super_admin(user_id):
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="🛡 Админ-панель", callback_data="profile_admin")
        ])

    return text, kb


async def show_profile(message: Message) -> None:
    text, kb = _profile_payload(msg_uid(message), msg_firstname(message) or "—")
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "profile_show")
async def cb_profile_show(callback: CallbackQuery) -> None:
    """Открывает профиль из инлайн-колбэка (без нового приветствия)."""
    _PENDING_BINDS.pop(cb_uid(callback), None)
    set_pending_bind(cb_uid(callback), None)
    if callback.message is None:
        return
    text, kb = _profile_payload(cb_uid(callback), cb_firstname(callback) or "—")
    await render_callback(callback, text, kb)


# ═══════════════ Передача прав владельца ═══════════════

async def _master_bot_username(bot) -> str:
    """Возвращает username мастер-бота (YamoBot) для ссылки-приглашения."""
    try:
        me = await bot.get_me()
        return me.username or ""
    except Exception:
        return ""


def _user_display(user_id: int) -> str:
    """Имя пользователя YamoBot (для подписи «<ник> передаёт вам права»)."""
    u = get_user_registry(user_id)
    if u:
        return u.get("username") or u.get("first_name") or f"ID:{user_id}"
    return f"ID:{user_id}"


def _rights_lines_from(transfer: dict) -> list[str]:
    """Читаемый список того, что передаётся, по данным ссылки-передачи."""
    kind = transfer.get("kind")
    if kind == "bot" and transfer.get("bot_id"):
        bot = get_bot_by_id_any_owner(int(transfer["bot_id"]))
        if bot:
            return [f"• {bot_display_name(bot)}"]
        return ["• бот (удалён)"]
    bots = get_user_bots(transfer.get("from_user_id") or 0)
    if not bots:
        return ["• все права"]
    return [f"• {bot_display_name(b)}" for b in bots]


@router.callback_query(F.data == "transfer")
async def cb_transfer_open(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    bots = get_user_bots(user_id)
    if not bots:
        await render_callback(
            callback,
            "👑 <b>Передача прав</b>\n\nУ тебя нет ботов — передавать нечего.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")]
            ]),
        )
        return

    text = (
        "👑 <b>Передача прав</b>\n\n"
        "⚠️ <b>Внимание!</b> При передаче все привязанные данные "
        "(боты, админы, совладельцы, привязанные чаты и настройки) "
        "перейдут другому владельцу.\n\n"
        "Что передаём?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👑 Все права", callback_data="transfer_all")],
        [InlineKeyboardButton(text="🤖 Только одного бота", callback_data="transfer_one")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")],
    ])
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "transfer_one")
async def cb_transfer_one(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    bots = get_user_bots(user_id)
    if not bots:
        await callback.answer("У тебя нет ботов", show_alert=True)
        return

    rows = [
        [InlineKeyboardButton(text=bot_display_name(b), callback_data=f"transfer_pick_{b['id']}")]
        for b in bots
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="transfer")])
    await render_callback(
        callback,
        "🤖 <b>Передать одного бота</b>\n\nВыбери бота, которого хочешь передать:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _send_confirm_link(callback: CallbackQuery, token: str) -> None:
    """Отправляет владельцу ссылку для передачи прав."""
    username = await _master_bot_username(callback.bot)
    if not username:
        await callback.answer("⚠️ Не удалось сформировать ссылку", show_alert=True)
        return
    link = f"https://t.me/{username}?start=transfer_{token}"
    text = (
        "🔗 <b>Ссылка для передачи прав готова!</b>\n\n"
        "Отправь её новому владельцу. После перехода он должен подтвердить принятие.\n\n"
        f"<code>{link}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")]
    ])
    if callback.message:
        await try_edit_answer(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "transfer_all")
async def cb_transfer_all(callback: CallbackQuery) -> None:
    token = create_transfer(cb_uid(callback), "all")
    await _send_confirm_link(callback, token)


@router.callback_query(F.data.startswith("transfer_pick_"))
async def cb_transfer_pick(callback: CallbackQuery) -> None:
    bot_id = int(cb_data(callback).split("_")[-1])
    token = create_transfer(cb_uid(callback), "bot", bot_id)
    await _send_confirm_link(callback, token)


async def handle_transfer_link(message: Message, token: str) -> None:
    """Обрабатывает переход нового владельца по ссылке `?start=transfer_<token>`."""
    transfer = get_transfer(token)
    if not transfer:
        await message.answer("❌ Ссылка на передачу прав недействительна.")
        return

    from_uid = transfer.get("from_user_id")
    to_uid = msg_uid(message)

    if to_uid == from_uid:
        await message.answer("⚠️ Ты не можешь передать права самому себе.")
        return
    if is_registry_user_banned(to_uid) and not is_super_admin(to_uid):
        await message.answer("🚫 Вы заблокированы администрацией.")
        return

    lines = _rights_lines_from(transfer)

    # Регистрируем нового владельца в реестре.
    if not get_user_registry(to_uid):
        from services.storage import register_user
        register_user(to_uid, msg_username(message) or "", msg_firstname(message) or "")

    text = (
        f"👑 <b>Вам передают права!</b>\n\n"
        f"Пользователь <b>{_user_display(int(from_uid or 0))}</b> передаёт вам следующие права:\n"
        + "\n".join(lines)
        + "\n\nПодтверди принятие, чтобы данные перешли к тебе навсегда."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"transfer_accept_{token}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"transfer_reject_{token}")],
    ])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("transfer_accept_"))
async def cb_transfer_accept(callback: CallbackQuery) -> None:
    from aiogram.exceptions import TelegramBadRequest
    token = cb_data(callback).split("transfer_accept_", 1)[1]
    transfer = get_transfer(token)
    if not transfer:
        await callback.answer("⚠️ Ссылка уже недействительна.", show_alert=True)
        return

    from_uid = transfer.get("from_user_id")
    to_uid = cb_uid(callback)
    kind = transfer.get("kind")

    if to_uid == from_uid:
        await callback.answer("⚠️ Нельзя принять у самого себя.", show_alert=True)
        return

    username = cb_username(callback) or ""
    first_name = cb_firstname(callback) or ""

    if kind == "bot":
        bot_id = int(transfer.get("bot_id") or 0)
        ok = transfer_bot(int(from_uid or 0), to_uid, bot_id, username, first_name)
        if not ok:
            await callback.answer("⚠️ Не удалось передать бота.", show_alert=True)
            return
        bot = get_bot_by_id_any_owner(bot_id)
        bot_name = bot_display_name(bot) if bot else f"бот {bot_id}"
        rights_text = f"• {bot_name}"
        summary = f"🤖 Теперь бот <b>{bot_name}</b> принадлежит тебе."
    else:
        count = transfer_all_rights(int(from_uid or 0), to_uid, username, first_name)
        rights_text = "все права"
        summary = f"👑 <b>Все права приняты!</b>\n\nПередано ботов: <b>{count}</b>."

    delete_transfer(token)

    if callback.message:
        await try_edit_answer(
            callback.message,
            summary,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile_show")]
            ]),
        )

    # Уведомляем старого владельца.
    try:
        bot = getattr(callback, "bot", None)
        if bot is not None:
            await bot.send_message(
                int(from_uid or 0),
                f"🔁 <b>Права переданы.</b>\n\n"
                f"<b>{_user_display(to_uid)}</b> принял ваши права: {rights_text}.",
            )
    except Exception:
        pass


@router.callback_query(F.data.startswith("transfer_reject_"))
async def cb_transfer_reject(callback: CallbackQuery) -> None:
    token = cb_data(callback).split("transfer_reject_", 1)[1]
    transfer = get_transfer(token)
    delete_transfer(token)

    if callback.message:
        await try_edit_answer(callback.message, "❌ <b>Вы отклонили передачу прав.</b>")

    if transfer:
        try:
            bot = getattr(callback, "bot", None)
            if bot is not None:
                await bot.send_message(
                    int(transfer.get("from_user_id") or 0),
                    "❌ Новый владелец отклонил передачу прав.",
                )
        except Exception:
            pass


# ═══════════════ Админ-панель пользователей ═══════════════

@router.callback_query(F.data == "profile_admin")
async def cb_profile_admin(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await render_callback(callback, "🛡 <b>Админ-панель</b>\n\nВыбери раздел:", admin_kb())


@router.callback_query(F.data == "profiles_list")
async def cb_profiles_list(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    users = get_all_users_registry()
    if not users:
        await render_callback(callback, "👥 <b>Профили</b>\n\nПока нет пользователей.", admin_kb())
        return
    text = f"👥 <b>Профили пользователей</b> ({len(users)})\n\nВыбери пользователя:"
    await render_callback(callback, text, profiles_kb(users))


@router.callback_query(F.data.startswith("profile_view_"))
async def cb_profile_view(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(cb_data(callback).split("_")[-1])
    users = [u for u in get_all_users_registry() if u["user_id"] == uid]
    if not users:
        await callback.answer("Пользователь не найден")
        return
    u = users[0]
    bots = get_user_bots(uid)
    status = "🚫 заблокирован" if u.get("blocked") else "🟢 активен"
    text = (
        f"👤 <b>{u.get('username') or u.get('first_name') or uid}</b>\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📅 Регистрация: {u.get('created_at', '—')[:10]}\n"
        f"🤖 Ботов: <b>{len(bots)}</b>\n"
        f"📌 Статус: {status}"
    )
    await render_callback(callback, text, profile_admin_kb(uid))


@router.callback_query(F.data.startswith("profile_ban_"))
async def cb_profile_ban(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(cb_data(callback).split("_")[-1])
    set_registry_user_blocked(uid, True)
    await render_callback(callback, f"🚫 Пользователь <code>{uid}</code> забанен.", profile_admin_kb(uid))


@router.callback_query(F.data.startswith("profile_unban_"))
async def cb_profile_unban(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(cb_data(callback).split("_")[-1])
    set_registry_user_blocked(uid, False)
    await render_callback(callback, f"✅ Пользователь <code>{uid}</code> разбанен.", profile_admin_kb(uid))


@router.callback_query(F.data.startswith("profile_stats_"))
async def cb_profile_stats(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(cb_data(callback).split("_")[-1])
    bots = get_user_bots(uid)
    lines = []
    total = {"users_total": 0, "messages_in": 0, "messages_out": 0}
    for b in bots:
        s = get_stats(b["id"])
        for k in total:
            total[k] += s[k]
        lines.append(f"  • {bot_display_name(b)} — 👥 {s['users_total']}")
    bot_lines = "\n".join(lines) if lines else "  — нет ботов —"
    text = (
        f"📊 <b>Статистика пользователя</b> <code>{uid}</code>\n\n"
        f"👥 Всего пользователей: <b>{total['users_total']}</b>\n"
        f"📩 Получено: <b>{total['messages_in']}</b>\n"
        f"📤 Отправлено: <b>{total['messages_out']}</b>\n\n"
        f"{bot_lines}"
    )
    await render_callback(callback, text, profile_admin_kb(uid))


@router.callback_query(F.data.startswith("profile_del_bots_"))
async def cb_profile_del_bots(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(cb_data(callback).split("_")[-1])
    bots = get_user_bots(uid)
    for b in bots:
        remove_user_bot(uid, b["id"])
    await render_callback(callback, f"🗑 Удалены все боты пользователя <code>{uid}</code>.", profile_admin_kb(uid))


# ═══════════════ Привязка «чата работы» и «чата админов» ═══════════════

_BIND_LABELS = {
    "work": "💼 Чат работы",
    "admin": "🛡 Чат админов",
}


def _bind_wait_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я добавил бота", callback_data="bind_done")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="bind_cancel")],
    ])


async def _bind_instructions(callback: CallbackQuery, kind: str) -> str:
    try:
        bot = getattr(callback, "bot", None)
        me = await bot.get_me() if bot is not None else None
        bot_ref = f"@{me.username}" if me and me.username else "бота"
    except Exception:
        bot_ref = "бота"

    label = _BIND_LABELS[kind]
    if kind == "work":
        tail = "После привязки бот запомнит ID чата и <b>покинет</b> его."
    else:
        tail = "После привязки бот запомнит ID чата и <b>останется</b> в нём — "
        tail += "сюда будут приходить уведомления о новых ПЗ."

    admin_note = ""
    if kind == "admin":
        admin_note = (
            "3. Выдай боту <b>права администратора</b> в этом чате — иначе "
            "он не сможет в полной мере работать с уведомлениями.\n"
        )

    return (
        f"📌 <b>{label}</b>\n\n"
        f"1. Добавь <b>{bot_ref}</b> в групповой чат, который хочешь "
        f"использовать как «{label}».\n"
        f"2. Дождись подтверждения привязки.\n"
        f"{admin_note}\n"
        f"{tail}"
    )


@router.callback_query(F.data.in_({"bind_work", "bind_admin"}))
async def cb_bind_start(callback: CallbackQuery) -> None:
    kind = "work" if callback.data == "bind_work" else "admin"
    _PENDING_BINDS[cb_uid(callback)] = kind
    set_pending_bind(cb_uid(callback), kind)  # в БД — переживает рестарт бота
    text = await _bind_instructions(callback, kind)
    await render_callback(callback, text, _bind_wait_kb())


@router.callback_query(F.data == "bind_done")
async def cb_bind_done(callback: CallbackQuery) -> None:
    """Пользователь сообщил, что добавил бота. Привязываем сами, если событие не пришло."""
    user_id = cb_uid(callback)
    kind = get_pending_bind(user_id) or _PENDING_BINDS.get(user_id)

    # Если бот ещё ждёт привязку — попробуем привязать последний добавленный чат.
    if kind:
        chat_id = _LAST_ADDED.get(user_id)
        if chat_id:
            # Защита от путаницы: нельзя привязать «чат админов» как «чат работы»
            # (и наоборот) и тем более выйти из нужного чата. Иначе при отвязке/
            # привязке одного чата бот мог «уходить» из другого.
            other = "admin" if kind == "work" else "work"
            other_bound = get_bound_chat(user_id, other)
            if other_bound and chat_id == other_bound:
                set_pending_bind(user_id, None)
                await callback.answer(
                    "⚠️ Этот чат уже привязан как другой тип. Добавь бота в новый чат.",
                    show_alert=True,
                )
                return

            _PENDING_BINDS.pop(user_id, None)
            set_pending_bind(user_id, None)
            set_bound_chat(user_id, kind, chat_id)
            if kind == "work":
                # Чат работы — бот запоминает и покидает его.
                bot = getattr(callback, "bot", None)
                try:
                    if bot is not None:
                        await bot.leave_chat(chat_id)
                except Exception:
                    pass
            await callback.answer("✅ Привязано!")
            text, kb = _profile_payload(user_id, cb_firstname(callback) or "—")
            await render_callback(callback, text, kb)
        else:
            # Бот пока не видит добавление — короткое уведомление, без повтора инструкции.
            await callback.answer("⏳ Добавь бота в чат, затем нажми ещё раз")
            return
    else:
        # Уже привязано через событие — просто открываем профиль.
        await callback.answer()
        text, kb = _profile_payload(user_id, cb_firstname(callback) or "—")
        await render_callback(callback, text, kb)


@router.callback_query(F.data == "bind_cancel")
async def cb_bind_cancel(callback: CallbackQuery) -> None:
    _PENDING_BINDS.pop(cb_uid(callback), None)
    set_pending_bind(cb_uid(callback), None)
    await callback.answer("❌ Привязка отменена")
    if callback.message:
        text, kb = _profile_payload(cb_uid(callback), cb_firstname(callback) or "—")
        await render_callback(callback, text, kb)


@router.callback_query(F.data == "unbind_work")
async def cb_unbind_work(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    set_bound_chat(user_id, "work", None)
    await callback.answer("💼 Чат работы отвязан")
    if callback.message:
        text, kb = _profile_payload(user_id, cb_firstname(callback) or "—")
        await render_callback(callback, text, kb)


@router.callback_query(F.data == "unbind_admin")
async def cb_unbind_admin(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    chat_id = get_bound_chat(user_id, "admin")
    set_bound_chat(user_id, "admin", None)
    if chat_id:
        bot = getattr(callback, "bot", None)
        try:
            if bot is not None:
                await bot.leave_chat(chat_id)
        except Exception:
            pass
    await callback.answer("🛡 Чат админов отвязан")
    if callback.message:
        text, kb = _profile_payload(user_id, cb_firstname(callback) or "—")
        await render_callback(callback, text, kb)


# Событие: YamoBot добавили в группу/супергруппу.
@router.my_chat_member()
async def on_bot_added_to_chat(event) -> None:
    adder = getattr(event, "from_user", None)
    if adder is not None and not getattr(adder, "is_bot", False):
        # Запоминаем последний чат, куда добавили бота (для кнопки «я добавил бота»).
        chat = event.chat
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            _LAST_ADDED[adder.id] = chat.id

    adder = getattr(event, "from_user", None)
    if adder is None or getattr(adder, "is_bot", False):
        return

    kind = get_pending_bind(adder.id) or _PENDING_BINDS.pop(adder.id, None)
    if not kind:
        return

    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        # Если это не группа — оставляем ожидание в БД, чтобы не потерять запрос.
        return

    new_status = getattr(event.new_chat_member, "status", None)
    old_status = getattr(event.old_chat_member, "status", None)
    was_member = old_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    is_member = new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    if was_member or not is_member:
        return

    # Какой бы чат ни добавил бота — это и есть привязываемый чат, он точный.
    set_bound_chat(adder.id, kind, chat.id)
    set_pending_bind(adder.id, None)

    bot = event.bot
    chat_name = chat.title or f"чат {chat.id}"
    if kind == "work":
        try:
            await bot.send_message(
                chat.id,
                "💼 Чат работы привязан. YamoBot запомнил его и покидает чат. 👋",
            )
        except Exception:
            pass
        try:
            await bot.leave_chat(chat.id)
        except Exception:
            pass
        confirm_text = (
            f"✅ <b>Чат работы привязан!</b>\n\n"
            f"📎 Чат: <b>{chat_name}</b>\n"
            f"🆔 ID: <code>{chat.id}</code>\n\n"
            f"Бот запомнил чат и вышел из него."
        )
    else:
        welcome_admin = ADMIN_CHAT_WELCOME
        try:
            await bot.send_message(chat.id, welcome_admin)
        except Exception:
            pass
        confirm_text = (
            f"✅ <b>Чат админов привязан!</b>\n\n"
            f"📎 Чат: <b>{chat_name}</b>\n"
            f"🆔 ID: <code>{chat.id}</code>\n\n"
            f"⚠️ <b>Выдай боту права администратора</b> в этом чате — "
            f"иначе он не сможет в полной мере работать с уведомлениями.\n"
            f"Сделай это через: «Управление чатом → Администраторы → YamoBot → "
            f"Назначить администратором».\n\n"
            f"Теперь сюда будут приходить уведомления о новых ПЗ. "
            f"Я отправил в чат приветствие со списком команд."
        )

    try:
        await bot.send_message(adder.id, confirm_text)
    except Exception:
        pass

