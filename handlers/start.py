from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.storage import (
    get_admin_invite_owner,
    consume_admin_invite,
    add_admin,
    get_admin_by_user_id,
)

router = Router()


class StartFSM(StatesGroup):
    waiting_admin_tag = State()

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

    # Ссылка-приглашение админа: /start addadmin_<token>
    if message.text and "addadmin_" in message.text:
        token = message.text.split("addadmin_", 1)[1].strip().split()[0]
        owner_id = get_admin_invite_owner(token)
        if owner_id is None:
            await message.answer(
                "❌ Ссылка-приглашение недействительна или устарела.\n\n"
                "Попроси владельца бота сгенерировать новую ссылку."
            )
            return

        await state.set_state(StartFSM.waiting_admin_tag)
        await state.update_data(admin_owner_id=owner_id, admin_token=token)
        await message.answer(
            "🎉 <b>Вас пригласили стать админом!</b>\n\n"
            "Отправь свой <b>тег</b> (например: продажи, маркетинг). "
            "Бот привяжет тебя к ботам владельца."
        )
        return

    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.message(StartFSM.waiting_admin_tag)
async def fsm_waiting_admin_tag(message: Message, state: FSMContext) -> None:
    tag = (message.text or "").strip().lstrip("#")
    if not tag:
        await message.answer("❌ Тег не может быть пустым. Отправь тег ещё раз.")
        return

    data = await state.get_data()
    owner_id = data.get("admin_owner_id")
    token = data.get("admin_token")
    user_id = message.from_user.id
    username = message.from_user.username or ""

    consume_admin_invite(token)

    already = get_admin_by_user_id(owner_id, user_id)
    if already:
        await state.clear()
        await message.answer(
            "⚠️ Ты уже являешься админом.\n\n"
            f"Твой текущий тег: <b>#{already['tag']}</b>"
        )
        return

    ok = add_admin(owner_id, user_id, username, tag)
    await state.clear()

    if ok:
        await message.answer(
            f"✅ <b>Ты стал админом!</b>\n\n"
            f"👤 @{username or user_id}\n"
            f"🏷 Тег: <b>#{tag}</b>\n\n"
            f"Теперь ты можешь отвечать пользователям в ПЗ бота."
        )
    else:
        await message.answer("⚠️ Не удалось добавить тебя как админа. Попробуй позже.")


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
    "Каждому админу можно задать тег. Нажми на админа, чтобы изменить тег или удалить. "
    "Админа можно добавить вручную («➕ Добавить админа») или отправив ему ссылку-приглашение "
    "(«🔗 Добавить админа ссылкой») — он перейдёт по ней, введёт тег и станет админом.\n\n"
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