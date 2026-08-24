from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from services.storage import (
    get_user_bots,
    bot_display_name,
    get_stats,
    get_all_stats,
    get_admins_all,
    get_admin_active_topics,
    get_all_admins_stats,
    get_all_topics_for_bot,
)
from services.child_manager import ChildManager

router = Router()


def _main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")],
    ])


async def _render(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Показывает (правит) текст с клавиатурой в текущем сообщении."""
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, reply_markup=kb)
    await callback.answer()
# ═══════════════ Глобальная статистика ═══════════════

@router.callback_query(F.data == "gstats")
async def cb_global_stats(callback: CallbackQuery, child_manager: ChildManager) -> None:
    user_id = callback.from_user.id
    bots = get_user_bots(user_id)

    if not bots:
        text = "📊 <b>Статистика</b>\n\nУ тебя пока нет подключённых ботов."
        await _render(callback, text, _main_kb())
        return

    s = get_all_stats([b["id"] for b in bots])

    lines = []
    for b in bots:
        bs = get_stats(b["id"])
        status = "🟢" if child_manager.is_running(b["id"]) else "🔴"
        lines.append(
            f"  {status} {bot_display_name(b)} — 👥 {bs['users_total']}"
        )

    text = (
        f"📊 <b>Общая статистика</b> ({len(bots)} ботов)\n\n"
        f"👥 Всего пользователей: <b>{s['users_total']}</b>\n"
        f"🚫 Заблокировали: <b>{s['users_blocked']}</b>\n"
        f"✅ Активных: <b>{s['users_active']}</b>\n\n"
        f"📩 Получено: <b>{s['messages_in']}</b>\n"
        f"📤 Отправлено: <b>{s['messages_out']}</b>\n\n"
        f"📨 Рассылок: <b>{s['mailings_count']}</b>\n"
        f"  ├ Доставлено: <b>{s['mailings_sent']}</b>\n"
        f"  └ Не доставлено: <b>{s['mailings_failed']}</b>\n\n"
        f"👥 По каждому боту:\n" + "\n".join(lines)
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="gstats")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")],
    ])

    await _render(callback, text, kb)

# ═══════════════ Админы (глобально) ═══════════════

@router.callback_query(F.data == "gadmins")
async def cb_global_admins(callback: CallbackQuery) -> None:
    admins = get_admins_all()

    if admins:
        lines = []
        for a in admins:
            uname = f"@{a['username']}" if a["username"] else f"ID:{a['user_id']}"
            status = "✅ активен" if a["active"] else "❌ неактивен"
            topics = get_admin_active_topics(a["user_id"])
            lines.append(f"  {uname} #{a['tag']} — {status} (ПЗ: {topics})")
        text = "👤 <b>Админы</b> (глобально)\n\n" + "\n".join(lines)
    else:
        text = "👤 <b>Админы</b>\n\nПока нет ни одного админа."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика админов", callback_data="gadmstats")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")],
    ])

    await _render(callback, text, kb)

@router.callback_query(F.data == "gadmstats")
async def cb_global_admins_stats(callback: CallbackQuery) -> None:
    all_stats = get_all_admins_stats()

    if not all_stats:
        text = "📊 <b>Статистика админов</b>\n\nНет админов."
    else:
        lines = []
        for item in all_stats:
            a = item["admin"]
            s = item["stats"]
            t = item["active_topics"]
            lines.append(
                f"  #{a['tag']}\n"
                f"    📅 День: <b>{s['day']}</b>  "
                f"📅 Неделя: <b>{s['week']}</b>  "
                f"📅 Месяц: <b>{s['month']}</b>\n"
                f"    📊 Всего: <b>{s['total']}</b>  "
                f"👥 ПЗ: <b>{t}</b>"
            )
        text = "📊 <b>Статистика админов</b>\n\n" + "\n\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="gadmstats")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="gadmins")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")],
    ])

    await _render(callback, text, kb)

# ═══════════════ ПЗ (по всем ботам) ═══════════════

@router.callback_query(F.data == "gpz")
async def cb_global_pz(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    bots = get_user_bots(user_id)

    if not bots:
        text = "📋 <b>ПЗ</b>\n\nУ тебя пока нет подключённых ботов."
        await _render(callback, text, _main_kb())
        return

    total = 0
    assigned = 0
    bot_rows = []
    for b in bots:
        topics = get_all_topics_for_bot(b["id"])
        a = sum(1 for t in topics if t.get("admin_user_id"))
        total += len(topics)
        assigned += a
        bot_rows.append(
            f"  • {bot_display_name(b)} — 📋 {len(topics)} "
            f"(🟢 {a} / ⏳ {len(topics) - a})"
        )

    text = (
        f"📋 <b>ПЗ — все боты</b> ({len(bots)} ботов)\n\n"
        f"Всего ПЗ: <b>{total}</b>\n"
        f"🟢 С админом: <b>{assigned}</b>\n"
        f"⏳ Без админа: <b>{total - assigned}</b>\n\n"
        f"По каждому боту:\n" + "\n".join(bot_rows)
    )

    buttons: list[list[InlineKeyboardButton]] = []
    for b in bots:
        buttons.append([
            InlineKeyboardButton(text=f"📋 {bot_display_name(b)}", callback_data=f"pzlist_{b['id']}_0")
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await _render(callback, text, kb)