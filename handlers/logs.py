"""Раздел «📋 Логи».

Пользователь может попросить прислать ему логи переписки с ботами: бот
спрашивает подтверждение и отправляет логи владельцу платформы в личку —
в цитировании (ответом на сообщение) и свёрнутом виде. Отправка не чаще
раза в 5 минут, чтобы не заваливать техподдержку.

В админ-панели есть свой вход: посмотреть логи конкретного бота.
"""

import logging
import time
from html import escape

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from handlers._common import cb_data, cb_uid, event_bot, render_callback, try_edit
from services.config import is_super_admin
from services.storage import (
    bot_display_name,
    format_bot_errors,
    get_all_bots_flat,
    get_bot_errors,
    get_user_bots,
)

logger = logging.getLogger(__name__)

router = Router()

# Не чаще раза в 5 минут на пользователя.
LOGS_COOLDOWN = 300.0
_last_sent: dict[int, float] = {}


def _html(text: str) -> str:
    return escape(text or "", quote=False)


def _logs_block(text: str) -> str:
    """Свёрнутый блок с логами (цитата, которую можно развернуть)."""
    return f"<blockquote>{_html(text)}</blockquote>"


def logs_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои логи", callback_data="logs_send",
                              style="primary")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="other", style="primary")],
    ])


@router.callback_query(F.data == "logs_send")
async def cb_logs_ask(callback: CallbackQuery) -> None:
    """Предлагаем отправить логи ошибок по конкретному боту."""
    user_id = cb_uid(callback)
    left = int(LOGS_COOLDOWN - (time.time() - _last_sent.get(user_id, 0.0)))

    if left > 0:
        minutes = (left + 59) // 60
        await callback.answer(f"⏳ Логи можно отправить через {minutes} мин", show_alert=True)
        return

    bots = get_user_bots(user_id)
    if not bots:
        await render_callback(
            callback,
            "📋 <b>Логи</b>\n\n"
            "Логи — это ошибки, которые происходили при работе твоих ботов.\n\n"
            "У тебя пока нет ботов — логи смотреть нечего.",
            logs_menu_kb(),
        )
        return

    rows = [
        [InlineKeyboardButton(text=f"🤖 {bot_display_name(b)[:20]}",
                              callback_data=f"logs_bot_{b['id']}", style="primary")]
        for b in bots[:10]
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="other",
                                      style="primary")])

    await render_callback(
        callback,
        "📋 <b>Отправить логи?</b>\n\n"
        "Это ошибки, которые происходили при работе ботов: по ним видно, что "
        "именно сломалось. Логи уйдут владельцу платформы в личку — "
        "в цитировании и свёрнутым блоком.\n\n"
        "Чьи боты смотрим? Отправлять можно не чаще раза в 5 минут.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^logs_bot_\d+$"))
async def cb_logs_bot(callback: CallbackQuery) -> None:
    """Отправляет владельцу ошибки выбранного бота."""
    user_id = cb_uid(callback)
    if time.time() - _last_sent.get(user_id, 0.0) < LOGS_COOLDOWN:
        await callback.answer("⏳ Слишком часто, попробуй позже", show_alert=True)
        return

    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    bot = next((b for b in get_user_bots(user_id) if int(b["id"]) == bot_id), None)
    if not bot:
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    owner_id = user_id if is_super_admin(user_id) else _platform_owner()
    if not owner_id:
        await callback.answer("⚠️ Не нашёл, кому отправить", show_alert=True)
        return

    errors = get_bot_errors(bot_id)
    text = (
        f"🆘 <b>Логи бота {bot_display_name(bot)}</b>\n\n"
        f"👤 Отправитель: {callback.from_user.first_name or '—'}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"🤖 <code>{bot_id}</code>\n"
        f"⚠️ Ошибок в журнале: <b>{len(errors)}</b>\n\n"
        f"<blockquote>{_html(format_bot_errors(errors))}</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Открыть тикет",
                              callback_data=f"tickets_from_user_{user_id}",
                              style="success")],
    ])

    try:
        await event_bot(callback).send_message(owner_id, text, reply_markup=kb)
    except Exception as e:
        logger.warning("Не удалось отправить логи владельцу: %s", e)
        await callback.answer("⚠️ Не удалось отправить", show_alert=True)
        return

    _last_sent[user_id] = time.time()
    await callback.answer("✅ Отправил")
    if callback.message:
        await try_edit(
            callback.message,
            "✅ <b>Логи отправлены.</b>\n\nСледующий раз — через 5 минут."
        )


@router.callback_query(F.data == "logs_no")
async def cb_logs_no(callback: CallbackQuery) -> None:
    await callback.answer("👌 Не отправляю")
    if callback.message:
        await try_edit(callback.message, "👌 <b>Логи не отправлены.</b>")


def _platform_owner() -> int | None:
    """ID владельца платформы (того, кому уходят логи и тикеты)."""
    for row in get_all_bots_flat():
        if is_super_admin(int(row.get("owner_id") or 0)):
            return int(row["owner_id"])
    return None


# ═══════════════ Админ-панель: логи конкретного бота ═══════════════

def bot_logs_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🤖 {bot_display_name(b)[:18]}",
                              callback_data=f"botlogs_{b['id']}", style="primary")]
        for b in get_all_bots_flat()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Админ-панель",
                                      callback_data="profile_admin", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "botlogs")
async def cb_botlogs_list(callback: CallbackQuery) -> None:
    """Список ботов для просмотра логов."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    bots = get_all_bots_flat()
    kb = bot_logs_kb() if bots else InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="profile_admin")]
    ])
    await render_callback(callback, "🗂 <b>Логи ботов</b>\n\nВыбери бота:", kb)


@router.callback_query(F.data.regexp(r"^botlogs_\d+$"))
async def cb_botlogs_show(callback: CallbackQuery) -> None:
    """Показывает последние сообщения выбранного бота."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    bot = next((b for b in get_all_bots_flat() if int(b["id"]) == bot_id), None)
    if not bot:
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    errors = get_bot_errors(bot_id)
    await render_callback(
        callback,
        f"🗂 <b>{bot_display_name(bot)}</b>\n🆔 <code>{bot_id}</code>\n\n"
        f"⚠️ Ошибок в журнале: <b>{len(errors)}</b>\n\n"
        f"{_logs_block(format_bot_errors(errors))}",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"botlogs_{bot_id}",
                                  style="primary")],
            [InlineKeyboardButton(text="⬅️ К списку ботов", callback_data="botlogs",
                                  style="primary")],
        ]),
    )

