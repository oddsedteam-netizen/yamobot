from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.storage import (
    get_bot_by_id,
    bot_display_name,
    get_admins_all,
    add_admin,
    remove_admin,
    get_admin_by_tag,
    get_admin_by_user_id,
    update_admin_tag,
    get_admin_tag_history,
    get_admin_message_stats,
    get_admin_active_topics,
    get_all_admins_stats,
)

router = Router()


class AdminFSM(StatesGroup):
    waiting_add_admin = State()
    waiting_delete_admin = State()
    waiting_edit_tag = State()
    waiting_search_tag = State()


# ═══════════════ Клавиатуры ═══════════════

def admins_menu_kb(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика админов", callback_data=f"admstats_{bot_id}")],
            [InlineKeyboardButton(text="📋 Список админов", callback_data=f"admlist_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}")],
        ]
    )


def admins_list_kb(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить админа", callback_data=f"admadd_{bot_id}")],
            [InlineKeyboardButton(text="🗑 Удалить админа", callback_data=f"admdel_{bot_id}")],
            [InlineKeyboardButton(text="✏️ Редактор тегов", callback_data=f"admedit_{bot_id}")],
            [InlineKeyboardButton(text="🔍 Найти по тегу", callback_data=f"admsearch_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admins_{bot_id}")],
        ]
    )


# ═══════════════ Главное меню админов ═══════════════

@router.callback_query(F.data.regexp(r"^admins_\d+$"))
async def cb_admins_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    bot_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    name = bot_display_name(bot_info)
    admins = get_admins_all()

    text = (
        f"👤 <b>Админы — {name}</b>\n\n"
        f"Всего админов (глобально): <b>{len(admins)}</b>\n\n"
        f"Админы привязаны ко всем ботам.\n\n"
        f"Выбери действие:"
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=admins_menu_kb(bot_id))
        except Exception:
            await callback.message.answer(text, reply_markup=admins_menu_kb(bot_id))
    await callback.answer()


# ═══════════════ Список ═══════════════

@router.callback_query(F.data.regexp(r"^admlist_\d+$"))
async def cb_admins_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    bot_id = int(callback.data.split("_", 1)[1])

    admins = get_admins_all()

    if admins:
        lines = []
        for a in admins:
            uname = f"@{a['username']}" if a['username'] else f"ID:{a['user_id']}"
            status = "✅ активен" if a["active"] else "❌ неактивен"
            topics = get_admin_active_topics(a["user_id"])
            lines.append(f"  {uname} #{a['tag']} — {status} (ПЗ: {topics})")

        admin_list = "\n".join(lines)
        text = (
            f"📋 <b>Список админов</b>\n\n"
            f"{admin_list}\n\n"
            f"Для подробной инфо — «Найти по тегу»."
        )
    else:
        text = "📋 <b>Список админов</b>\n\nПока нет ни одного админа."

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=admins_list_kb(bot_id))
        except Exception:
            await callback.message.answer(text, reply_markup=admins_list_kb(bot_id))
    await callback.answer()


# ═══════════════ Статистика ═══════════════

@router.callback_query(F.data.regexp(r"^admstats_\d+$"))
async def cb_admins_stats(callback: CallbackQuery) -> None:
    bot_id = int(callback.data.split("_", 1)[1])

    all_stats = get_all_admins_stats()

    if not all_stats:
        text = "📊 <b>Статистика админов</b>\n\nНет админов."
    else:
        lines = []
        for item in all_stats:
            a = item["admin"]
            s = item["stats"]
            t = item["active_topics"]
            tag = f"#{a['tag']}"
            lines.append(
                f"  {tag}\n"
                f"    📅 День: <b>{s['day']}</b>  "
                f"📅 Неделя: <b>{s['week']}</b>  "
                f"📅 Месяц: <b>{s['month']}</b>\n"
                f"    📊 Всего: <b>{s['total']}</b>  "
                f"👥 ПЗ: <b>{t}</b>"
            )

        stats_text = "\n\n".join(lines)
        text = f"📊 <b>Статистика админов</b>\n\n{stats_text}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admstats_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admins_{bot_id}")],
        ]
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# ═══════════════ Добавить ═══════════════

