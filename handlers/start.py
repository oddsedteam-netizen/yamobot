from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from handlers._common import render_callback
from services.child_manager import ChildManager
from services.config import is_super_admin
from services.storage import (
    get_admin_invite_owner,
    consume_admin_invite,
    add_admin,
    get_admin_by_user_id,
    register_user,
    is_registry_user_banned,
)

router = Router()


class StartFSM(StatesGroup):
    waiting_admin_tag = State()


WELCOME_TEXT = (
    "👋 <b>Добро пожаловать в YamoBot!</b>\n\n"
    "Это бот-менеджер. Через него ты сможешь подключать "
    "и управлять другими Telegram-ботами.\n\n"
    "Выбери действие кнопками ниже:"
)


def main_menu_kb() -> ReplyKeyboardMarkup:
    """Главное меню — reply-клавиатура."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🤖 Боты")],
            [KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="👥 Админы")],
            [KeyboardButton(text="📋 ПЗ")],
            [KeyboardButton(text="⚠️ Жалоба")],
            [KeyboardButton(text="❓ FAQ")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


async def _show_main(message: Message) -> None:
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = message.from_user.id
    register_user(user_id, message.from_user.username or "", message.from_user.first_name or "")
    if is_registry_user_banned(user_id) and not is_super_admin(user_id):
        await message.answer("🚫 Вы заблокированы администрацией.")
        return
    if message.text and "addadmin_" in message.text:
        token = message.text.split("addadmin_", 1)[1].strip().split()[0]
        owner_id = get_admin_invite_owner(token)
        if owner_id is None:
            await message.answer("❌ Ссылка-приглашение недействительна.")
            return
        await state.set_state(StartFSM.waiting_admin_tag)
        await state.update_data(admin_owner_id=owner_id, admin_token=token)
        await message.answer("🎉 <b>Вас пригласили стать админом!</b>\n\nОтправь свой <b>тег</b>.")
        return
    await _show_main(message)


@router.message(StartFSM.waiting_admin_tag)
async def fsm_waiting_admin_tag(message: Message, state: FSMContext) -> None:
    tag = (message.text or "").strip().lstrip("#")
    if not tag:
        await message.answer("❌ Тег не может быть пустым.")
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
        await message.answer(f"⚠️ Ты уже админ с тегом #{already['tag']}.")
        return
    ok = add_admin(owner_id, user_id, username, tag)
    await state.clear()
    if ok:
        await message.answer(f"✅ <b>Ты стал админом!</b>\n\n🏷 Тег: <b>#{tag}</b>")
    else:
        await message.answer("⚠️ Не удалось добавить тебя как админа.")


# ═══════════════ Reply-кнопки главного меню ═══════════════

@router.message(F.text == "🤖 Боты")
async def on_bots_button(message: Message, state: FSMContext,
                         child_manager: ChildManager) -> None:
    await state.clear()
    from handlers.my_bots import show_my_bots
    await show_my_bots(message, child_manager)


@router.message(F.text == "👤 Профиль")
async def on_profile_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    from handlers.profile import show_profile
    await show_profile(message)


@router.message(F.text == "👥 Админы")
async def on_admins_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    from handlers.admins import show_admins
    await show_admins(message)


@router.message(F.text == "📋 ПЗ")
async def on_pz_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    from handlers.overview import show_global_pz
    await show_global_pz(message)


@router.message(F.text == "⚠️ Жалоба")
async def on_complaint_button(message: Message, state: FSMContext) -> None:
    from handlers.complaints import start_complaint
    await start_complaint(message, state)


@router.message(F.text == "❓ FAQ")
async def on_faq_button(message: Message) -> None:
    await message.answer(FAQ_TEXT)


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if callback.message:
        try:
            await callback.message.edit_text(WELCOME_TEXT, reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_main(message)


@router.message(Command("adm"))
async def cmd_adm(message: Message) -> None:
    """Открывает админ-панель только для супер-админа (переменная ADMIN)."""
    user_id = message.from_user.id
    if not is_super_admin(user_id):
        await message.answer("⛔ Доступ запрещён.")
        return
    from handlers.profile import admin_kb
    await message.answer("🛡 <b>Админ-панель</b>\n\nВыбери раздел:", reply_markup=admin_kb())


FAQ_TEXT = (
    "❓ <b>Как пользоваться YamoBot</b>\n\n"
    "YamoBot — это менеджер для подключения и управления твоими Telegram-ботами. "
    "Всё управление идёт через кнопки главного меню.\n\n"
    "🤖 <b>Боты</b> — список всех твоих подключённых ботов. "
    "Можно открыть любого бота, выбрать все боты разом или добавить нового. "
    "Если у тебя несколько типов ботов, сначала бот спросит, какую категорию открыть.\n"
    "➕ <b>Добавить бота</b> — отправь токен бота от @BotFather. "
    "Бот спросит тип (стандарт или анкетница) и настроит клавиатуру.\n"
    "👤 <b>Профиль</b> — твои данные: имя, ID, количество ботов, админов и обращений. "
    "Рядом с каждым ботом показано количество его обращений.\n"
    "👥 <b>Админы</b> — управление админами твоих ботов: список, добавление, удаление.\n"
    "📋 <b>ПЗ</b> — просмотр обращений пользователей твоих ботов.\n"
    "⚠️ <b>Жалоба</b> — отправь жалобу администрации. "
    "Понадобится указать категорию, приложить скриншот и оставить комментарий.\n\n"
    "Все данные привязаны к твоему аккаунту. Просто нажимай нужную кнопку и следуй подсказкам."
)


@router.callback_query(F.data == "faq")
async def cb_faq(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_TEXT, InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]]
    ))