from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from services.storage import get_user_bots, bot_display_name, get_all_stats
from services.child_manager import ChildManager

router = Router()


def select_all_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📨 Рассылка", callback_data="all_mailing")],
            [InlineKeyboardButton(text="📊 Общая статистика", callback_data="all_stats")],
            [InlineKeyboardButton(text="⛔ Остановить все", callback_data="all_stop")],
            [InlineKeyboardButton(text="▶️ Запустить все", callback_data="all_start_all")],
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


@router.callback_query(F.data == "all_stats")
async def cb_all_stats(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    bots = get_user_bots(user_id)

    if not bots:
        await callback.answer("⚠️ Нет ботов")
        return

    bot_ids = [b["id"] for b in bots]
    s = get_all_stats(bot_ids)

    text = (
        f"📊 <b>Общая статистика</b> ({len(bots)} ботов)\n\n"
        f"👥 Всего пользователей: <b>{s['users_total']}</b>\n"
        f"🚫 Заблокировали: <b>{s['users_blocked']}</b>\n"
        f"✅ Активных: <b>{s['users_active']}</b>\n\n"
        f"📩 Получено сообщений: <b>{s['messages_in']}</b>\n"
        f"📤 Отправлено сообщений: <b>{s['messages_out']}</b>\n\n"
        f"📨 Рассылок: <b>{s['mailings_count']}</b>\n"
        f"  ├ Доставлено: <b>{s['mailings_sent']}</b>\n"
        f"  └ Не доставлено: <b>{s['mailings_failed']}</b>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="all_stats")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="select_all")],
    ])

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "all_stop")
async def cb_all_stop(callback: CallbackQuery,
                      child_manager: ChildManager) -> None:
    user_id = callback.from_user.id
    bots = get_user_bots(user_id)

    stopped = 0
    for b in bots:
        if child_manager.is_running(b["id"]):
            await child_manager.stop_child(b["id"])
            stopped += 1

    await callback.answer(f"⛔ Остановлено: {stopped}")

    if callback.message:
        try:
            await callback.message.edit_text(
                f"⛔ <b>Все боты остановлены</b>\n\nОстановлено: {stopped}",
                reply_markup=select_all_kb()
            )
        except Exception:
            pass


@router.callback_query(F.data == "all_start_all")
async def cb_all_start(callback: CallbackQuery,
                       child_manager: ChildManager) -> None:
    user_id = callback.from_user.id
    bots = get_user_bots(user_id)

    started = 0
    for b in bots:
        if not child_manager.is_running(b["id"]):
            ok = await child_manager.start_child(b)
            if ok:
                started += 1

    await callback.answer(f"▶️ Запущено: {started}")

    if callback.message:
        try:
            await callback.message.edit_text(
                f"▶️ <b>Все боты запущены</b>\n\nЗапущено: {started}",
                reply_markup=select_all_kb()
            )
        except Exception:
            pass