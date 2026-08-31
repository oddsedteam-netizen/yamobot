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
    get_user_bots,
    get_bot_keyboard,
    set_bot_keyboard,
    bot_display_name,
)

router = Router()

ADMIN_TEXT = "сменить админа"


class KeyboardFSM(StatesGroup):
    waiting_link_name = State()
    waiting_link_url = State()


def _back_to_bots_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К списку ботов", callback_data="keyboard_menu")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")],
    ])


def bot_keyboard_kb(bot_id: int, buttons: list[dict]) -> InlineKeyboardMarkup:
    has_admin = any(b.get("kind") == "admin" for b in buttons)
    url_buttons = [(i, b) for i, b in enumerate(buttons) if b.get("kind") == "url"]

    admin_text = f"✅ «{ADMIN_TEXT}»" if has_admin else f"❌ «{ADMIN_TEXT}»"
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=f"🔄 {admin_text}", callback_data=f"kb_admin_{bot_id}")],
    ]

    for i, b in url_buttons:
        rows.append([
            InlineKeyboardButton(text=f"🔗 {b.get('text', 'кнопка')}", url=b.get("url", "")),
            InlineKeyboardButton(text="🗑", callback_data=f"kb_del_{bot_id}_{i}"),
        ])

    rows.append([InlineKeyboardButton(text="➕ Добавить кнопку-ссылку", callback_data=f"kb_add_{bot_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="keyboard_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def describe(buttons: list[dict]) -> str:
    if not buttons:
        return "— кнопок нет —"
    lines = []
    for b in buttons:
        if b.get("kind") == "url":
            lines.append(f"  🔗 {b.get('text', 'кнопка')} → {b.get('url', '')}")
        else:
            lines.append(f"  🖱 «{b.get('text', ADMIN_TEXT)}»")
    return "\n".join(lines) or "— кнопок нет —"


async def _render(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# ═══════════════ Список ботов ═══════════════

@router.callback_query(F.data == "keyboard_menu")
async def cb_keyboard_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user_id = callback.from_user.id
    bots = get_user_bots(user_id)

    if not bots:
        text = "⌨️ <b>Клавиатура</b>\n\nУ тебя пока нет подключённых ботов."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ])
        await _render(callback, text, kb)
        return

    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(text=f"🤖 {bot_display_name(b)}", callback_data=f"kbbot_{b['id']}")
    ] for b in bots]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
# ═══════════════ Тумблер «сменить админа» ═══════════════

@router.callback_query(F.data.startswith("kb_admin_"))
async def cb_toggle_admin(callback: CallbackQuery) -> None:
    bot_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    buttons = get_bot_keyboard(user_id, bot_id)
    if any(b.get("kind") == "admin" for b in buttons):
        buttons = [b for b in buttons if b.get("kind") != "admin"]
    else:
        buttons.append({"kind": "admin", "text": ADMIN_TEXT})

    set_bot_keyboard(user_id, bot_id, buttons)
    text = (
        f"⌨️ <b>Клавиатура</b>\n\n"
        f"Текущие кнопки:\n{describe(buttons)}"
    )
    await _render(callback, text, bot_keyboard_kb(bot_id, buttons))


# ═══════════════ Добавить кнопку-ссылку ═══════════════

@router.callback_query(F.data.startswith("kb_add_"))
async def cb_add_link(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_")[-1])
    await state.set_state(KeyboardFSM.waiting_link_name)
    await state.update_data(kb_bot_id=bot_id)

    text = "➕ <b>Добавить кнопку-ссылку</b>\n\nОтправь <b>название кнопки</b>.\nПример: <code>Наш канал</code>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"kbbot_{bot_id}")],
    ])
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.message(KeyboardFSM.waiting_link_name)
async def fsm_link_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название не может быть пустым.")
        return
    await state.update_data(kb_link_name=name)
    await state.set_state(KeyboardFSM.waiting_link_url)

    await message.answer(
        f"✅ Название: <b>{name}</b>\n\nТеперь отправь <b>ссылку</b>, на которую будет вести кнопка.\n"
        f"Пример: <code>https://t.me/мой_канал</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="keyboard_menu")],
        ])
    )


@router.message(KeyboardFSM.waiting_link_url)
async def fsm_link_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not url.startswith(("http://", "https://", "tg://")):
        await message.answer("❌ Ссылка должна начинаться с http://, https:// или tg://")
        return

    data = await state.get_data()
    bot_id = data.get("kb_bot_id")
    name = data.get("kb_link_name", "Кнопка")
    user_id = message.from_user.id
    await state.clear()

    buttons = get_bot_keyboard(user_id, bot_id)
    buttons.append({"kind": "url", "text": name, "url": url})
    set_bot_keyboard(user_id, bot_id, buttons)

    await message.answer(
        f"✅ <b>Кнопка добавлена!</b>\n\n🔗 {name} → {url}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⌨️ Клавиатура", callback_data=f"kbbot_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data="back_main")],
        ])
    )


# ═══════════════ Удалить кнопку-ссылку ═══════════════

@router.callback_query(F.data.startswith("kb_del_"))
async def cb_delete_link(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    bot_id = int(parts[2])
    idx = int(parts[3])
    user_id = callback.from_user.id

    buttons = get_bot_keyboard(user_id, bot_id)
    url_buttons_idx = [i for i, b in enumerate(buttons) if b.get("kind") == "url"]
    if 0 <= idx < len(url_buttons_idx):
        del buttons[url_buttons_idx[idx]]

    set_bot_keyboard(user_id, bot_id, buttons)
    text = f"⌨️ <b>Клавиатура</b>\n\nТекущие кнопки:\n{describe(buttons)}"
    await _render(callback, text, bot_keyboard_kb(bot_id, buttons))

    text = (
        "⌨️ <b>Клавиатура</b>\n\n"
        "Выбери бота, для которого настроить кнопки. "
        "В каждом боте будет показываться свой набор кнопок."
    )
    await _render(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("kbbot_"))
async def cb_edit_bot_keyboard(callback: CallbackQuery) -> None:
    bot_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    buttons = get_bot_keyboard(user_id, bot_id)
    name = "бот"
    for b in get_user_bots(user_id):
        if b["id"] == bot_id:
            name = bot_display_name(b)

    text = (
        f"⌨️ <b>Клавиатура — {name}</b>\n\n"
        f"Текущие кнопки:\n{describe(buttons)}\n\n"
        f"Что-то из этого будет показываться пользователям бота после /start."
    )
    await _render(callback, text, bot_keyboard_kb(bot_id, buttons))