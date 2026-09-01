import json

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
    update_bot_field,
    bot_display_name,
    get_bot_links,
    set_bot_links,
    get_user_bots,
    set_welcome_for_all,
    set_links_for_all,
)
from services.child_manager import ChildManager

router = Router()


class EditorFSM(StatesGroup):
    waiting_welcome_text = State()
    waiting_link_name = State()
    waiting_link_url = State()
    waiting_global_welcome = State()
    waiting_global_link_name = State()
    waiting_global_link_url = State()


# ═══════════════ Одиночный редактор ═══════════════

def editor_kb(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Изменить приветствие", callback_data=f"edit_welcome_{bot_id}")],
        [InlineKeyboardButton(text="🔗 Линки", callback_data=f"edit_links_{bot_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}")],
    ])


def links_kb(bot_id: int, links: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, link in enumerate(links):
        rows.append([
            InlineKeyboardButton(text=f"🔗 {link['text']}", callback_data=f"viewlink_{bot_id}_{i}"),
            InlineKeyboardButton(text="🗑", callback_data=f"dellink_{bot_id}_{i}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить линк", callback_data=f"addlink_{bot_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к редактору", callback_data=f"editor_{bot_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("editor_"))
async def cb_editor(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    bot_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    name = bot_display_name(bot_info)
    welcome = bot_info.get("welcome_text", "") or "— не задано —"
    links = get_bot_links(user_id, bot_id)

    if links:
        links_text = "\n🔗 Линки:\n" + "\n".join(f"  • {l['text']} → {l['url']}" for l in links)
    else:
        links_text = "\n🔗 Линки: — нет —"

    text = (
        f"✏️ <b>Редактор — {name}</b>\n\n"
        f"💬 Приветствие:\n{welcome}\n"
        f"{links_text}\n\n"
        f"Выбери что изменить:"
    )

    await render_callback(callback, text, editor_kb(bot_id))


@router.callback_query(F.data.startswith("edit_welcome_"))
async def cb_edit_welcome(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_")[-1])

    await state.set_state(EditorFSM.waiting_welcome_text)
    await state.update_data(editing_bot_id=bot_id)

    text = (
        "💬 <b>Новое приветствие</b>\n\n"
        "Отправь текст, HTML и премиум-эмодзи поддерживаются."
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"editor_{bot_id}")]
            ]))
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(EditorFSM.waiting_welcome_text)
async def fsm_welcome_text(message: Message, state: FSMContext, child_manager: ChildManager) -> None:
    data = await state.get_data()
    bot_id = data.get("editing_bot_id")
    user_id = message.from_user.id

    if not bot_id:
        await state.clear()
        return

    new_welcome = message.html_text or message.text or ""
    if not new_welcome.strip():
        await message.answer("❌ Текст не может быть пустым.")
        return

    update_bot_field(user_id, bot_id, "welcome_text", new_welcome)
    await state.clear()

    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info and child_manager.is_running(bot_id):
        await child_manager.restart_child(bot_info)
        status = "🟢 Бот перезапущен"
    else:
        status = "💾 Сохранено"

    await message.answer(
        f"✅ Приветствие обновлено!\n\n{status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактор", callback_data=f"editor_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
        ])
    )


@router.callback_query(F.data.startswith("edit_links_"))
async def cb_edit_links(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    bot_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    links = get_bot_links(user_id, bot_id)

    if links:
        text = f"🔗 <b>Линки</b> ({len(links)})\n\n"
        for i, link in enumerate(links, 1):
            text += f"{i}. {link['text']} → {link['url']}\n"
    else:
        text = "🔗 <b>Линки</b>\n\nПока нет ни одного линка."

    await render_callback(callback, text, links_kb(bot_id, links))


@router.callback_query(F.data.startswith("addlink_"))
async def cb_add_link(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_", 1)[1])

    await state.set_state(EditorFSM.waiting_link_name)
    await state.update_data(link_bot_id=bot_id)

    text = "🔗 <b>Новый линк</b>\n\nОтправь <b>название кнопки</b>.\nПример: <code>Наш ТГК</code>"

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_links_{bot_id}")]
            ]))
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(EditorFSM.waiting_link_name)
async def fsm_link_name(message: Message, state: FSMContext) -> None:
    link_name = (message.text or "").strip()
    if not link_name:
        await message.answer("❌ Название не может быть пустым.")
        return

    await state.update_data(link_name=link_name)
    await state.set_state(EditorFSM.waiting_link_url)

    data = await state.get_data()
    bot_id = data.get("link_bot_id")

    await message.answer(
        f"✅ Название: <b>{link_name}</b>\n\nТеперь отправь <b>ссылку</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_links_{bot_id}")]
        ])
    )


