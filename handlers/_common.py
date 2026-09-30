"""Общие вспомогательные утилиты для рендеринга сообщений в обработчиках."""

import asyncio
import logging
import re
from html import escape as _escape
from typing import Any, cast

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    MaybeInaccessibleMessage,
)

logger = logging.getLogger(__name__)

# Реэкспорт для хендлеров: экранировать чужой текст удобнее из общего модуля,
# чем тащить `from html import escape` в каждый файл. `__all__` нужен, чтобы
# линтер не считал импорт неиспользованным.
__all__ = ["html_escape"]


def html_escape(value: Any) -> str:
    """Экранирует текст для HTML-разметки Telegram.

    Отличие от ``html.escape`` только одно, но важное: здесь можно передать
    ``None``. Поля в базе необязательные (имя, username, название чата), а
    ``html.escape(None)`` падает с ``AttributeError`` — экран не показывался
    бы из-за пустого поля.
    """
    if value is None:
        return ""
    return _escape(str(value))


# ═══════════════ Нормализация ссылок ═══════════════════════════════════
# Единая точка «приведения ссылки к рабочему виду»: используется и в редакторе
# кнопок дочерних ботов, и в настройках ссылок админ-панели. Пользователю
# достаточно отправить @username или короткую ссылку — формат поправим сами.

# Домен без схемы: example.com, t.me/x, sub.example.co.uk/page?x=1
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(/[^\s]*)?$")
_SHORT_HOSTS = ("t.me/", "telegram.me/", "telegram.dog/")


def normalize_link(raw: str) -> str:
    """Приводит ссылку к виду, который принимает Telegram в инлайн-кнопке.

    Понимает:
      • полные ссылки (``http://``, ``https://``, ``tg://``) — оставляет как есть;
      • короткие (``t.me/…``, ``telegram.me/…``) — достраивает ``https://``;
      • ``@username`` — превращает в ``https://t.me/username``;
      • домен без схемы (``example.com/page``) — достраивает ``https://``.

    Возвращает пустую строку, если на ссылку это не похоже.
    """
    link = (raw or "").strip().strip("<>").strip()
    if not link:
        return ""
    # Если скопировали «ссылку + текст» — берём только первый фрагмент.
    link = link.split()[0]
    if link.startswith("@"):
        link = "https://t.me/" + link[1:]
    if link.startswith(_SHORT_HOSTS):
        link = "https://" + link
    if link.startswith(("http://", "https://", "tg://")):
        return link
    if _DOMAIN_RE.match(link):
        return "https://" + link
    return ""

# Приветствие, которое бот шлёт в привязанный «чат админов» (и при перезапуске).
ADMIN_CHAT_WELCOME = (
    "🛡 <b>Чат админов привязан!</b>\n\n"
    "⚠️ <b>Боту нужны права администратора</b> в этом чате — "
    "выдай их через «Управление чатом → Администраторы → YamoBot → "
    "Назначить администратором», иначе уведомления могут не доходить.\n\n"
    "👋 Приветствую тебя в чате админов YamoBot!\n\n"
    "🧭 <b>Команды в этом чате:</b>\n"
    "• <code>/стата</code> или <code>/.стата</code> — сводка по ПЗ всех ботов.\n"
    "• <code>/стата неделя|день|месяц</code> — статистика админов за период.\n"
    "• <code>/perezap</code> — перезапуск бота с перепривязкой чата "
    "(только владелец).\n"
    "• <code>/perestart</code> — простой перезапуск без изменения привязки.\n\n"
    "🧭 <b>Команды для админов (работают в топиках):</b>\n"
    "• <code>/otkaz</code> — отказаться от ПЗ / сбросить админа.\n"
    "• <code>/ban</code> — забанить пользователя.\n"
    "• <code>/unban</code> — разбанить пользователя.\n"
    "Сюда будут приходить уведомления о новых ПЗ."
)


def _is_not_modified(err: Exception) -> bool:
    """True, если при редактировании Telegram вернул «message is not modified»."""
    msg = getattr(err, "message", "") or ""
    return "message is not modified" in msg.lower()


# Признаки того, что Telegram отказал именно из-за СОДЕРЖИМОГО сообщения
# (а не потому, что сообщение нельзя править). Такой текст упадёт одинаково
# и при edit, и при send — значит, повторять его как есть бессмысленно.
_CONTENT_ERROR_MARKERS = (
    "can't parse entities",
    "can't parse message text",
    "unsupported start tag",
    "can't find end tag",
    "can't find end of the entity",
    "entity is not allowed",
    "message is too long",
    "caption is too long",
    "message text is empty",
)

# Лимит Telegram на длину текста сообщения.
_TEXT_LIMIT = 4096


def _is_content_error(err: Exception) -> bool:
    """True, если отказ Telegram вызван разметкой или длиной текста."""
    msg = (getattr(err, "message", "") or str(err)).lower()
    return any(marker in msg for marker in _CONTENT_ERROR_MARKERS)


