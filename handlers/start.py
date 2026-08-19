from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

router = Router()

WELCOME_TEXT = (
    "👋 <b>Добро пожаловать в YamoBot!</b>\n\n"
    "Это бот-менеджер. Через него ты сможешь подключать "
    "и управлять другими Telegram-ботами.\n\n"
    "Выбери действие:"
)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Мои боты", callback_data="my_bots")],
            [InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")],
            [InlineKeyboardButton(text="👥 Совлад", callback_data="coowners")],
        ]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message:
        try:
            await callback.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
        except Exception:
            await callback.message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())
    await callback.answer()