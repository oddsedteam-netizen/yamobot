"""Устойчивый polling для aiogram: понятные ошибки вместо бесконечных ретраев.

Штатный ``Dispatcher.start_polling`` на ЛЮБУЮ ошибку ``getUpdates`` просто спит и
повторяет запрос. Из-за этого в логах накапливались тысячи строк вида
«Sleep for 4.9 seconds and try again... (tryings = 14000, bot id = 8616871006)»,
хотя повторять было бессмысленно:

* ``TelegramConflictError`` — у токена стоит вебхук или апдейты бота «слушает»
  второй экземпляр (копия бота на другом сервере, другой конструктор ботов).
  Здесь мы сбрасываем вебхук, повторяем с растущей паузой и пишем в лог ОДНУ
  строку вместо тысяч: сообщения при этом не теряются.
* ``TelegramUnauthorizedError`` — токен отозван/недействителен. Повторять
  бессмысленно: останавливаем polling этого бота и один раз уведомляем владельца.

Остальные ошибки (сеть, серверы Telegram, flood control) ретраятся с backoff —
как в самом aiogram, но с редкими строками в логе.
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetUpdates
from aiogram.types import Update
from aiogram.utils.backoff import Backoff, BackoffConfig

logger = logging.getLogger(__name__)

# Значения по умолчанию те же, что у aiogram: 1 → 5 секунд с фактором 1.3.
DEFAULT_BACKOFF_CONFIG = BackoffConfig(min_delay=1.0, max_delay=5.0, factor=1.3, jitter=0.1)

# Конфликт токена: первая пауза, максимум паузы и как часто заново сбрасывать вебхук.
CONFLICT_START_DELAY = 5.0
CONFLICT_MAX_DELAY = 60.0
WEBHOOK_FIX_INTERVAL = 120.0

# Сколько неудачных попыток подряд было у бота (ключ — id бота в Telegram).
# Нужно только для того, чтобы не заливать лог одной и той же строкой.
_attempts: dict[int, int] = {}
# Когда последний раз пытались сбросить вебхук при конфликте (monotonic).
_webhook_fixed_at: dict[int, float] = {}
# О мёртвом токене сообщаем один раз за запуск процесса.
_unauthorized_seen: set[int] = set()

# Обработчик проблем polling: (бот, вид проблемы) — «conflict» или «unauthorized».
PollingProblemHook = Callable[[Bot, str], Awaitable[None]]
_problem_hook: PollingProblemHook | None = None


def set_polling_problem_hook(hook: PollingProblemHook | None) -> None:
    """Задаёт обработчик проблем polling (например, уведомление владельца бота).

    Хук нужен, чтобы модуль не зависел от хранилища и «чата админов»: сама
    логика уведомления живёт в ``services.child_manager``.
    """
    global _problem_hook
    _problem_hook = hook


async def _report(bot: Bot, kind: str) -> None:
    """Сообщает наружу о проблеме polling (ошибка обработчика polling не ломает)."""
    hook = _problem_hook
    if hook is None:
        return
    try:
        await hook(bot, kind)
    except Exception as e:
        logger.debug("Обработчик проблем polling упал: %s", e)


def _short(err: Exception) -> str:
    """Короткое описание ошибки Telegram для лога."""
    text = str(getattr(err, "message", "") or err).strip().replace("\n", " ")
    return text if len(text) <= 160 else text[:157] + "..."


async def _note_attempt(bot: Bot, reason: str) -> int:
    """Считает неудачную попытку и пишет её в лог (редко, без спама).

    Первые три строки и каждая сотая — уровнем WARNING, остальные — DEBUG:
    так в логе видно, что бот не «залип» молча, но нет тысяч повторений.
    """
    count = _attempts.get(bot.id, 0) + 1
    _attempts[bot.id] = count
    if count <= 3:
        logger.warning("Бот %s: %s — попытка %d, повторяю с паузой", bot.id, reason, count)
    elif count % 100 == 0:
        logger.warning("Бот %s: %s — попытка %d, всё ещё не удаётся", bot.id, reason, count)
    else:
        logger.debug("Бот %s: %s — попытка %d", bot.id, reason, count)
    return count


async def _handle_conflict(bot: Bot, err: TelegramConflictError) -> None:
    """Конфликт polling: сбрасывает вебхук и (редко) уведомляет наружу.

    Причина конфликта — либо оставшийся вебхук на токене, либо второй экземпляр
    бота, который тоже вызывает ``getUpdates``. ``delete_webhook`` вызывается БЕЗ
    ``drop_pending_updates``: иначе мы удалили бы сообщения пользователей, которые
    ещё ждут обработки.
    """
    count = await _note_attempt(bot, f"конфликт polling ({_short(err)})")
    if count <= 2:
        logger.warning(
            "Бот %s: апдейты забирает вебхук или второй экземпляр бота. "
            "Сброшу вебхук и буду ждать: сообщения не теряются, но пока часть "
            "их может уходить «на ту сторону».", bot.id,
        )

    now = time.monotonic()
    if now - _webhook_fixed_at.get(bot.id, 0.0) >= WEBHOOK_FIX_INTERVAL:
        _webhook_fixed_at[bot.id] = now
        try:
            await bot.delete_webhook()
        except Exception as e:
            logger.debug("Бот %s: не удалось сбросить вебхук: %s", bot.id, e)

    # Владельцу пишем один раз за серию попыток (первая и десятая).
    if count in (1, 10):
        await _report(bot, "conflict")


async def _handle_unauthorized(bot: Bot, err: TelegramUnauthorizedError) -> None:
    """Мёртвый токен: пишем в лог один раз и останавливаем polling этого бота."""
    if bot.id in _unauthorized_seen:
        logger.debug("Бот %s: токен по-прежнему недействителен", bot.id)
        return
    _unauthorized_seen.add(bot.id)
    logger.error(
        "Бот %s: Telegram ответил «Unauthorized» — токен отозван или недействителен. "
        "Polling остановлен: повторять бессмысленно, нужен рабочий токен "
        "(проверь бота в @BotFather).", bot.id,
    )
    await _report(bot, "unauthorized")


class ResilientDispatcher(Dispatcher):
    """Dispatcher, который не «залипает» на конфликте вебхука и мёртвом токене.

    Поведение отличается от штатного только обработкой ошибок ``getUpdates``
    (см. описание модуля). Всё остальное — запуск, роутеры, middleware — как в
    обычном ``Dispatcher``, поэтому класс можно использовать и для основного бота,
    и для дочерних.
    """

    @classmethod
    async def _listen_updates(
        cls,
        bot: Bot,
        polling_timeout: int = 30,
        backoff_config: BackoffConfig = DEFAULT_BACKOFF_CONFIG,
        allowed_updates: list[str] | None = None,
    ) -> AsyncGenerator[Update, None]:
        backoff = Backoff(config=backoff_config)
        get_updates = GetUpdates(timeout=polling_timeout, allowed_updates=allowed_updates)
        kwargs: dict[str, Any] = {}
        if bot.session.timeout:
            # Таймаут запроса должен быть больше long-polling, иначе будут
            # ложные TimeoutError.
            kwargs["request_timeout"] = int(bot.session.timeout + polling_timeout)

        failed = False
        conflict_delay = CONFLICT_START_DELAY

        while True:
            try:
                updates = await bot(get_updates, **kwargs)
            except TelegramUnauthorizedError as e:
                # Мёртвый токен: polling этого бота завершается без исключения —
                # задача не падает, поэтому автоперезапуск не запускается.
                await _handle_unauthorized(bot, e)
                return
            except TelegramConflictError as e:
                failed = True
                await _handle_conflict(bot, e)
                await asyncio.sleep(conflict_delay)
                conflict_delay = min(conflict_delay * 2, CONFLICT_MAX_DELAY)
                continue
            except TelegramRetryAfter as e:
                failed = True
                delay = float(getattr(e, "retry_after", 1) or 1)
                logger.warning(
                    "Бот %s: flood control при получении апдейтов — жду %.0fс",
                    bot.id, delay,
                )
                await asyncio.sleep(min(delay, CONFLICT_MAX_DELAY) + 1.0)
                continue
            except (TelegramServerError, TelegramNetworkError) as e:
                failed = True
                await _note_attempt(bot, f"сбой связи с Telegram ({type(e).__name__})")
                await backoff.asleep()
                continue
            except Exception as e:  # noqa: BLE001 — polling не должен падать
                failed = True
                await _note_attempt(bot, f"ошибка получения апдейтов ({type(e).__name__})")
                await backoff.asleep()
                continue

            if failed:
                count = _attempts.pop(bot.id, 0)
                logger.info(
                    "Бот %s: связь с Telegram восстановлена (неудачных попыток: %d)",
                    bot.id, count,
                )
                backoff.reset()
                conflict_delay = CONFLICT_START_DELAY
                _webhook_fixed_at.pop(bot.id, None)
                failed = False

            for update in updates:
                yield update
                # Подтверждаем апдейт: всё, что меньше offset, Telegram больше не отдаст.
                get_updates.offset = update.update_id + 1