def _plain_text(text: str) -> str:
    """Приводит текст к виду, который Telegram примет без разметки.

    Обрезаем по лимиту: иначе «message is too long» повторится и в простом
    режиме, и мы вернёмся к тому же отказу.
    """
    if len(text) <= _TEXT_LIMIT:
        return text
    return text[:_TEXT_LIMIT - 1] + "…"


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
            logger.warning("Flood wait %.1fs (попытка %d/%d), жду", delay, attempt + 1, attempts)
            await asyncio.sleep(delay)
        except TelegramBadRequest as e:
            if _is_not_modified(e):
                return None
            raise
    if last_error is not None:
        raise last_error
    return None


async def _show_screen(target: MaybeInaccessibleMessage | None, text: str,
                        reply_markup: InlineKeyboardMarkup | None = None,
                        force_answer: bool = False) -> str:
    """Показывает экран: правит сообщение или отправляет новое. Без исключений.

    Возвращает результат для вызывающего кода:

    * ``shown``       — экран показан (правкой или новым сообщением);
    * ``shown_plain`` — показан без разметки: Telegram отказал в разметке/длине;
    * ``failed``      — показать не удалось вовсе.

    Почему так, а не «просто edit_text»
    ----------------------------------
    Раньше при ошибке правки шёл фолбэк на ``answer()`` с ТЕМ ЖЕ текстом.
    Если проблема была в сообщении (его удалили) — это лечило. А если в
    СОДЕРЖИМОМ (кривая разметка или слишком длинный текст) — повтор падал
    точно так же, и исключение вылетало наружу: кнопка «умирала» без
    объяснений, а в журнал уходил трейсбек.

    Здесь три разных причины лечатся по отдельности:

    1. сообщение нельзя править → отправляем новое;
    2. текст/разметка не приняты → повторяем ПРОСТЫМ текстом (с обрезкой);
    3. ничего не вышло → просто возвращаем ``failed``, наружу не бросаем.

    Ни один шаг не пробрасывает исключение: худший исход — админ увидит
    «не удалось обновить экран» (это делает ``render_callback``).
    """
    if target is None:
        return "failed"

    edit = getattr(target, "edit_text", None)

    def _edit_kwargs(body: str, plain: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"reply_markup": reply_markup}
        if plain:
            # parse_mode=None — Telegram не разбирает разметку вовсе.
            kwargs["parse_mode"] = None
        return kwargs

    if edit is not None:
        try:
            await edit(text, reply_markup=reply_markup)
            return "shown"
        except TelegramBadRequest as e:
            if _is_not_modified(e) and not force_answer:
                # Экран уже ровно такой — это не ошибка, ничего не делаем.
                return "shown"
            if _is_content_error(e):
                logger.warning(
                    "Не удалось показать экран с разметкой (%s) — повторяю простым текстом",
                    e,
                )
                try:
                    await edit(_plain_text(text), **_edit_kwargs(text, plain=True))
                    return "shown_plain"
                except Exception as plain_error:
                    logger.debug("Повтор простым текстом не удался: %s", plain_error)
            # Прочие причины: сообщение удалили, оно слишком старое и т.п.
        except Exception:
            logger.debug("Исключение при правке сообщения", exc_info=True)

    answer = getattr(target, "answer", None)
    if answer is None:
        return "failed"

    # Отправка нового сообщения — ТОЖЕ внутри try: раньше исключение отсюда
    # вылетало в хендлер и роняло кнопку.
    try:
        await answer(text, reply_markup=reply_markup)
        return "shown"
    except TelegramBadRequest as e:
        if _is_content_error(e):
            logger.warning(
                "Новое сообщение с разметкой не прошло (%s) — повторяю простым текстом",
                e,
            )
            try:
                await answer(_plain_text(text), reply_markup=reply_markup, parse_mode=None)
                return "shown_plain"
            except Exception as plain_error:
                logger.warning("Экран не удалось показать даже простым текстом: %s",
                               plain_error)
                return "failed"
        logger.warning("Не удалось отправить экран: %s", e)
        return "failed"
    except Exception as e:
        logger.warning("Не удалось отправить экран: %s", e)
        return "failed"


async def edit_or_answer(target: MaybeInaccessibleMessage | None, text: str,
                          reply_markup: InlineKeyboardMarkup | None = None,
                          force_answer: bool = False) -> None:
    """Правит сообщение, иначе отправляет новое (см. ``_show_screen``).

    Ошибка «message is not modified» молча игнорируется, чтобы повторное
    нажатие «Обновить» не присылало дублирующее сообщение.

    Если ``force_answer=True`` (навигация в FAQ), то даже при «message is not
    modified» отправляем новое сообщение, чтобы кнопка ВСЕГДА давала видимый
    результат.
    """
    await _show_screen(target, text, reply_markup, force_answer=force_answer)


