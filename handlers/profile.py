from aiogram import Router, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import render_callback
from services.config import is_super_admin
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
        f"<b>По ботам:</b>\n{bots_list}"
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
    if is_super_admin(user_id):
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="🛡 Админ-панель", callback_data="profile_admin")
        ])

    return text, kb


async def show_profile(message: Message) -> None:
    text, kb = _profile_payload(message.from_user.id, message.from_user.first_name or "—")
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "profile_show")
async def cb_profile_show(callback: CallbackQuery) -> None:
    """Открывает профиль из инлайн-колбэка (без нового приветствия)."""
    _PENDING_BINDS.pop(callback.from_user.id, None)
    if callback.message is None:
        return
    text, kb = _profile_payload(callback.from_user.id, callback.from_user.first_name or "—")
    await render_callback(callback, text, kb)


# ═══════════════ Админ-панель пользователей ═══════════════

@router.callback_query(F.data == "profile_admin")
async def cb_profile_admin(callback: CallbackQuery) -> None:
    if not is_super_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await render_callback(callback, "🛡 <b>Админ-панель</b>\n\nВыбери раздел:", admin_kb())


@router.callback_query(F.data == "profiles_list")
async def cb_profiles_list(callback: CallbackQuery) -> None:
    if not is_super_admin(callback.from_user.id):
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
    if not is_super_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(callback.data.split("_")[-1])
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
    if not is_super_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(callback.data.split("_")[-1])
    set_registry_user_blocked(uid, True)
    await render_callback(callback, f"🚫 Пользователь <code>{uid}</code> забанен.", profile_admin_kb(uid))


@router.callback_query(F.data.startswith("profile_unban_"))
async def cb_profile_unban(callback: CallbackQuery) -> None:
    if not is_super_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(callback.data.split("_")[-1])
    set_registry_user_blocked(uid, False)
    await render_callback(callback, f"✅ Пользователь <code>{uid}</code> разбанен.", profile_admin_kb(uid))


@router.callback_query(F.data.startswith("profile_stats_"))
async def cb_profile_stats(callback: CallbackQuery) -> None:
    if not is_super_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(callback.data.split("_")[-1])
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
    if not is_super_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(callback.data.split("_")[-1])
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
        me = await callback.bot.get_me()
        bot_ref = f"@{me.username}" if me.username else "бота"
    except Exception:
        bot_ref = "бота"

    label = _BIND_LABELS[kind]
    if kind == "work":
        tail = "После привязки бот запомнит ID чата и <b>покинет</b> его."
    else:
        tail = "После привязки бот запомнит ID чата и <b>останется</b> в нём — "
        tail += "сюда будут приходить уведомления о новых ПЗ."

    return (
        f"📌 <b>{label}</b>\n\n"
        f"1. Добавь <b>{bot_ref}</b> в групповой чат, который хочешь "
        f"использовать как «{label}».\n"
        f"2. Дождись подтверждения привязки.\n\n"
        f"{tail}"
    )


@router.callback_query(F.data.in_({"bind_work", "bind_admin"}))
async def cb_bind_start(callback: CallbackQuery) -> None:
    kind = "work" if callback.data == "bind_work" else "admin"
    _PENDING_BINDS[callback.from_user.id] = kind
    text = await _bind_instructions(callback, kind)
    await render_callback(callback, text, _bind_wait_kb())


@router.callback_query(F.data == "bind_done")
async def cb_bind_done(callback: CallbackQuery) -> None:
    """Пользователь сообщил, что добавил бота. Привязываем сами, если событие не пришло."""
    user_id = callback.from_user.id
    kind = _PENDING_BINDS.get(user_id)

    # Если бот ещё ждёт привязку — попробуем привязать последний добавленный чат.
    if kind:
        chat_id = _LAST_ADDED.get(user_id)
        if chat_id:
            _PENDING_BINDS.pop(user_id, None)
            set_bound_chat(user_id, kind, chat_id)
            if kind == "work":
                # Чат работы — бот запоминает и покидает его.
                try:
                    await callback.bot.leave_chat(chat_id)
                except Exception:
                    pass
            await callback.answer("✅ Привязано!")
            text, kb = _profile_payload(user_id, callback.from_user.first_name or "—")
            await render_callback(callback, text, kb)
        else:
            # Бот пока не видит добавление — короткое уведомление, без повтора инструкции.
            await callback.answer("⏳ Добавь бота в чат, затем нажми ещё раз")
            return
    else:
        # Уже привязано через событие — просто открываем профиль.
        await callback.answer()
        text, kb = _profile_payload(user_id, callback.from_user.first_name or "—")
        await render_callback(callback, text, kb)


@router.callback_query(F.data == "bind_cancel")
async def cb_bind_cancel(callback: CallbackQuery) -> None:
    _PENDING_BINDS.pop(callback.from_user.id, None)
    await callback.answer("❌ Привязка отменена")
    if callback.message:
        text, kb = _profile_payload(callback.from_user.id, callback.from_user.first_name or "—")
        await render_callback(callback, text, kb)


@router.callback_query(F.data == "unbind_work")
async def cb_unbind_work(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    set_bound_chat(user_id, "work", None)
    await callback.answer("💼 Чат работы отвязан")
    if callback.message:
        text, kb = _profile_payload(user_id, callback.from_user.first_name or "—")
        await render_callback(callback, text, kb)


@router.callback_query(F.data == "unbind_admin")
async def cb_unbind_admin(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    chat_id = get_bound_chat(user_id, "admin")
    set_bound_chat(user_id, "admin", None)
    if chat_id:
        try:
            await callback.bot.leave_chat(chat_id)
        except Exception:
            pass
    await callback.answer("🛡 Чат админов отвязан")
    if callback.message:
        text, kb = _profile_payload(user_id, callback.from_user.first_name or "—")
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

    kind = _PENDING_BINDS.pop(adder.id, None)
    if not kind:
        return

    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        # Если это не группа — возвращаем «ожидание», чтобы не потерять запрос.
        _PENDING_BINDS[adder.id] = kind
        return

    new_status = getattr(event.new_chat_member, "status", None)
    old_status = getattr(event.old_chat_member, "status", None)
    was_member = old_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    is_member = new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    if was_member or not is_member:
        return

    set_bound_chat(adder.id, kind, chat.id)

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
        welcome_admin = (
            "🛡 <b>Чат админов привязан!</b>\n\n"
            "👋 Приветствую тебя в чате админов YamoBot!\n\n"
            "🧭 <b>Команды для админов (работают в топиках):</b>\n"
            "• <code>/smena</code> — сменить админа у ПЗ без подтверждения.\n"
            "• <code>/otkaz</code> — отказаться от ПЗ / сбросить админа.\n"
            "• <code>/ban</code> — забанить пользователя.\n"
            "• <code>/unban</code> — разбанить пользователя.\n"
            "• <code>/.стата</code> — сводка по ПЗ всех ботов (в т.ч. в этом чате).\n"
            "• <code>/стата</code> или <code>/stata</code> — то же самое через слеш.\n\n"
            "Сюда будут приходить уведомления о новых ПЗ."
        )
        try:
            await bot.send_message(chat.id, welcome_admin)
        except Exception:
            pass
        confirm_text = (
            f"✅ <b>Чат админов привязан!</b>\n\n"
            f"📎 Чат: <b>{chat_name}</b>\n"
            f"🆔 ID: <code>{chat.id}</code>\n\n"
            f"Теперь сюда будут приходить уведомления о новых ПЗ. "
            f"Я отправил в чат приветствие со списком команд."
        )

    try:
        await bot.send_message(adder.id, confirm_text)
    except Exception:
        pass

