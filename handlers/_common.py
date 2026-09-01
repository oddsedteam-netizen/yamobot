"""Общие вспомогательные утилиты для рендеринга сообщений в обработчиках."""

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


async def edit_or_answer(target, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Пытается отредактировать существующее сообщение, иначе отправляет новое."""
    if target is None:
        return
    try:
        await target.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await target.answer(text, reply_markup=reply_markup)


async def render_callback(callback: CallbackQuery, text: str,
                          reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение колбэка (с фолбэком на новое) и гасит спиннер."""
    await edit_or_answer(callback.message, text, reply_markup)
    await callback.answer()


async def safe_edit(target: Message | None, text: str,
                    reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Только редактирует сообщение, молча игнорируя ошибки (сообщение изменили/удалили)."""
    if target:
        try:
            await target.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass