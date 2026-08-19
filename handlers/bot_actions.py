from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from services.storage import (
    get_user_bots,
    get_bot_by_id,
    remove_user_bot,
    update_bot_field,
    bot_display_name,
)
from services.child_manager import ChildManager
from handlers.my_bots import my_bots_kb
from handlers.start import main_menu_kb

router = Router()


def single_bot_kb(bot_id: int, is_running: bool) -> InlineKeyboardMarkup:
    stop_text = "⛔ Остановить" if is_running else "▶️ Запустить"
    stop_data = f"action_stop_{bot_id}" if is_running else f"action_start_{bot_id}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📨 Рассылка", callback_data=f"mailing_{bot_id}")],
            [InlineKeyboardButton(text="🛡 Антиспам", callback_data=f"action_antispam_{bot_id}")],
            [InlineKeyboardButton(text=stop_text, callback_data=stop_data)],
            [InlineKeyboardButton(text="📊 Статистика", callback_data=f"action_stats_{bot_id}")],
            [InlineKeyboardButton(text="✏️ Редактор", callback_data=f"editor_{bot_id}")],
            [InlineKeyboardButton(text="🗑 Удалить бота", callback_data=f"action_delete_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ Назад к ботам", callback_data="my_bots")],
        ]
    )


@router.callback_query(F.data.startswith("bot_"))
async def cb_single_bot(callback: CallbackQuery,
                        child_manager: ChildManager) -> None:
    bot_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info is None:
        if callback.message:
            await callback.message.edit_text("⚠️ Бот не найден.", reply_markup=main_menu_kb())
        await callback.answer()
        return

    name = bot_display_name(bot_info)
    running = child_manager.is_running(bot_id)
    status = "🟢 Работает" if running else "🔴 Остановлен"

    welcome = bot_info.get("welcome_text", "") or "— не задано —"

    text = (
        f"🤖 <b>{name}</b>\n"
        f"🆔 <code>{bot_id}</code>\n"
        f"Статус: {status}\n\n"
        f"💬 Приветствие:\n{welcome}\n\n"
        f"Выбери действие:"
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=single_bot_kb(bot_id, running))
        except Exception:
            await callback.message.answer(text, reply_markup=single_bot_kb(bot_id, running))
    await callback.answer()


@router.callback_query(F.data.startswith("action_stop_"))
async def cb_stop_bot(callback: CallbackQuery,
                      child_manager: ChildManager) -> None:
    bot_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    await child_manager.stop_child(bot_id)
    update_bot_field(user_id, bot_id, "stopped", True)

    await callback.answer("⛔ Бот остановлен")

    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info and callback.message:
        name = bot_display_name(bot_info)
        text = f"🤖 <b>{name}</b>\n🆔 <code>{bot_id}</code>\nСтатус: 🔴 Остановлен\n\nВыбери действие:"
        try:
            await callback.message.edit_text(text, reply_markup=single_bot_kb(bot_id, False))
        except Exception:
            pass


@router.callback_query(F.data.startswith("action_start_"))
async def cb_start_bot(callback: CallbackQuery,
                       child_manager: ChildManager) -> None:
    bot_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    update_bot_field(user_id, bot_id, "stopped", False)
    started = await child_manager.start_child(bot_info)

    if started:
        await callback.answer("▶️ Бот запущен")
    else:
        await callback.answer("⚠️ Не удалось запустить")

    if callback.message:
        name = bot_display_name(bot_info)
        running = child_manager.is_running(bot_id)
        status = "🟢 Работает" if running else "🔴 Остановлен"
        text = f"🤖 <b>{name}</b>\n🆔 <code>{bot_id}</code>\nСтатус: {status}\n\nВыбери действие:"
        try:
            await callback.message.edit_text(text, reply_markup=single_bot_kb(bot_id, running))
        except Exception:
            pass


@router.callback_query(F.data.startswith("action_antispam_"))
async def cb_antispam(callback: CallbackQuery) -> None:
    bot_id = int(callback.data.split("_")[-1])
    if callback.message:
        try:
            await callback.message.edit_text(
                "🛡 <b>Антиспам</b>\n\nФункция в разработке.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"bot_{bot_id}")]
                ])
            )
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data.startswith("action_stats_"))
async def cb_stats(callback: CallbackQuery) -> None:
    bot_id = int(callback.data.split("_")[-1])
    if callback.message:
        try:
            await callback.message.edit_text(
                "📊 <b>Статистика</b>\n\nФункция в разработке.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"bot_{bot_id}")]
                ])
            )
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data.startswith("action_delete_"))
async def cb_delete_bot(callback: CallbackQuery,
                        child_manager: ChildManager) -> None:
    bot_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    await child_manager.stop_child(bot_id)
    removed = remove_user_bot(user_id, bot_id)

    if removed:
        text = "🗑 <b>Бот удалён.</b>"
    else:
        text = "⚠️ Бот не найден."

    bots = get_user_bots(user_id)
    if bots:
        text += f"\n\nОсталось ботов: {len(bots)}"
        kb = my_bots_kb(user_id, child_manager)
    else:
        text += "\n\nУ тебя больше нет подключённых ботов."
        kb = main_menu_kb()

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, reply_markup=kb)
    await callback.answer()