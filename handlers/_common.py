"""Общие вспомогательные утилиты для рендеринга сообщений в обработчиках."""

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


def _is_not_modified(err: Exception) -> bool:
    """True, если при редактировании Telegram вернул «message is not modified»."""
    msg = getattr(err, "message", "") or ""
    return "message is not modified" in msg.lower()


async def edit_or_answer(target, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Пытается отредактировать существующее сообщение, иначе отправляет новое.

    Ошибка «message is not modified» (контент не изменился) молча игнорируется,
    чтобы повторное нажатие «Обновить» не присылало дублирующее сообщение со статистикой.
    """
    if target is None:
        return
    try:
        await target.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if _is_not_modified(e):
            return
        await target.answer(text, reply_markup=reply_markup)
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