@router.callback_query(F.data.regexp(r"^admadd_\d+$"))
async def cb_add_admin(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_", 1)[1])

    await state.set_state(AdminFSM.waiting_add_admin)
    await state.update_data(admin_bot_id=bot_id)

    text = (
        "➕ <b>Добавить админа</b>\n\n"
        "Отправь <b>user ID</b>, <b>username</b> и <b>тег</b>.\n\n"
        "Формат:\n<code>123456789 @username тег</code>\n\n"
        "Пример:\n<code>123456789 @ivan_admin продажи</code>\n\n"
        "User ID можно узнать через @userinfobot"
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admlist_{bot_id}")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(AdminFSM.waiting_add_admin)
async def fsm_add_admin(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    bot_id = data.get("admin_bot_id")

    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=2)

    if len(parts) < 3:
        await message.answer(
            "❌ Неверный формат.\n\n"
            "Нужно: <code>user_id @username тег</code>\n"
            "Пример: <code>123456789 @ivan продажи</code>"
        )
        return

    try:
        admin_user_id = int(parts[0])
    except ValueError:
        await message.answer("❌ Первый аргумент должен быть числовым user ID.")
        return

    username = parts[1].strip().lstrip("@")
    tag = parts[2].strip().lstrip("#")

    if not tag:
        await message.answer("❌ Тег не может быть пустым.")
        return

    success = add_admin(admin_user_id, username, tag)
    await state.clear()

    if success:
        await message.answer(
            f"✅ <b>Админ добавлен!</b>\n\n"
            f"🆔 <code>{admin_user_id}</code>\n"
            f"👤 @{username}\n"
            f"🏷 #{tag}\n\n"
            f"Админ привязан ко всем ботам.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Список", callback_data=f"admlist_{bot_id}")],
                [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
            ])
        )
    else:
        await message.answer(
            "⚠️ Этот пользователь уже добавлен как админ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Список", callback_data=f"admlist_{bot_id}")]
            ])
        )


# ═══════════════ Удалить ═══════════════

