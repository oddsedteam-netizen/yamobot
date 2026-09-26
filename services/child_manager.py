import asyncio
import io
import json
import logging
import random
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from html import escape as _html_escape
from typing import Any, TypeVar

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType, ChatType, ChatMemberStatus
from aiogram.exceptions import (
    TelegramBadRequest, TelegramConflictError, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter, TelegramServerError, TelegramUnauthorizedError,
)
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    BufferedInputFile,
    ChatMemberUpdated,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    CallbackQuery,
    MessageEntity,
    MessageReactionUpdated,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReplyKeyboardMarkup,
)

from handlers._common import (cb_data, cb_uid, cb_username, cb_firstname,
                              msg_uid, msg_username, msg_firstname,
                              try_edit_answer, try_edit)
from services import premium_emoji as premium
from services.config import proxy_settings
from services.constants import BASE_WELCOME, BOT_CREDIT
from services.polling import ResilientDispatcher, set_polling_problem_hook
from services.promo import detect_promo_from_message
from services.rich import rich_payload_from_json, send_rich

from services.storage import (
    add_child_user,
    add_stat,
    add_admin_message,
    ban_user,
    unban_user,
    is_user_banned,
    is_user_muted,
    save_banned_topic,
    get_banned_topic_user,
    delete_banned_topic,
    get_all_bots_flat,
    get_bot_by_id_any_owner,
    get_child_users,
    get_admin_by_user_id,
    get_emoji_map,
    mark_user_blocked,
    save_mailing,
    get_antispam_mode,
    set_feedback_chat,
    get_feedback_chat,
    get_topic_by_user,
    get_topic_by_topic_id,
    create_topic_record,
    delete_topic_record,
    assign_admin_to_topic,
    reset_topic_admin,
    save_feedback_message,
    get_feedback_msg_by_group_msg,
    get_feedback_msg_by_user_msg,
    get_bot_owner,
    get_bot_keyboard_by_bot,
    bot_display_name,
    is_bot_anonymous,
    get_bound_chat,
    get_user_bots,
    update_bot_field,
    get_antinakrutka_settings,
    set_antinakrutka_field,
    set_antinakrutka_triggered,
    clear_antinakrutka_snapshot,
    set_stats_offsets,
    get_raw_counts,
    get_stats,
    reserve_topic_slot,
    set_topic_id,
    get_cat_ask_settings,
    get_categories_for_pz,
    get_work_hours,
    is_within_work_hours,
    save_log_message,
    save_bot_error,
    DEFAULT_WORK_START,
    DEFAULT_WORK_END,
    DEFAULT_WORK_MESSAGE,
    admin_changes_left,
    log_admin_change,
    mark_bot_dead,
    clear_bot_dead,
)

logger = logging.getLogger(__name__)

# Основной (YamoBot) бот — используется для уведомлений в привязанный «чат админов».
_MAIN_BOT: Bot | None = None

# Минимальный интервал (секунды) между /start одного и того же пользователя
# в дочернем боте — защита от спама командой.
CHILD_START_MIN_INTERVAL = 3.0

# ── Защита от Flood control (429) ──────────────────────────────
# Telegram разрешает ~20 сообщений в минуту в один групповой чат, поэтому при
# наплыве ПЗ бот ловил «Flood control exceeded» и терял сообщения (ждал только
# 5 секунд и делал 3 попытки). Здесь держим «шлюз» на каждый чат:
#   • отправки в один чат идут строго по очереди с небольшой паузой;
#   • если Telegram попросил подождать — пауза выдерживается ОДИН раз для всех
#     задач бота, а сообщения не теряются, а доставляются после паузы.
_SEND_MIN_INTERVAL = 0.5      # пауза между отправками в один чат (сек)
_FLOOD_MAX_SLEEP = 60.0       # максимум сна одним куском
_SEND_ATTEMPTS = 8            # сколько раз повторяем при flood-wait


class _ChatGate:
    """Очередь отправок в один чат + выдержка flood-wait."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0
        # Чтобы не заливать лог одним и тем же «flood» при каждом повторе.
        self.flood_logged = False

    def hold(self, seconds: float) -> None:
        """Запрещает отправку в этот чат на ``seconds`` секунд (для всех задач)."""
        self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)

    def pace(self) -> None:
        """Небольшая пауза после успешной отправки (сглаживает наплыв)."""
        self.hold(_SEND_MIN_INTERVAL)

    async def acquire(self) -> None:
        await self._lock.acquire()
        while True:
            delay = self._next_allowed - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, _FLOOD_MAX_SLEEP))

    def release(self) -> None:
        if self._lock.locked():
            self._lock.release()


_chat_gates: dict[tuple[int, int], _ChatGate] = {}

# Тип результата отправки: нужен, чтобы «шлюз» сохранял тип ответа Telegram
# (Message, ForumTopic и т.д.) — иначе Pyright считает результат None.
_T = TypeVar("_T")


def _chat_gate(bot: Bot | None, chat_id: int) -> _ChatGate:
    """Шлюз отправок для пары (бот, чат)."""
    key = (id(bot), int(chat_id))
    gate = _chat_gates.get(key)
    if gate is None:
        gate = _ChatGate()
        _chat_gates[key] = gate
    return gate


async def _send_with_gate(bot: Bot | None, chat_id: int,
                          call: Callable[[], Awaitable[_T]]) -> _T:
    """Отправка с учётом лимитов Telegram: очередь + выдержка flood-wait.

    ``call`` — корутина без аргументов, которая делает саму отправку.
    Если Telegram вернул 429 (``TelegramRetryAfter``), ждём указанное время
    и повторяем — сообщение не теряется.

    Возвращает результат ``call`` (тип сохраняется: Message, ForumTopic и т.д.).
    """
    gate = _chat_gate(bot, chat_id)
    last_error: Exception | None = None
    for attempt in range(_SEND_ATTEMPTS):
        await gate.acquire()
        try:
            result = await call()
        except TelegramRetryAfter as e:
            delay = float(getattr(e, "retry_after", 1) or 1)
            last_error = e
            if not gate.flood_logged:
                gate.flood_logged = True
                logger.warning(
                    "Flood control в чате %s: жду %.1fс (отправки в этот чат "
                    "поставлены на паузу, сообщения не теряются)",
                    chat_id, delay,
                )
            gate.hold(delay)
            gate.release()
            continue
        except (TelegramServerError, TelegramNetworkError) as e:
            # Telegram «икнул» или пропала сеть: сообщение НЕ теряем — небольшая
            # пауза и повтор. Раньше такая ошибка сразу теряла сообщение.
            last_error = e
            pause = min(2.0 * (attempt + 1), 10.0)
            logger.warning(
                "Сбой связи при отправке в чат %s (%s) — повтор через %.0fс",
                chat_id, type(e).__name__, pause,
            )
            gate.hold(pause)
            gate.release()
            continue
        except Exception:
            gate.release()
            raise
        # Успех: небольшая пауза, чтобы наплыв не собрал новый flood-wait.
        gate.flood_logged = False
        gate.pace()
        gate.release()
        return result
    if last_error is not None:
        raise last_error
    # Сюда попадаем только если попытки кончились, а ошибки не было (не бывает
    # на практике) — явное исключение лучше, чем «пустой» результат.
    raise RuntimeError("Не удалось выполнить отправку: попытки исчерпаны")


def set_main_bot(bot: Bot) -> None:
    """Сохраняет ссылку на основной бот YamoBot для отправки уведомлений."""
    global _MAIN_BOT
    _MAIN_BOT = bot


def repair_premium_emoji_for_bot(bot_id: int) -> int:
    """Мягко возвращает премиум-эмодзи в приветствие одного бота.

    Ничего не удаляет и не перепривязывает: если в сохранённом приветствии
    эмодзи потерял премиум (например, владелец вставил его копированием),
    оборачиваем такой эмодзи в ``<tg-emoji>`` по известному словарю
    (services/premium_emoji.py). Возвращает сколько эмодзи восстановлено.
    """
    bot = get_bot_by_id_any_owner(bot_id)
    if not bot:
        return 0
    welcome = bot.get("welcome_text") or ""
    if not welcome:
        return 0
    if premium.has_premium_markup(welcome):
        # Уже размечено — чинить нечего (ремонт идемпотентен).
        return 0

    upgraded, fixed = premium.upgrade_markup(welcome)
    if fixed and upgraded != welcome:
        update_bot_field(bot.get("owner_id") or 0, bot_id, "welcome_text", upgraded)
        logger.info("Бот %s: восстановлено премиум-эмодзи в приветствии: %d", bot_id, fixed)
    return fixed


def repair_premium_emoji_for_owner(owner_id: int) -> dict:
    """Мягкий ремонт премиум-эмодзи во всех ботах владельца.

    Возвращает: сколько ботов проверено, в скольких приветствиях удалось
    восстановить премиум-эмодзи и сколько новых эмодзи добавлено в словарь
    (словарь обновляется по сохранённым приветствиям ботов).
    """
    fixed_bots = 0
    checked = 0
    learned = 0
    try:
        for bot in get_user_bots(owner_id):
            checked += 1
            welcome = bot.get("welcome_text") or ""
            # Если приветствие уже размечено — просто пополняем словарь.
            for emoji_id, emoji in premium.EMOJI_TAG_RE.findall(welcome):
                if premium.remember_custom_emoji(emoji, emoji_id):
                    learned += 1
            if repair_premium_emoji_for_bot(bot["id"]):
                fixed_bots += 1
    except Exception as e:
        logger.error("Ошибка ремонта премиум-эмодзи владельца %s: %s", owner_id, e)
    return {"checked": checked, "fixed": fixed_bots, "learned": learned}


# Боты, о которых уже сообщали в лог, что Telegram выбросил премиум-эмодзи.
# Нужно только для того, чтобы не спамить одним и тем же предупреждением.
_premium_drop_warned: set[int] = set()

# Боты, про токен которых уже сообщали владельцу, что он используется где-то ещё
# (чужой вебхук / второй polling-процесс). Тоже только против спама.
_token_used_warned: set[int] = set()


def check_premium_delivered(sent: Message | None, source_text: str,
                            bot_id: int | None = None) -> bool:
    """True, если Telegram выбросил премиум-эмодзи из отправленного сообщения.

    Бывает так: разметка ``<tg-emoji>`` в тексте есть, но сервер её игнорирует —
    сообщение уходит, а премиум-эмодзи превращается в обычный смайлик. Ошибки
    при этом нет, поэтому единственный способ заметить проблему — сверить
    сущности отправленного сообщения с тем, что мы отправляли.

    Такое встречается после передачи прав на бота через @BotFather: Telegram
    привязывает «право» на премиум-эмодзи к боту/владельцу, и после передачи
    разметка может тихо перестать работать. Починить это со стороны кода
    нельзя (решение сервера Telegram), но мы это замечаем и пишем в лог.
    """
    expected = premium.count_premium_markup(source_text)
    if sent is None or not expected:
        return False

    # У постов с фото (и любого медиа) разметка живёт в подписи, а не в тексте.
    entities = list(getattr(sent, "entities", None) or [])
    if not entities:
        entities = list(getattr(sent, "caption_entities", None) or [])
    delivered = sum(
        1 for e in entities if str(getattr(e, "type", "")) == "custom_emoji"
    )
    if delivered >= expected:
        if bot_id is not None:
            # Заработало — разрешаем сообщить снова, если опять сломается.
            _premium_drop_warned.discard(bot_id)
        return False

    if bot_id is not None:
        if bot_id in _premium_drop_warned:
            return True
        _premium_drop_warned.add(bot_id)

    logger.warning(
        "Бот %s: Telegram не принял премиум-эмодзи (%d из %d) — сообщение ушло "
        "обычными смайликами. Чаще всего так бывает после передачи прав на бота "
        "в @BotFather: «право» на премиум-эмодзи остаётся у прежнего владельца, "
        "и сервер тихо вырезает разметку. Со стороны кода это не лечится — "
        "если нужно, пересоздай бота или оставь обычные эмодзи.",
        bot_id, delivered, expected,
    )
    return True


def get_main_bot() -> Bot | None:
    """Возвращает основной бот YamoBot (используется фоновыми сервисами)."""
    return _MAIN_BOT


async def send_with_gate(bot: Bot, chat_id: int,
                         call: Callable[[], Awaitable[_T]]) -> _T:
    """Публичная обёртка над «шлюзом» отправок (для фоновых сервисов).

    Учитывает лимиты Telegram: очередь на чат + выдержка flood-wait (429).
    ``call`` — корутина без аргументов, делающая саму отправку.
    """
    return await _send_with_gate(bot, chat_id, call)


def _bot_name_of(bot_id: int) -> str:
    """Человекочитаемое имя бота по его id (``bot_<id>``, если данных нет)."""
    info = get_bot_by_id_any_owner(bot_id)
    return bot_display_name(info) if info else f"bot_{bot_id}"


async def _main_send(chat_id: int, **kwargs: Any):
    """Отправка от основного бота YamoBot (через «шлюз», с учётом flood-wait).

    Хелпер существует ради типов: у модульной переменной ``_MAIN_BOT``
    (``Bot | None``) Pyright не сохраняет сужение типа внутри лямбд, а у
    локальной переменной — сохраняет.
    """
    bot = _MAIN_BOT
    if bot is None:
        raise RuntimeError("Основной бот YamoBot ещё не инициализирован")
    return await _send_with_gate(
        bot, chat_id, lambda: bot.send_message(chat_id=chat_id, **kwargs)
    )


def _is_message_in_topic(message: Message, topic_id: int) -> bool:
    """True, если сообщение реально легло в топик ``topic_id``.

    Telegram может принять ``message_thread_id`` уже удалённого топика и
    опубликовать сообщение в «General» (message_thread_id = None). Такой
    ответ считается ошибкой доставки: топик надо пересоздать, иначе все
    сообщения ПЗ будут улетать в общий топик.
    """
    thread_id = getattr(message, "message_thread_id", None)
    if thread_id is None:
        return False
    try:
        return int(thread_id) == int(topic_id)
    except (TypeError, ValueError):
        return False


async def _delete_stray_message(bot: Bot, chat_id: int, message_id: int) -> None:
    """Убирает «залётное» сообщение, попавшее не в тот топик."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(
            "Не удалось удалить сообщение %s в чате %s (топик удалён): %s",
            message_id, chat_id, e,
        )


# Кэш скачанных байт медиа по file_id: file_id -> bytes.
# Нужен, чтобы при рассылке на несколько ботов не качать файл повторно.
_mailing_bytes_cache: dict[str, bytes] = {}


async def _cached_media_bytes(file_id: str) -> bytes | None:
    """Скачивает файл через основной бот и кэширует байты.

    file_id из YamoBot не работает в дочерних ботах (file_id привязан к боту,
    принявшему файл), поэтому для рассылки медиа перекачиваем его байты и
    перезаливаем дочерним ботом как новый файл.
    """
    if file_id in _mailing_bytes_cache:
        return _mailing_bytes_cache[file_id]
    if _MAIN_BOT is None:
        return None
    buffer = io.BytesIO()
    try:
        await _MAIN_BOT.download(file_id, destination=buffer)
        data = buffer.getvalue()
    except Exception as e:
        logger.warning("Не удалось скачать медиа %s: %s", file_id, e)
        return None
    _mailing_bytes_cache[file_id] = data
    return data


def _topic_web_link(group_chat_id: int, topic_id: int) -> str:
    """Строит ссылку на топик вида https://t.me/c/<chat>/<thread>."""
    cid = group_chat_id
    if cid < 0 and str(cid).startswith("-100"):
        chat_part = int(str(cid)[4:])
    else:
        chat_part = int(cid)
    return f"https://t.me/c/{chat_part}/{topic_id}"


def topic_web_link(group_chat_id: int, topic_id: int) -> str:
    """Публичная обёртка для формирования ссылки на топик."""
    return _topic_web_link(group_chat_id, topic_id)


