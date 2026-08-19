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
    get_admins,
    add_admin,
    remove_admin,
    get_admin_by_tag,
    update_admin_tag,
    get_admin_tag_history,
    get_admin_message_stats,
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
    admins = get_admins(bot_id)

    text = (
        f"👤 <b>Админы — {name}</b>\n\n"
        f"Всего админов: <b>{len(admins)}</b>\n\n"
        f"Выбери действие:"
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=admins_menu_kb(bot_id))
        except Exception:
            await callback.message.answer(text, reply_markup=admins_menu_kb(bot_id))
    await callback.answer()


# ═══════════════ Список админов ═══════════════

@router.callback_query(F.data.regexp(r"^admlist_\d+$"))
async def cb_admins_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    bot_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    admins = get_admins(bot_id)
    name = bot_display_name(bot_info)

    if admins:
        lines = []
        for a in admins:
            uname = f"@{a['username']}" if a['username'] else f"ID:{a['user_id']}"
            status = "✅ активен" if a["active"] else "❌ неактивен"
            lines.append(f"  {uname} #{a['tag']} — {status}")

        admin_list = "\n".join(lines)
        text = (
            f"📋 <b>Список админов — {name}</b>\n\n"
            f"{admin_list}\n\n"
            f"Чтобы посмотреть подробную инфу — нажми «Найти по тегу» "
            f"и введи тег админа."
        )
    else:
        text = (
            f"📋 <b>Список админов — {name}</b>\n\n"
            f"Пока нет ни одного админа."
        )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=admins_list_kb(bot_id))
        except Exception:
            await callback.message.answer(text, reply_markup=admins_list_kb(bot_id))
    await callback.answer()


# ═══════════════ Статистика админов ═══════════════

@router.callback_query(F.data.regexp(r"^admstats_\d+$"))
async def cb_admins_stats(callback: CallbackQuery) -> None:
    bot_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    all_stats = get_all_admins_stats(bot_id)
    name = bot_display_name(bot_info)

    if not all_stats:
        text = f"📊 <b>Статистика админов — {name}</b>\n\nНет админов."
    else:
        lines = []
        for item in all_stats:
            a = item["admin"]
            s = item["stats"]
            tag = f"#{a['tag']}"
            lines.append(
                f"  {tag}\n"
                f"    📅 День: <b>{s['day']}</b>  "
                f"📅 Неделя: <b>{s['week']}</b>  "
                f"📅 Месяц: <b>{s['month']}</b>\n"
                f"    📊 Всего: <b>{s['total']}</b>"
            )

        stats_text = "\n\n".join(lines)
        text = (
            f"📊 <b>Статистика админов — {name}</b>\n\n"
            f"{stats_text}"
        )

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


# ═══════════════ Добавить админа ═══════════════

@router.callback_query(F.data.regexp(r"^admadd_\d+$"))
async def cb_add_admin(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_", 1)[1])

    await state.set_state(AdminFSM.waiting_add_admin)
    await state.update_data(admin_bot_id=bot_id)

    text = (
        "➕ <b>Добавить админа</b>\n\n"
        "Отправь <b>username</b> и <b>тег</b> через пробел.\n\n"
        "Формат:\n<code>@username тег</code>\n\n"
        "Пример:\n<code>@ivan_admin продажи</code>"
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
    user_id = message.from_user.id

    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer(
            "❌ Неверный формат.\n\n"
            "Нужно: <code>@username тег</code>\n"
            "Пример: <code>@ivan_admin продажи</code>"
        )
        return

    username_raw = parts[0].strip().lstrip("@")
    tag = parts[1].strip().lstrip("#")

    if not username_raw:
        await message.answer("❌ Username не может быть пустым.")
        return

    if not tag:
        await message.answer("❌ Тег не может быть пустым.")
        return

    # Используем username как временный ID (реальный user_id будет когда админ напишет боту)
    # Для простоты используем хеш от username как user_id
    admin_user_id = hash(username_raw) % (10 ** 9)

    success = add_admin(bot_id, admin_user_id, username_raw, tag)
    await state.clear()

    if success:
        await message.answer(
            f"✅ <b>Админ добавлен!</b>\n\n"
            f"👤 @{username_raw}\n"
            f"🏷 #{tag}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Список админов", callback_data=f"admlist_{bot_id}")],
                [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
            ])
        )
    else:
        await message.answer(
            f"⚠️ Этот пользователь уже добавлен как админ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Список админов", callback_data=f"admlist_{bot_id}")]
            ])
        )


# ═══════════════ Удалить админа ═══════════════

@router.callback_query(F.data.regexp(r"^admdel_\d+$"))
async def cb_delete_admin(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_", 1)[1])

    admins = get_admins(bot_id)
    if not admins:
        await callback.answer("Нет админов для удаления")
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
        f"Текущие админы:\n{admin_list}\n\n"
        f"Отправь <b>тег</b> админа, которого хочешь удалить.\n\n"
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

    admin = get_admin_by_tag(bot_id, tag)

    if not admin:
        await message.answer(
            f"⚠️ Админ с тегом <b>#{tag}</b> не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Список", callback_data=f"admlist_{bot_id}")]
            ])
        )
        return

    uname = f"@{admin['username']}" if admin['username'] else f"ID:{admin['user_id']}"

    remove_admin(bot_id, admin["user_id"])
    await state.clear()

    await message.answer(
        f"✅ <b>Админ удалён!</b>\n\n"
        f"👤 {uname} #{tag}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список админов", callback_data=f"admlist_{bot_id}")],
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
        "Отправь <b>старый тег</b> и <b>новый тег</b> через пробел.\n\n"
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
        await message.answer(
            "❌ Неверный формат.\n\n"
            "Нужно: <code>старый_тег новый_тег</code>"
        )
        return

    old_tag = parts[0].strip().lstrip("#")
    new_tag = parts[1].strip().lstrip("#")

    admin = get_admin_by_tag(bot_id, old_tag)
    if not admin:
        await message.answer(f"⚠️ Админ с тегом <b>#{old_tag}</b> не найден.")
        return

    update_admin_tag(bot_id, admin["user_id"], new_tag)
    await state.clear()

    uname = f"@{admin['username']}" if admin['username'] else f"ID:{admin['user_id']}"

    await message.answer(
        f"✅ <b>Тег обновлён!</b>\n\n"
        f"👤 {uname}\n"
        f"🏷 #{old_tag} → #{new_tag}",
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

    text = (
        "🔍 <b>Поиск по тегу</b>\n\n"
        "Отправь <b>тег</b> админа.\n\n"
        "Пример: <code>продажи</code>"
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


@router.message(AdminFSM.waiting_search_tag)
async def fsm_search_tag(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    bot_id = data.get("admin_bot_id")

    tag = (message.text or "").strip().lstrip("#")

    if not tag:
        await message.answer("❌ Тег не может быть пустым.")
        return

    admin = get_admin_by_tag(bot_id, tag)
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

    stats = get_admin_message_stats(bot_id, admin["user_id"])
    history = get_admin_tag_history(bot_id, admin["user_id"])

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
        f"🏷 Текущий тег: #{admin['tag']}\n"
        f"📌 Статус: {status}\n"
        f"📅 Добавлен: {admin['created_at'][:10]}\n\n"
        f"📊 <b>Сообщения:</b>\n"
        f"  📅 День: <b>{stats['day']}</b>\n"
        f"  📅 Неделя: <b>{stats['week']}</b>\n"
        f"  📅 Месяц: <b>{stats['month']}</b>\n"
        f"  📊 Всего: <b>{stats['total']}</b>"
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