"""Надёжная доставка сообщений: очередь в БД + повторы до успеха.

Проблема, которую решает модуль
------------------------------
Раньше отправка была «отправил и забыл»: если Telegram в этот момент вернул
flood-control, сеть моргнула или процесс перезапустился — сообщение просто
пропадало, и никто об этом не узнавал. Для владельца это выглядело как «бот
не работает».

Как работает здесь
------------------
1. Важные сообщения (ответы ПЗ, уведомления, напоминалки) кладутся в таблицу
   ``outbox`` — они переживают перезапуск бота.
2. Фоновый воркер забирает их и пытается отправить.
3. Ошибки делятся на два вида:

   * **временные** (flood-control, сеть, 5xx Telegram) — повтор с нарастающей
     паузой, сообщение остаётся в очереди;
   * **окончательные** (юзер заблокировал бота, чат удалён) — статус
     ``failed`` с причиной, её видно в «Диагностике».

Таким образом, сообщение не теряется молча: оно либо дойдёт, либо будет
явно помечено как недоставленное с указанием причины.
"""

import asyncio
import json
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from services.storage import (
    fail_outbox,
    fail_pending_for_chat,
    fetch_due_outbox,
    mark_outbox_sent,
    outbox_counts,
    purge_outbox,
    reschedule_outbox,
)

logger = logging.getLogger(__name__)

# Как часто воркер проверяет очередь (секунды).
POLL_INTERVAL = 3.0

# Сколько записей забираем за один проход.
BATCH_SIZE = 20

# Попытки до окончательного отказа. 8 попыток с backoff — примерно 4 минуты
# терпения; для «Юзер заблокировал бота» повторы бессмысленны и лимит не
# расходуется.
MAX_ATTEMPTS = 8

# Пауза между попытками: 3с → 6с → 12с → … , но не дольше 10 минут.
BACKOFF_BASE = 3.0
BACKOFF_MAX = 600.0

# Эти фразы означают, что повтор НЕ поможет: чата нет / бота заблокировали.
_FINAL_MESSAGES = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "kicked from the supergroup chat",
    "bot was kicked",
    "forbidden",
    "bot can't initiate conversation",
    "not enough rights",
    "have no rights to send a message",
)


def _is_final(err: Exception) -> bool:
    """Окончательный ли отказ (повтор не поможет)?"""
    if isinstance(err, TelegramForbiddenError):
        return True
    text = (getattr(err, "message", "") or str(err)).lower()
    return any(marker in text for marker in _FINAL_MESSAGES)


# Чаты, в которые писать бессмысленно: Telegram ответил «chat not found» или
# «bot was kicked». Второй раз в тот же чат не стучимся — иначе бот бесконечно
# долбит удалённую группу, а владелец видит только растущий счётчик
# «недоставленные сообщения». Набор живёт в памяти: после перезапуска
# пробуем снова (за это время чат могли привязать заново).
_DEAD_CHATS: set[tuple[int, int]] = set()


def _chat_is_gone(err: Exception) -> bool:
    """True, если САМОГО чата больше нет (удалён / бота из него выгнали)."""
    text = (getattr(err, "message", "") or str(err)).lower()
    return ("chat not found" in text
            or "bot was kicked" in text
            or "bot is not a member" in text
            or "group chat was deactivated" in text
            or "chat was deleted" in text)


def _backoff(attempts: int) -> float:
    """Пауза перед следующей попыткой (с ограничением сверху)."""
    return min(BACKOFF_BASE * (2 ** max(0, attempts - 1)), BACKOFF_MAX)


def _retry_after(err: Exception) -> float | None:
    """Сколько секунд Telegram сам попросил подождать (flood-control)."""
    if isinstance(err, TelegramRetryAfter):
        return float(getattr(err, "retry_after", 0) or 0)
    return None


def _describe(err: Exception) -> str:
    """Короткое описание ошибки для журнала и поля ``last_error``."""
    text = (getattr(err, "message", "") or str(err) or "").strip().replace("\n", " ")
    return f"{type(err).__name__}: {text[:250]}" if text else type(err).__name__


