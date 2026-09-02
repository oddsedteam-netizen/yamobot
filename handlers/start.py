from aiogram import Router, F
from aiogram.enums import ChatType
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
    get_user_bots,
    get_all_topics_for_bot,
    bot_display_name,
    get_owner_by_admin_chat,
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
            [KeyboardButton(text="📋 ПЗ"), KeyboardButton(text="👥 Админы")],
            [KeyboardButton(text="⚠️ Жалоба"), KeyboardButton(text="❓ FAQ")],
            [KeyboardButton(text="👤 Профиль")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


async def _show_main(message: Message) -> None:
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
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


@router.message(StartFSM.waiting_admin_tag, F.chat.type == ChatType.PRIVATE)
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
    if callback.message is None:
        return

    # Возвращаемся в главное меню: старый inline-экран сворачиваем,
    # а меню выводим КОМПАКТНО (без повторного большого приветствия).
    try:
        await callback.message.edit_text(
            "🏠 <b>Главное меню</b>\n\nДля навигации используйте кнопки ниже 👇",
            reply_markup=None,
        )
    except Exception:
        pass

    try:
        await callback.message.answer(
            "🏠 <b>Главное меню</b>\n\nВыбери действие кнопками ниже 👇",
            reply_markup=main_menu_kb(),
        )
    except Exception:
        pass


@router.message(Command("menu"), F.chat.type == ChatType.PRIVATE)
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_main(message)


@router.message(Command("adm"), F.chat.type == ChatType.PRIVATE)
async def cmd_adm(message: Message) -> None:
    """Открывает админ-панель только для супер-админа (переменная ADMIN)."""
    user_id = message.from_user.id
    if not is_super_admin(user_id):
        await message.answer("⛔ Доступ запрещён.")
        return
    from handlers.profile import admin_kb
    await message.answer("🛡 <b>Админ-панель</b>\n\nВыбери раздел:", reply_markup=admin_kb())


@router.message(Command("status"), F.chat.type == ChatType.PRIVATE)
async def cmd_status(message: Message, child_manager: ChildManager) -> None:
    """Диагностика: состояние всех дочерних ботов (только для супер-админа)."""
    user_id = message.from_user.id
    if not is_super_admin(user_id):
        await message.answer("⛔ Доступ запрещён.")
        return

    from services.storage import (
        get_all_bots_flat,
        get_feedback_chat,
        get_admins_all,
        get_child_users,
    )

    all_bots = get_all_bots_flat()
    if not all_bots:
        await message.answer("📭 Боты не подключены.")
        return

    lines = ["📊 <b>Состояние ботов</b>\n"]
    for b in all_bots:
        bot_id = b["id"]
        running = child_manager.is_running(bot_id)
        status = "🟢 работает" if running else "🔴 не работает"
        fchat = get_feedback_chat(bot_id)
        admins = len(get_admins_all(b["owner_id"]))
        users = len(get_child_users(bot_id, only_active=False))
        chat_info = "есть" if fchat else "нет"
        lines.append(
            f"🤖 @{b.get('username') or b['id']} — {status}\n"
            f"   Тип: {b.get('bot_type') or 'standard'} | /connect: {chat_info} | "
            f"админов: {admins} | юзеров: {users}"
        )

    await message.answer("\n".join(lines))


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
    "📋 <b>ПЗ</b> — это список пользователей твоих ботов.\n"
    "⚠️ <b>Жалоба</b> — отправь жалобу администрации. "
    "Понадобится указать категорию, приложить скриншот и оставить комментарий.\n\n"
    "🧭 <b>Команды бота</b>\n"
    "• <code>/start</code> — запуск и главное меню.\n"
    "• <code>/menu</code> — открыть главное меню.\n"
    "• <code>.стата</code> — краткая сводка по ПЗ всех ботов "
    "(сколько всего ПЗ, с админом и без админа).\n"
    "• <code>/status</code> — диагностика состояния дочерних ботов (для супер-админа).\n"
    "• <code>/adm</code> — админ-панель (для супер-админа).\n\n"
    "Все данные привязаны к твоему аккаунту. Просто нажимай нужную кнопку и следуй подсказкам."
)


@router.callback_query(F.data == "faq")
async def cb_faq(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_TEXT, InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]]
    ))


# ═══════════════ .стата — сводка в чате админов ═══════════════

def _stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ ПЗ без админов", callback_data="gstat_noadmin")]
    ])


def _stats_payload(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Сводка по ПЗ всех ботов владельца (общая и по каждому боту)."""
    bots = get_user_bots(owner_id)
    if not bots:
        return "📭 Нет подключённых ботов.", _stats_kb()

    total = 0
    noadmin = 0
    lines = []
    for b in bots:
        ts = get_all_topics_for_bot(b["id"])
        a = sum(1 for t in ts if t.get("admin_user_id"))
        noadmin += len(ts) - a
        total += len(ts)
        lines.append(f"  • {bot_display_name(b)} — 📋 {len(ts)} (🟢 {a} / ⏳ {len(ts) - a})")

    text = (
        f"📊 <b>Сводка по ПЗ</b>\n\n"
        f"Всего ПЗ: <b>{total}</b>\n"
        f"🟢 С админом: <b>{total - noadmin}</b>\n"
        f"⏳ Без админа: <b>{noadmin}</b>\n\n"
        f"<b>По ботам:</b>\n" + "\n".join(lines)
    )
    return text, _stats_kb()


def _resolve_stats_owner(chat, fallback_user_id: int) -> int:
    """Владелец: в ЛС — сам юзер, в группе — владелец привязанного «чата админов»."""
    if chat and chat.type != ChatType.PRIVATE:
        own = get_owner_by_admin_chat(chat.id)
        if own:
            return own
    return fallback_user_id


@router.message(F.text.regexp(r"(?i)^\.\s*стата"))
async def cmd_simple_stats(message: Message) -> None:
    # Виден всем: в ЛС — сводка по своему аккаунту, в чате админов — по владельцу чата.
    owner_id = _resolve_stats_owner(message.chat, message.from_user.id)
    text, kb = _stats_payload(owner_id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "gstat_noadmin")
async def cb_gstat_noadmin(callback: CallbackQuery) -> None:
    chat = callback.message.chat if callback.message else None
    owner_id = _resolve_stats_owner(chat, callback.from_user.id)
    bots = get_user_bots(owner_id)

    links = []
    for b in bots:
        for t in get_all_topics_for_bot(b["id"]):
            if not t.get("admin_user_id"):
                cid, tid = t["group_chat_id"], t["topic_id"]
                if cid < 0 and str(cid).startswith("-100"):
                    chat_part = int(str(cid)[4:])
                else:
                    chat_part = int(cid)
                link = f"https://t.me/c/{chat_part}/{tid}"
                links.append(f"  • {bot_display_name(b)}: {link}")

    if not links:
        text = "🎉 Все ПЗ закрыты админами! Топиков без админа нет."
    else:
        text = f"⏳ <b>ПЗ без админа ({len(links)})</b>\n\n" + "\n".join(links)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="gstat_noadmin")],
        [InlineKeyboardButton(text="⬅️ К сводке", callback_data="gstat_back")],
    ])
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "gstat_back")
async def cb_gstat_back(callback: CallbackQuery) -> None:
    chat = callback.message.chat if callback.message else None
    owner_id = _resolve_stats_owner(chat, callback.from_user.id)
    text, kb = _stats_payload(owner_id)
    await render_callback(callback, text, kb)