from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import render_callback, safe_edit
from services.storage import (
    get_bot_by_id,
    bot_display_name,
    get_all_topics_for_bot,
    get_topic_by_user_id_search,
    get_pz_stats,
    get_user_info_from_pz,
    get_admin_by_user_id,
    is_user_banned,
    is_bot_anonymous,
)

router = Router()


class PZFSM(StatesGroup):
    waiting_search_id = State()


PZ_PER_PAGE = 10


def pz_menu_kb(bot_id: int, anon_mode: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if not anon_mode:
        rows.append([InlineKeyboardButton(text="📋 Список ПЗ", callback_data=f"pzlist_{bot_id}_0")])
        rows.append([InlineKeyboardButton(text="🔍 Найти по ID юзера", callback_data=f"pzsearch_{bot_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pz_menu_text(bot_info: dict, topics: list[dict]) -> str:
    """Текст меню ПЗ (с учётом анонимного режима)."""
    name = bot_display_name(bot_info)
    bot_id = bot_info["id"]
    anon_mode = bool(bot_info.get("anonymous_mode", 0))

    assigned = sum(1 for t in topics if t.get("admin_user_id"))
    open_count = len(topics) - assigned

    text = (
        f"📋 <b>ПЗ — {name}</b>\n\n"
        f"Всего ПЗ: <b>{len(topics)}</b>\n"
        f"🟢 С админом: <b>{assigned}</b>\n"
        f"⏳ Без админа: <b>{open_count}</b>\n\n"
    )

    if anon_mode:
        text += (
            "🕶 <b>Анонимный режим включён.</b>\n"
            "Список и поиск ПЗ для этого бота недоступны: "
            "идентичность пользователей скрыта.\n\n"
        )

    text += "Выбери действие:"
    return text


# ═══════════════ Главное меню ПЗ ═══════════════

@router.callback_query(F.data.regexp(r"^pz_\d+$"))
async def cb_pz_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    bot_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    topics = get_all_topics_for_bot(bot_id)
    text = _pz_menu_text(bot_info, topics)
    anon_mode = bool(bot_info.get("anonymous_mode", 0))

    await render_callback(callback, text, pz_menu_kb(bot_id, anon_mode))


# ═══════════════ Список ПЗ с пагинацией ═══════════════

@router.callback_query(F.data.regexp(r"^pzlist_\d+_\d+$"))
async def cb_pz_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    parts = callback.data.split("_")
    bot_id = int(parts[1])
    page = int(parts[2])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    anon_mode = bool(bot_info.get("anonymous_mode", 0))
    if anon_mode:
        await render_callback(
            callback,
            "🔒 <b>Список ПЗ недоступен</b>\n\n"
            "У этого бота включён анонимный режим.",
            pz_menu_kb(bot_id, True),
        )
        return

    topics = get_all_topics_for_bot(bot_id)

    if not topics:
        text = "📋 <b>Список ПЗ</b>\n\nПока нет ни одного ПЗ."
        await safe_edit(callback.message, text, pz_menu_kb(bot_id, False))
        await callback.answer()
        return

    total_pages = (len(topics) - 1) // PZ_PER_PAGE + 1
    page = max(0, min(page, total_pages - 1))

    start = page * PZ_PER_PAGE
    end = start + PZ_PER_PAGE
    page_topics = topics[start:end]

    lines = []
    for i, t in enumerate(page_topics, start=start + 1):
        if t.get("admin_tag"):
            admin_info = f"#{t['admin_tag']}"
        elif t.get("admin_user_id"):
            admin_info = f"ID:{t['admin_user_id']}"
        else:
            admin_info = "⏳ без админа"

        lines.append(f"{i}. <code>{t['user_chat_id']}</code> — {admin_info}")

    text = (
        f"📋 <b>Список ПЗ</b> (стр. {page + 1}/{total_pages})\n"
        f"Всего: {len(topics)}\n\n"
        + "\n".join(lines)
        + "\n\nЧтобы посмотреть детали — «🔍 Найти по ID»"
    )

    # Пагинация
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"pzlist_{bot_id}_{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"pzlist_{bot_id}_{page + 1}"))

    kb_rows = []
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([InlineKeyboardButton(text="🔍 Найти по ID", callback_data=f"pzsearch_{bot_id}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"pz_{bot_id}")])

    await render_callback(callback, text, InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ═══════════════ Поиск ПЗ по user_id ═══════════════

@router.callback_query(F.data.regexp(r"^pzsearch_\d+$"))
async def cb_pz_search(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info and bool(bot_info.get("anonymous_mode", 0)):
        await render_callback(
            callback,
            "🔒 <b>Поиск ПЗ недоступен</b>\n\n"
            "У этого бота включён анонимный режим.",
            pz_menu_kb(bot_id, True),
        )
        return

    await state.set_state(PZFSM.waiting_search_id)
    await state.update_data(pz_bot_id=bot_id)

    text = (
        "🔍 <b>Поиск ПЗ</b>\n\n"
        "Отправь <b>user ID</b> пользователя.\n\n"
        "Пример: <code>123456789</code>"
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"pz_{bot_id}")]
            ]))
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(PZFSM.waiting_search_id)
async def fsm_pz_search(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    bot_id = data.get("pz_bot_id")

    raw = (message.text or "").strip()
    try:
        user_chat_id = int(raw)
    except ValueError:
        await message.answer("❌ ID должен быть числом.")
        return

    await state.clear()
    await _show_pz_details(message, bot_id, user_chat_id)


async def _show_pz_details(msg_or_cb, bot_id: int, user_chat_id: int) -> None:
    """Показывает подробную инфу о ПЗ."""
    topic = get_topic_by_user_id_search(bot_id, user_chat_id)
    user_info = get_user_info_from_pz(bot_id, user_chat_id)
    stats = get_pz_stats(bot_id, user_chat_id)
    banned = is_user_banned(bot_id, user_chat_id)

    if not topic and not user_info:
        text = (
            f"❌ Пользователь <code>{user_chat_id}</code> не найден.\n\n"
            f"Возможно, он ещё не писал этому боту."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К меню ПЗ", callback_data=f"pz_{bot_id}")]
        ])
    else:
        # Инфа о юзере
        username = f"@{user_info['username']}" if user_info and user_info.get('username') else "—"
        first_name = user_info.get('first_name', '—') if user_info else '—'
        first_seen = user_info.get('first_seen', '—')[:16] if user_info else '—'

        # Инфа о топике
        if topic:
            if topic.get("admin_tag"):
                admin_str = f"#{topic['admin_tag']}"
            elif topic.get("admin_user_id"):
                admin_str = f"ID:{topic['admin_user_id']}"
            else:
                admin_str = "⏳ без админа"

            topic_status = topic.get("status", "open")
            topic_created = topic.get("created_at", "—")[:16]
        else:
            admin_str = "— (нет ПЗ)"
            topic_status = "—"
            topic_created = "—"

        # Статус
        status_line = "🚫 <b>ЗАБАНЕН</b>" if banned else "✅ Активен"

        text = (
            f"👤 <b>ПЗ — {user_chat_id}</b>\n\n"
            f"<b>Пользователь:</b>\n"
            f"  📛 Имя: {first_name}\n"
            f"  👤 Username: {username}\n"
            f"  🆔 ID: <code>{user_chat_id}</code>\n"
            f"  📅 Первый визит: {first_seen}\n"
            f"  📌 Статус: {status_line}\n\n"
            f"<b>ПЗ:</b>\n"
            f"  👤 Админ: {admin_str}\n"
            f"  📅 Создан: {topic_created}\n"
            f"  🔖 Статус: {topic_status}\n\n"
            f"<b>Сообщения:</b>\n"
            f"  📩 От юзера: <b>{stats['messages_from_user']}</b>\n"
            f"  📤 Ответов админа: <b>{stats['messages_to_user']}</b>\n"
            f"  🕐 Первое: {stats['first_message_at'][:16] if stats['first_message_at'] else '—'}\n"
            f"  🕐 Последнее: {stats['last_message_at'][:16] if stats['last_message_at'] else '—'}"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"pzview_{bot_id}_{user_chat_id}")],
            [InlineKeyboardButton(text="📋 Список ПЗ", callback_data=f"pzlist_{bot_id}_0")],
            [InlineKeyboardButton(text="⬅️ К меню ПЗ", callback_data=f"pz_{bot_id}")],
        ])

    if hasattr(msg_or_cb, "answer") and not hasattr(msg_or_cb, "message"):
        # это Message
        await msg_or_cb.answer(text, reply_markup=kb)
    else:
        # это CallbackQuery
        if msg_or_cb.message:
            try:
                await msg_or_cb.message.edit_text(text, reply_markup=kb)
            except Exception:
                await msg_or_cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.regexp(r"^pzview_\d+_\d+$"))
async def cb_pz_view(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    bot_id = int(parts[1])
    user_chat_id = int(parts[2])
    await _show_pz_details(callback, bot_id, user_chat_id)
    await callback.answer()