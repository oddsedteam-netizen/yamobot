from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from services.storage import get_user_bots, bot_display_name

router = Router()


def select_all_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📨 Рассылка", callback_data="all_mailing")],
            [InlineKeyboardButton(text="🛡 Антиспам", callback_data="all_antispam")],
            [InlineKeyboardButton(text="⛔ Остановить все", callback_data="all_stop")],
            [InlineKeyboardButton(text="📊 Общая статистика", callback_data="all_stats")],
            [InlineKeyboardButton(text="⬅️ Назад к ботам", callback_data="my_bots")],
        ]
    )


@router.callback_query(F.data == "select_all")
async def cb_select_all(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    bots = get_user_bots(user_id)

    names = "\n".join(f"  • {bot_display_name(b)}" for b in bots)
    text = (
        f"📌 <b>Все боты</b> ({len(bots)} шт.)\n\n"
        f"{names}\n\n"
        f"Выбери действие:"
    )
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=select_all_kb())
        except Exception:
            await callback.message.answer(text, reply_markup=select_all_kb())
    await callback.answer()


@router.callback_query(F.data == "all_mailing")
async def cb_all_mailing(callback: CallbackQuery) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(
                "📨 <b>Рассылка для всех</b>\n\nФункция в разработке.",
                reply_markup=select_all_kb()
            )
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data == "all_antispam")
async def cb_all_antispam(callback: CallbackQuery) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(
                "🛡 <b>Антиспам для всех</b>\n\nФункция в разработке.",
                reply_markup=select_all_kb()
            )
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data == "all_stop")
async def cb_all_stop(callback: CallbackQuery) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(
                "⛔ <b>Остановка всех</b>\n\nФункция в разработке.",
                reply_markup=select_all_kb()
            )
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data == "all_stats")
async def cb_all_stats(callback: CallbackQuery) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(
                "📊 <b>Общая статистика</b>\n\nФункция в разработке.",
                reply_markup=select_all_kb()
            )
        except Exception:
            pass
    await callback.answer()