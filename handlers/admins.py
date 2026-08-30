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

def admins_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список админов", callback_data="gadmins_list")],
            [InlineKeyboardButton(text="📊 Статистика админов", callback_data="gadmins_stats")],
            [InlineKeyboardButton(text="➕ Добавить админа", callback_data="gadmins_add")],
            [InlineKeyboardButton(text="🗑 Удалить админа", callback_data="gadmins_del")],
            [InlineKeyboardButton(text="✏️ Редактировать теги", callback_data="gadmins_edit")],
            [InlineKeyboardButton(text="🔍 Найти по тегу", callback_data="gadmins_search")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")],
        ]
    )


def admins_list_kb(extra_rows: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if extra_rows:
        rows.extend(extra_rows)
    rows.append([InlineKeyboardButton(text="➕ Добавить админа", callback_data="gadmins_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Меню админов", callback_data="gadmins")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_detail_kb(admin_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить тег", callback_data=f"gadmins_editone_{admin_user_id}")],
            [InlineKeyboardButton(text="🗑 Удалить админа", callback_data=f"gadmins_delone_{admin_user_id}")],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="gadmins_list")],
        ]
    )


async def _render(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# ═══════════════ Главное меню админов ═══════════════

@router.callback_query(F.data == "gadmins")
async def cb_admins_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    admins = get_admins_all()

    text = (
        f"👤 <b>Управление админами</b>\n\n"
        f"Всего админов (глобально): <b>{len(admins)}</b>\n\n"
        f"Админы привязаны ко всем ботам.\n\n"
        f"Выбери действие:"
    )

    await _render(callback, text, admins_menu_kb())


# ═══════════════ Список ═══════════════

@router.callback_query(F.data == "gadmins_list")
async def cb_admins_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()

    admins = get_admins_all()

    if admins:
        lines = []
        extra_rows = []
        for i, a in enumerate(admins, 1):
            uname = f"@{a['username']}" if a['username'] else f"ID:{a['user_id']}"
            status = "🟢 активен" if a["active"] else "🔴 неактивен"
            topics = get_admin_active_topics(a["user_id"])
            lines.append(f"{i}. <b>#{a['tag']}</b>  {uname}")
            lines.append(f"   {status} · ПЗ за ним: <b>{topics}</b>")
            lines.append("")
            extra_rows.append([
                InlineKeyboardButton(
                    text=f"{i}. #{a['tag']} — {uname}",
                    callback_data=f"gadmins_view_{a['user_id']}"
                )
            ])

        admin_list = "\n".join(lines).rstrip()
        text = (
            f"📋 <b>Список админов</b> ({len(admins)})\n\n"
            f"{admin_list}\n"
            f"Нажми на админа — откроется его карточка с действиями."
        )
        kb = admins_list_kb(extra_rows)
    else:
        text = "📋 <b>Список админов</b>\n\nПока нет ни одного админа."
        kb = admins_list_kb()

    await _render(callback, text, kb)
# ═══════════════ Карточка админа ═══════════════

@router.callback_query(F.data.regexp(r"^gadmins_view_\d+$"))
async def cb_admin_view(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    admin_user_id = int(callback.data.split("_")[-1])

    admin = get_admin_by_user_id(admin_user_id)
    if not admin:
        await callback.answer("❌ Админ не найден")
        return

    uname = f"@{admin['username']}" if admin['username'] else f"ID:{admin['user_id']}"
    status = "🟢 активен" if admin["active"] else "🔴 неактивен"

    stats = get_admin_message_stats(admin["user_id"])
    topics = get_admin_active_topics(admin["user_id"])
    history = get_admin_tag_history(admin["user_id"])

    history_text = ""
    if history:
        history_lines = []
        for h in history:
            old = f"#{h['old_tag']}" if h['old_tag'] else "—"
            history_lines.append(f"  {old} → #{h['new_tag']} ({h['changed_at'][:10]})")
        history_text = "\n\n🏷 <b>История тегов:</b>\n" + "\n".join(history_lines)

    text = (
        f"👤 <b>Админ — #{admin['tag']}</b>\n\n"
        f"📛 Username: <b>{uname}</b>\n"
        f"🆔 ID: <code>{admin['user_id']}</code>\n"
        f"🏷 Тег: <b>#{admin['tag']}</b>\n"
        f"📌 Статус: {status}\n"
        f"📅 Добавлен: {admin['created_at'][:10]}\n\n"
        f"📊 <b>Сообщения:</b>\n"
        f"  📅 День: <b>{stats['day']}</b>  📅 Неделя: <b>{stats['week']}</b>\n"
        f"  📅 Месяц: <b>{stats['month']}</b>  📊 Всего: <b>{stats['total']}</b>\n"
        f"👥 ПЗ за ним: <b>{topics}</b>"
        f"{history_text}"
    )

    await _render(callback, text, admin_detail_kb(admin_user_id))


# ═══════════════ Действия с конкретным админом ═══════════════

@router.callback_query(F.data.regexp(r"^gadmins_editone_\d+$"))
async def cb_edit_one_admin(callback: CallbackQuery, state: FSMContext) -> None:
    admin_user_id = int(callback.data.split("_")[-1])

    admin = get_admin_by_user_id(admin_user_id)
    if not admin:
        await callback.answer("❌ Админ не найден")
        return

    await state.set_state(AdminFSM.waiting_edit_tag)
    await state.update_data(admin_edit_user_id=admin_user_id)

    text = (
        f"✏️ <b>Новый тег для #{admin['tag']}</b>\n\n"
        f"Введи новый тег (без решётки):\n"
        f"Пример: <code>маркетинг</code>"
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"gadmins_view_{admin_user_id}")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^gadmins_delone_\d+$"))
async def cb_delete_one_admin(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    admin_user_id = int(callback.data.split("_")[-1])

    admin = get_admin_by_user_id(admin_user_id)
    if not admin:
        await callback.answer("❌ Админ не найден")
        return

    uname = f"@{admin['username']}" if admin['username'] else f"ID:{admin['user_id']}"
    text = (
        f"🗑 <b>Удалить админа?</b>\n\n"
        f"👤 {uname}\n"
        f"🏷 #{admin['tag']}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"gadmins_delconfirm_{admin_user_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"gadmins_view_{admin_user_id}")],
    ])

    await _render(callback, text, kb)


@router.callback_query(F.data.regexp(r"^gadmins_delconfirm_\d+$"))
async def cb_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    admin_user_id = int(callback.data.split("_")[-1])

    admin = get_admin_by_user_id(admin_user_id)
    uname = f"@{admin['username']}" if admin and admin['username'] else f"ID:{admin_user_id}"
    tag = admin["tag"] if admin else "?"

    remove_admin(admin_user_id)

    text = f"✅ <b>Админ удалён!</b>\n\n👤 {uname}\n🏷 #{tag}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список", callback_data="gadmins_list")],
        [InlineKeyboardButton(text="⬅️ Меню админов", callback_data="gadmins")],
    ])

    await _render(callback, text, kb)


