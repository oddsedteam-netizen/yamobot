from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import render_callback
from services.child_manager import ChildManager
from services.storage import (
    get_user_bots,
    get_user_bot_types,
    bot_display_name,
)

router = Router()

TYPE_LABELS = {
    "standard": "🗂 Стандарт",
    "anketa": "📝 Анкетница",
}


def _norm(bot_type: str | None) -> str:
    """Нормализует тип бота: пустое/None -> 'standard'."""
    return bot_type if bot_type in TYPE_LABELS else "standard"


def _bot_list_kb(bots: list[dict], child_manager: ChildManager,
                 bot_type: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for b in bots:
        running = child_manager.is_running(b["id"])
        status = "🟢" if running else "🔴"
        rows.append([InlineKeyboardButton(
            text=f"{status} {bot_display_name(b)}",
            callback_data=f"bot_{b['id']}",
        )])
    if bot_type:
        rows.append([InlineKeyboardButton(text="⬅️ Категории", callback_data="my_bots")])
    if bots:
        rows.append([InlineKeyboardButton(text="📌 Выбрать все", callback_data="select_all")])
    rows.append([InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _types_kb(bot_types: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for t in bot_types:
        rows.append([InlineKeyboardButton(
            text=TYPE_LABELS.get(t, t), callback_data=f"my_bots_type_{t}"
        )])
    rows.append([InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _filter_bots(bots: list[dict], bot_type: str) -> list[dict]:
    return [b for b in bots if _norm(b.get("bot_type")) == bot_type]


def my_bots_kb(user_id: int, child_manager: ChildManager) -> InlineKeyboardMarkup:
    all_types = get_user_bot_types(user_id)
    all_bots = get_user_bots(user_id)
    if not all_bots:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")]
        ])
    bot_type = _norm(all_types[0]) if all_types else "standard"
    type_bots = _filter_bots(all_bots, bot_type)
    return _bot_list_kb(type_bots, child_manager, bot_type)


def _bot_list_text(bots: list[dict], bot_type: str | None) -> str:
    seg = f" — {TYPE_LABELS.get(_norm(bot_type), _norm(bot_type))}" if bot_type else ""
    return (
        f"🤖 <b>Боты{seg}</b> ({len(bots)} шт.)\n\n"
        f"🟢 — работает  🔴 — остановлен\n\nВыбери бота:"
    )
async def show_my_bots(message: Message, child_manager: ChildManager) -> None:
    user_id = message.from_user.id
    all_bots = get_user_bots(user_id)

    if not all_bots:
        await message.answer(
            "🤖 <b>Боты</b>\n\nУ тебя пока нет подключённых ботов.\nНажми «➕ Добавить бота».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")]
            ]),
        )
        return

    bot_types = get_user_bot_types(user_id)
    if len(bot_types) > 1:
        label = " / ".join(TYPE_LABELS.get(t, t) for t in bot_types)
        await message.answer(
            f"🤖 <b>Боты</b>\n\nУ тебя несколько категорий ботов: <b>{label}</b>\n\nКакую категорию открываем?",
            reply_markup=_types_kb(bot_types),
        )
        return

    bot_type = _norm(bot_types[0]) if bot_types else "standard"
    bots = _filter_bots(all_bots, bot_type)
    await message.answer(_bot_list_text(bots, bot_type),
                         reply_markup=_bot_list_kb(bots, child_manager, bot_type))


@router.callback_query(F.data == "my_bots")
async def cb_my_bots(callback: CallbackQuery, state: FSMContext,
                     child_manager: ChildManager) -> None:
    await state.clear()
    user_id = callback.from_user.id
    all_bots = get_user_bots(user_id)

    if not all_bots:
        await render_callback(callback, "🤖 <b>Боты</b>\n\nУ тебя пока нет подключённых ботов.",
                              InlineKeyboardMarkup(inline_keyboard=[
                                  [InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")]
                              ]))
        return

    bot_types = get_user_bot_types(user_id)
    if len(bot_types) > 1:
        label = " / ".join(TYPE_LABELS.get(t, t) for t in bot_types)
        await render_callback(
            callback,
            f"🤖 <b>Боты</b>\n\nУ тебя несколько категорий ботов: <b>{label}</b>\n\nКакую категорию открываем?",
            _types_kb(bot_types),
        )
        return

    bot_type = _norm(bot_types[0]) if bot_types else "standard"
    bots = _filter_bots(all_bots, bot_type)
    await render_callback(callback, _bot_list_text(bots, bot_type),
                          _bot_list_kb(bots, child_manager, bot_type))


@router.callback_query(F.data.startswith("my_bots_type_"))
async def cb_my_bots_type(callback: CallbackQuery, state: FSMContext,
                          child_manager: ChildManager) -> None:
    await state.clear()
    bot_type = _norm(callback.data.split("_")[-1])
    user_id = callback.from_user.id
    bots = _filter_bots(get_user_bots(user_id), bot_type)
    if not bots:
        await callback.answer("В этой категории пока нет ботов")
        return
    await render_callback(callback, _bot_list_text(bots, bot_type),
                          _bot_list_kb(bots, child_manager, bot_type))