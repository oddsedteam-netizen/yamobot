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

from services.storage import (
    get_bot_by_id,
    update_bot_field,
    bot_display_name,
    get_bot_links,
    set_bot_links,
)
from services.child_manager import ChildManager

router = Router()


class EditorFSM(StatesGroup):
    waiting_welcome_text = State()
    waiting_link_name = State()
    waiting_link_url = State()


def editor_kb(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="💬 Изменить приветствие",
                callback_data=f"edit_welcome_{bot_id}"
            )],
            [InlineKeyboardButton(
                text="🔗 Линки",
                callback_data=f"edit_links_{bot_id}"
            )],
            [InlineKeyboardButton(
                text="⬅️ Назад к боту",
                callback_data=f"bot_{bot_id}"
            )],
        ]
    )


def links_kb(bot_id: int, links: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for i, link in enumerate(links):
        rows.append([
            InlineKeyboardButton(
                text=f"🔗 {link['text']}",
                callback_data=f"viewlink_{bot_id}_{i}"
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"dellink_{bot_id}_{i}"
            ),
        ])

    rows.append([
        InlineKeyboardButton(
            text="➕ Добавить линк",
            callback_data=f"addlink_{bot_id}"
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="⬅️ Назад к редактору",
            callback_data=f"editor_{bot_id}"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═══════════════ Редактор — главная ═══════════════

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
    links_text = ""
    if links:
        links_text = "\n🔗 Линки:\n"
        for link in links:
            links_text += f"  • <a href=\"{link['url']}\">{link['text']}</a>\n"
    else:
        links_text = "\n🔗 Линки: — нет —"

    text = (
        f"✏️ <b>Редактор — {name}</b>\n\n"
        f"💬 Приветствие:\n{welcome}\n"
        f"{links_text}\n\n"
        f"Выбери что изменить:"
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=editor_kb(bot_id))
        except Exception:
            await callback.message.answer(text, reply_markup=editor_kb(bot_id))
    await callback.answer()


# ═══════════════ Приветствие ═══════════════

@router.callback_query(F.data.startswith("edit_welcome_"))
async def cb_edit_welcome(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_")[-1])

    await state.set_state(EditorFSM.waiting_welcome_text)
    await state.update_data(editing_bot_id=bot_id)

    text = (
        "💬 <b>Новое приветствие</b>\n\n"
        "Отправь текст, который дочерний бот покажет при /start.\n\n"
        "Поддерживается HTML и премиум-эмодзи.\n\n"
        "Пример:\n<code>👋 Привет! Добро пожаловать!</code>"
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"editor_{bot_id}")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(EditorFSM.waiting_welcome_text)
async def fsm_welcome_text(message: Message, state: FSMContext,
                           child_manager: ChildManager) -> None:
    data = await state.get_data()
    bot_id = data.get("editing_bot_id")
    user_id = message.from_user.id

    if not bot_id:
        await state.clear()
        await message.answer("⚠️ Ошибка. Попробуй снова.")
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
        status = "🟢 Бот перезапущен с новым приветствием"
    else:
        status = "💾 Сохранено. Применится при запуске."

    name = bot_display_name(bot_info) if bot_info else f"bot_{bot_id}"

    await message.answer(
        f"✅ <b>Приветствие обновлено!</b>\n\n"
        f"🤖 {name}\n"
        f"{status}\n\n"
        f"💬 Новый текст:\n{new_welcome}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактор", callback_data=f"editor_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
        ])
    )


# ═══════════════ Линки ═══════════════

@router.callback_query(F.data.startswith("edit_links_"))
async def cb_edit_links(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    bot_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    links = get_bot_links(user_id, bot_id)

    if links:
        text = f"🔗 <b>Линки</b> ({len(links)} шт.)\n\n"
        for i, link in enumerate(links, 1):
            text += f"{i}. <a href=\"{link['url']}\">{link['text']}</a>\n"
    else:
        text = "🔗 <b>Линки</b>\n\nПока нет ни одного линка."

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=links_kb(bot_id, links))
        except Exception:
            await callback.message.answer(text, reply_markup=links_kb(bot_id, links))
    await callback.answer()


@router.callback_query(F.data.startswith("addlink_"))
async def cb_add_link(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_", 1)[1])

    await state.set_state(EditorFSM.waiting_link_name)
    await state.update_data(link_bot_id=bot_id)

    text = (
        "🔗 <b>Новый линк</b>\n\n"
        "Отправь <b>название кнопки</b>.\n\n"
        "Пример: <code>Наш ТГК</code>"
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_links_{bot_id}")]
                ])
            )
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
        f"✅ Название: <b>{link_name}</b>\n\n"
        f"Теперь отправь <b>ссылку</b> (URL).\n\n"
        f"Пример: <code>https://t.me/yourchannel</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_links_{bot_id}")]
        ])
    )


@router.message(EditorFSM.waiting_link_url)
async def fsm_link_url(message: Message, state: FSMContext,
                       child_manager: ChildManager) -> None:
    link_url = (message.text or "").strip()

    if not link_url.startswith(("http://", "https://", "tg://")):
        await message.answer(
            "❌ Ссылка должна начинаться с <code>http://</code>, "
            "<code>https://</code> или <code>tg://</code>"
        )
        return

    data = await state.get_data()
    bot_id = data.get("link_bot_id")
    link_name = data.get("link_name", "Кнопка")
    user_id = message.from_user.id

    await state.clear()

    links = get_bot_links(user_id, bot_id)
    links.append({"text": link_name, "url": link_url})
    set_bot_links(user_id, bot_id, links)

    # Перезапуск дочерки
    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info and child_manager.is_running(bot_id):
        await child_manager.restart_child(bot_info)

    await message.answer(
        f"✅ <b>Линк добавлен!</b>\n\n"
        f"🔗 {link_name} → {link_url}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Линки", callback_data=f"edit_links_{bot_id}")],
            [InlineKeyboardButton(text="✏️ Редактор", callback_data=f"editor_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
        ])
    )


@router.callback_query(F.data.startswith("dellink_"))
async def cb_delete_link(callback: CallbackQuery,
                         child_manager: ChildManager) -> None:
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
        await callback.answer("⚠️ Линк не найден")

    if links:
        text = f"🔗 <b>Линки</b> ({len(links)} шт.)\n\n"
        for i, link in enumerate(links, 1):
            text += f"{i}. <a href=\"{link['url']}\">{link['text']}</a>\n"
    else:
        text = "🔗 <b>Линки</b>\n\nВсе линки удалены."

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=links_kb(bot_id, links))
        except Exception:
            pass


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
        await callback.answer("⚠️ Линк не найден")