# ═══════════════ Статистика ═══════════════

@router.callback_query(F.data == "gadmins_stats")
async def cb_admins_stats(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
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
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="gadmins_stats")],
            [InlineKeyboardButton(text="⬅️ Меню админов", callback_data="gadmins")],
        ]
    )

    await _render(callback, text, kb)
# ═══════════════ Добавить ═══════════════

@router.callback_query(F.data == "gadmins_add")
async def cb_add_admin(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.waiting_add_admin)

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
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="gadmins_list")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(AdminFSM.waiting_add_admin)
async def fsm_add_admin(message: Message, state: FSMContext) -> None:
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
                [InlineKeyboardButton(text="📋 Список", callback_data="gadmins_list")],
                [InlineKeyboardButton(text="⬅️ Меню админов", callback_data="gadmins")],
            ])
        )
    else:
        await message.answer(
            "⚠️ Этот пользователь уже добавлен как админ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Список", callback_data="gadmins_list")]
            ])
        )


# ═══════════════ Удалить ═══════════════

@router.callback_query(F.data == "gadmins_del")
async def cb_delete_admin(callback: CallbackQuery, state: FSMContext) -> None:
    admins = get_admins_all()
    if not admins:
        await callback.answer("Нет админов")
        return

    await state.set_state(AdminFSM.waiting_delete_admin)

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
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="gadmins_list")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(AdminFSM.waiting_delete_admin)
async def fsm_delete_admin(message: Message, state: FSMContext) -> None:
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
            [InlineKeyboardButton(text="📋 Список", callback_data="gadmins_list")],
            [InlineKeyboardButton(text="⬅️ Меню админов", callback_data="gadmins")],
        ])
    )
# ═══════════════ Редактор тегов ═══════════════

@router.callback_query(F.data == "gadmins_edit")
async def cb_edit_tag(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.waiting_edit_tag)
    await state.update_data(admin_edit_user_id=None)

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
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="gadmins_list")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(AdminFSM.waiting_edit_tag)
async def fsm_edit_tag(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    admin_eid = data.get("admin_edit_user_id")

    # Редактирование тега из карточки конкретного админа (вводим только новый тег)
    if admin_eid is not None:
        admin = get_admin_by_user_id(admin_eid)
        new_tag = (message.text or "").strip().lstrip("#")
        if not admin or not new_tag:
            await message.answer("❌ Некорректный тег. Введи тег ещё раз.")
            return

        old_tag = admin["tag"]
        update_admin_tag(admin_eid, new_tag)
        await state.clear()
        uname = f"@{admin['username']}" if admin.get("username") else f"ID:{admin_eid}"
        await message.answer(
            f"✅ <b>Тег обновлён!</b>\n\n"
            f"👤 {uname}\n"
            f"🏷 #{old_tag} → #{new_tag}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Список", callback_data="gadmins_list")],
                [InlineKeyboardButton(text="⬅️ Меню админов", callback_data="gadmins")],
            ])
        )
        return

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
        f"👤 {uname}\n"
        f"🏷 #{old_tag} → #{new_tag}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список", callback_data="gadmins_list")],
            [InlineKeyboardButton(text="⬅️ Меню админов", callback_data="gadmins")],
        ])
    )


# ═══════════════ Поиск по тегу ═══════════════

@router.callback_query(F.data == "gadmins_search")
async def cb_search_tag(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.waiting_search_tag)

    text = "🔍 <b>Поиск по тегу</b>\n\nОтправь тег.\nПример: <code>продажи</code>"

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="gadmins_list")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(AdminFSM.waiting_search_tag)
async def fsm_search_tag(message: Message, state: FSMContext) -> None:
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
                [InlineKeyboardButton(text="📋 Список", callback_data="gadmins_list")]
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
            [InlineKeyboardButton(text="📋 Список", callback_data="gadmins_list")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="gadmins_stats")],
            [InlineKeyboardButton(text="⬅️ Меню админов", callback_data="gadmins")],
        ])
    )