@router.message(EditorFSM.waiting_link_url)
async def fsm_link_url(message: Message, state: FSMContext, child_manager: ChildManager) -> None:
    link_url = (message.text or "").strip()

    if not link_url.startswith(("http://", "https://", "tg://")):
        await message.answer("❌ Ссылка должна начинаться с http:// https:// или tg://")
        return

    data = await state.get_data()
    bot_id = data.get("link_bot_id")
    link_name = data.get("link_name", "Кнопка")
    user_id = message.from_user.id

    await state.clear()

    links = get_bot_links(user_id, bot_id)
    links.append({"text": link_name, "url": link_url})
    set_bot_links(user_id, bot_id, links)

    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info and child_manager.is_running(bot_id):
        await child_manager.restart_child(bot_info)
        status = "🟢 Бот перезапущен — линк уже в приветствии."
    else:
        status = "⚠️ Бот не запущен — линк применится при запуске бота."

    await message.answer(
        f"✅ Линк добавлен!\n\n🔗 {link_name} → {link_url}\n\n{status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Линки", callback_data=f"edit_links_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
        ])
    )


@router.callback_query(F.data.startswith("dellink_"))
async def cb_delete_link(callback: CallbackQuery, child_manager: ChildManager) -> None:
    parts = callback.data.split("_")
    bot_id = int(parts[1])
    link_idx = int(parts[2])
    user_id = callback.from_user.id

    links = get_bot_links(user_id, bot_id)
    if 0 <= link_idx < len(links):
        removed = links.pop(link_idx)
        set_bot_links(user_id, bot_id, links)

        bot_info = get_bot_by_id(user_id, bot_id)
        if bot_info and child_manager.is_running(bot_id):
            await child_manager.restart_child(bot_info)

        await callback.answer(f"🗑 Удалён: {removed['text']}")
    else:
        await callback.answer("⚠️ Не найден")

    if links:
        text = f"🔗 <b>Линки</b> ({len(links)})\n\n"
        for i, link in enumerate(links, 1):
            text += f"{i}. {link['text']} → {link['url']}\n"
    else:
        text = "🔗 <b>Линки</b>\n\nВсе линки удалены."

    await safe_edit(callback.message, text, reply_markup=links_kb(bot_id, links))


@router.callback_query(F.data.startswith("viewlink_"))
async def cb_view_link(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    bot_id = int(parts[1])
    link_idx = int(parts[2])
    user_id = callback.from_user.id

    links = get_bot_links(user_id, bot_id)
    if 0 <= link_idx < len(links):
        link = links[link_idx]
        await callback.answer(f"{link['text']}: {link['url']}", show_alert=True)
    else:
        await callback.answer("⚠️ Не найден")


# ═══════════════ ГЛОБАЛЬНЫЙ редактор для всех ботов ═══════════════

def global_editor_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Приветствие для всех", callback_data="all_edit_welcome")],
        [InlineKeyboardButton(text="🔗 Добавить линк всем", callback_data="all_edit_link")],
        [InlineKeyboardButton(text="🗑 Удалить все линки", callback_data="all_clear_links")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="select_all")],
    ])