async def _send_one(bot: Bot, record: dict) -> None:
    """Отправляет одну запись очереди вызовом подходящего метода Bot API.

    Метод выбирается лениво (``getattr``): так мы не трогаем методы, которые
    для этого сообщения не нужны, и не падаем, если у бота нет, скажем,
    ``send_document``.

    Поднимает исключение — вызывающий сам разберётся, временная ошибка это
    или окончательная.
    """
    kind = str(record.get("kind") or "message")
    chat_id = int(record["chat_id"])
    try:
        payload = json.loads(record.get("payload") or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    method_name = {
        "message": "send_message",
        "photo": "send_photo",
        "document": "send_document",
        "sticker": "send_sticker",
        "video": "send_video",
        "voice": "send_voice",
    }.get(kind, "send_message")

    method = getattr(bot, method_name, None)
    if method is None:
        raise RuntimeError(f"бот не умеет отправлять «{kind}»")
    await method(chat_id=chat_id, **payload)



class OutboxWorker:
    """Фоновый воркер очереди недоставленных сообщений."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._bots: dict[int, Bot] = {}
        self.sent_total = 0
        self.failed_total = 0

    # ── Боты ──────────────────────────────────────────────────
    def register_bot(self, bot_id: int, bot: Bot) -> None:
        """Регистрирует бота, от имени которого идут сообщения очереди."""
        self._bots[int(bot_id)] = bot

    def unregister_bot(self, bot_id: int) -> None:
        self._bots.pop(int(bot_id), None)

    def bot_for(self, bot_id: int) -> Bot | None:
        return self._bots.get(int(bot_id))

    # ── Жизненный цикл ─────────────────────────────────────────
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="outbox_worker")
        logger.info("Очередь доставки запущена (интервал %.0fс)", POLL_INTERVAL)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info(
            "Очередь доставки остановлена (доставлено: %d, не доставлено: %d)",
            self.sent_total, self.failed_total,
        )

    async def _run(self) -> None:
        while True:
            try:
                await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Ошибка самого воркера не должна его убивать: ждём и пробуем.
                logger.exception("Ошибка в цикле очереди доставки")
            await asyncio.sleep(POLL_INTERVAL)

    # ── Работа с очередью ──────────────────────────────────────
    async def _forget_chat(self, bot_id: int, chat_id: int, err: Exception) -> None:
        """Чат недоступен: больше не пишем и чистим очередь по нему.

        Иначе бот стучится в удалённую группу до бесконечности, а владелец
        видит только растущий счётчик «недоставленные сообщения» и не
        понимает, что чат надо привязать заново.
        """
        key = (int(bot_id), int(chat_id))
        if key in _DEAD_CHATS:
            return
        _DEAD_CHATS.add(key)
        closed = fail_pending_for_chat(bot_id, chat_id, _describe(err))
        logger.warning(
            "Чат %s недоступен боту %s (%s) — больше в него не пишем, "
            "закрыто сообщений: %d",
            chat_id, bot_id, _describe(err), closed,
        )
        # Снимаем привязку (если чат привязан) и объясняем владельцу, что
        # делать. Импорт внутри функции: child_manager сам импортирует delivery.
        try:
            from services.child_manager import handle_dead_chat
            await handle_dead_chat(bot_id, chat_id, _describe(err))
        except Exception:
            logger.debug("Не удалось обработать недоступный чат %s", chat_id,
                         exc_info=True)

    async def drain(self, limit: int = BATCH_SIZE) -> tuple[int, int]:
        """Обрабатывает очередь один раз. Возвращает (доставлено, отказов)."""
        sent = failed = 0
        for record in fetch_due_outbox(limit):
            row_id = int(record["id"])
            bot_id = int(record.get("bot_id") or 0)
            chat_id = int(record.get("chat_id") or 0)

            if (bot_id, chat_id) in _DEAD_CHATS:
                # Чат уже признан недоступным — Telegram не дёргаем вовсе.
                fail_outbox(row_id, int(record.get("attempts") or 0) + 1,
                            "чат недоступен (chat not found)")
                self.failed_total += 1
                failed += 1
                continue

            bot = self.bot_for(bot_id)
            if bot is None:
                # Бота нет в памяти (удалён или ещё не поднялся) — ждём:
                # сообщение остаётся в очереди, НЕ помечаем как отказ.
                continue

            attempts = int(record.get("attempts") or 0) + 1
            try:
                await _send_one(bot, record)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                wait = _retry_after(err)
                if _chat_is_gone(err):
                    # Чат мёртв: повторять бессмысленно, снимаем привязку.
                    await self._forget_chat(bot_id, chat_id, err)
                if _is_final(err):
                    fail_outbox(row_id, attempts, _describe(err))
                    self.failed_total += 1
                    failed += 1
                    logger.warning(
                        "Сообщение #%s доставить невозможно (%s) — чат %s",
                        row_id, _describe(err), chat_id,
                    )
                    continue
                if attempts >= MAX_ATTEMPTS:
                    fail_outbox(row_id, attempts, _describe(err))
                    self.failed_total += 1
                    failed += 1
                    logger.error(
                        "Сообщение #%s не доставлено после %d попыток: %s",
                        row_id, attempts, _describe(err),
                    )
                    continue
                delay = wait if wait and wait > 0 else _backoff(attempts)
                reschedule_outbox(row_id, attempts, _describe(err), delay)
                logger.warning(
                    "Сообщение #%s не отправлено (попытка %d/%d), повтор через "
                    "%.0fс: %s", row_id, attempts, MAX_ATTEMPTS, delay,
                    _describe(err),
                )
            else:
                mark_outbox_sent(row_id)
                self.sent_total += 1
                sent += 1
        return sent, failed

    # ── Диагностика ───────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        """Сводка для «Диагностики»: сколько ждёт и сколько не дошло."""
        counts = outbox_counts()
        return {
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "sent_total": self.sent_total,
            "failed_total": self.failed_total,
            "bots": len(self._bots),
            "dead_chats": len(_DEAD_CHATS),
            "running": bool(self._task and not self._task.done()),
        }

    def maintenance(self) -> int:
        """Разовая уборка старых записей (вызывается при старте)."""
        return purge_outbox(keep_days=7)


# Единственный воркер на весь процесс: его регистрируют в app.py, а хендлеры
# и сервисы кладут сообщения через общий API.
_worker = OutboxWorker()


def get_outbox() -> OutboxWorker:
    """Доступ к воркеру очереди (например, чтобы зарегистрировать бота)."""
    return _worker


def enqueue(bot_id: int, chat_id: int, text: str,
            kind: str = "message", **extra: Any) -> None:
    """Кладёт сообщение в очередь доставки (с записью в журнал).

    Используется там, где потеря сообщения недопустима: уведомления,
    напоминалки, ответы ПЗ. Обычные ответы в чат идут напрямую — они
    показываются пользователю мгновенно, и очередь лишь задерживала бы их.
    """
    from services.storage import enqueue_outbox

    payload: dict[str, Any] = {"text": text, **extra}
    row_id = enqueue_outbox(bot_id, chat_id, kind, payload)
    logger.debug("Сообщение в очередь: #%s → чат %s (%s)", row_id, chat_id, kind)
