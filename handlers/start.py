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
            [InlineKeyboardButton(text="👥 Совладельцы", callback_data="coowners")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="gstats")],
            [InlineKeyboardButton(text="👤 Админы", callback_data="gadmins")],
            [InlineKeyboardButton(text="📋 ПЗ", callback_data="gpz")],
            [InlineKeyboardButton(text="❓ FAQ", callback_data="faq")],
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


FAQ_TEXT = (
    "❓ <b>Как пользоваться YamoBot</b>\n\n"
    "1️⃣ <b>🤖 Мои боты</b> — зайди сюда, чтобы увидеть все подключённые боты. "
    "Нажми на бота, чтобы открыть его меню: рассылка, антиспам, статистика, редактор, ПЗ.\n\n"
    "2️⃣ <b>➕ Добавить бота</b> — пришли токен от @BotFather. Бот будет добавлен и запущен.\n\n"
    "3️⃣ <b>📊 Статистика</b> — общая статистика по всем ботам.\n\n"
    "4️⃣ <b>👤 Админы</b> — добавь админов, которые будут отвечать пользователям в ПЗ. "
    "Каждому админу можно задать тег. Нажми на админа, чтобы изменить тег или удалить.\n\n"
    "5️⃣ <b>📋 ПЗ</b> — обращения пользователей. Пришли боту в личку — создастся топик "
    "в подключённой группе (команда /connect). Админ может нажать «Я беру» (/take).\n\n"
    "6️⃣ <b>👥 Совладельцы</b> — добавь людей, которые увидят твоих ботов и статистику.\n\n"
    "Все данные привязаны к твоему аккаунту. Чужие боты и админы тебе не видны."
)


@router.callback_query(F.data == "faq")
async def cb_faq(callback: CallbackQuery) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(FAQ_TEXT, reply_markup=main_menu_kb())
        except Exception:
            await callback.message.answer(FAQ_TEXT, reply_markup=main_menu_kb())
    await callback.answer()