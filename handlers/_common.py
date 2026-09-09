"""Общие вспомогательные утилиты для рендеринга сообщений в обработчиках."""

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

_logger = logging.getLogger(__name__)

# Приветствие, которое бот шлёт в привязанный «чат админов» (и при перезапуске /perezap).
ADMIN_CHAT_WELCOME = (
    "🛡 <b>Чат админов привязан!</b>\n\n"
    "👋 Приветствую тебя в чате админов YamoBot!\n\n"
    "🧭 <b>Команды для админов (работают в топиках):</b>\n"
    "• <code>/smena</code> — сменить админа у ПЗ без подтверждения.\n"
    "• <code>/otkaz</code> — отказаться от ПЗ / сбросить админа.\n"
    "• <code>/ban</code> — забанить пользователя.\n"
    "• <code>/unban</code> — разбанить пользователя.\n"
    "• <code>/.стата</code> — сводка по ПЗ всех ботов (в т.ч. в этом чате).\n"
    "• <code>/стата</code> или <code>/stata</code> — то же самое через слеш.\n\n"
    "Сюда будут приходить уведомления о новых ПЗ."
)


def _is_not_modified(err: Exception) -> bool:
    """True, если при редактировании Telegram вернул «message is not modified»."""
    msg = getattr(err, "message", "") or ""
    return "message is not modified" in msg.lower()


async def _retry_send(coro_factory, attempts: int = 6):
    """Выполняет корутину-фабрику с ретраями при flood-wait (TelegramRetryAfter).

    В группах с включённым «медленным режимом»/антиспамом Telegram на быстрые
    повторы команд отдаёт TelegramRetryAfter, и aiogram сам не повторяет запрос.
    Здесь ждём указанный `retry_after` и пробуем ещё раз, чтобы бот ВСЕГДА
    отвечал на команду (например, /стата), а не «молча пропадал».

    Если все попытки исчерпаны — пробрасываем последнее исключение, чтобы
    вызывающий код гарантированно ушёл в ветку «отправить текст ошибки» (а не
    вернул None и «промолчал»).
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            last_error = e
            delay = float(getattr(e, "retry_after", 1) or 1)
            # Не «засыпаем» надолго: каптируем до 30с за попытку, чтобы не блокировать
            # обработку остальных сообщений. При 6 попытках это максимум ~30–180с.
            delay = min(delay, 30.0)
            _logger.warning("Flood wait %.1fs (попытка %d/%d), жду", delay, attempt + 1, attempts)
            await asyncio.sleep(delay)
        except TelegramBadRequest as e:
            if _is_not_modified(e):
                return None
            raise
    if last_error is not None:
        raise last_error
    return None


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