def _is_thread_not_found(err: Exception) -> bool:
    """True, если ошибка означает, что топик удалён/недоступен (message thread not found)."""
    msg = (getattr(err, "message", "") or "").lower()
    return ("message thread not found" in msg
            or "thread not found" in msg
            or "topic not found" in msg)


# ── Категория ПЗ (хэштег в первом сообщении) ───────────────────
# ПЗ обычно помечает обращение категорией в первом сообщении после /start:
# «#общение», «#поддержка», «#универсал» и т.п. Категорию показываем в
# уведомлении и в шапке топика; если ПЗ её не написал — строки просто нет.
# \w в Python понимает кириллицу, поэтому регулярка не зависит от языка.
_PZ_TAG_RE = re.compile(r"#(\w{2,32})", re.UNICODE)
# Больше трёх категорий не показываем: это уже не категория, а мусор.
PZ_CATEGORIES_LIMIT = 3


def extract_pz_categories(message: Message) -> str:
    """Хэштеги-категории из первого сообщения ПЗ (или пустая строка).

    Сначала берём хэштеги из ``entities`` — Telegram сам их размечает и знает,
    где хэштег начинается и заканчивается. Если сущностей нет (старый апдейт),
    ищем регуляркой по тексту. Дубликаты и регистр приводим к виду «как написал
    ПЗ», лишние отбрасываем. Пустая строка означает «ПЗ категорию не указал» —
    тогда в уведомлении строки с категорией нет.
    """
    text = (message.text or message.caption or "").strip()
    if not text:
        return ""

    tags: list[str] = []
    for entity in list(message.entities or message.caption_entities or []):
        if str(getattr(entity, "type", "")) != "hashtag":
            continue
        offset = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        tag = text[offset:offset + length].strip()
        if tag:
            tags.append(tag)
    if not tags:
        tags = [f"#{found}" for found in _PZ_TAG_RE.findall(text)]

    unique: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        key = tag.lower().lstrip("#")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(tag if tag.startswith("#") else f"#{tag}")
    return " ".join(unique[:PZ_CATEGORIES_LIMIT])


# Слова сообщения — для поиска категории, написанной без хэштега
# («поддержка», «нужна поддержка»).
_PZ_WORD_RE = re.compile(r"\w{2,32}", re.UNICODE)


def pick_pz_category(message: Message, categories: list[str],
                     hashtags: str = "") -> str:
    """Категория ПЗ по списку включённых категорий (или пустая строка).

    Понимает и хэштег («#поддержка»), и обычное слово («поддержка»,
    «нужна поддержка»). Возвращает категорию в виде ``#имя`` — в таком виде её
    и показываем админам. Пустая строка значит «ПЗ категорию не назвал»: тогда
    (при включённой настройке) бот уточняет её кнопками.
    """
    wanted = [str(c).strip().lower().lstrip("#") for c in categories if str(c).strip()]
    if not wanted:
        return ""

    tags = hashtags or extract_pz_categories(message)
    for tag in tags.split():
        name = tag.lstrip("#").lower()
        if name in wanted:
            return f"#{name}"

    text = (message.text or message.caption or "").lower()
    if not text:
        return ""
    words = set(_PZ_WORD_RE.findall(text))
    # Порядок как в настройках: первая включённая категория «побеждает».
    for name in wanted:
        if name in words:
            return f"#{name}"
    return ""


# ── Уточнение категории у ПЗ ───────────────────────────────────
# Пока ПЗ не выбрал категорию, уведомление в «чат админов» откладывается:
# ключ — (id бота, чат ПЗ). Одновременно ждём ответ максимум
# PZ_CATEGORY_TIMEOUT секунд, потом уведомляем БЕЗ категории — иначе ПЗ
# потерялся бы совсем.
PZ_ASK_TEXT = (
    "🏷 <b>Какая категория админов тебе нужна?</b>\n\n"
    "Выбери кнопкой ниже — я передам обращение нужным админам."
)
PZ_CATEGORY_TIMEOUT = 180.0

_pz_category_pending: dict[tuple[int, int], dict[str, Any]] = {}
_pz_category_tasks: dict[tuple[int, int], asyncio.Task] = {}


def _pop_pz_category(bot_id: int, user_chat_id: int,
                     cancel_task: bool = True) -> dict[str, Any] | None:
    """Забирает ожидание категории у ПЗ (и отменяет таймер-страховку)."""
    key = (bot_id, user_chat_id)
    task = _pz_category_tasks.pop(key, None)
    if cancel_task and task is not None and not task.done():
        task.cancel()
    return _pz_category_pending.pop(key, None)


async def apply_pz_category(bot_obj: Bot, bot_id: int, user_chat_id: int,
                            index: int) -> tuple[bool, str]:
    """Применяет выбранную ПЗ категорию: уведомляет «чат админов».

    Возвращает ``(приняли, название)``. ``False`` — уточнение уже неактуально
    (истёк таймер) либо категория не найдена; в этом случае уведомление уходит
    без категории, чтобы ПЗ не потерялся.
    """
    pending = _pop_pz_category(bot_id, user_chat_id)
    if not pending:
        return False, ""

    categories = pending["categories"]
    group_chat_id = pending["group_chat_id"]
    topic_id = pending["topic_id"]

    if index < 0 or index >= len(categories):
        await _notify_new_pz(bot_id, group_chat_id, topic_id)
        return False, ""

    name = categories[index]
    try:
        await _notify_new_pz(bot_id, group_chat_id, topic_id, None, f"#{name}")
    except Exception as e:
        logger.warning("Не удалось уведомить о ПЗ с категорией: %s", e)

    # Заодно отмечаем категорию прямо в топике — админам так виднее.
    try:
        await _send_with_gate(
            bot_obj, group_chat_id,
            lambda: bot_obj.send_message(
                chat_id=group_chat_id, message_thread_id=topic_id,
                text=f"🏷 Категория: <b>#{_html_escape(name)}</b>"),
        )
    except Exception as e:
        logger.debug("Не удалось отметить категорию в топике: %s", e)
    return True, name


async def _ask_pz_category(bot_obj: Bot, bot_id: int, user_chat_id: int,
                           topic_id: int, group_chat_id: int,
                           categories: list[str]) -> bool:
    """Спрашивает у ПЗ категорию инлайн-кнопками (без покраски).

    Кнопки раскладываем по 2–3 в ряд: сообщение с кнопками в один столбик
    растягивалось и выглядело «простынёй», особенно когда к стандартным
    категориям добавлены свои.

    False — вопрос отправить не удалось (тогда вызывающий код уведомит админов
    как раньше, без категории).
    """
    buttons = [
        InlineKeyboardButton(text=name[:24], callback_data=f"pzcat_{index}")
        for index, name in enumerate(categories)
    ]
    # Раскладка: до 3 кнопок в ряд — так сообщение остаётся компактным.
    per_row = 3 if len(buttons) > 4 else 2
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    try:
        await _send_with_gate(
            bot_obj, user_chat_id,
            lambda: bot_obj.send_message(chat_id=user_chat_id, text=PZ_ASK_TEXT,
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)),
        )
    except Exception as e:
        logger.warning("Не удалось спросить категорию у ПЗ %s: %s", user_chat_id, e)
        return False

    key = (bot_id, user_chat_id)
    _pz_category_pending[key] = {
        "topic_id": topic_id,
        "group_chat_id": group_chat_id,
        "categories": list(categories),
    }
    old = _pz_category_tasks.pop(key, None)
    if old is not None and not old.done():
        old.cancel()
    _pz_category_tasks[key] = asyncio.create_task(
        _pz_category_timeout(bot_id, user_chat_id),
        name=f"pzcat_{bot_id}_{user_chat_id}",
    )
    return True


async def _pz_category_timeout(bot_id: int, user_chat_id: int) -> None:
    """Страховка: ПЗ не выбрал категорию — уведомляем админов без неё."""
    try:
        await asyncio.sleep(PZ_CATEGORY_TIMEOUT)
    except asyncio.CancelledError:
        return

    pending = _pop_pz_category(bot_id, user_chat_id, cancel_task=False)
    if not pending:
        return
    logger.info("ПЗ %s (бот %s) не выбрал категорию за %.0fс — уведомляю без неё",
                user_chat_id, bot_id, PZ_CATEGORY_TIMEOUT)
    try:
        await _notify_new_pz(bot_id, pending["group_chat_id"], pending["topic_id"])
    except Exception as e:
        logger.warning("Не удалось отправить отложенное уведомление о ПЗ: %s", e)


async def notify_owner(bot_id: int, text: str) -> None:
    """Шлёт владельцу бота сообщение от основного бота (если это возможно).

    Используется для «мягких» предупреждений: например, что токен бота уже
    используется другой программой (конфликт polling) — владелец видит это
    прямо в чате с YamoBot, а не ищет в логах.
    """
    if _MAIN_BOT is None:
        return
    owner_id = get_bot_owner(bot_id)
    if not owner_id:
        return
    try:
        await _MAIN_BOT.send_message(chat_id=owner_id, text=text)
    except Exception as e:
        logger.warning("Не удалось уведомить владельца %s: %s", owner_id, e)


def _find_bot_id_by_telegram_id(telegram_id: int) -> int | None:
    """Проверяет, есть ли такой бот в нашей БД (id записи = Telegram id бота).

    Нужно для уведомлений из ``services.polling``: там есть только сам ``Bot``,
    а чтобы написать владельцу, нужен id записи бота в БД.
    """
    try:
        if any(int(info.get("id") or 0) == telegram_id for info in get_all_bots_flat()):
            return telegram_id
    except Exception as e:
        logger.debug("Не удалось найти бота %s в БД: %s", telegram_id, e)
    return None


async def _on_polling_problem(bot: Bot, kind: str) -> None:
    """Реакция на проблемы polling у дочернего бота (см. ``services.polling``).

    «conflict» — апдейты бота забирает вебхук или второй экземпляр бота:
    предупреждаем владельца один раз за серию.
    «unauthorized» — токен отозван/недействителен: polling остановлен, сообщаем
    владельцу, что нужно переподключить бота с рабочим токеном.
    """
    bot_id = _find_bot_id_by_telegram_id(bot.id)
    if bot_id is None:
        return

    if kind == "conflict":
        if bot_id in _token_used_warned:
            return
        _token_used_warned.add(bot_id)
        await notify_owner(
            bot_id,
            "⚠️ <b>Бот не может получать сообщения: токен занят</b>\n\n"
            f"🤖 Бот: <b>{_bot_name_of(bot_id)}</b>\n\n"
            "У бота стоит вебхук или второй экземпляр бота, который тоже забирает "
            "апдейты (например, копия бота на другом сервере или подключение к "
            "другому конструктору). Я сбрасываю вебхук сам и повторяю попытки — "
            "сообщения приходят, но часть их может уходить «на ту сторону».\n\n"
            "Останови второго «слушателя» — и бот заработает нормально, "
            "перезапускать вручную не нужно.",
        )
        return

    await notify_owner(
        bot_id,
        "⛔ <b>Токен бота больше не работает</b>\n\n"
        f"🤖 Бот: <b>{_bot_name_of(bot_id)}</b>\n\n"
        "Telegram ответил «Unauthorized»: токен отозван или недействителен. "
        "Бот не может получать сообщения, и повторять попытки бессмысленно — "
        "я остановил его опрос.\n\n"
        "Проверь бота в @BotFather (он мог быть удалён или пересоздан) и добавь "
        "его в панель заново с рабочим токеном.",
    )
    # Авто-детект «мёртвого» бота: попал в список админ-панели, откуда его
    # можно удалить пачкой вместе с остальными нерабочими ботами.
    try:
        mark_bot_dead(bot_id, "unauthorized")
    except Exception as e:
        logger.debug("Не удалось пометить бота %s мёртвым: %s", bot_id, e)


# Проблемы polling (конфликт токена / мёртвый токен) показываем владельцу.
set_polling_problem_hook(_on_polling_problem)


async def _notify_new_pz(bot_id: int, group_chat_id: int, topic_id: int,
                         promo_hint: str | None = None,
                         categories: str = "") -> None:
    """Шлёт в привязанный «чат админов» владельца уведомление о новом ПЗ.

    Ссылка на топик отправляется всегда; имя/ID пользователя — только вне
    анонимного режима (в анонимном личность скрыта). Если первое сообщение ПЗ
    похоже на предложение пиара/ВП — добавляем пометку «возможно пиар»/«возможно ВП».
    Если ПЗ указал категорию хэштегом в первом сообщении (#общение, #поддержка,
    #универсал) — показываем её; не указал — строки с категорией нет.

    Строки разделяем пустыми строками: так уведомление читается легче, а по
    ссылке-топику проще попасть пальцем.
    """
    if _MAIN_BOT is None:
        return
    owner_id = get_bot_owner(bot_id)
    if not owner_id:
        return

    # Во время тревоги антинакрутки уведомления о новых ПЗ не шлём —
    # вместо них владелец уже получил предупреждение о возможной накрутке.
    if is_antinakrutka_active(owner_id):
        return

    admin_chat = get_bound_chat(owner_id, "admin")
    if not admin_chat:
        return

    bot_info = get_bot_by_id_any_owner(bot_id)
    bot_name = bot_display_name(bot_info) if bot_info else f"bot_{bot_id}"
    link = _topic_web_link(group_chat_id, topic_id)

    text = (
        f"🆕 <b>Новый ПЗ</b>\n\n"
        f"🤖 Бот: <b>{bot_name}</b>\n\n"
        f"🔗 Топик: {link}"
    )
    if categories:
        # Категорию пишет сам ПЗ хэштегом в первом сообщении (#общение,
        # #поддержка, #универсал). Не написал — строки нет.
        text += f"\n\n🏷 Категория: <b>{_html_escape(categories)}</b>"

    if promo_hint:
        # Подсказка админам: обращение, скорее всего, про рекламу/взаимный пиар.
        text += f"\n\n🔎 <b>{promo_hint}</b>"

    try:
        await _main_send(admin_chat, text=text)
    except Exception as e:
        logger.warning("Не удалось отправить уведомление о новом ПЗ в чат админов: %s", e)


async def _notify_admin_change(bot_id: int, group_chat_id: int, topic_id: int) -> None:
    """Шлёт в «чат админов» владельца уведомление о запросе смены админа.

    Топик к этому моменту уже сброшен в «без админа», поэтому автоматически
    попадает в список «ПЗ без админов» (/стата → «ПЗ без админов»).
    """
    if _MAIN_BOT is None:
        return
    owner_id = get_bot_owner(bot_id)
    if not owner_id:
        return
    admin_chat = get_bound_chat(owner_id, "admin")
    if not admin_chat:
        return

    bot_info = get_bot_by_id_any_owner(bot_id)
    bot_name = bot_display_name(bot_info) if bot_info else f"bot_{bot_id}"
    link = _topic_web_link(group_chat_id, topic_id)

    text = (
        f"🔄 <b>ПЗ просит смену админа!</b>\n\n"
        f"🤖 Бот: <b>{bot_name}</b>\n\n"
        f"🔗 Топик: {link}"
    )

    try:
        await _main_send(admin_chat, text=text)
    except Exception as e:
        logger.warning("Не удалось отправить уведомление о смене админа: %s", e)


# ═══════════════════════════════════════════════════════════════
#  Антинакрутка: защита от наплыва фейковых «новых ПЗ»
# ═══════════════════════════════════════════════════════════════

# Когда пришли новые ПЗ: owner_id -> [monotonic-время, ...]
_PZ_TIMES: dict[int, list[float]] = defaultdict(list)
# Владельцы, которым уже сообщили о возможной накрутке (чтобы не спамить).
_PZ_ALERTS: set[int] = set()


def is_antinakrutka_active(owner_id: int) -> bool:
    """True, если у владельца сейчас активна защита от накрутки ПЗ."""
    if not owner_id:
        return False
    settings = get_antinakrutka_settings(owner_id)
    # Защита может быть выключена переключателем «🔴 Выключить» — тогда даже
    # «сработавшее» состояние не считается активным.
    if not int(settings.get("enabled", 1)):
        return False
    return bool(settings["triggered"])