async def render_callback(callback: CallbackQuery, text: str,
                          reply_markup: InlineKeyboardMarkup | None = None,
                          force_answer: bool = False) -> None:
    """Редактирует сообщение колбэка (с фолбэком на новое) и гасит спиннер.

    Ответ на колбэк может не пройти: если обработчик работал долго (например,
    проверял 120 ботов по сети), Telegram к этому моменту уже считает query
    «протухшим» и отвечает «query is too old …». Это не поломка — просто
    часики на кнопке не закрылись, поэтому такую ошибку молча гасим.

    Перед ответом на колбэк проверяем, что экран действительно показан. Если
    Telegram отказал совсем — сообщаем админу всплывашкой: иначе кнопка
    выглядит «мёртвой» и непонятно, что пошло не так.
    """
    result = await _show_screen(callback.message, text, reply_markup,
                                force_answer=force_answer)

    if result == "failed":
        # Единственный случай, когда админа нужно звать на помощь.
        try:
            await callback.answer("⚠️ Не удалось обновить экран. Попробуйте ещё раз.",
                                  show_alert=True)
        except Exception as e:
            logger.debug("Не удалось показать уведомление об ошибке: %s", e)
        return

    try:
        await callback.answer()
    except TelegramBadRequest as e:
        if "query is too old" in str(e) or "query ID is invalid" in str(e):
            logger.debug("Колбэк протух (экран уже показан): %s", e)
        else:
            logger.debug("Не удалось ответить на колбэк: %s", e)
    except Exception as e:
        logger.debug("Не удалось ответить на колбэк: %s", e)


async def safe_edit(target: MaybeInaccessibleMessage | None, text: str,
                    reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Только редактирует сообщение, молча игнорируя ошибки (сообщение изменили/удалили)."""
    if target:
        edit = getattr(target, "edit_text", None)
        if edit is not None:
            try:
                await edit(text, reply_markup=reply_markup)
            except Exception:
                logger.debug(
                    "Исключение проглочено",
                    exc_info=True,
                )


# ═══════════════ Доступ к полям aiogram ═══════════════════════════════
# Стубы типов aiogram помечают некоторые поля как Optional (callback.data,
# from_user у message/callback), хотя в обработанных фильтрами апдейтах они
# гарантированно присутствуют. Хелперы ниже сужают тип без «тихания» ошибок
# Pyright/Pylance и без лишних проверок на каждом вызове.

def cb_data(callback: CallbackQuery) -> str:
    """data колбэка (в зарегистрированных фильтрах всегда непустая строка)."""
    return callback.data or ""


def cb_uid(callback: CallbackQuery) -> int:
    """from_user.id колбэка (в ЛС всегда есть отправитель)."""
    user = callback.from_user
    return user.id if user else 0


def msg_uid(message: Message) -> int:
    """from_user.id сообщения (в ЛС/группах всегда есть отправитель)."""
    user = message.from_user
    return user.id if user else 0


def msg_username(message: Message) -> str:
    """username отправителя сообщения (или пустая строка)."""
    user = message.from_user
    return (user.username or "") if user else ""


def msg_firstname(message: Message) -> str:
    """first_name отправителя сообщения (или пустая строка)."""
    user = message.from_user
    return (user.first_name or "") if user else ""


def cb_username(callback: CallbackQuery) -> str:
    """username отправителя колбэка (или пустая строка)."""
    user = callback.from_user
    return (user.username or "") if user else ""


def cb_firstname(callback: CallbackQuery) -> str:
    """first_name отправителя колбэка (или пустая строка)."""
    user = callback.from_user
    return (user.first_name or "") if user else ""


def event_bot(obj: Any) -> Bot:
    """Бот апдейта: CallbackQuery / Message / ChatMemberUpdated.

    aiogram объявляет поле ``bot`` как Optional, хотя в уже обработанных
    апдейтах бот есть всегда — сужаем тип так же, как в хелперах выше
    (без «тихания» ошибок Pyright/Pylance).
    """
    return cast(Bot, getattr(obj, "bot", None))


async def try_edit(target: MaybeInaccessibleMessage | None, text: str,
                   reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Вызывает edit_text, молча игнорируя ошибки и отсутствие метода
    (безопасно для InaccessibleMessage и None)."""
    if target is None:
        return
    edit = getattr(target, "edit_text", None)
    if edit is not None:
        try:
            await edit(text, reply_markup=reply_markup)
        except Exception:
            logger.debug(
                "Исключение проглочено",
                exc_info=True,
            )


async def try_edit_answer(target: MaybeInaccessibleMessage | None, text: str,
                          reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Правит сообщение, иначе отправляет новое — без исключений наружу.

    Тонкая обёртка над ``_show_screen``: у неё тот же набор «лечений»
    (новое сообщение вместо правки, повтор простым текстом, обрезка длины).
    Раньше здесь был свой код, который при ошибке разметки повторял ТОТ ЖЕ
    текст и падал повторно — исключение вылетало в хендлер.
    """
    await _show_screen(target, text, reply_markup)
