from aiogram import Router, F
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
)

router = Router()


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


async def show_profile(message: Message) -> None:
    user_id = message.from_user.id
    fname = message.from_user.first_name or "—"

    bots = get_user_bots(user_id)
    admins = get_admins_all(user_id)

    lines = []
    total_pz = 0
    for b in bots:
        topics = get_all_topics_for_bot(b["id"])
        total_pz += len(topics)
        lines.append(f"  • {bot_display_name(b)} — 📋 ПЗ: <b>{len(topics)}</b>")
    bots_list = "\n".join(lines) if lines else "  — нет ботов —"

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"📛 Имя: <b>{fname}</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"
        f"🤖 Ботов: <b>{len(bots)}</b>\n"
        f"👥 Админов: <b>{len(admins)}</b>\n"
        f"📋 Всего ПЗ: <b>{total_pz}</b>\n\n"
        f"<b>По ботам:</b>\n{bots_list}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Боты", callback_data="my_bots")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="gadmins")],
        [InlineKeyboardButton(text="📋 ПЗ", callback_data="gpz")],
    ])
    if is_super_admin(user_id):
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="🛡 Админ-панель", callback_data="profile_admin")
        ])

    await message.answer(text, reply_markup=kb)


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