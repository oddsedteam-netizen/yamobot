from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from services.storage import get_user_bots, bot_display_name
from services.child_manager import ChildManager

router = Router()


def my_bots_kb(user_id: int, child_manager: ChildManager) -> InlineKeyboardMarkup:
    bots = get_user_bots(user_id)
    rows: list[list[InlineKeyboardButton]] = []

    for b in bots:
        name = bot_display_name(b)
        running = child_manager.is_running(b["id"])
        status = "🟢" if running else "🔴"
        rows.append([
            InlineKeyboardButton(
                text=f"{status} {name}",
                callback_data=f"bot_{b['id']}"
            )
        ])

    if bots:
        rows.append([
            InlineKeyboardButton(text="📌 Выбрать все", callback_data="select_all")
        ])

    rows.append([
        InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")
    ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "my_bots")
async def cb_my_bots(callback: CallbackQuery, state: FSMContext,
                     child_manager: ChildManager) -> None:
    await state.clear()
    user_id = callback.from_user.id
    bots = get_user_bots(user_id)

    if bots:
        text = (
            f"🤖 <b>Мои боты</b>  ({len(bots)} шт.)\n\n"
            "🟢 — работает  🔴 — остановлен\n\n"
            "Выбери бота или нажми «Выбрать все»:"
        )
    else:
        text = (
            "🤖 <b>Мои боты</b>\n\n"
            "У тебя пока нет подключённых ботов.\n"
            "Нажми «➕ Добавить бота»."
        )

    if callback.message:
        try:
            await callback.message.edit_text(
                text, reply_markup=my_bots_kb(user_id, child_manager)
            )
        except Exception:
            await callback.message.answer(
                text, reply_markup=my_bots_kb(user_id, child_manager)
            )
    await callback.answer()