# ── Режим защиты: сообщения не доставляются, но и не теряются ──
# Последнее предупреждение «бот в режиме защиты» на пользователя: чтобы сам
# алерт не превратился в спам (и не собрал flood-control).
_PROTECTION_NOTICE_INTERVAL = 60.0
_protection_notice_at: dict[tuple[int, int], float] = {}


def _is_antinakrutka_blocking(owner_id: int) -> bool:
    """True, если защита включена — топики не создаём, сообщения не шлём."""
    return is_antinakrutka_active(owner_id)


async def _notify_protection(message: Message, owner_id: int) -> None:
    """Сообщает пользователю, что бот в режиме защиты от спама.

    Не чаще одного раза в минуту на пользователя: во время наплыва ПЗ это
    предупреждение само могло бы стать источником спама.
    """
    key = (owner_id, msg_uid(message))
    now = time.monotonic()
    if now - _protection_notice_at.get(key, 0.0) < _PROTECTION_NOTICE_INTERVAL:
        return
    _protection_notice_at[key] = now
    try:
        await _safe_answer(
            message,
            "🛡 <b>Бот находится в режиме защиты от спама.</b>\n\n"
            "Сообщения временно не доходят — как только защита снимется, "
            "мы снова сможем принять ваше обращение. Попробуйте позже.",
        )
    except Exception:
        pass


def clear_antinakrutka_state(owner_id: int) -> None:
    """Сбрасывает тревогу антинакрутки и внутренние счётчики владельца."""
    _PZ_TIMES.pop(owner_id, None)
    _PZ_ALERTS.discard(owner_id)
    for key in [k for k in _protection_notice_at if k[0] == owner_id]:
        _protection_notice_at.pop(key, None)


def _make_stats_snapshot(owner_id: int) -> str:
    """Снимок статистики всех ботов владельца на момент срабатывания защиты."""
    snapshot: dict[str, dict] = {}
    for bot in get_user_bots(owner_id):
        stats = get_stats(int(bot["id"]))
        snapshot[str(bot["id"])] = {
            "messages_in": stats["messages_in"],
            "messages_out": stats["messages_out"],
            "users_total": stats["users_total"],
        }
    return json.dumps(snapshot, ensure_ascii=False)


async def _notify_antinakrutka(owner_id: int, settings: dict) -> None:
    """Сообщает владельцу и в «чат админов» о возможной накрутке ПЗ."""
    if _MAIN_BOT is None:
        return
    count = int(settings.get("count") or 0)
    window = int(settings.get("window_minutes") or 0)
    block_line = (
        "🚫 На время защиты бот <b>не создаёт новые ПЗ</b> и <b>не присылает "
        "уведомления</b> в этот чат: пользователям пишется, что бот в режиме "
        "защиты от спама и сообщения временно не доходят.\n"
    )

    owner_text = (
        "⚠️ <b>Возможная накрутка ПЗ!</b>\n\n"
        f"За последние <b>{window}</b> мин пришло <b>{count}+</b> новых ПЗ.\n"
        f"{block_line}\n"
        "📊 Статистика на момент срабатывания защиты сохранена.\n\n"
        "❓ <b>Засчитывать этот наплыв в статистику?</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить (не накрутка)",
                              callback_data=f"an_keep_{owner_id}",
                              style="success")],
        [InlineKeyboardButton(text="🚫 Это накрутка (не засчитывать)",
                              callback_data=f"an_drop_{owner_id}",
                              style="danger")],
    ])
    try:
        await _main_send(owner_id, text=owner_text, reply_markup=kb)
    except Exception as e:
        logger.warning("Не удалось уведомить владельца о накрутке ПЗ: %s", e)

    admin_chat = get_bound_chat(owner_id, "admin")
    if not admin_chat:
        return
    try:
        await _main_send(
            admin_chat,
            text=(
                "⚠️ <b>Возможная накрутка ПЗ!</b>\n\n"
                f"За <b>{window}</b> мин пришло <b>{count}+</b> новых ПЗ.\n"
                "🔕 Уведомления о новых ПЗ <b>временно приостановлены</b>, "
                "пока владелец не подтвердит, что это реальные обращения."
            ),
        )
    except Exception as e:
        logger.warning("Не удалось уведомить чат админов о накрутке ПЗ: %s", e)


async def _notify_antinakrutka_release(owner_id: int) -> None:
    """Второй вопрос: снимаем ли защиту (при любом решении по статистике)."""
    if _MAIN_BOT is None:
        return
    text = (
        "🛡 <b>Снимаю защиту?</b>\n\n"
        "• <b>Да</b> — бот снова создаёт топики ПЗ и присылает уведомления "
        "в «чат админов».\n"
        "• <b>Нет</b> — защита остаётся: топики не создаются, уведомления не "
        "приходят. Снять её потом можно кнопкой <b>«🔄 Сбросить защиту»</b> "
        "(Профиль → 🚨 Антинакрутка)."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, снять защиту",
                              callback_data=f"an_release_yes_{owner_id}",
                              style="success")],
        [InlineKeyboardButton(text="❌ Нет, оставить защиту",
                              callback_data=f"an_release_no_{owner_id}",
                              style="danger")],
    ])
    try:
        await _main_send(owner_id, text=text, reply_markup=kb)
    except Exception as e:
        logger.warning("Не удалось спросить про снятие защиты: %s", e)


async def notify_antinakrutka_release(owner_id: int) -> None:
    """Публичная обёртка: второй вопрос владельцу — «снимаем защиту?»."""
    await _notify_antinakrutka_release(owner_id)


async def register_new_pz(owner_id: int) -> bool:
    """Фиксирует новое ПЗ и при превышении порога включает защиту.

    Возвращает True, если защита сработала именно на этом ПЗ.
    """
    if not owner_id:
        return False
    settings = get_antinakrutka_settings(owner_id)
    # Выключенная защита не следит за наплывом вообще.
    if not int(settings.get("enabled", 1)):
        return False
    if settings["triggered"]:
        return False

    now = time.monotonic()
    window_sec = float(settings["window_minutes"]) * 60.0
    times = [t for t in _PZ_TIMES.get(owner_id, []) if now - t < window_sec]
    times.append(now)
    _PZ_TIMES[owner_id] = times

    if len(times) < int(settings["count"]):
        return False

    # Порог превышен — «взводим» защиту и запоминаем статистику.
    # ``block_topics`` выставляем в 1: во время защиты бот всегда не создаёт
    # новые ПЗ и не присылает уведомления (в БД храним фактическое состояние).
    snapshot = _make_stats_snapshot(owner_id)
    set_antinakrutka_triggered(owner_id, True, snapshot)
    set_antinakrutka_field(owner_id, "block_topics", 1)
    _PZ_TIMES[owner_id] = []
    logger.warning(
        "Антинакрутка сработала у владельца %s: %s ПЗ за %s мин",
        owner_id, settings["count"], settings["window_minutes"],
    )

    if owner_id not in _PZ_ALERTS:
        _PZ_ALERTS.add(owner_id)
        await _notify_antinakrutka(owner_id, settings)
    return True


def apply_antinakrutka_decision(owner_id: int, keep_stats: bool) -> dict:
    """Применяет решение владельца по наплыву ПЗ (только статистика).

    * ``keep_stats=True`` — наплыв признан реальным: статистика не меняется
      (наплыв остаётся засчитанным);
    * ``keep_stats=False`` — это накрутка: статистика откатывается к снимку
      на момент срабатывания (накрученные ПЗ вычитаются навсегда).

    Защита при этом НЕ снимается — её владелец снимает отдельно
    («Снять защиту» / «Сброс защиты»).
    """
    settings = get_antinakrutka_settings(owner_id)
    raw_snapshot = settings.get("snapshot") or ""
    snapshot: dict = {}
    if raw_snapshot:
        try:
            snapshot = json.loads(raw_snapshot)
        except (json.JSONDecodeError, TypeError):
            snapshot = {}

    bots = get_user_bots(owner_id)
    restored = 0
    if not keep_stats:
        # Откатываем статистику: вычитаем всё, что «накапало» с момента
        # срабатывания. Смещения считаются АБСОЛЮТНЫМИ от «сырых» счётчиков,
        # поэтому прошлые подтверждённые накрутки остаются вычтенными.
        for bot in bots:
            bot_id = int(bot["id"])
            want = snapshot.get(str(bot_id))
            if not want:
                continue
            raw = get_raw_counts(bot_id)
            set_stats_offsets(
                bot_id,
                raw["messages_in"] - int(want.get("messages_in", 0)),
                raw["messages_out"] - int(want.get("messages_out", 0)),
                raw["users_total"] - int(want.get("users_total", 0)),
            )
            restored += 1

    # Снимок больше не нужен — решение по статистике принято. Защиту при этом
    # НЕ снимаем: владелец отдельно решает, снимать ли её (см. lift_antinakrutka).
    clear_antinakrutka_snapshot(owner_id)
    return {"bots": len(bots), "restored": restored, "keep": keep_stats}


async def lift_antinakrutka(owner_id: int) -> dict:
    """Снимает защиту антинакрутки: топики и уведомления снова работают.

    Вызывается по кнопкам «✅ Да, снять защиту» (после вопроса) и
    «🔄 Сбросить защиту» в профиле.
    """
    set_antinakrutka_triggered(owner_id, False)
    clear_antinakrutka_state(owner_id)
    logger.info("Антинакрутка выключена у владельца %s — уведомления возобновлены",
                owner_id)

    admin_chat = get_bound_chat(owner_id, "admin")
    if admin_chat:
        try:
            await _main_send(
                admin_chat,
                text=(
                    "✅ <b>Защита от накрутки снята.</b>\n"
                    "Новые ПЗ снова создаются, уведомления включены."
                ),
            )
        except Exception as e:
            logger.warning("Не удалось уведомить чат админов о снятии защиты: %s", e)

    return {"bots": len(get_user_bots(owner_id))}


def _build_welcome_kb(bot_data: dict) -> InlineKeyboardMarkup | None:
    """Строит инлайн-клавиатуру из сохранённых ссылок.

    Устойчива к битым/невалидным записям: невалидные ссылки пропускаются,
    а не роняют всю клавиатуру. У каждой кнопки может быть свой цвет
    (стиль): ``primary`` (синий), ``success`` (зелёный), ``danger`` (красный).
    """
    raw = bot_data.get("links", "[]")
    if isinstance(raw, str):
        try:
            links = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            links = []
    elif isinstance(raw, list):
        links = raw
    else:
        links = []

    if not links:
        return None

    rows: list[list[InlineKeyboardButton]] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        text = str(link.get("text", "")).strip()
        url = str(link.get("url", "")).strip()
        if not text or not url.startswith(("http://", "https://", "tg://")):
            continue
        style = str(link.get("style", "") or "").strip().lower()
        if style not in ("primary", "success", "danger", "link"):
            style = None
        rows.append([InlineKeyboardButton(text=text[:64], url=url, style=style)])
        if len(rows) >= 50:  # предохранитель от неадекватно большого списка
            break

    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _msg_bot_id(message: Message) -> int | None:
    """id бота, который прислал сообщение (None, если бот неизвестен/нет id)."""
    bot = getattr(message, "bot", None)
    return getattr(bot, "id", None)


async def _safe_answer(message: Message, text: str, reply_markup=None) -> bool:
    """Отправляет сообщение, переживая ошибки HTML-разметки.

    Если Telegram не смог распарсить HTML (в тексте владельца попался символ,
    который ломает разметку), сообщение НЕ должно потерять премиум-эмодзи:
    повторяем отправку с сущностями ``custom_emoji``, собранными из тегов
    ``<tg-emoji>``, и только в крайнем случае — совсем без форматирования.
    """
    try:
        chat_id = getattr(getattr(message, "chat", None), "id", 0) or 0
        bot = getattr(message, "bot", None)
        sent = await _send_with_gate(
            bot, chat_id, lambda: message.answer(text, reply_markup=reply_markup)
        )
        # Тихий сбой: разметка есть, а Telegram её вырезал (например, после
        # передачи прав на бота в @BotFather). Пишем в лог один раз на бота.
        check_premium_delivered(sent, text, _msg_bot_id(message))
        return True
    except TelegramBadRequest:
        logger.warning("HTML-отправка не удалась, отправляю без форматирования: %.120s", text)
        raw_text, entities = premium.markup_to_entities(text)
        if entities:
            try:
                await message.answer(raw_text, reply_markup=reply_markup,
                                     entities=entities, parse_mode=None)
                return True
            except Exception:
                logger.warning("Отправка сущностями тоже не удалась: %.120s", raw_text)
        try:
            await message.answer(text, reply_markup=reply_markup, parse_mode=None)
            return True
        except Exception:
            return False
    except Exception:
        return False


async def _send_welcome_rich(message: Message, welcome_rich: str,
                             extra_text: str = "",
                             reply_markup=None) -> bool:
    """Отправляет приветствие как rich-сообщение («статью»).

    False — если разметку/отправку не удалось выполнить (тогда вызывающий код
    отправит обычный текст). Работает на любой версии aiogram: запрос
    ``sendRichMessage`` собирается в ``services.rich``.
    """
    payload = rich_payload_from_json(welcome_rich)
    if not payload:
        return False

    if extra_text:
        payload["blocks"].append({"type": "footer", "text": extra_text})

    thread_id = None
    if getattr(message, "is_topic_message", False):
        thread_id = getattr(message, "message_thread_id", None)

    sent = await send_rich(message.bot, message.chat.id, payload,
                           message_thread_id=thread_id, reply_markup=reply_markup)
    return sent is not None


async def _send_welcome_photo(message: Message, photo_id: str, caption: str,
                              reply_markup=None) -> bool:
    """Отправляет приветствие с фото.

    Фото владелец загружал в диалог с основным ботом, поэтому файл
    перекачивается и заливается дочерним ботом заново (file_id чужого бота
    не работает).
    """
    data = await _cached_media_bytes(photo_id)
    if data is None:
        return False
    # Подпись к фото ограничена 1024 символами.
    cap = (caption or "").strip()[:1024]
    photo = BufferedInputFile(data, filename="welcome.jpg")
    try:
        await message.answer_photo(photo=photo, caption=cap or None,
                                   reply_markup=reply_markup)
        return True
    except Exception as e:
        logger.warning("Приветствие с фото не отправилось (%s)", e)
        return False


def _build_reply_kb(bot_data: dict) -> ReplyKeyboardMarkup | None:
    """Строит reply-клавиатуру дочернего бота из его настроек.

    Для анкетницы (anketa) reply-кнопки не используются — возвращает None.
    Для обычного бота используются сохранённые кнопки (или дефолт «сменить админа»).
    """
    if bot_data.get("bot_type") == "anketa":
        return None

    buttons = get_bot_keyboard_by_bot(bot_data["id"])
    rows: list[list[KeyboardButton]] = []
    for item in buttons:
        if not isinstance(item, dict):
            continue
        text = item.get("text", "").strip()
        if not text:
            continue
        rows.append([KeyboardButton(text=text)])

    if not rows:
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="сменить админа")]], resize_keyboard=True)

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _caption_kwargs(source_msg: Message, native: bool) -> dict[str, Any]:
    """Аргументы caption/caption_entities для копии сообщения.

    native=False — HTML-разметка (как раньше);
    native=True — обычный текст + родные сущности Telegram (премиум-эмодзи
    и форматирование сохраняются, даже если HTML не парсится).
    """
    if native:
        return {
            "caption": source_msg.caption or "",
            "caption_entities": source_msg.caption_entities,
        }
    return {"caption": source_msg.html_text or source_msg.caption or ""}


def _text_kwargs(source_msg: Message, native: bool) -> dict[str, Any]:
    """Аргументы text/entities для копии сообщения (см. _caption_kwargs)."""
    if native:
        return {"text": source_msg.text or "", "entities": source_msg.entities}
    return {"text": source_msg.html_text or source_msg.text or ""}


