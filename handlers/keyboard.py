# -*- coding: utf-8 -*-

from html import escape

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from services.storage import (
    get_user_bots,
    get_bot_keyboard,
    set_bot_keyboard,
    bot_display_name,
)

router = Router()

ADMIN_TEXT = "сменить админа"


def bot_keyboard_kb(bot_id: int, buttons: list[dict]) -> InlineKeyboardMarkup:
    has_admin = any(b.get("kind") == "admin" for b in buttons)
    admin_text = f"✅ «{ADMIN_TEXT}»" if has_admin else f"❌ «{ADMIN_TEXT}»"
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=f"🔄 {admin_text}", callback_data=f"kb_admin_{bot_id}")],
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="keyboard_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def describe(buttons: list[dict]) -> str:
    has_admin = any(b.get("kind") == "admin" for b in buttons)
    if has_admin:
        return f"  🖱 «{escape(ADMIN_TEXT)}»"
    return "— кнопок нет —"


async def _render(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            try:
                await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
    try:
        await callback.answer()
    except Exception:
        pass


# ═══════════════ Меню выбора бота ═══════════════

@router.callback_query(F.data == "keyboard_menu")
async def cb_keyboard_menu(callback: CallbackQuery, state) -> None:
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
        f"⌨️ <b>Клавиатура — {escape(name)}</b>\n\n"
        f"Текущие кнопки:\n{describe(buttons)}\n\n"
        f"Включи/выключи готовую кнопку «сменить админа»."
    )
    await _render(callback, text, bot_keyboard_kb(bot_id, buttons))


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