@router.callback_query(F.data.regexp(r"^admdel_\d+$"))
async def cb_delete_admin(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_", 1)[1])

    admins = get_admins_all()
    if not admins:
        await callback.answer("Нет админов")
        return

    await state.set_state(AdminFSM.waiting_delete_admin)
    await state.update_data(admin_bot_id=bot_id)

    lines = []
    for a in admins:
        uname = f"@{a['username']}" if a['username'] else f"ID:{a['user_id']}"
        lines.append(f"  {uname} #{a['tag']}")

    admin_list = "\n".join(lines)

    text = (
        f"🗑 <b>Удалить админа</b>\n\n"
        f"{admin_list}\n\n"
        f"Отправь <b>тег</b> админа для удаления.\n"
        f"Пример: <code>продажи</code>"
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admlist_{bot_id}")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(AdminFSM.waiting_delete_admin)
async def fsm_delete_admin(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    bot_id = data.get("admin_bot_id")

    tag = (message.text or "").strip().lstrip("#")
    if not tag:
        await message.answer("❌ Тег не может быть пустым.")
        return

    admin = get_admin_by_tag(tag)
    if not admin:
        await message.answer(f"⚠️ Админ с тегом <b>#{tag}</b> не найден.")
        return

    uname = f"@{admin['username']}" if admin['username'] else f"ID:{admin['user_id']}"
    remove_admin(admin["user_id"])
    await state.clear()

    await message.answer(
        f"✅ <b>Админ удалён!</b>\n\n👤 {uname} #{tag}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список", callback_data=f"admlist_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
        ])
    )


# ═══════════════ Редактор тегов ═══════════════

@router.callback_query(F.data.regexp(r"^admedit_\d+$"))
async def cb_edit_tag(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_", 1)[1])

    await state.set_state(AdminFSM.waiting_edit_tag)
    await state.update_data(admin_bot_id=bot_id)

    text = (
        "✏️ <b>Редактор тегов</b>\n\n"
        "Формат:\n<code>старый_тег новый_тег</code>\n\n"
        "Пример:\n<code>продажи маркетинг</code>"
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admlist_{bot_id}")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(AdminFSM.waiting_edit_tag)
async def fsm_edit_tag(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    bot_id = data.get("admin_bot_id")

    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer("❌ Нужно: <code>старый_тег новый_тег</code>")
        return

    old_tag = parts[0].strip().lstrip("#")
    new_tag = parts[1].strip().lstrip("#")

    admin = get_admin_by_tag(old_tag)
    if not admin:
        await message.answer(f"⚠️ Админ с тегом <b>#{old_tag}</b> не найден.")
        return

    update_admin_tag(admin["user_id"], new_tag)
    await state.clear()

    uname = f"@{admin['username']}" if admin['username'] else f"ID:{admin['user_id']}"

    await message.answer(
        f"✅ <b>Тег обновлён!</b>\n\n"
        f"👤 {uname}\n🏷 #{old_tag} → #{new_tag}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список", callback_data=f"admlist_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
        ])
    )


# ═══════════════ Поиск по тегу ═══════════════

@router.callback_query(F.data.regexp(r"^admsearch_\d+$"))
async def cb_search_tag(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_", 1)[1])

    await state.set_state(AdminFSM.waiting_search_tag)
    await state.update_data(admin_bot_id=bot_id)

    text = "🔍 <b>Поиск по тегу</b>\n\nОтправь тег.\nПример: <code>продажи</code>"

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admlist_{bot_id}")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(AdminFSM.waiting_search_tag)
async def fsm_search_tag(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    bot_id = data.get("admin_bot_id")

    tag = (message.text or "").strip().lstrip("#")
    if not tag:
        await message.answer("❌ Тег не может быть пустым.")
        return

    admin = get_admin_by_tag(tag)
    await state.clear()

    if not admin:
        await message.answer(
            f"⚠️ Админ с тегом <b>#{tag}</b> не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Список", callback_data=f"admlist_{bot_id}")]
            ])
        )
        return

    uname = f"@{admin['username']}" if admin['username'] else f"ID:{admin['user_id']}"
    status = "✅ активен" if admin["active"] else "❌ неактивен"

    stats = get_admin_message_stats(admin["user_id"])
    topics = get_admin_active_topics(admin["user_id"])
    history = get_admin_tag_history(admin["user_id"])

    history_text = ""
    if history:
        history_lines = []
        for h in history:
            old = f"#{h['old_tag']}" if h['old_tag'] else "—"
            history_lines.append(f"  {old} → #{h['new_tag']} ({h['changed_at'][:10]})")
        history_text = "\n🏷 История тегов:\n" + "\n".join(history_lines)

    text = (
        f"👤 <b>Админ — #{tag}</b>\n\n"
        f"📛 Username: {uname}\n"
        f"🆔 ID: <code>{admin['user_id']}</code>\n"
        f"🏷 Тег: #{admin['tag']}\n"
        f"📌 Статус: {status}\n"
        f"📅 Добавлен: {admin['created_at'][:10]}\n\n"
        f"📊 <b>Сообщения:</b>\n"
        f"  📅 День: <b>{stats['day']}</b>\n"
        f"  📅 Неделя: <b>{stats['week']}</b>\n"
        f"  📅 Месяц: <b>{stats['month']}</b>\n"
        f"  📊 Всего: <b>{stats['total']}</b>\n\n"
        f"👥 ПЗ за ним: <b>{topics}</b>"
        f"{history_text}"
    )

    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список", callback_data=f"admlist_{bot_id}")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data=f"admstats_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
        ])
    )