# Лимит Telegram на длину подписи к медиа. Сообщение с более длинной подписью
# Telegram не принимал ЦЕЛИКОМ (ошибка «message caption is too long»), из-за чего
# у части чатов пропадали сообщения с фото. Теперь длинная подпись уходит
# отдельным сообщением, а медиа — без неё.
CAPTION_LIMIT = 1024

# Признаки того, что Telegram отказал именно в МЕДИА, а не в чате: запрет медиа
# в этом чате, нечитаемое/битое фото, слишком большой файл. В таких случаях
# текст сообщения всё равно можно доставить, поэтому шлём его отдельной копией.
_MEDIA_ERROR_MARKERS = (
    "not enough rights to send",
    "no rights to send",
    "chat_send_photos_forbidden",
    "chat_send_videos_forbidden",
    "photo_invalid_dimensions",
    "image_process_failed",
    "wrong file identifier",
    "file is too big",
    "unsupported",
)


def _error_text(err: Exception) -> str:
    """Текст ошибки Telegram в нижнем регистре (для поиска по подстроке)."""
    return str(getattr(err, "message", "") or err).lower()


def _is_media_error(err: Exception) -> bool:
    """True, если Telegram отказал именно в отправке медиа."""
    text = _error_text(err)
    return any(marker in text for marker in _MEDIA_ERROR_MARKERS)


def _is_reply_error(err: Exception) -> bool:
    """True, если ошибка из-за ответа (reply) на недоступное сообщение."""
    text = _error_text(err)
    return ("replied message not found" in text
            or "reply message not found" in text
            or "message to be replied not found" in text)


