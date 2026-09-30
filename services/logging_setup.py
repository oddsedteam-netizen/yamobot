"""Логирование проекта: файлы, ротация и отдельный журнал на каждого бота.

Зачем это нужно
---------------
Раньше всё писалось в stdout через ``logging.basicConfig``, и при сбое было
непонятно, где искать причину. Теперь:

* ``logs/yamobot.log``      — общий журнал (ротация 10 МБ × 5 файлов);
* ``logs/bots/bot_<id>.log`` — отдельный файл на каждого дочернего бота;
* ``logs/errors.log``       — только ERROR и CRITICAL.

Каждая строка дочернего бота помечается его ``bot_id`` и ``@username``,
поэтому в общем журнале видно, чей именно бот упал::

    2024-05-01 12:00:00 | ERROR | bot 8616871006 @MyBot | handlers.start | ...

Как это работает
---------------
Подписывать каждый вызов ``logger.warning(...)`` вручную нереально: в
``child_manager`` их сотни. Поэтому метка бота берётся из ``contextvars``:
дочерний бот выставляет ``current_bot_id`` на время обработки апдейта, а
``logging.Filter`` подставляет его в каждую запись. В журнал попадает всё —
включая сообщения сторонних библиотек — без единой правки вызовов.

Пометить бота можно и явно: ``bot_logger(bot_id, __name__)``.

Использование::

    from services.logging_setup import setup_logging, bot_context

    setup_logging()                    # один раз при старте (app.py)
    with bot_context(bot_id, "@MyBot"):  # всё внутри → в bot_<id>.log
        await handle_update(update)
"""

import logging
import logging.handlers
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
BOTS_LOGS_DIR = LOGS_DIR / "bots"

MAIN_LOG = LOGS_DIR / "yamobot.log"
ERRORS_LOG = LOGS_DIR / "errors.log"

# Ротация: 10 МБ на файл, храним 5 последних (итого до ~50 МБ на журнал).
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

# Консоль — по важному, файлы — подробнее, чтобы окно консоли не засорялось
# INFO-строками от десятков ботов.
CONSOLE_LEVEL = logging.INFO
FILE_LEVEL = logging.DEBUG

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(botinfo)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Подставляется в формат для записей без метки бота (основной бот, служебное).
_BOT_PLACEHOLDER = "main"

# Текущий бот в контексте обработки апдейта. None — запись про основной бот.
current_bot_id: ContextVar[int | None] = ContextVar("current_bot_id", default=None)
current_bot_name: ContextVar[str] = ContextVar("current_bot_name", default="")

# bot_id → @username, чтобы подписи были читаемыми даже вне контекста.
BOT_NAMES: dict[int, str] = {}


@contextmanager
def bot_context(bot_id: int, bot_name: str = ""):
    """Помечает все записи внутри блока как принадлежащие этому боту.

    Работает как контекстный менеджер, но асинхронно-безопасен: contextvars
    копируются вместе с задачей, поэтому параллельные апдейты разных ботов
    не путают друг друга.
    """
    bid = int(bot_id or 0)
    if bid and bot_name:
        BOT_NAMES[bid] = bot_name
    tok_id = current_bot_id.set(bid or None)
    tok_name = current_bot_name.set(bot_name or "")
    try:
        yield
    finally:
        current_bot_id.reset(tok_id)
        current_bot_name.reset(tok_name)


