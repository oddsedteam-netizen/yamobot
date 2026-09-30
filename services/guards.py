"""Защита фоновых задач: задача не должна молча умирать.

Раньше фоновые циклы (напоминалки, публикация постов, автоперезапуск ботов)
были обычными ``asyncio.create_task``. Любое непойманное исключение внутри
цикла завершало задачу НАВСЕГДА: и напоминалки переставали работать, и
владелец об этом не узнавал — бот выглядел исправным, а функция отключена.

``supervised_task`` решает это: при исключении задача пишет подробную запись
в журнал (в том числе в журнал конкретного бота) и перезапускается с
нарастающей паузой. Пока цикл жив — сервис работает.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Пауза между перезапусками упавшей задачи: 5с → 10с → 20с → … и далее 60с.
RESTART_BASE_DELAY = 5.0
RESTART_MAX_DELAY = 60.0

# Сколько раз подряд перезапускаем задачу, прежде чем признать проблему
# серьёзной и поднять уровень лога до CRITICAL.
LOUD_AFTER = 5


# Ссылки на запущенные фоновые задачи. Нужны, чтобы работа не потерялась,
# даже если вызывающий код не сохранил задачу у себя: без сильной ссылки
# сборщик мусора может забрать задачу до её завершения. Завершившиеся задачи
# из списка убираются — иначе он рос бы с каждым перезапуском сервиса.
_TRACKED: list[asyncio.Task] = []


def _track(task: asyncio.Task) -> asyncio.Task:
    """Ставит задачу на учёт и убирает её из списка, когда она завершится."""
    _TRACKED.append(task)
    task.add_done_callback(lambda t: _TRACKED.remove(t) if t in _TRACKED else None)
    return task


def supervised_task(coro_factory: Callable[[], Awaitable[Any]],
                    name: str,
                    bot_id: int = 0,
                    log: logging.Logger | Any | None = None) -> asyncio.Task:
    """Запускает бесконечный цикл под присмотром.

    ``coro_factory`` — **функция без аргументов**, возвращающая корутину
    (например ``lambda: service._run()``). Фабрика вызывается заново при
    каждом перезапуске, поэтому нельзя передавать уже созданную корутину:
    создать её можно лишь один раз, а нам нужно много раз.

    Пример::

        task = supervised_task(lambda: reminder_service._run(), "reminders")
    """
    return _track(
        asyncio.create_task(_supervise(coro_factory, name, bot_id, log), name=name)
    )


def fire_and_forget(coro: Awaitable[Any], name: str = "background") -> asyncio.Task:
    """Запускает короткую операцию, не дожидаясь её результата.

    Обычный ``asyncio.create_task(...)`` без сохранения ссылки опасен: пока
    задача не завершилась, её может собрать сборщик мусора — и работа
    (например, автоперезапуск упавшего бота или уведомление владельца) просто
    не выполнится. Здесь ссылка удерживается до завершения задачи.
    """
    return _track(asyncio.create_task(coro, name=name))


async def _supervise(coro_factory: Callable[[], Awaitable[Any]],
                    name: str,
                    bot_id: int,
                    log: Any) -> None:
    """Вечный цикл: выполняет задачу, при ошибке ждёт и повторяет."""
    target = log if log is not None else logger
    attempts = 0

    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            # Остановка бота — это штатно, пробрасываем наверх.
            raise
        except Exception:
            attempts += 1
            delay = min(RESTART_BASE_DELAY * (2 ** (attempts - 1)),
                        RESTART_MAX_DELAY)
            if attempts >= LOUD_AFTER:
                target.critical(
                    "Задача '%s' падает %d раз подряд — перезапуск через %.0fс. "
                    "Что-то сломано в самом сервисе, проверьте журнал.",
                    name, attempts, delay, exc_info=True,
                )
            else:
                target.error(
                    "Задача '%s' упала (попытка %d) — перезапуск через %.0fс: %s",
                    name, attempts, delay, _describe(), exc_info=True,
                )
            await asyncio.sleep(delay)
        else:
            # Корутина завершилась без исключения — считаем, что цикл вышел
            # штатно (например, остановлен). Перезапускаем с небольшой паузой,
            # чтобы не получить горячий цикл, если он вернёт управление сразу.
            attempts = 0
            target.warning(
                "Задача '%s' завершилась без исключения — перезапуск через %.0fс",
                name, RESTART_BASE_DELAY,
            )
            await asyncio.sleep(RESTART_BASE_DELAY)


def _describe() -> str:
    """Краткое описание текущего исключения — для строки в журнале."""
    import sys

    exc = sys.exc_info()[1]
    return f"{type(exc).__name__}: {exc}" if exc else "неизвестная ошибка"