@router.callback_query(F.data == "all_editor")
async def cb_all_editor(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user_id = callback.from_user.id
    bots = get_user_bots(user_id)

    text = (
        f"✏️ <b>Редактор для всех ботов</b>\n\n"
        f"Ботов: <b>{len(bots)}</b>\n\n"
        f"Изменения применятся ко <b>всем</b> твоим ботам сразу."
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=global_editor_kb())
        except Exception:
            await callback.message.answer(text, reply_markup=global_editor_kb())
    await callback.answer()


@router.callback_query(F.data == "all_edit_welcome")
async def cb_all_edit_welcome(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditorFSM.waiting_global_welcome)

    text = (
        "💬 <b>Приветствие для всех ботов</b>\n\n"
        "Отправь текст. Он применится ко всем твоим ботам.\n\n"
        "Поддерживается HTML и премиум-эмодзи."
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="all_editor")]
            ]))
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(EditorFSM.waiting_global_welcome)
async def fsm_global_welcome(message: Message, state: FSMContext, child_manager: ChildManager) -> None:
    user_id = message.from_user.id
    new_welcome = message.html_text or message.text or ""

    if not new_welcome.strip():
        await message.answer("❌ Текст не может быть пустым.")
        return

    count = set_welcome_for_all(user_id, new_welcome)
    await state.clear()

    # Перезапускаем все дочерки
    bots = get_user_bots(user_id)
    restarted = 0
    for b in bots:
        if child_manager.is_running(b["id"]):
            await child_manager.restart_child(b)
            restarted += 1

    await message.answer(
        f"✅ <b>Приветствие обновлено для всех!</b>\n\n"
        f"📊 Изменено: <b>{count}</b> ботов\n"
        f"🔄 Перезапущено: <b>{restarted}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактор всех", callback_data="all_editor")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="back_main")],
        ])
    )


@router.callback_query(F.data == "all_edit_link")
async def cb_all_edit_link(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditorFSM.waiting_global_link_name)

    text = (
        "🔗 <b>Добавить линк ко всем ботам</b>\n\n"
        "Отправь <b>название кнопки</b>."
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="all_editor")]
            ]))
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(EditorFSM.waiting_global_link_name)
async def fsm_global_link_name(message: Message, state: FSMContext) -> None:
    link_name = (message.text or "").strip()
    if not link_name:
        await message.answer("❌ Название не может быть пустым.")
        return

    await state.update_data(global_link_name=link_name)
    await state.set_state(EditorFSM.waiting_global_link_url)

    await message.answer(
        f"✅ Название: <b>{link_name}</b>\n\nТеперь отправь <b>ссылку</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="all_editor")]
        ])
    )


@router.message(EditorFSM.waiting_global_link_url)
async def fsm_global_link_url(message: Message, state: FSMContext, child_manager: ChildManager) -> None:
    link_url = (message.text or "").strip()

    if not link_url.startswith(("http://", "https://", "tg://")):
        await message.answer("❌ Ссылка должна начинаться с http:// https:// или tg://")
        return

    data = await state.get_data()
    link_name = data.get("global_link_name", "Кнопка")
    user_id = message.from_user.id

    await state.clear()

    # Добавляем линк ко всем ботам
    bots = get_user_bots(user_id)
    for b in bots:
        links = get_bot_links(user_id, b["id"])
        links.append({"text": link_name, "url": link_url})
        set_bot_links(user_id, b["id"], links)

    # Перезапускаем
    restarted = 0
    for b in bots:
        if child_manager.is_running(b["id"]):
            await child_manager.restart_child(b)
            restarted += 1

    await message.answer(
        f"✅ <b>Линк добавлен ко всем ботам!</b>\n\n"
        f"🔗 {link_name} → {link_url}\n\n"
        f"📊 Ботов: <b>{len(bots)}</b>\n"
        f"🔄 Перезапущено: <b>{restarted}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактор всех", callback_data="all_editor")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="back_main")],
        ])
    )


@router.callback_query(F.data == "all_clear_links")
async def cb_all_clear_links(callback: CallbackQuery, child_manager: ChildManager) -> None:
    user_id = callback.from_user.id
    bots = get_user_bots(user_id)

    set_links_for_all(user_id, [])

    restarted = 0
    for b in bots:
        if child_manager.is_running(b["id"]):
            await child_manager.restart_child(b)
            restarted += 1

    if callback.message:
        try:
            await callback.message.edit_text(
                f"✅ <b>Все линки удалены!</b>\n\n"
                f"📊 Ботов: <b>{len(bots)}</b>\n"
                f"🔄 Перезапущено: <b>{restarted}</b>",
                reply_markup=global_editor_kb()
            )
        except Exception:
            pass
    await callback.answer("Все линки удалены")