class _BotContextFilter(logging.Filter):
    """Подставляет bot_id из контекста, если он не задан явно.

    Ставится первым в цепочке, поэтому работают оба способа: контекстный
    (автоматически для всех записей задачи) и явный (``bot_logger``).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "bot_id", 0):
            bid = current_bot_id.get()
            if bid:
                record.bot_id = bid
                record.bot_name = (
                    current_bot_name.get() or BOT_NAMES.get(bid, "")
                )
        return True



class _BotInfoFormatter(logging.Formatter):
    """Подставляет ``bot 123 @name`` (или ``main``) в каждую строку."""

    def format(self, record: logging.LogRecord) -> str:
        bot_id = getattr(record, "bot_id", 0) or 0
        bot_name = str(getattr(record, "bot_name", "") or "")
        if bot_id:
            # @ подставляем только если его ещё нет: иначе выходило бы
            # «bot 123 @@MyBot» — вызовы передают имя то с @, то без.
            if bot_name and not bot_name.startswith("@"):
                bot_name = f"@{bot_name}"
            record.botinfo = f"bot {bot_id}{' ' + bot_name if bot_name else ''}"
        else:
            record.botinfo = _BOT_PLACEHOLDER
        return super().format(record)


def _formatter() -> _BotInfoFormatter:
    return _BotInfoFormatter(LOG_FORMAT, datefmt=DATE_FORMAT)


def _file_handler(path: Path, level: int) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(_formatter())
    return handler


class _NullWriter:
    """Приёмник для хендлера, который сам ничего не печатает.

    Нужен потому, что ``logging`` требует у хендлера объект с ``write``:
    записи уже разложены по файлам фильтром.
    """

    def write(self, _data: str) -> int:
        return 0

    def flush(self) -> None:
        return None


class _BotFilter(logging.Filter):
    """Раскладывает записи по файлам: в ``bots/bot_<id>.log`` — свои."""

    def __init__(self, bots_dir: Path) -> None:
        super().__init__()
        self._bots_dir = bots_dir
        # Кэш хендлеров: на каждый бот — один файл, создаётся лениво.
        self._handlers: dict[int, logging.Handler] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        bot_id = getattr(record, "bot_id", 0) or 0
        if not bot_id:
            return False  # основной бот и служебные модули — в общий журнал

        handler = self._handlers.get(bot_id)
        if handler is None:
            self._handlers[bot_id] = handler = self._make_handler(bot_id)
        handler.handle(record)
        return False  # в общий журнал запись уже ушла своим хендлером

    def _make_handler(self, bot_id: int) -> logging.Handler:
        self._bots_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            self._bots_dir / f"bot_{bot_id}.log",
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setLevel(FILE_LEVEL)
        handler.setFormatter(_formatter())
        return handler


_configured = False


def setup_logging(console_level: int = CONSOLE_LEVEL,
                  file_level: int = FILE_LEVEL) -> None:
    """Настраивает логирование. Вызывается один раз при старте приложения.

    Повторный вызов ничего не ломает: настроенные нами хендлеры снимаются,
    чтобы не задваивать записи.
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    # Снимаем прежние хендлеры, чтобы после перезапуска в том же процессе
    # строки не дублировались.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            logging.getLogger(__name__).debug(
                "Не удалось закрыть прошлый хендлер логов", exc_info=True,
            )

    # Метка бота должна попасть в запись ДО форматирования и до раскладки по
    # файлам. Фильтры корневого логгера для этого не подходят: они применяются
    # только к записям самого корня, а пишут через него десятки дочерних
    # логгеров. Поэтому фильтр вешаем на КАЖДЫЙ хендлер — они прогоняют
    # запись всегда.
    ctx_filter = _BotContextFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(_formatter())
    console.addFilter(ctx_filter)
    root.addHandler(console)

    main_file = _file_handler(MAIN_LOG, file_level)
    main_file.addFilter(ctx_filter)
    root.addHandler(main_file)

    # В errors.log — только ошибки: открыл файл и сразу видишь проблемы.
    errors_file = _file_handler(ERRORS_LOG, logging.ERROR)
    errors_file.addFilter(ctx_filter)
    root.addHandler(errors_file)

    # Журнал по ботам. Сначала контекстная метка, потом раскладка по файлам —
    # порядок фильтров важен.
    bot_handler = logging.StreamHandler(_NullWriter())
    bot_handler.addFilter(ctx_filter)
    bot_handler.addFilter(_BotFilter(BOTS_LOGS_DIR))
    bot_handler.setLevel(file_level)
    root.addHandler(bot_handler)

    root.setLevel(min(console_level, file_level))

    # aiogram шумит на уровне DEBUG (каждый апдейт) — приглушаем до INFO.
    for noisy in ("aiohttp.access", "aiogram.event", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    _configured = True



class BotLoggerAdapter(logging.LoggerAdapter):
    """Логгер, который во все записи подставляет ``bot_id``/``bot_name``."""

    def __init__(self, logger: logging.Logger, bot_id: int, bot_name: str = "") -> None:
        super().__init__(logger, {"bot_id": int(bot_id), "bot_name": bot_name or ""})


def bot_logger(bot_id: int | None, name: str, bot_name: str = ""):
    """Возвращает логгер для конкретного бота.

    ``bot_id`` = 0 или None даёт обычный логгер без метки бота (для основного
    бота и служебных модулей). Тип возврата намеренно не указан: для ``0``
    это ``Logger``, иначе ``BotLoggerAdapter``.
    """
    logger = logging.getLogger(name)
    if not bot_id:
        return logger
    return BotLoggerAdapter(logger, bot_id, bot_name)


def log_path_for_bot(bot_id: int) -> Path:
    """Путь к журналу бота (для кнопки «отправить логи» в интерфейсе)."""
    return BOTS_LOGS_DIR / f"bot_{bot_id}.log"


def tail_bot_log(bot_id: int, lines: int = 200) -> str:
    """Последние строки журнала бота — для диагностики и техподдержки."""
    path = log_path_for_bot(bot_id)
    if not path.exists():
        return "Журнал этого бота пока пуст — ошибок не было."
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError as e:
        return f"Не удалось прочитать журнал: {e}"


def log_startup_banner() -> None:
    """Пишет в журнал версию, время старта и путь к логам."""
    from services.constants import BOT_VERSION

    logging.getLogger("yamobot").info(
        "Старт YamoBot v%s | %s | логи: %s",
        BOT_VERSION,
        time.strftime("%Y-%m-%d %H:%M:%S"),
        LOGS_DIR,
    )