def _drop_reply(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Копия аргументов отправки без reply (сообщение дойдёт без цитаты)."""
    return {key: value for key, value in kwargs.items() if key != "reply_to_message_id"}


def _source_media(source_msg: Message) -> tuple[str, str] | None:
    """Тип и file_id самого крупного медиа сообщения (или None)."""
    if source_msg.photo:
        return "photo", source_msg.photo[-1].file_id
    if source_msg.video:
        return "video", source_msg.video.file_id
    if source_msg.animation:
        return "animation", source_msg.animation.file_id
    if source_msg.document:
        return "document", source_msg.document.file_id
    if source_msg.audio:
        return "audio", source_msg.audio.file_id
    if source_msg.voice:
        return "voice", source_msg.voice.file_id
    return None


async def _send_media_call(bot: Bot, kwargs: dict[str, Any], kind: str, file_id: str,
                           cap: dict[str, Any], extra: dict[str, Any]) -> Message | None:
    """Отправляет одно медиа указанным способом (по типу медиа)."""
    if kind == "photo":
        return await bot.send_photo(**kwargs, photo=file_id, **cap, **extra)
    if kind == "video":
        return await bot.send_video(**kwargs, video=file_id, **cap, **extra)
    if kind == "animation":
        return await bot.send_animation(**kwargs, animation=file_id, **cap, **extra)
    if kind == "document":
        return await bot.send_document(**kwargs, document=file_id, **cap, **extra)
    if kind == "audio":
        return await bot.send_audio(**kwargs, audio=file_id, **cap, **extra)
    if kind == "voice":
        return await bot.send_voice(**kwargs, voice=file_id, **cap, **extra)
    return None


async def _send_caption_separately(source_msg: Message, bot: Bot, kwargs: dict[str, Any],
                                   caption: str, extra: dict[str, Any]) -> None:
    """Доставляет текст медиа-сообщения отдельным сообщением (с разметкой).

    Нужно, когда подпись не влезает в лимит Telegram: медиа уходит без неё, а
    текст — следом. Ошибку глотаем: медиа уже доставлено, а потеря текста не
    должна ломать всю отправку.
    """
    text_kwargs: dict[str, Any] = {"text": caption}
    if extra.get("parse_mode", "html") is None:
        # Родной режим: текст обычный, разметка — в сущностях.
        text_kwargs["entities"] = source_msg.caption_entities
    try:
        await bot.send_message(**kwargs, **text_kwargs, **extra)
    except Exception as e:
        logger.warning("Не удалось отправить текст медиа-сообщения отдельно: %s", e)


async def _send_media_copy(source_msg: Message, bot: Bot, kwargs: dict[str, Any],
                           cap: dict[str, Any], extra: dict[str, Any],
                           kind: str, file_id: str) -> Message | None:
    """Отправляет медиа-копию, не теряя длинную подпись."""
    caption = cap.get("caption") or ""
    if len(caption) > CAPTION_LIMIT:
        logger.info(
            "Подпись медиа-сообщения длиннее лимита Telegram (%d симв.) — "
            "отправляю медиа без подписи, а текст отдельным сообщением", len(caption),
        )
        sent = await _send_media_call(bot, kwargs, kind, file_id, {}, extra)
        await _send_caption_separately(source_msg, bot, kwargs, caption, extra)
        return sent
    return await _send_media_call(bot, kwargs, kind, file_id, cap, extra)


async def _copy_as_document(bot: Bot, kwargs: dict[str, Any], cap: dict[str, Any],
                            extra: dict[str, Any], file_id: str) -> Message | None:
    """Отправляет медиа документом (фолбэк для «нечитаемых» фото/анимаций)."""
    return await bot.send_document(**kwargs, document=file_id, **cap, **extra)


async def _copy_as_text_only(source_msg: Message, bot: Bot, kwargs: dict[str, Any],
                             cap: dict[str, Any], extra: dict[str, Any],
                             original: Exception) -> Message | None:
    """Последний шанс: доставляет текст медиа-сообщения, когда медиа запрещено.

    Если текста нет (например, фото без подписи) — пробрасывает исходную ошибку,
    чтобы вызывающий код обработал её как раньше (например, пересоздал топик).
    """
    caption = (cap.get("caption") or "").strip()
    if not caption:
        raise original
    logger.warning("Медиа отправить не удалось (%s) — доставляю текст сообщения",
                   original)
    if len(caption) > CAPTION_LIMIT:
        # Обрезанный HTML может оказаться невалидным — шлём простым текстом.
        return await bot.send_message(**kwargs, text=caption[:CAPTION_LIMIT],
                                      parse_mode=None)
    text_kwargs: dict[str, Any] = {"text": caption}
    if extra.get("parse_mode", "html") is None:
        text_kwargs["entities"] = source_msg.caption_entities
    return await bot.send_message(**kwargs, **text_kwargs, **extra)


async def _copy_message(source_msg: Message, bot: Bot, kwargs: dict[str, Any],
                        native: bool = False) -> Message | None:
    """Копирует сообщение юзера/админа в другой чат.

    Родной режим (native=True) отправляет текст и сущности как есть —
    это страховка на случай, если HTML-разметку не удалось распарсить:
    сообщение дойдёт с премиум-эмодзи и форматированием вместо того,
    чтобы потеряться целиком.

    Медиа-копия защищена каскадом фолбэков: длинная подпись уходит отдельным
    сообщением, «нечитаемое» фото пробуем отправить документом, а при запрете
    медиа в чате текст всё равно доходит. Без этого сообщения с фото терялись
    у части чатов, хотя обычный текст доставлялся нормально.
    """
    extra: dict[str, Any] = {"parse_mode": None} if native else {}
    cap = _caption_kwargs(source_msg, native)
    media = _source_media(source_msg)

    if media is not None:
        kind, file_id = media
        try:
            return await _send_media_copy(source_msg, bot, kwargs, cap, extra,
                                          kind, file_id)
        except TelegramBadRequest as e:
            if _is_reply_error(e) and "reply_to_message_id" in kwargs:
                # Сообщение, на которое отвечали, удалено: шлём без цитаты.
                logger.warning("Копия: ответ на недоступное сообщение — шлю без reply")
                return await _send_media_copy(source_msg, bot, _drop_reply(kwargs),
                                              cap, extra, kind, file_id)
            if kind in ("photo", "animation") and _is_media_error(e):
                # Фото, которое Telegram не смог обработать: доходит документом.
                try:
                    return await _copy_as_document(bot, kwargs, cap, extra, file_id)
                except TelegramBadRequest as doc_error:
                    logger.warning("Копия: не ушло ни фото, ни документом (%s)",
                                   doc_error)
            if _is_media_error(e):
                return await _copy_as_text_only(source_msg, bot, kwargs, cap, extra, e)
            raise

    if source_msg.sticker:
        return await bot.send_sticker(**kwargs, sticker=source_msg.sticker.file_id)
    if source_msg.video_note:
        return await bot.send_video_note(**kwargs, video_note=source_msg.video_note.file_id)

    text_kw = _text_kwargs(source_msg, native)
    if not text_kw["text"]:
        return None
    try:
        return await bot.send_message(**kwargs, **text_kw, **extra)
    except TelegramBadRequest as e:
        if _is_reply_error(e) and "reply_to_message_id" in kwargs:
            logger.warning("Копия: ответ на недоступное сообщение — шлю без reply")
            return await bot.send_message(**_drop_reply(kwargs), **text_kw, **extra)
        raise


async def _copy_message_smart(source_msg: Message, bot: Bot,
                              kwargs: dict[str, Any]) -> Message | None:
    """Копирует сообщение: сначала HTML, при ошибке разметки — родными сущностями."""
    try:
        return await _copy_message(source_msg, bot, kwargs, native=False)
    except TelegramBadRequest as html_error:
        logger.warning("Копия сообщения HTML-разметкой не удалась (%s) — пробую сущностями",
                       html_error)
        try:
            return await _copy_message(source_msg, bot, kwargs, native=True)
        except TelegramBadRequest:
            # Пробрасываем ИСХОДНУЮ ошибку: по её тексту вызывающий код понимает,
            # что топик удалён (message thread not found) и его нужно пересоздать.
            raise html_error from None


async def _send_to_topic(source_msg: Message, bot: Bot,
                         group_chat_id: int, topic_id: int,
                         reply_to: int | None = None) -> Message | None:
    kwargs: dict[str, Any] = {"chat_id": group_chat_id, "message_thread_id": topic_id}
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to

    try:
        return await _copy_message_smart(source_msg, bot, kwargs)
    except Exception as e:
        # НЕ глотаем ошибку: вызывающий код (_send_to_topic_retry) сам решает,
        # повторить отправку на временном сбое или пересоздать удалённый топик.
        logger.error("Ошибка отправки в топик: %s", e)
        raise


async def _send_to_topic_retry(source_msg: Message, bot: Bot,
                               group_chat_id: int, topic_id: int,
                               reply_to: int | None = None) -> tuple[Message | None, bool]:
    """Отправляет сообщение в топик, переживая flood-control (429).

    Возвращает (sent, thread_not_found):
      • sent             — доставленное сообщение или None;
      • thread_not_found — True, только если топик реально удалён/устарел
        (такие ошибки ретраить бессмысленно — топик надо пересоздавать).

    Flood-wait обрабатывает «шлюз» чата (``_send_with_gate``): ждём ровно
    столько, сколько попросил Telegram, и повторяем — сообщения НЕ теряются
    (раньше пауза обрезалась до 5 секунд и после 3 попыток сообщение пропадало).
    """
    try:
        sent = await _send_with_gate(
            bot, group_chat_id,
            lambda: _send_to_topic(source_msg, bot, group_chat_id, topic_id, reply_to),
        )
        if sent is not None and not _is_message_in_topic(sent, topic_id):
            # Топик удалили вручную, но Telegram принял его message_thread_id и
            # опубликовал сообщение НЕ в нём (обычно — в «General»). Считаем это
            # «топик не найден»: убираем залётное сообщение, а вызывающий код
            # сотрёт старую запись и создаст свежий топик, как новому ПЗ.
            logger.warning(
                "Сообщение для топика %s ушло вне топика (message_thread_id=%s) — "
                "топик удалён, пересоздаю ПЗ.",
                topic_id, getattr(sent, "message_thread_id", None),
            )
            await _delete_stray_message(bot, group_chat_id, sent.message_id)
            return None, True
        return sent, False
    except TelegramBadRequest as e:
        if _is_thread_not_found(e):
            logger.warning("Топик %s не найден (thread not found) — требуется пересоздание.",
                           topic_id)
            return None, True
        logger.error("Ошибка отправки в топик %s: %s", topic_id, e)
    except Exception as e:
        logger.error("Не удалось отправить в топик %s: %s", topic_id, e)
    return None, False


async def _send_to_user(source_msg: Message, bot: Bot,
                        chat_id: int, reply_to: int | None = None,
                        bot_id: int | None = None) -> Message | None:
    kwargs: dict[str, Any] = {"chat_id": chat_id}
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to

    try:
        return await _send_with_gate(
            bot, chat_id,
            lambda: _copy_message_smart(source_msg, bot, kwargs),
        )
    except TelegramForbiddenError:
        # Юзер заблокировал/забанил бота — обрабатываем топик
        if bot_id:
            await _handle_user_blocked(bot, bot_id, chat_id)
        return None
    except Exception as e:
        logger.error("Ошибка отправки юзеру: %s", e)
    return None


async def _handle_user_blocked(bot: Bot, bot_id: int, user_chat_id: int) -> None:
    """Юзер забанил бота: помечаем заблокированным, переименовываем и закрываем топик,
    уведомляем админа в этом же топике."""
    try:
        mark_user_blocked(bot_id, user_chat_id)
    except Exception:
        pass

    topic = get_topic_by_user(bot_id, user_chat_id)
    if not topic:
        return

    g_id = topic["group_chat_id"]
    t_id = topic["topic_id"]
    reset_topic_admin(bot_id, t_id, g_id)
    # ПЗ пользователя, заблокировавшего бота, скрываем из списков ПЗ.
    delete_topic_record(bot_id, user_chat_id)

    try:
        await bot.edit_forum_topic(chat_id=g_id, message_thread_id=t_id, name="🚫 забанил бота")
    except Exception:
        pass

    try:
        await bot.send_message(
            chat_id=g_id, message_thread_id=t_id,
            text=f"🚫 Пользователь <code>{user_chat_id}</code> забанил бота.\nТопик закрыт."
        )
    except Exception:
        pass

    try:
        await bot.close_forum_topic(chat_id=g_id, message_thread_id=t_id)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  Иконки тем ПЗ и реакции (обычные + премиум)
# ═══════════════════════════════════════════════════════════════

# Иконки-эмодзи для новых топиков: список отдаёт Telegram
# (getForumTopicIconStickers). Раздаём их по кругу, чтобы соседние ПЗ
# получали РАЗНЫЕ иконки, а не одну и ту же.
_TOPIC_ICONS: list[str] = []
_TOPIC_ICON_INDEX = 0

# Разрешённые цвета иконки темы (если эмодзи получить не удалось).
_TOPIC_COLORS = [0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F]


async def _next_topic_icon(bot_obj: Bot) -> tuple[str | None, int | None]:
    """Иконка для новой темы ПЗ: (custom_emoji_id, icon_color).

    Эмодзи-иконки спрашиваем у Telegram один раз и кэшируем. Если получить
    не удалось — отдаём случайный цвет из разрешённого набора, чтобы темы
    всё равно отличались друг от друга.
    """
    global _TOPIC_ICONS, _TOPIC_ICON_INDEX
    if not _TOPIC_ICONS:
        try:
            stickers = await bot_obj.get_forum_topic_icon_stickers()
            _TOPIC_ICONS = [s.custom_emoji_id for s in stickers if s.custom_emoji_id]
        except Exception as e:
            logger.debug("Не удалось получить иконки тем: %s", e)
            _TOPIC_ICONS = []
    if _TOPIC_ICONS:
        icon_id = _TOPIC_ICONS[_TOPIC_ICON_INDEX % len(_TOPIC_ICONS)]
        _TOPIC_ICON_INDEX += 1
        return icon_id, None
    return None, random.choice(_TOPIC_COLORS)


def _emoji_for_custom_id(custom_emoji_id: str) -> str | None:
    """Обычный эмодзи для премиум-реакции (по словарю премиум-эмодзи)."""
    if not custom_emoji_id:
        return None
    for emoji, cid in get_emoji_map().items():
        if cid == custom_emoji_id:
            return emoji
    return None


def _mirror_reactions(reactions: list) -> tuple[list | None, str | None]:
    """Готовит реакцию для повторения на парном сообщении.

    Возвращает пару (реакции для setMessageReaction, id премиум-эмодзи).

    * пустой список — реакцию сняли, значит и на копии её надо снять;
    * ``None`` — повторять нечего (например, платная реакция: боты их не ставят);
    * бот без премиума может поставить только одну реакцию, поэтому берём первую.
    """
    if not reactions:
        return [], None
    for reaction in reactions:
        if isinstance(reaction, ReactionTypeEmoji):
            return [reaction], None
        if isinstance(reaction, ReactionTypeCustomEmoji):
            custom_id = str(getattr(reaction, "custom_emoji_id", "") or "")
            return [reaction], (custom_id or None)
    return None, None


# ── Время работы: автоответ ПЗ вне рабочего времени ───────────────────
# Ключ (bot_id, user_chat_id) → когда последний раз слали автоответ этому ПЗ.
_work_hours_replied: dict[tuple[int, int], float] = {}
# Не чаще раза в час на пользователя: иначе на серию сообщений прилетела бы
# серия одинаковых автоответов.
_WORK_HOURS_REPEAT = 3600.0


async def _send_work_hours_reply(bot_obj: Bot, owner_id: int, chat_id: int) -> None:
    """Отвечает ПЗ в нерабочее время: бот отдыхает, свободный админ ответит.

    Сообщение настраивается владельцем (раздел «🕐 Время работы» в профиле),
    вместе с фото и премиум-эмодзи. Само обращение ПЗ при этом не теряется —
    оно всё равно уходит в топик.
    """
    settings = get_work_hours(owner_id)
    start = str(settings.get("start") or DEFAULT_WORK_START)
    end = str(settings.get("end") or DEFAULT_WORK_END)
    raw = str(settings.get("msg_text") or DEFAULT_WORK_MESSAGE)
    text = raw.replace("{start}", start).replace("{end}", end)

    entities = None
    try:
        from handlers.hours import build_entities
        entities = build_entities(str(settings.get("msg_entities") or "[]"))
    except Exception as e:  # премиум-эмодзи — украшение, ошибка тут не критична
        logger.debug("Не удалось восстановить эмодзи ответа о времени работы: %s", e)

    photo = str(settings.get("msg_photo") or "")
    try:
        if photo:
            # file_id из лички владельца дочернему боту не подходит: тожимся через
            # основной бот, у которого файл был принят.
            if _MAIN_BOT is not None:
                buffer = await _cached_media_bytes(photo)
                if buffer is not None:
                    await bot_obj.send_photo(
                        chat_id=chat_id,
                        photo=BufferedInputFile(buffer, filename="offline.jpg"),
                        caption=text, caption_entities=entities, parse_mode=None,
                    )
                    return
            await bot_obj.send_photo(chat_id=chat_id, photo=photo, caption=text,
                                     caption_entities=entities, parse_mode=None)
            return
        await bot_obj.send_message(chat_id=chat_id, text=text, entities=entities,
                                   parse_mode=None)
    except Exception as e:
        logger.warning("Не удалось отправить ответ о времени работы: %s", e)


def _make_child_dp(bot_data: dict, bot_obj: Bot) -> Dispatcher:
    # Устойчивый диспетчер: конфликт токена (вебхук/второй экземпляр бота) и
    # мёртвый токен больше не превращаются в бесконечные «tryings = 14000» в
    # логе — см. services/polling.py.
    child_dp = ResilientDispatcher()
    bot_id = bot_data["id"]

    # Ошибки обработчиков пишем в журнал по этому боту: пользователь сможет
    # прислать их в поддержку, а владелец платформы — посмотреть в админке.
    async def _log_child_error(event: ErrorEvent) -> bool:
        try:
            update = getattr(event, "update", None)
            save_bot_error(
                bot_id,
                type(event.exception).__name__ + ": " + str(event.exception)[:200],
                detail=str(event.exception),
                owner_id=int(bot_data.get("owner_id") or 0),
                source=getattr(update, "event_type", "") if update else "",
            )
        except Exception as log_error:  # журнал не должен ломать обработку
            logger.debug("Не удалось записать ошибку бота %s: %s", bot_id, log_error)
        logger.error("Бот %s: ошибка обработчика: %s", bot_id, event.exception,
                     exc_info=event.exception)
        return True

    child_dp.errors.register(_log_child_error)

    sticker_counts: dict[int, list[float]] = defaultdict(list)
    sticker_warnings: dict[int, bool] = defaultdict(bool)
    last_message_time: dict[int, float] = {}

    # Анти-спам /start: повторные /start в течение интервала игнорируются,
    # чтобы наплыв команд не ронял бота и не плодил дубликаты приветствий.
    last_start_time: dict[int, float] = {}
    # Локи на пользователя: сообщения одного ПЗ обрабатываются строго по очереди,
    # благодаря чему топик создаётся ровно один раз даже при спаме сразу после
    # нажатия /start, и каждое сообщение попадает в один и тот же топик.
    user_locks: dict[tuple[int, int], asyncio.Lock] = {}

    def _get_user_lock(user_chat_id: int) -> asyncio.Lock:
        key = (bot_id, user_chat_id)
        lock = user_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            user_locks[key] = lock
        return lock

    # ═══════════════ /start в ЛС ═══════════════

    @child_dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
    async def child_start(message: Message) -> None:
        """/start дочернего бота с защитой от спама и от падений."""
        try:
            uid = msg_uid(message)
            now = time.monotonic()
            prev = last_start_time.get(uid, 0.0)
            if now - prev < CHILD_START_MIN_INTERVAL:
                logger.debug("Пропускаю повторный /start от %s (анти-спам)", uid)
                return
            last_start_time[uid] = now
            await _do_child_start(message)
        except Exception as e:
            logger.exception("Ошибка в /start дочернего бота %s: %s", bot_id, e)

    async def _do_child_start(message: Message) -> None:
        if is_user_banned(bot_id, msg_uid(message)):
            return

        add_child_user(
            bot_id, msg_uid(message),
            msg_username(message) or "",
            msg_firstname(message) or ""
        )
        add_stat(bot_id, "message_in")

        fresh = get_bot_by_id_any_owner(bot_id)
        if not fresh:
            return

        welcome = fresh.get("welcome_text", "") or ""

        # Если премиум-эмодзи в приветствии потерял разметку (владелец вставил
        # эмодзи копированием) — мягко возвращаем её по словарю, см.
        # services/premium_emoji.py. Пустое приветствие НЕ восстанавливаем.
        if welcome.strip() and not premium.has_premium_markup(welcome):
            if repair_premium_emoji_for_bot(bot_id):
                fresh = get_bot_by_id_any_owner(bot_id) or fresh
                welcome = fresh.get("welcome_text", "") or welcome

        # Приветствие не задано — показываем базовое, зашитое в боте.
        if not welcome.strip():
            welcome = BASE_WELCOME

        # В анонимном режиме в конце приветствия добавляем плашку.
        if is_bot_anonymous(bot_id):
            welcome = f"{welcome}\n\n🕶 <b>Анонимный режим включён.</b>"

        # В самом низу любого приветствия — плашка-кредит.
        welcome = f"{welcome}{BOT_CREDIT}"

        # Инлайн-кнопки (ссылки) прикрепляем ПРЯМО к приветствию.
        # В одном сообщении reply- и inline-клавиатуру показать нельзя, поэтому
        # приветствие отправляется РОВНО ОДИН раз, а reply-клавиатура (если она
        # есть у стандартного бота) показывается отдельным коротким сообщением.
        welcome_kb = _build_welcome_kb(fresh)
        reply_kb = _build_reply_kb(fresh)

        # ── Красивое приветствие: rich-«статья» или фото ──
        # Если владелец оформил приветствие как rich-сообщение (статью) или
        # прикрепил фото — отправляем именно его. Текст-подпись/плашки
        # добавляем к фото или отдельным футером статьи.
        welcome_rich = str(fresh.get("welcome_rich") or "").strip()
        welcome_photo = str(fresh.get("welcome_photo") or "").strip()
        sent_special = False
        if welcome_rich:
            rich_extra = BOT_CREDIT.strip()
            if is_bot_anonymous(bot_id):
                rich_extra = f"🕶 Анонимный режим включён.\n{rich_extra}"
            sent_special = await _send_welcome_rich(message, welcome_rich,
                                                    rich_extra, welcome_kb)
        elif welcome_photo:
            sent_special = await _send_welcome_photo(message, welcome_photo,
                                                     welcome, welcome_kb)

        if sent_special:
            # Приветствие ушло статьёй/фото — reply-клавиатуру показываем отдельно.
            if reply_kb:
                await _safe_answer(message, "👇 Меню действий — кнопкой ниже:", reply_kb)
            add_stat(bot_id, "message_out")
            return

        if welcome_kb and reply_kb:
            # Сначала приветствие с инлайн-кнопками, затем подсказка с reply.
            if not await _safe_answer(message, welcome, welcome_kb):
                await _safe_answer(message, welcome)
            await _safe_answer(message, "👇 Меню действий — кнопкой ниже:", reply_kb)
        elif welcome_kb:
            if not await _safe_answer(message, welcome, welcome_kb):
                await _safe_answer(message, welcome)
        elif reply_kb:
            await _safe_answer(message, welcome, reply_kb)
        else:
            await _safe_answer(message, welcome)
        add_stat(bot_id, "message_out")

    # ═══════════════ /connect в группе ═══════════════

    @child_dp.message(Command("connect"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
    async def cmd_connect_group(message: Message) -> None:
        chat = message.chat
        is_forum = bool(getattr(chat, "is_forum", False))
        if not is_forum:
            await message.answer(
                "⚠️ <b>В этом чате не включены темы.</b>\n\n"
                "Включи «Темы» в настройках чата, после чего напиши "
                "<code>/connect</code> ещё раз — тогда бот начнёт работу."
            )
            return
        set_feedback_chat(bot_id, chat.id)
        await message.answer(
            f"✅ Чат <b>{chat.title}</b> подключён.\n"
            f"Сообщения от пользователей будут создавать топики здесь."
        )

    # ═══════════════ Реакции: обычные и премиум ═══════════════

    @child_dp.message_reaction()
    async def child_reaction(event: MessageReactionUpdated) -> None:
        """Повторяет реакцию на парном сообщении, чтобы её было ВИДНО.

        Реакция, поставленная в топике ПЗ, ставится на то же сообщение в личке
        пользователя (и наоборот) через ``setMessageReaction`` — собеседник
        видит реакцию прямо на сообщении, никаких отдельных сообщений-уведомлений
        не приходит.

        Премиум-реакцию пробуем повторить как есть; если Telegram её не
        разрешает (нужна премиум-подписка владельца бота или разрешение админов
        чата) — повторяем обычный эмодзи из словаря премиум-эмодзи, а если
        эмодзи неизвестен — просто ничего не делаем.
        """
        try:
            chat = event.chat
            if chat.type == ChatType.PRIVATE:
                # Реакция в личке → повторяем на сообщении в топике ПЗ.
                pair = get_feedback_msg_by_user_msg(bot_id, chat.id, event.message_id)
                if not pair:
                    return
                target_chat = int(pair["group_chat_id"])
                target_msg = int(pair.get("group_msg_id") or 0)
            else:
                # Реакция в топике → повторяем на сообщении в личке пользователя.
                pair = get_feedback_msg_by_group_msg(bot_id, chat.id, event.message_id)
                if not pair:
                    return
                target_chat = int(pair["user_chat_id"])
                target_msg = int(pair.get("user_msg_id") or 0)

            if not target_msg:
                return

            reactions, custom_id = _mirror_reactions(list(event.new_reaction or []))
            if reactions is None:
                return

            try:
                await bot_obj.set_message_reaction(
                    chat_id=target_chat, message_id=target_msg, reaction=reactions,
                )
            except TelegramBadRequest:
                # Премиум-реакцию поставить не получилось — пробуем обычный
                # эмодзи, который ей соответствует.
                emoji = _emoji_for_custom_id(custom_id or "")
                if not emoji:
                    return
                await bot_obj.set_message_reaction(
                    chat_id=target_chat, message_id=target_msg,
                    reaction=[ReactionTypeEmoji(emoji=emoji)],
                )
        except Exception as e:
            logger.debug("Не удалось повторить реакцию бота %s: %s", bot_id, e)

    # ═══════════════ Автоподключение при добавлении в группу ═══════════════

    @child_dp.my_chat_member()
    async def on_child_my_chat_member(event: ChatMemberUpdated) -> None:
        """Бот сам подключается, когда его добавили в рабочий чат с темами.

        Правила безопасности:
          • подключение происходит ТОЛЬКО если чат ещё не привязан
            (get_feedback_chat вернул None) — уже подключённых ботов
            мы НИКОГДА не отключаем и не переподключаем;
          • если бот УЖЕ привязан к ДРУГОМУ чату, а его добавили в новый —
            сообщаем об этом в новом чате, выходим из него и уведомляем
            владельца: его бота пытались добавить в чужой чат;
          • подключаемся только к чату с включёнными темами (is_forum);
          • если бота добавили в «обычный» чат без тем — подсказываем,
            как включить темы, и ничего не трогаем.
        """
        chat = event.chat
        if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return

        old_status = getattr(event.old_chat_member, "status", "")
        new_status = getattr(event.new_chat_member, "status", "")
        was_member = old_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
        is_member_now = new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
        if was_member or not is_member_now:
            # Это удаление/выход или повышение уже добавленного бота.
            return

        title = chat.title or f"чат {chat.id}"
        existing_chat = get_feedback_chat(bot_id)

        # ─ ЗАЩИТА: бот уже привязан к другому чату ─
        if existing_chat is not None and int(existing_chat) != int(chat.id):
            logger.warning(
                "Бот %s уже привязан к чату %s — выхожу из чужого чата %s (%s)",
                bot_id, existing_chat, chat.id, title,
            )
            try:
                await bot_obj.send_message(
                    chat.id,
                    "🚫 <b>Этот бот уже привязан к другому чату.</b>\n\n"
                    "Один бот может обслуживать только один рабочий чат. "
                    "Я покидаю этот чат. Если нужно перенести бота — сначала "
                    "отвяжите его от прежнего чата в панели владельца.",
                )
            except Exception as e:
                logger.warning("Не удалось предупредить чужой чат: %s", e)
            try:
                await bot_obj.leave_chat(chat.id)
            except Exception as e:
                logger.warning("Не удалось выйти из чужого чата %s: %s", chat.id, e)

            await notify_owner(
                bot_id,
                "🚨 <b>Внимание: вашего бота пытались добавить в чужой чат!</b>\n\n"
                f"🤖 Бот: <b>{_bot_name_of(bot_id)}</b>\n"
                f"📎 Чужой чат: <b>{title}</b>\n"
                f"🆔 ID чата: <code>{chat.id}</code>\n"
                f"👤 Добавил: <code>{event.from_user.id if event.from_user else 'неизвестно'}</code>\n\n"
                f"✅ Бот уже привязан к чату <code>{existing_chat}</code> и "
                f"<b>вышел</b> из нового. Рабочий чат не изменён.",
            )
            return

        is_forum = bool(getattr(chat, "is_forum", False))
        if not is_forum:
            try:
                await bot_obj.send_message(
                    chat.id,
                    "👋 Привет! Я подключусь к этому чату, как только ты "
                    "<b>включишь темы</b>:\n"
                    "«Управление чатом → ⋮ → Включить темы».\n"
                    "После этого добавь меня заново или напиши <code>/connect</code>.",
                )
            except Exception as e:
                logger.warning("Не удалось отправить подсказку про темы: %s", e)
            return

        set_feedback_chat(bot_id, chat.id)
        logger.info("Бот %s автоподключён к чату %s (%s)", bot_id, chat.id, title)
        try:
            await bot_obj.send_message(
                chat.id,
                f"✅ <b>Чат <code>{title}</code> подключён автоматически!</b>\n"
                f"Сообщения от пользователей будут создавать топики здесь.\n\n"
                f"На всякий случай: команда <code>/connect</code> в теме General "
                f"тоже сработает — она нужна, если бот вдруг «потерял» чат.",
            )
        except Exception as e:
            logger.warning("Не удалось поздравить с подключением: %s", e)

    # ═══════════════ /ban в топике ═══════════════

    @child_dp.message(
        Command("ban"),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id")
    )
    async def cmd_ban(message: Message, thread_id: int) -> None:
        group_chat_id = message.chat.id
        topic = get_topic_by_topic_id(bot_id, group_chat_id, thread_id)
        if not topic:
            return

        user_chat_id = topic["user_chat_id"]
        ban_user(bot_id, user_chat_id)

        try:
            await bot_obj.send_message(
                chat_id=user_chat_id,
                text="🚫 Вас заблокировали в данном боте навсегда. Всего доброго."
            )
        except Exception as e:
            logger.warning("Не удалось отправить бан-уведомление: %s", e)

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=thread_id, name="🚫 забанен"
            )
        except Exception:
            pass

        try:
            await bot_obj.close_forum_topic(
                chat_id=group_chat_id, message_thread_id=thread_id
            )
        except Exception:
            pass

        reset_topic_admin(bot_id, thread_id, group_chat_id)
        # Забаненный/закрытый ПЗ не должен показываться в списках ПЗ.
        delete_topic_record(bot_id, user_chat_id)
        # Но запоминаем маппинг «топик → юзер», чтобы /unban из топика работал.
        save_banned_topic(bot_id, user_chat_id, group_chat_id, thread_id)
        await message.answer(f"✅ Пользователь <code>{user_chat_id}</code> заблокирован.")

    # ═══════════════ /unban в топике ═══════════════

    @child_dp.message(
        Command("unban"),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id")
    )
    async def cmd_unban(message: Message, thread_id: int) -> None:
        group_chat_id = message.chat.id
        topic = get_topic_by_topic_id(bot_id, group_chat_id, thread_id)
        # При бане запись ПЗ удаляется, поэтому ищем юзера по маппингу забаненных топиков.
        if topic:
            user_chat_id = topic["user_chat_id"]
        else:
            user_chat_id = get_banned_topic_user(bot_id, group_chat_id, thread_id)

        if user_chat_id is None:
            await message.answer("⚠️ Не удалось найти ПЗ для этого топика.")
            return

        # Снимаем бан
        success = unban_user(bot_id, user_chat_id)
        if not success:
            await message.answer("⚠️ Пользователь не найден в базе.")
            return

        # Убираем флаг «забаненный топик» и восстанавливаем запись ПЗ.
        delete_banned_topic(bot_id, group_chat_id, thread_id)
        create_topic_record(bot_id, user_chat_id, group_chat_id, thread_id)

        # Отправляем юзеру сообщение
        try:
            await bot_obj.send_message(
                chat_id=user_chat_id,
                text="✅ Вас разбанили в боте, можете снова писать."
            )
        except Exception as e:
            logger.warning("Не удалось отправить разбан-уведомление: %s", e)

        # Сбрасываем админа
        reset_topic_admin(bot_id, thread_id, group_chat_id)

        # Переименовываем топик
        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id,
                message_thread_id=thread_id,
                name="⏳ без админа"
            )
        except Exception:
            pass

        # Открываем топик если был закрыт
        try:
            await bot_obj.reopen_forum_topic(
                chat_id=group_chat_id,
                message_thread_id=thread_id
            )
        except Exception:
            pass

        # Отправляем кнопку "Я беру"
        await bot_obj.send_message(
            chat_id=group_chat_id,
            message_thread_id=thread_id,
            text=f"🔓 Пользователь <code>{user_chat_id}</code> разбанен.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✋ Я беру",
                    callback_data=f"take_user_{thread_id}_{group_chat_id}"
                )]
            ])
        )

        await message.answer(f"✅ Пользователь <code>{user_chat_id}</code> разбанен.")

    # ═══════════════ /otkaz в топике ═══════════════

    @child_dp.message(
        Command("otkaz"),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id")
    )
    async def cmd_otkaz(message: Message, thread_id: int) -> None:
        group_chat_id = message.chat.id
        topic = get_topic_by_topic_id(bot_id, group_chat_id, thread_id)
        if not topic:
            return

        user_chat_id = topic["user_chat_id"]
        reset_topic_admin(bot_id, thread_id, group_chat_id)
        try:
            await _notify_admin_change(bot_id, group_chat_id, thread_id)
        except Exception as e:
            logger.warning("Не удалось уведомить о смене админа: %s", e)

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=thread_id, name="🔄 смена админа"
            )
        except Exception:
            pass

        await bot_obj.send_message(
            chat_id=user_chat_id,
            text="⚠️ Ваш администратор отказался от вас.\nПодобрать нового?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Найти админа",
                                       callback_data=f"find_admin_{thread_id}_{group_chat_id}")]
            ])
        )
        await message.answer("✅ Пользователю отправлено уведомление.")

# ═══════════════ Callback: пользователь нажал «подобрать нового» ═══════════════

    @child_dp.callback_query(F.data.startswith("picknew_"))
    async def cb_pick_new(callback: CallbackQuery) -> None:
        parts = cb_data(callback).split("_")
        thread_id = int(parts[1])
        group_chat_id = int(parts[2])

        topic = get_topic_by_topic_id(bot_id, group_chat_id, thread_id)
        if not topic:
            await callback.answer("❌ Топик не найден")
            return

        reset_topic_admin(bot_id, thread_id, group_chat_id)
        try:
            await _notify_admin_change(bot_id, group_chat_id, thread_id)
        except Exception as e:
            logger.warning("Не удалось уведомить о смене админа: %s", e)

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=thread_id, name="🔄 смена админа"
            )
        except Exception:
            pass

        await bot_obj.send_message(
            chat_id=group_chat_id, message_thread_id=thread_id,
            text="🔔 Пользователь запросил нового админа!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✋ Я беру",
                                       callback_data=f"take_user_{thread_id}_{group_chat_id}")]
            ])
        )

        try:
            await bot_obj.send_message(
                chat_id=topic["user_chat_id"],
                text="👀 Запрос на нового администратора отправлен. Скоро с вами свяжутся.",
            )
        except Exception:
            pass

        await try_edit(callback.message, "✅ Запрос нового админа отправлен.")
        await callback.answer()

    # ═══════════════ /smena — смена админа без подтверждения (для админа) ═══════════════
    # ═══════════════ /smena — смена админа без подтверждения (для админа) ═══════════════

    @child_dp.message(
        Command("smena"),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id")
    )
    async def cmd_smena(message: Message, thread_id: int) -> None:
        group_chat_id = message.chat.id
        topic = get_topic_by_topic_id(bot_id, group_chat_id, thread_id)
        if not topic:
            return

        user_chat_id = topic["user_chat_id"]
        reset_topic_admin(bot_id, thread_id, group_chat_id)
        try:
            await _notify_admin_change(bot_id, group_chat_id, thread_id)
        except Exception as e:
            logger.warning("Не удалось уведомить о смене админа: %s", e)

        # Уведомляем самого юзера, что его админа меняют
        try:
            await bot_obj.send_message(
                chat_id=user_chat_id,
                text="🔄 Вашего администратора меняют. С вами скоро свяжется новый админ."
            )
        except Exception:
            pass

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=thread_id, name="🔄 смена админа"
            )
        except Exception:
            pass

        await bot_obj.send_message(
            chat_id=group_chat_id, message_thread_id=thread_id,
            text="🔔 Пользователь запросил смену админа!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✋ Я беру",
                                       callback_data=f"take_user_{thread_id}_{group_chat_id}")]
            ])
        )
        await message.answer("✅ Запрос на смену админа отправлен.")


    # ═══════════════ Callback: "я беру" ═══════════════

    @child_dp.callback_query(F.data.startswith("take_user_"))
    async def cb_take_user(callback: CallbackQuery) -> None:
        parts = cb_data(callback).split("_")
        topic_id = int(parts[2])
        group_chat_id = int(parts[3])

        # Владелец чата — тот, кому принадлежит бот. После «передачи прав»
        # этим владельцем становится новый юзер, поэтому админа ищем именно у него.
        owner_id = get_bot_owner(bot_id) or 0
        admin = get_admin_by_user_id(owner_id, cb_uid(callback))
        if not admin and owner_id != 0:
            # Легаси-записи (до введения owner_id) лежат с owner_id = 0.
            admin = get_admin_by_user_id(0, cb_uid(callback))

        if admin:
            tag = admin["tag"]
        elif owner_id == cb_uid(callback):
            # Владелец/новый владелец, у которого нет записи админа — используем его
            # ник как тег, чтобы топик назывался админским тегом, а не личным именем.
            if getattr(callback.from_user, "username", None):
                tag = f"@{cb_username(callback)}"
            elif getattr(callback.from_user, "first_name", None):
                tag = cb_firstname(callback)
            else:
                tag = str(cb_uid(callback))
            logger.debug("Владелец %s взял ПЗ без тега админа, используем ник как тег", owner_id)
        else:
            tag = cb_firstname(callback) or str(cb_uid(callback))

        # Назначаем и получаем инфу
        result = assign_admin_to_topic(bot_id, topic_id, group_chat_id, cb_uid(callback), tag)

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=topic_id, name=f"#{tag}"
            )
        except Exception as e:
            logger.warning("Не удалось переименовать топик: %s", e)

        if admin:
            add_admin_message(bot_id, cb_uid(callback), "action")

        # Только редактируем текст — НЕ удаляем инфу о юзере
        try:
            if callback.message:
                rm = getattr(callback.message, "reply_markup", None)
                if rm:
                    original_text = getattr(callback.message, "html_text", None) \
                        or getattr(callback.message, "text", None) or ""
                    new_text = f"{original_text}\n\n✅ Взял: <b>#{tag}</b>"
                    await try_edit(callback.message, new_text, reply_markup=None)
        except Exception:
            pass

        # Если это была смена админа — уведомляем
        if result["is_change"]:
            try:
                await bot_obj.send_message(
                    chat_id=group_chat_id,
                    message_thread_id=topic_id,
                    text=f"🔄 Админ сменился на <b>#{tag}</b>"
                )
            except Exception:
                pass

        await callback.answer(f"Ты взял пользователя. Тег: #{tag}")

    # ═══════════════ Callback: "найти админа" ═══════════════

    @child_dp.callback_query(F.data.startswith("find_admin_"))
    async def cb_find_admin(callback: CallbackQuery) -> None:
        parts = cb_data(callback).split("_")
        topic_id = int(parts[2])
        group_chat_id = int(parts[3])

        reset_topic_admin(bot_id, topic_id, group_chat_id)

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=topic_id, name="⏳ без админа"
            )
        except Exception:
            pass

        await bot_obj.send_message(
            chat_id=group_chat_id, message_thread_id=topic_id,
            text="🔔 Пользователь запросил нового админа!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✋ Я беру",
                                       callback_data=f"take_user_{topic_id}_{group_chat_id}")]
            ])
        )

        await try_edit(callback.message, "✅ Запрос отправлен.")
        await callback.answer()

    # ═══════════════ Callback: подтверждение смены ═══════════════

    @child_dp.callback_query(F.data.startswith("confirm_change_"))
    async def cb_confirm_change(callback: CallbackQuery) -> None:
        parts = cb_data(callback).split("_")
        answer = parts[2]
        topic_id = int(parts[3])
        group_chat_id = int(parts[4])
        topic = get_topic_by_topic_id(bot_id, group_chat_id, topic_id)

        if answer == "yes":
            # Лимит смен в сутки: считаем здесь же, чтобы не пустить сверх лимита.
            if topic is not None:
                if admin_changes_left(bot_id, topic["user_chat_id"]) == 0:
                    await try_edit(callback.message,
                                   "⏳ Смен на сегодня не осталось — попробуй завтра.")
                    await callback.answer("Лимит смен исчерпан", show_alert=True)
                    return
                log_admin_change(bot_id, topic["user_chat_id"])

            reset_topic_admin(bot_id, topic_id, group_chat_id)
            try:
                await _notify_admin_change(bot_id, group_chat_id, topic_id)
            except Exception as e:
                logger.warning("Не удалось уведомить о смене админа: %s", e)

            try:
                await bot_obj.edit_forum_topic(
                    chat_id=group_chat_id, message_thread_id=topic_id, name="🔄 смена админа"
                )
            except Exception:
                pass

            await bot_obj.send_message(
                chat_id=group_chat_id, message_thread_id=topic_id,
                text="🔔 Пользователь запросил смену админа!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✋ Я беру",
                                           callback_data=f"take_user_{topic_id}_{group_chat_id}")]
                ])
            )

            await try_edit(callback.message, "✅ Запрос на смену админа отправлен.")
        else:
            await try_edit(callback.message, "👌 Оставляем текущего админа.")

        await callback.answer()

    # ═══════════════ Уточнение категории ПЗ (кнопки у ПЗ) ═══════════════

    @child_dp.callback_query(F.data.startswith("pzcat_"))
    async def cb_pz_category(callback: CallbackQuery) -> None:
        """ПЗ выбрал категорию — уведомляем «чат админов» и отмечаем в топике."""
        user_chat_id = cb_uid(callback)
        try:
            index = int(cb_data(callback).rsplit("_", 1)[-1])
        except ValueError:
            index = -1

        accepted, name = await apply_pz_category(bot_obj, bot_id, user_chat_id, index)
        if not accepted:
            # Ответ пришёл после таймера: уведомление уже ушло без категории.
            await callback.answer("⌛ Уточнение уже неактуально", show_alert=True)
            return

        await callback.answer(f"🏷 Категория: {name}")
        await try_edit_answer(callback.message,
                              f"✅ <b>Категория: #{_html_escape(name)}</b>")

    # ═══════════════ Сообщения из ЛС → топик ═══════════════

    @child_dp.message(F.chat.type == ChatType.PRIVATE)
    async def private_message(message: Message) -> None:
        if is_user_banned(bot_id, msg_uid(message)):
            return

        # ── Режим защиты (антинакрутка) ──
        # Пока защита активна, сообщения НЕ доставляются в чат админов, а
        # пользователю отправляется уведомление, что бот под защитой.
        _owner = get_bot_owner(bot_id) or 0
        if _owner and _is_antinakrutka_blocking(_owner):
            await _notify_protection(message, _owner)
            return

        if is_user_muted(bot_id, msg_uid(message)):
            try:
                await message.answer("🔇 Вы временно ограничены в отправке сообщений. Попробуйте позже.")
            except Exception:
                pass
            return

        add_stat(bot_id, "message_in")
        add_child_user(
            bot_id, msg_uid(message),
            msg_username(message) or "",
            msg_firstname(message) or ""
        )

        user_chat_id = msg_uid(message)

        # ── Время работы: вне рабочего времени отвечаем сами ──
        # Обращение при этом НЕ теряется: ниже оно всё равно уйдёт в топик, чтобы
        # свободный админ мог ответить позже. Отвечаем не чаще раза в час на
        # пользователя, иначе на серию сообщений прилетит серия автоответов.
        _work_owner = get_bot_owner(bot_id) or 0
        if _work_owner and not is_within_work_hours(_work_owner):
            _key = (bot_id, user_chat_id)
            _last = _work_hours_replied.get(_key, 0.0)
            if time.time() - _last > _WORK_HOURS_REPEAT:
                _work_hours_replied[_key] = time.time()
                await _send_work_hours_reply(bot_obj, _work_owner, user_chat_id)

        # Обработка "сменить админа"
        if message.text and message.text.strip().lower() == "сменить админа":
            topic = get_topic_by_user(bot_id, user_chat_id)
            if not topic or not topic["admin_user_id"]:
                await message.answer("У вас сейчас нет назначенного админа.")
                return

            # Показываем, сколько смен у юзера осталось (если лимит включён).
            left = admin_changes_left(bot_id, user_chat_id)
            if left == 0:
                await message.answer(
                    "⏳ <b>Смен на сегодня не осталось.</b>\n\n"
                    "Лимит смен админа в сутки уже исчерпан. Завтра он "
                    "обновится — или просто подожди ответа текущего админа.",
                )
                return

            left_line = (f"\n\n🔄 Осталось смен сегодня: <b>{left}</b>."
                         if left > 0 else "")
            await message.answer(
                f"❓ Вы уверены, что хотите сменить админа?{left_line}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Да",
                                              callback_data=f"confirm_change_yes_{topic['topic_id']}_{topic['group_chat_id']}"),
                        InlineKeyboardButton(text="❌ Нет",
                                              callback_data=f"confirm_change_no_{topic['topic_id']}_{topic['group_chat_id']}"),
                    ]
                ])
            )
            return

        # Настройки антиспама
        now = time.time()
        antispam = get_antispam_mode(bot_id)

        if antispam == "manual":
            last = last_message_time.get(user_chat_id, 0)
            if now - last < 60:
                await message.answer("⏳ Подождите минуту.")
                return
            last_message_time[user_chat_id] = now

        # Авто-антиспам (с предупреждением и блокировкой)
        if antispam == "auto":
            if message.content_type == ContentType.STICKER:
                sticker_counts[user_chat_id].append(now)
                sticker_counts[user_chat_id] = [t for t in sticker_counts[user_chat_id] if now - t < 30]

                has_warning = sticker_warnings[user_chat_id]

                if len(sticker_counts[user_chat_id]) >= 5:
                    if not has_warning:
                        # Этап 1: Предупреждение
                        sticker_warnings[user_chat_id] = True
                        sticker_counts[user_chat_id] = []  # Очищаем счётчик для отслеживания следующих 5 стикеров
                        await message.answer(
                            "⚠️ <b>Предупреждение!</b>\n\n"
                            "Пожалуйста, прекратите спам стикерами. "
                            "Если вы отправите ещё 5 стикеров подряд, вы будете заблокированы навсегда!"
                        )
                        return
                    else:
                        # Этап 2: Бан навсегда
                        ban_user(bot_id, user_chat_id)
                        sticker_counts[user_chat_id] = []
                        sticker_warnings[user_chat_id] = False

                        try:
                            await message.answer("🚫 Вас заблокировали в данном боте навсегда. Всего доброго.")
                        except Exception:
                            pass

                        # Находим топик и закрываем его с плашкой "бан спам"
                        topic = get_topic_by_user(bot_id, user_chat_id)
                        if topic:
                            g_id = topic["group_chat_id"]
                            t_id = topic["topic_id"]
                            reset_topic_admin(bot_id, t_id, g_id)
                            delete_topic_record(bot_id, user_chat_id)
                            try:
                                await bot_obj.edit_forum_topic(
                                    chat_id=g_id, message_thread_id=t_id, name="🚫 бан спам"
                                )
                                await bot_obj.close_forum_topic(
                                    chat_id=g_id, message_thread_id=t_id
                                )
                            except Exception:
                                pass
                        return
            else:
                # Если отправлен текст — сбрасываем контигуальный счетчик стикеров
                sticker_counts[user_chat_id] = []

        group_chat_id = get_feedback_chat(bot_id)
        if not group_chat_id:
            return

        anon_mode = is_bot_anonymous(bot_id)
        # Настройки антинакрутки хранятся по владельцу (прочитан выше).
        owner_id = _owner

        async def _open_new_topic() -> dict | None:
            """Создаёт свежий топик и пересылает сообщение.

            Вызывается ТОЛЬКО под локом на пользователя, поэтому даже при спаме
            топик создаётся ровно один раз. Возвращает словарь топика или None.

            Дополнительно «бронирует» ПЗ в БД атомарно (INSERT OR IGNORE) —
            это страховка от гонок между обработчиками/процессами: даже если
            два обработчика одновременно начнут создавать топик, в Telegram
            попадёт ровно один.
            """
            # Топик мог появиться, пока мы ждали лок — перепроверяем.
            existing = get_topic_by_user(bot_id, user_chat_id)
            if existing:
                if int(existing.get("topic_id") or 0):
                    return existing
                # Запись есть, но топик ещё создаётся другим обработчиком —
                # коротко ждём, чтобы не создать дубликат.
                for _ in range(6):
                    await asyncio.sleep(0.5)
                    again = get_topic_by_user(bot_id, user_chat_id)
                    if again and int(again.get("topic_id") or 0):
                        return again
                return None

            # ── Антинакрутка: во время защиты топики НЕ создаём ──
            # Пользователю пишем, что бот в режиме защиты от спама.
            if owner_id and _is_antinakrutka_blocking(owner_id):
                await _notify_protection(message, owner_id)
                return None

            # Атомарно «бронируем» ПЗ за пользователем ДО создания топика.
            if not reserve_topic_slot(bot_id, user_chat_id, group_chat_id):
                # Слот занят (гонка) — второй топик создавать нельзя.
                logger.warning(
                    "ПЗ для юзера %s уже занято другим обработчиком — дубликат не создаю.",
                    user_chat_id,
                )
                return get_topic_by_user(bot_id, user_chat_id)

            try:
                # Каждой теме даём свою иконку: эмодзи по кругу из набора
                # Telegram (или случайный цвет, если эмодзи недоступны) —
                # иначе все ПЗ выглядели бы одинаково.
                icon_id, icon_color = await _next_topic_icon(bot_obj)
                topic_kwargs: dict[str, Any] = {
                    "chat_id": group_chat_id,
                    "name": "⏳ без админа",
                }
                if icon_id:
                    topic_kwargs["icon_custom_emoji_id"] = icon_id
                elif icon_color is not None:
                    topic_kwargs["icon_color"] = icon_color

                forum_topic = await _send_with_gate(
                    bot_obj, group_chat_id,
                    lambda: bot_obj.create_forum_topic(**topic_kwargs),
                )
                new_topic_id = forum_topic.message_thread_id
            except Exception as e:
                logger.error("Не удалось создать топик: %s", e)
                # Снимаем «броню», иначе ПЗ останется без топика навсегда.
                delete_topic_record(bot_id, user_chat_id)
                return None

            set_topic_id(bot_id, user_chat_id, group_chat_id, new_topic_id)

            # Фиксируем новое ПЗ для антинакрутки (может включить защиту).
            try:
                await register_new_pz(owner_id)
            except Exception as e:
                logger.warning("Ошибка регистрации ПЗ для антинакрутки: %s", e)

            if anon_mode:
                header_text = "📩 <b>Новое сообщение</b> 🕶"
            else:
                user_name = msg_firstname(message) or msg_username(message) or str(user_chat_id)
                # Имя экранируем: ники с «<»/«&» ломали разметку, и шапка с
                # кнопкой «✋ Я беру» вообще не отправлялась.
                header_text = (f"👤 Новый пользователь: <b>{_html_escape(user_name)}</b>\n\n"
                               f"🆔 <code>{user_chat_id}</code>")

            # Категория ПЗ. Если у бота включено «уточнение категории» и ПЗ её не
            # назвал — спросим кнопками, а уведомление пришлём после ответа.
            # Спрашиваем только когда «чат админов» привязан: иначе уведомление
            # всё равно некуда отправить и вопрос был бы бесполезным.
            #
            # Исключение — ПР/ВП: если первое сообщение похоже на предложение
            # рекламы или взаимного пиара, категории не важны. Такое ПЗ сразу
            # уходит в «чат админов» с пометкой, без лишнего вопроса пользователю.
            promo_hint = detect_promo_from_message(message.text, message.caption)

            ask_enabled, ask_categories = get_cat_ask_settings(bot_id)
            categories = extract_pz_categories(message)
            if ask_enabled:
                categories = pick_pz_category(message, ask_categories, categories)
            # Кнопки у ПЗ предлагаем с учётом своих категорий бота.
            ask_list = get_categories_for_pz(owner_id, bot_id) if ask_enabled else []
            need_ask = (ask_enabled and not categories and not promo_hint and bool(ask_list)
                        and bool(owner_id and get_bound_chat(owner_id, "admin")))
            if categories:
                header_text += f"\n\n🏷 Категория: <b>{_html_escape(categories)}</b>"

            # О предложении пиара/ВП обычно пишут в первом сообщении — помечаем
            # новое ПЗ, чтобы админ сразу видел, о чём, скорее всего, речь.
            if promo_hint:
                header_text += f"\n\n🔎 <b>{promo_hint}</b>"

            try:
                await _send_with_gate(
                    bot_obj, group_chat_id,
                    lambda: bot_obj.send_message(
                        chat_id=group_chat_id, message_thread_id=new_topic_id,
                        text=header_text,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="✋ Я беру",
                                                   callback_data=f"take_user_{new_topic_id}_{group_chat_id}")]
                        ]),
                    ),
                )
            except Exception as e:
                logger.warning("Не удалось отправить заголовок топика: %s", e)

            sent, _ = await _send_to_topic_retry(message, bot_obj, group_chat_id, new_topic_id)
            if sent:
                save_feedback_message(bot_id, new_topic_id, group_chat_id, user_chat_id,
                                       "in", sent.message_id, message.message_id)
                if message.text:
                    save_log_message(bot_id, user_chat_id, "in", message.text,
                                     msg_username(message) or "")

            try:
                if need_ask:
                    # Спрашиваем категорию у ПЗ; уведомление уйдёт после ответа
                    # (или через PZ_CATEGORY_TIMEOUT, если ПЗ не ответит).
                    asked = await _ask_pz_category(bot_obj, bot_id, user_chat_id,
                                                   new_topic_id, group_chat_id, ask_list)
                    if not asked:
                        await _notify_new_pz(bot_id, group_chat_id, new_topic_id,
                                             promo_hint)
                else:
                    await _notify_new_pz(bot_id, group_chat_id, new_topic_id, promo_hint,
                                         categories)
            except Exception as e:
                logger.warning("Не удалось отправить уведомление о новом ПЗ: %s", e)

            return get_topic_by_user(bot_id, user_chat_id) or {
                "topic_id": new_topic_id, "group_chat_id": group_chat_id,
            }

        # ── Доставка сообщения в топик под локом на пользователя ──
        # Если ПЗ спамит сразу после /start, сообщения обрабатываются по очереди:
        # топик создаётся ровно один раз, а каждое сообщение попадает в него.
        async with _get_user_lock(user_chat_id):
            topic = get_topic_by_user(bot_id, user_chat_id)

            if not topic:
                await _open_new_topic()
                return

            topic_id = topic["topic_id"]

            reply_to_group = None
            if message.reply_to_message:
                orig = get_feedback_msg_by_user_msg(bot_id, user_chat_id, message.reply_to_message.message_id)
                if orig:
                    reply_to_group = orig["group_msg_id"]

            sent, thread_not_found = await _send_to_topic_retry(
                message, bot_obj, group_chat_id, topic_id, reply_to_group
            )

            if sent:
                save_feedback_message(bot_id, topic_id, group_chat_id, user_chat_id,
                                       "in", sent.message_id, message.message_id)
                # Текст сохраняем отдельно: Telegram не отдаёт историю, а по
                # логам пользователь шлёт техподдержку обращение.
                if message.text:
                    save_log_message(bot_id, user_chat_id, "in", message.text,
                                     msg_username(message) or "")
            elif thread_not_found:
                # Топик реально удалён/устарел — пересоздаём ровно один раз
                # под тем же локом, чтобы не наплодить дубликатов при спаме.
                if group_chat_id == topic.get("group_chat_id"):
                    logger.warning(
                        "Топик %s для юзера %s недоступен — пересоздаю.", topic_id, user_chat_id
                    )
                    delete_topic_record(bot_id, user_chat_id)
                await _open_new_topic()
            else:
                # Временный сбой доставки — топик НЕ трогаем: иначе при спаме
                # каждое неудачное сообщение плодило бы новый топик, а ПЗ
                # «размазывался» бы по нескольким топикам.
                logger.warning(
                    "Не удалось доставить сообщение в топик %s (юзера %s).",
                    topic_id, user_chat_id,
                )

    # ═══════════════ Сообщения из топика → юзеру ═══════════════

    @child_dp.message(
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id")
    )
    async def group_topic_message(message: Message, thread_id: int) -> None:
        if message.from_user and message.from_user.is_bot:
            return

        group_chat_id = message.chat.id
        topic = get_topic_by_topic_id(bot_id, group_chat_id, thread_id)
        if not topic:
            return

        user_chat_id = topic["user_chat_id"]

        if message.text and message.text.startswith("/"):
            return

        add_stat(bot_id, "message_out")

        admin = get_admin_by_user_id(get_bot_owner(bot_id) or 0, msg_uid(message))
        if admin:
            add_admin_message(bot_id, msg_uid(message), "out")

        reply_to_user = None
        if message.reply_to_message:
            orig = get_feedback_msg_by_group_msg(bot_id, group_chat_id, message.reply_to_message.message_id)
            if orig:
                reply_to_user = orig["user_msg_id"]

        sent = await _send_to_user(message, bot_obj, user_chat_id, reply_to_user, bot_id=bot_id)

        if sent:
            save_feedback_message(bot_id, thread_id, group_chat_id, user_chat_id,
                                   "out", message.message_id, sent.message_id)
            # Ответ админа тоже в логи: техподдержке видно всю переписку.
            if message.text:
                save_log_message(bot_id, user_chat_id, "out", message.text)

    # ═══════════════ Игнорируем general ═══════════════

    @child_dp.message(
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        ~F.message_thread_id
    )
    async def ignore_general(message: Message) -> None:
        pass

    return child_dp


class ChildManager:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        self._bots: dict[int, Bot] = {}
        self._dispatchers: dict[int, Dispatcher] = {}
        # Боты, которые останавливаем вручную (их не нужно перезапускать).
        self._stopping: set[int] = set()
        # Счётчик попыток автоперезапуска после аварийного падения.
        self._restart_attempts: dict[int, int] = {}
        # Боты, для которых уже идёт ретрай-цикл перезапуска (защита от дублей).
        self._restarting: set[int] = set()
        # Локи запуска: гарантируют, что для одного бота ровно ОДИН polling.
        # Без этого два параллельных вызова start_child (например, «добавил
        # бота» + «запустить всех») могли поднять два поллера на один токен,
        # апдейты делились между ними — и один пользователь получал ДВА топика.
        self._start_locks: dict[int, asyncio.Lock] = {}

    def _get_start_lock(self, bot_id: int) -> asyncio.Lock:
        lock = self._start_locks.get(bot_id)
        if lock is None:
            lock = asyncio.Lock()
            self._start_locks[bot_id] = lock
        return lock

    async def start_child(self, bot_data: dict) -> bool:
        """Запускает дочерний бот (строго один поллер на бота)."""
        bot_id = bot_data["id"]
        async with self._get_start_lock(bot_id):
            return await self._start_child_locked(bot_data)

    async def _start_child_locked(self, bot_data: dict) -> bool:
        bot_id = bot_data["id"]

        # Если задача уже жива — ничего не делаем.
        existing = self._tasks.get(bot_id)
        if existing and not existing.done():
            return True
        # Убираем зависшие записи от ранее упавшего процесса.
        if existing:
            self._tasks.pop(bot_id, None)
            self._bots.pop(bot_id, None)
            self._dispatchers.pop(bot_id, None)

        # Всегда берём СВЕЖИЕ данные из БД — там актуальные bot_type,
        # links и welcome_text (важно после добавления линков/редактирования).
        fresh = get_bot_by_id_any_owner(bot_id)
        if fresh is None:
            fresh = bot_data

        # Мягкий ремонт: возвращаем «премиум» эмодзи в сохранённом приветствии
        # (например, если эмодзи скопировали обычным символом). Данные бота
        # (пользователи, топики, диалоги) при этом не трогаются.
        if repair_premium_emoji_for_bot(bot_id):
            fresh = get_bot_by_id_any_owner(bot_id) or fresh

        token = fresh.get("token") or bot_data.get("token", "")

        try:
            # Прокси из .env нужен и дочерним ботам: без него они не смогут
            # достучаться до Telegram так же, как не смог основной.
            child_bot = Bot(
                token=token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
                **proxy_settings(),
            )
            me = await child_bot.get_me()
            logger.info("Подключаю бот: @%s (%s)", me.username, me.id)

            child_dp = _make_child_dp(fresh, child_bot)

            # Проверяем, не используется ли токен посторонним сервером: если у
            # бота стоит чужой вебхук, апдейты уходят туда, а не нам. Само по
            # себе это не ошибка (мы вебхук сбросим ниже), но владельцу полезно
            # знать, что токен «светился» на другом сервере/в другом сервисе.
            try:
                hook = await child_bot.get_webhook_info()
                if hook.url:
                    logger.warning(
                        "Бот %s: у токена установлен чужой вебхук %s — "
                        "токен используется другим сервером/сервисом",
                        bot_id, hook.url,
                    )
                    if bot_id not in _token_used_warned:
                        _token_used_warned.add(bot_id)
                        await notify_owner(
                            bot_id,
                            "⚠️ <b>Токен бота используется где-то ещё</b>\n\n"
                            f"🤖 Бот: <b>{bot_display_name(fresh)}</b>\n"
                            f"🔗 Внешний вебхук: <code>{hook.url}</code>\n\n"
                            "Пока вебхук стоит, часть сообщений уходит на тот сервер, "
                            "а не нам. Я сбрасываю вебхук при каждом запуске бота, но "
                            "если его ставят снова — отвяжи бота от того сервиса "
                            "(например, от другого конструктора ботов).",
                        )
            except Exception as e:
                logger.debug("Не удалось получить webhook_info бота %s: %s", bot_id, e)

            try:
                await child_bot.delete_webhook(drop_pending_updates=True)
            except Exception as e:
                # Конфликт/сетевые ошибки не должны ронять запуск: сам polling
                # обработает конфликт (409), а ретрай-цикл доведёт бота до старта.
                logger.warning("Не удалось сбросить webhook бота %s: %s", bot_id, e)

            # allowed_updates считаем по зарегистрированным обработчикам: так
            # дочерний бот получает и реакции (message_reaction) — по умолчанию
            # Telegram этот тип апдейтов НЕ присылает, из-за чего реакции
            # «пропадали».
            task = asyncio.create_task(
                child_dp.start_polling(
                    child_bot, allowed_updates=child_dp.resolve_used_update_types()
                ),
                name=f"child_{bot_id}",
            )
            task.add_done_callback(self._make_task_done_callback(bot_id))

            self._tasks[bot_id] = task
            self._bots[bot_id] = child_bot
            self._dispatchers[bot_id] = child_dp

            # Успешный запуск: сбрасываем счётчик перезапусков и флаг остановки.
            self._restart_attempts[bot_id] = 0
            self._stopping.discard(bot_id)
            # Бот поднялся — снимаем пометку «мёртвый», если она была раньше
            # (например, токен починили и добавили бота заново).
            try:
                clear_bot_dead(bot_id)
            except Exception:
                pass

            logger.info("Бот @%s запущен", me.username)
            return True
        except TelegramUnauthorizedError as e:
            # Токен отозван или бот удалён в @BotFather: ретраить бессмысленно,
            # помечаем бота мёртвым — он попадёт в список авто-детекта в
            # админ-панели, где его можно удалить пачкой.
            logger.error("Бот %s: токен недействителен — %s", bot_id, e)
            try:
                mark_bot_dead(bot_id, "unauthorized")
            except Exception:
                pass
            return False
        except Exception as e:
            logger.error("Не удалось запустить бот %s: %s", bot_id, e)
            return False

    def _make_task_done_callback(self, bot_id: int):
        """Возвращает колбэк, который чистит состояние при завершении задачи бота."""
        def _on_done(task: asyncio.Task) -> None:
            self._tasks.pop(bot_id, None)
            self._bots.pop(bot_id, None)
            self._dispatchers.pop(bot_id, None)
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                if isinstance(exc, TelegramConflictError):
                    # Токен одновременно «слушает» кто-то ещё (другая копия бота
                    # на тестовом/основном сервере, livegram и т.п.). Сообщения в
                    # этом случае делятся между процессами случайным образом —
                    # именно поэтому бот «иногда» отвечает не тем приветствием.
                    logger.warning(
                        "Бот %s: конфликт токена (кто-то ещё получает апдейты): %s",
                        bot_id, exc,
                    )
                    if bot_id not in _token_used_warned:
                        _token_used_warned.add(bot_id)
                        try:
                            asyncio.create_task(notify_owner(
                                bot_id,
                                "⚠️ <b>Токен бота занят другим процессом</b>\n\n"
                                "У этого бота второй экземпляр получает сообщения "
                                "(например, копия на другом сервере или подключение к "
                                "другому сервису). Из-за этого ответы и приветствие "
                                "могут приходить не от нашей панели.\n\n"
                                "Останови второго «слушателя» — и бот сам заработает "
                                "нормально (перезапускать вручную не нужно).",
                            ))
                        except RuntimeError:
                            pass
                else:
                    logger.error(
                        "Дочерний бот %s аварийно завершился: %s\n%s",
                        bot_id, exc, exc.__traceback__,
                    )
                # Планируем автоперезапуск (не из колбэка — это sync).
                try:
                    asyncio.create_task(self._restart_bot_later(bot_id))
                except RuntimeError:
                    logger.error("Не удалось запланировать перезапуск бота %s", bot_id)
        return _on_done

    async def _restart_bot_later(self, bot_id: int) -> None:
        """Перезапускает упавший бот с нарастающей паузой, пока он не поднимется.

        Бот может временно падать из-за конфликта токена (например, он ещё
        подключён к livegram или другому сервису). После отвязки такой бот
        должен подняться САМ, без удаления и повторного добавления — поэтому
        ретраим не ограничиваем тремя попытками, а наращиваем паузу:
        5 → 15 → 30 → 60с (далее каждые 60с).
        """
        if bot_id in self._restarting:
            return
        self._restarting.add(bot_id)
        try:
            delay = min(5 * (2 ** self._restart_attempts.get(bot_id, 0)), 60)
            await asyncio.sleep(delay)

            if bot_id in self._stopping:
                return
            fresh = get_bot_by_id_any_owner(bot_id)
            if not fresh or fresh.get("stopped"):
                return

            attempts = self._restart_attempts.get(bot_id, 0)
            self._restart_attempts[bot_id] = attempts + 1
            logger.info(
                "Перезапускаю упавший бот %s (попытка %d, пауза %.0fс)",
                bot_id, attempts + 1, delay,
            )
            await self.start_child(fresh)
        finally:
            self._restarting.discard(bot_id)

    async def stop_child(self, bot_id: int) -> bool:
        if bot_id not in self._tasks:
            return False

        self._stopping.add(bot_id)

        task = self._tasks.pop(bot_id)
        dp = self._dispatchers.pop(bot_id, None)
        bot = self._bots.pop(bot_id, None)

        if dp:
            await dp.stop_polling()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if bot:
            await bot.session.close()

        self._stopping.discard(bot_id)
        return True

    async def restart_child(self, bot_data: dict) -> bool:
        await self.stop_child(bot_data["id"])
        return await self.start_child(bot_data)

    async def restart_all_for_owner(self, owner_id: int) -> dict:
        """Полный перезапуск всех дочерних ботов владельца.

        Используется кнопкой «🔄 Полный перезапуск» в профиле: перезапускает
        всех ботов владельца, чтобы «разбудить» зависших — без удаления и
        перепривязки чатов.

        Молча (без сообщений в интерфейсе) выполняет «мягкий ремонт» данных:
        возвращает в приветствия премиум-эмодзи, которые владелец раньше
        отправлял как премиум (см. services/premium_emoji.py). Данные
        (пользователи, топики, админы, диалоги) при этом НЕ трогаются —
        меняется только разметка эмодзи в сохранённом приветствии.
        """
        repaired = repair_premium_emoji_for_owner(owner_id)

        bots = [
            b for b in get_all_bots_flat()
            if b.get("owner_id") == owner_id and not b.get("stopped")
        ]
        results = await asyncio.gather(
            *(self.restart_child(b) for b in bots),
            return_exceptions=True,
        )
        ok = 0
        for res in results:
            if isinstance(res, Exception):
                logger.error("Ошибка полного перезапуска: %s", res)
            elif res:
                ok += 1
        logger.info(
            "Полный перезапуск владельца %s: %s/%s ботов; ремонт премиум-эмодзи: %s",
            owner_id, ok, len(bots), repaired,
        )
        return {"total": len(bots), "ok": ok}

    async def start_all_children(self) -> None:
        all_bots = get_all_bots_flat()
        pending = [b for b in all_bots if not b.get("stopped")]
        if not pending:
            logger.info("Нет ботов для запуска")
            return

        results = await asyncio.gather(
            *(self.start_child(b) for b in pending),
            return_exceptions=True,
        )
        started = 0
        for bot_data, res in zip(pending, results):
            if isinstance(res, Exception):
                logger.error("Дочерний бот %s упал при запуске: %s", bot_data["id"], res)
            elif res:
                started += 1
            else:
                logger.warning("Не удалось запустить дочерний бот %s", bot_data["id"])
        logger.info("Запущено дочерних ботов: %s/%s", started, len(pending))

        for bot_data in pending:
            if get_feedback_chat(bot_data["id"]) is None:
                logger.info(
                    "Бот %s (%s): чат топиков не подключён — подключится сам "
                    "при добавлении в группу с темами (или через /connect)",
                    bot_data["id"], bot_data.get("username") or bot_data.get("first_name") or "?",
                )

    async def stop_all_children(self) -> None:
        bot_ids = list(self._tasks.keys())
        for bot_id in bot_ids:
            await self.stop_child(bot_id)

    def is_running(self, bot_id: int) -> bool:
        return bot_id in self._tasks and not self._tasks[bot_id].done()

    def get_bot(self, bot_id: int) -> Bot | None:
        return self._bots.get(bot_id)

    async def send_mailing(self, bot_id: int, text: str,
                           media_type: str = "", media_id: str = "",
                           entities=None, progress_callback=None,
                           reply_markup=None) -> dict:
        """Рассылает сообщение всем активным пользователям бота.

        Что изменилось по сравнению с прежней версией (жалобы были «из 100
        дошло 10», при этом бота никто не банил):

        * отправка идёт через «шлюз» :func:`_send_with_gate` — при 429 (flood)
          ждём указанное Telegram время и повторяем, а не теряем сообщение;
        * сетевые ошибки и ошибки серверов Telegram тоже ретраятся;
        * у каждого получателя несколько попыток вместо одной;
        * ведётся разбор причин недоставки и замер времени рассылки.

        Возвращает ``sent``, ``failed``, ``total``, ``duration``,
        ``reasons`` (причина → количество) и ``samples`` (примеры «кому и
        почему не дошло»).
        """
        bot = self._bots.get(bot_id)
        if not bot:
            return {"sent": 0, "failed": 0, "total": 0, "duration": 0.0,
                    "reasons": {"бот не запущен": 1}, "samples": []}

        msg_entities = None
        if entities:
            msg_entities = [MessageEntity(**e) for e in entities]

        users = get_child_users(bot_id, only_active=True)
        total = len(users)
        sent = 0
        failed = 0
        reasons: dict[str, int] = {}
        samples: list[str] = []
        started_at = time.monotonic()

        def _fail(reason: str, detail: str = "") -> None:
            nonlocal failed
            failed += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            if detail and len(samples) < 5:
                samples.append(f"• {detail} — {reason}")

        # Медиа: байты скачиваем через основной бот один раз и перезаливаем
        # дочерним ботом (file_id чужого бота не работает).
        media_bytes = None
        if media_type and media_id:
            media_bytes = await _cached_media_bytes(media_id)

        for i, user in enumerate(users):
            chat_id = user["chat_id"]

            def _call(chat_id: int = chat_id) -> Any:
                """Сама отправка: её умеет повторять «шлюз» _send_with_gate."""
                if media_type == "photo" and media_id:
                    photo = BufferedInputFile(media_bytes, filename="photo.jpg") \
                        if media_bytes is not None else media_id
                    return bot.send_photo(chat_id=chat_id, photo=photo,
                                          caption=text, caption_entities=msg_entities,
                                          parse_mode=None, reply_markup=reply_markup)
                if media_type == "video" and media_id:
                    video = BufferedInputFile(media_bytes, filename="video.mp4") \
                        if media_bytes is not None else media_id
                    return bot.send_video(chat_id=chat_id, video=video,
                                          caption=text, caption_entities=msg_entities,
                                          parse_mode=None, reply_markup=reply_markup)
                if media_type == "document" and media_id:
                    document = BufferedInputFile(media_bytes, filename="file.bin") \
                        if media_bytes is not None else media_id
                    return bot.send_document(chat_id=chat_id, document=document,
                                             caption=text, caption_entities=msg_entities,
                                             parse_mode=None, reply_markup=reply_markup)
                if media_type == "animation" and media_id:
                    animation = BufferedInputFile(media_bytes, filename="anim.gif") \
                        if media_bytes is not None else media_id
                    return bot.send_animation(chat_id=chat_id, animation=animation,
                                              caption=text, caption_entities=msg_entities,
                                              parse_mode=None, reply_markup=reply_markup)
                if media_type == "sticker" and media_id:
                    sticker = BufferedInputFile(media_bytes, filename="sticker.webp") \
                        if media_bytes is not None else media_id
                    return bot.send_sticker(chat_id=chat_id, sticker=sticker)
                return bot.send_message(chat_id=chat_id, text=text,
                                        entities=msg_entities, parse_mode=None,
                                        reply_markup=reply_markup)

            try:
                await _send_with_gate(bot, chat_id, _call)
                sent += 1
                add_stat(bot_id, "message_out")
            except TelegramForbiddenError:
                # Юзер заблокировал бота: помечаем в базе, чтобы больше не
                # пытаться слать (иначе он же и портит статистику).
                await _handle_user_blocked(bot, bot_id, chat_id)
                _fail("заблокировал бота", f"ID {chat_id}")
            except TelegramRetryAfter as e:
                _fail("лимит Telegram (flood)",
                      f"ID {chat_id} — нужно подождать {getattr(e, 'retry_after', '?')}с")
            except (TelegramNetworkError, TelegramServerError) as e:
                _fail("сеть / серверы Telegram", f"ID {chat_id} — {type(e).__name__}")
            except TelegramBadRequest as e:
                msg = (getattr(e, "message", "") or "").lower()
                if "chat not found" in msg:
                    _fail("чат не найден", f"ID {chat_id}")
                elif "bot was blocked" in msg or "user is deactivated" in msg:
                    await _handle_user_blocked(bot, bot_id, chat_id)
                    _fail("заблокировал бота", f"ID {chat_id}")
                else:
                    _fail("Telegram отклонил запрос",
                          f"ID {chat_id} — {getattr(e, 'message', e)}")
            except Exception as e:
                logger.warning("Ошибка рассылки %s -> %s: %s", bot_id, chat_id, e)
                _fail("прочая ошибка", f"ID {chat_id} — {type(e).__name__}")

            if progress_callback and ((i + 1) % 5 == 0 or (i + 1) == total):
                await progress_callback(sent, failed, total, i + 1)

            # Пауза между получателями: шлюз сам придерживает темп при 429,
            # а здесь — небольшая пауза, чтобы не ловить flood с ходу.
            await asyncio.sleep(0.1)

        duration = time.monotonic() - started_at
        save_mailing(bot_id, text, media_type, media_id, sent, failed)
        add_stat(bot_id, "mailing_done")

        return {
            "sent": sent,
            "failed": failed,
            "total": total,
            "duration": duration,
            "reasons": reasons,
            "samples": samples,
        }
