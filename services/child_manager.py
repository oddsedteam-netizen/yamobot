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
    BotCommand,
    Message,
    CallbackQuery,
    MessageEntity,
    MessageReactionUpdated,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
)

from handlers._common import (cb_data, cb_uid, cb_username, cb_firstname,
                              msg_uid, msg_username, msg_firstname,
                              try_edit_answer, try_edit)
from services import premium_emoji as premium
from services.config import proxy_settings
from services.constants import BASE_WELCOME, BOT_CREDIT
from services.delivery import get_outbox
from services.guards import fire_and_forget
from services.logging_setup import bot_context, bot_logger
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
    clear_feedback_chat,
    get_owner_by_admin_chat,
    set_bound_chat,
    get_topic_by_user,
    get_topic_by_topic_id,
    get_pinned_message_id,
    set_pinned_message_id,
    create_topic_record,
    delete_topic_record,
    assign_admin_to_topic,
    reset_topic_admin,
    save_feedback_message,
    touch_topic_activity,
    set_topic_closed,
    get_feedback_msg_by_group_msg,
    get_feedback_msg_by_user_msg,
    get_bot_owner,
    get_admin_greeting,
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


def _bot_admin_tag(bot_id: int, user_id: int) -> str | None:
    """Тег админа для топика (или None, если человек не админ этого бота).

    Единая точка «кто здесь админ»: используется и кнопкой «✋ Я беру»,
    и проверками команд ``/ban``, ``/unban``, ``/otkaz``. Раньше эти команды
    вообще не проверяли отправителя — любой участник «чата работы» мог
    забанить ПЗ или отказаться от обращения за админа.
    """
    owner_id = get_bot_owner(bot_id) or 0
    admin = get_admin_by_user_id(owner_id, user_id)
    if not admin and owner_id != 0:
        # Легаси-записи (до введения owner_id) лежат с owner_id = 0.
        admin = get_admin_by_user_id(0, user_id)
    if admin:
        return str(admin["tag"])

    # Владелец бота — всегда админ, даже без записи в таблице admins.
    if owner_id and owner_id == user_id:
        return f"owner:{user_id}"
    return None


def _is_bot_admin(bot_id: int, user_id: int) -> bool:
    """Админ ли этот человек у данного бота (или сам владелец).

    Нужен для команд в топике: они меняют состояние ПЗ, поэтому должны быть
    доступны только админам и владельцу.
    """
    if not user_id:
        return False
    return _bot_admin_tag(bot_id, user_id) is not None


async def _deny_not_admin(message: Message) -> None:
    """Отвечает, что команда доступна только админу.

    Отказ НЕ молчаливый: иначе человек решит, что команда сломалась, и
    напишет в поддержку.
    """
    try:
        await message.answer("⛔ Команда доступна только администратору этого бота.")
    except Exception:
        logger.debug("Не удалось ответить на отказ в команде", exc_info=True)


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


def _is_chat_gone(err: Exception) -> bool:
    """True, если САМОГО чата больше нет: удалён или бота из него выгнали.

    Такую ошибку бессмысленно повторять: пока владелец не привяжет чат
    заново, каждая отправка будет падать. Поэтому вызывающий код снимает
    привязку (``handle_dead_chat``), а не долбит мёртвый чат вечно.
    """
    msg = (getattr(err, "message", "") or str(err)).lower()
    return ("chat not found" in msg
            or "bot was kicked" in msg
            or "bot is not a member" in msg
            or "group chat was deactivated" in msg
            or "chat was deleted" in msg)


# ── Разбор callback_data кнопок в топиках ─────────────────────────
# Кнопки одного экрана делят общий префикс: «refuse_<топик>_<чат>»,
# «refuse_q_<топик>_<чат>_<0|1>», «refuse_cancel». Раньше на них стоял
# startswith("refuse_"), из-за чего ЛЮБАЯ форма попадала в первый хендлер, а
# он брал parts[1] числом: для «refuse_q_…» это давало int("q"), а для
# «refuse_cancel» — int("cancel"). Нажатие «Анонимно», «Сообщить ПЗ» и
# «Отмена» падало с ValueError и ничего не делало.
#
# Поэтому разбираем callback через единый безопасный хелпер: он не роняет
# бота на битых или устаревших кнопках, а просто возвращает None.
def parse_topic_ids(data: str, offset: int, limit: int | None = None) -> tuple[int, ...] | None:
    """Числа из ``callback_data`` начиная с ``offset``.

    ``None`` — данные не разобрались (битая кнопка, старый формат). Вызывающий
    код обязан это обработать, а не полагаться на исключение.
    """
    parts = (data or "").split("_")
    wanted = limit if limit is not None else (len(parts) - offset)
    if len(parts) < offset + wanted:
        return None
    values: list[int] = []
    for raw in parts[offset:offset + wanted]:
        try:
            values.append(int(raw))
        except (TypeError, ValueError):
            return None
    return tuple(values)


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


async def handle_dead_chat(bot_id: int, chat_id: int, reason: str = "") -> None:
    """Чат недоступен боту: снимаем привязку и говорим владельцу.

    Когда бота выгнали из группы или чат удалили, каждая отправка падала с
    «chat not found». Бот продолжал туда писать, сообщения копились в
    очереди, в «Диагностике» рос счётчик «недоставленные», а понять причину
    было нечем. Теперь привязка снимается один раз — и владелец видит, что
    чат надо привязать заново.

    Идемпотентна: повторный вызов для уже снятой привязки ничего не делает.
    ``bot_id`` = 0 — это сообщение основного бота (например, в «чат админов»).
    """
    chat_id = int(chat_id or 0)
    if not chat_id:
        return

    detached: list[str] = []

    # 1) «Чат работы» дочернего бота — группа, в которой живут топики ПЗ.
    if bot_id:
        try:
            if get_feedback_chat(bot_id) == chat_id:
                clear_feedback_chat(bot_id)
                detached.append("чат работы бота")
        except Exception:
            logger.debug("Не удалось снять привязку чата работы бота %s",
                         bot_id, exc_info=True)

    # 2) «Чат админов» владельца — туда идут уведомления и напоминалки.
    owner_id = 0
    try:
        found = get_owner_by_admin_chat(chat_id)
        if found:
            owner_id = int(found)
            set_bound_chat(owner_id, "admin", None)
            detached.append("чат админов")
    except Exception:
        logger.debug("Не удалось снять привязку чата админов %s",
                     chat_id, exc_info=True)

    if not detached:
        return

    logger.warning(
        "Чат %s недоступен боту %s (%s) — снято: %s. Больше туда не пишем.",
        chat_id, bot_id, reason or "chat not found", ", ".join(detached),
    )

    # Владельцу — понятное объяснение, что делать дальше. Пишем через ЛС
    # основного бота: это другой чат, он не сломан.
    if bot_id:
        await notify_owner(
            bot_id,
            "⚠️ <b>Чат работы недоступен</b>\n\n"
            f"Бот не может писать в привязанную группу: {reason or 'чат удалён'}.\n\n"
            "Привязка снята — привяжи чат заново: "
            "профиль → «🔗 Привязать чаты».",
        )
    elif owner_id:
        main = get_main_bot()
        if main is not None:
            try:
                await main.send_message(
                    chat_id=owner_id,
                    text=(
                        "⚠️ <b>«Чат админов» недоступен</b>\n\n"
                        f"YamoBot не может писать в привязанный чат: "
                        f"{reason or 'чат удалён'}.\n\n"
                        "Привязка снята — привяжи чат заново: "
                        "профиль → «🔗 Привязать чаты»."
                    ),
                )
            except Exception as e:
                logger.debug("Не удалось уведомить владельца %s: %s", owner_id, e)



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


def _topic_action_kb(topic_id: int, group_chat_id: int) -> InlineKeyboardMarkup:
    """Кнопки под новым ПЗ: взять или отказаться.

    Раньше здесь была одна кнопка «Я беру», и админу, который не может
    ответить, оставалось только молча переименовывать топик вручную — об
    этом просили отдельную кнопку отказа.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✋ Я беру",
                             callback_data=f"take_user_{topic_id}_{group_chat_id}",
                             style="success"),
        InlineKeyboardButton(text="🚫 Отказ",
                             callback_data=f"refuse_{topic_id}_{group_chat_id}",
                             style="danger"),
    ]])


def _topic_refuse_only_kb(topic_id: int, group_chat_id: int) -> InlineKeyboardMarkup:
    """Только «Отказ» — то, что остаётся под шапкой ПЗ после «✋ Я беру».

    Нажатие «Я беру» больше не убирает клавиатуру целиком (иначе админ терял
    возможность отказаться от обращения, которое он уже взял): исчезает
    только сама кнопка «Я беру», а «Отказ» остаётся.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚫 Отказ",
                             callback_data=f"refuse_{topic_id}_{group_chat_id}",
                             style="danger"),
    ]])


def _topic_refuse_choice_kb(topic_id: int, group_chat_id: int) -> InlineKeyboardMarkup:
    """Выбор «как сообщить ПЗ об отказе» + «⬅️ Отмена»."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🤫 Анонимно",
            callback_data=f"refuse_q_{topic_id}_{group_chat_id}_0",
            style="primary",
        ),
        InlineKeyboardButton(
            text="📣 Сообщить ПЗ",
            callback_data=f"refuse_q_{topic_id}_{group_chat_id}_1",
            style="danger",
        ),
    ], [
        InlineKeyboardButton(text="⬅️ Отмена",
                             callback_data="refuse_cancel",
                             style="primary"),
    ]])


# Вопрос об отказе. Вынесен в константу, чтобы текст не разъезжался с тестами.
REFUSE_ASK_TEXT = (
    "🚫 <b>Отказаться от обращения?</b>\n\n"
    "Как сообщить об этом пользователю?\n\n"
    "🤫 <b>Анонимно</b> — пользователь ничего не узнает, увидит "
    "только обычную смену админа.\n"
    "📣 <b>Сообщить ПЗ</b> — пользователь получит уведомление, "
    "что админ отказался и ему ищут нового."
)


async def _ask_refuse_confirm(bot_obj: Bot, topic_id: int,
                              group_chat_id: int) -> bool:
    """Спрашивает у админа, как сообщить ПЗ об отказе.

    Вопрос уходит **отдельным** сообщением, а шапка ПЗ с кнопками
    «✋ Я беру» / «🚫 Отказ» остаётся нетронутой.

    Регресс, ради которого так сделано: раньше вопрос ПОДМЕНЯЛ текст шапки
    (``render_callback``), а «⬅️ Отмена» ставила «Отказ отменён» вообще без
    клавиатуры. Вернуть шапку было нечем — админ терял и инфу о ПЗ, и обе
    кнопки: ни взять обращение, ни отказаться позже было нельзя.

    False — вопрос отправить не удалось (наружу не бросаем: админ просто
    увидит уведомление Telegram об ошибке).
    """
    try:
        await _send_with_gate(
            bot_obj, group_chat_id,
            lambda: bot_obj.send_message(
                chat_id=group_chat_id, message_thread_id=topic_id,
                text=REFUSE_ASK_TEXT,
                reply_markup=_topic_refuse_choice_kb(topic_id, group_chat_id),
            ),
        )
    except Exception as e:
        logger.warning("Не удалось отправить вопрос об отказе: %s", e)
        return False
    return True


async def _pin_topic_action(bot_obj: Bot, bot_id: int, group_chat_id: int,
                            topic_id: int, sent: Message | None) -> None:
    """Закрепляет шапку ПЗ — сообщение с кнопками «✋ Я беру» / «🚫 Отказ».

    Зачем нужен закреп
    ------------------
    Шапка — единственное место, где админ может взять обращение или отказаться
    от него. В переписке ПЗ и админа она быстро уезжает вверх, кнопка
    «теряется», и отказаться от ПЗ становится негде. Поэтому шапку держим в
    закрепе.

    Когда приходит НОВАЯ шапка (смена админа, отказ, повторный запрос) —
    снимаем старый закреп и закрепляем новую: в закрепе не должно остаться
    устаревшего «никто не взял».

    Ошибки закрепа сценарий не ломают: если бот не админ чата или у него нет
    права «Закреплять сообщения», Telegram вернёт ошибку — ПЗ всё равно
    получит админа, просто без закрепа в журнале появится предупреждение.
    """
    message_id = int(getattr(sent, "message_id", 0) or 0)
    if not message_id:
        return

    previous = get_pinned_message_id(bot_id, topic_id, group_chat_id)
    if previous and previous != message_id:
        try:
            await bot_obj.unpin_chat_message(chat_id=group_chat_id,
                                             message_id=previous)
        except Exception:
            logger.debug("Не удалось снять прошлый закреп в топике %s",
                         topic_id, exc_info=True)

    try:
        await bot_obj.pin_chat_message(chat_id=group_chat_id,
                                       message_id=message_id,
                                       disable_notification=True)
    except Exception as e:
        logger.warning("Не удалось закрепить шапку ПЗ в топике %s: %s", topic_id, e)

    try:
        set_pinned_message_id(bot_id, topic_id, group_chat_id, message_id)
    except Exception:
        logger.debug("Не удалось сохранить id закреплённой шапки ПЗ %s",
                     topic_id, exc_info=True)


async def _release_and_announce(bot_obj: Bot, bot_id: int, topic_id: int,
                                group_chat_id: int, user_chat_id: int) -> None:
    """Освобождает ПЗ от админа и просит нового.

    Используется кнопкой «Отказ» от админа и подтверждённым запросом смены
    от самого ПЗ. ПЗ в обоих случаях получает сообщение о смене.
    """
    reset_topic_admin(bot_id, topic_id, group_chat_id)
    touch_topic_activity(bot_id, topic_id, group_chat_id, "in")

    try:
        await _notify_admin_change(bot_id, group_chat_id, topic_id)
    except Exception as e:
        logger.warning("Не удалось уведомить о смене админа: %s", e)

    try:
        await bot_obj.send_message(
            chat_id=user_chat_id,
            text="🔄 Вашего администратора меняют. С вами скоро свяжется новый админ.",
        )
    except Exception:
        logger.debug("Не удалось уведомить ПЗ о смене админа", exc_info=True)

    await _rename_topic(bot_obj, group_chat_id, topic_id, "🔄 смена админа")

    sent = await bot_obj.send_message(
        chat_id=group_chat_id, message_thread_id=topic_id,
        text="🔔 Пользователь запросил смену админа!",
        reply_markup=_topic_action_kb(topic_id, group_chat_id),
    )
    # Новая шапка занимает место старой в закрепе: админ должен видеть
    # актуальные кнопки, а не устаревшее «обращение уже взято».
    await _pin_topic_action(bot_obj, bot_id, group_chat_id, topic_id, sent)


async def _ask_change_admin(bot_obj: Bot, bot_id: int, user_chat_id: int,
                            topic: dict) -> bool:
    """Единый вход для смены админа: и команда ``/smena``, и текст.

    Раньше эти пути различались: команда ``/smena`` сбрасывала админа сразу,
    без подтверждения, без проверки суточного лимита и без записи в журнал
    смен. Теперь оба пути приводят к одному экрану подтверждения, а сама
    смена происходит только в ``confirm_change_yes`` — там уже есть проверка
    лимита и ``log_admin_change``.

    ПЗ сразу видит, сколько смен у него осталось на сегодня.
    False — отвечать не о чем (нет обращения или нет назначенного админа),
    текст уже отправлен.
    """
    topic_id = int(topic["topic_id"])
    group_chat_id = int(topic["group_chat_id"])

    if not topic.get("admin_user_id"):
        await bot_obj.send_message(
            chat_id=user_chat_id,
            text="У вас сейчас нет назначенного админа.",
        )
        return False

    # Показываем, сколько смен у юзера осталось (если лимит включён).
    left = admin_changes_left(bot_id, user_chat_id)
    if left == 0:
        await bot_obj.send_message(
            chat_id=user_chat_id,
            text="⏳ <b>Смен на сегодня не осталось.</b>\n\n"
                 "Лимит смен админа в сутки уже исчерпан. Завтра он "
                 "обновится — или просто подожди ответа текущего админа.",
        )
        return False

    # -1 — ограничение выключено: упоминать нечего.
    left_line = f"\n\n🔄 Осталось смен сегодня: <b>{left}</b>." if left > 0 else ""
    await bot_obj.send_message(
        chat_id=user_chat_id,
        text=f"❓ Вы уверены, что хотите сменить админа?{left_line}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да", style="success",
                                 callback_data=f"confirm_change_yes_{topic_id}_{group_chat_id}"),
            InlineKeyboardButton(text="❌ Нет", style="primary",
                                 callback_data=f"confirm_change_no_{topic_id}_{group_chat_id}"),
        ]]),
    )
    return True


async def _send_admin_greeting(bot_obj: Bot, bot_id: int, user_chat_id: int,
                              admin_user_id: int) -> bool:
    """Отправляет ПЗ заготовленное приветствие админа (если оно есть).

    Приветствие заводится админом в основном боте: «🆔 YID» → «Моё
    приветствие» → выбрать бота. Хранится на пару (бот, админ), потому что
    стиль общения у каждого бота свой.

    Не заменяет ответ админа — уходит первым сообщением, чтобы ПЗ сразу
    получил представление о том, кто ему отвечает.

    Возвращает True, только если приветствие реально доставлено: вызывающий
    код показывает админу «✅ отправлено» лишь при успехе, иначе он решил бы,
    что ПЗ получил текст, которого тот не видел.
    """
    if not user_chat_id or not admin_user_id:
        return False
    try:
        greeting = get_admin_greeting(bot_id, admin_user_id)
    except Exception:
        logger.debug("Не удалось прочитать приветствие админа", exc_info=True)
        return False
    if not greeting:
        return

    text = str(greeting.get("text") or "").strip()
    photo = str(greeting.get("photo_id") or "").strip()
    if not text and not photo:
        return False

    entities = None
    raw_entities = str(greeting.get("text_entities") or "[]")
    if raw_entities and raw_entities != "[]":
        try:
            entities = json.loads(raw_entities)
        except (TypeError, ValueError):
            entities = None

    # Подпись к фото ограничена 1024 символами.
    caption = text[:1024] if photo else text

    try:
        if photo:
            # file_id выдал ОСНОВНОЙ бот, а отправляет дочерний: такой файл ему
            # не принадлежит, и Telegram отклоняет его. Поэтому качаем байты
            # основным ботом и заливаем заново — как в _send_work_hours_reply.
            # Раньше файл уходил «как есть», ошибка глоталась в except, и фото
            # молча не отправлялось.
            data = await _cached_media_bytes(photo)
            if data is not None:
                await bot_obj.send_photo(
                    chat_id=user_chat_id,
                    photo=BufferedInputFile(data, filename="greeting.jpg"),
                    caption=caption or None,
                    caption_entities=entities,
                    parse_mode=None,
                )
            else:
                # Файл скачать не вышло — шлём по исходному file_id.
                logger.warning(
                    "Приветствие админа: не удалось скачать фото, пробую file_id"
                )
                await bot_obj.send_photo(
                    chat_id=user_chat_id,
                    photo=photo,
                    caption=caption or None,
                    caption_entities=entities,
                    parse_mode=None,
                )
        else:
            await bot_obj.send_message(
                chat_id=user_chat_id,
                text=text,
                entities=entities,
                parse_mode=None,
            )
    except Exception:
        # Приветствие — украшение: не отправка не должна ломать взятие
        # обращения. Но молчать нельзя — админ должен видеть, что фото не ушло.
        logger.warning("Не удалось отправить приветствие админа ПЗ", exc_info=True)
        # Страховка: хотя бы текст. Иначе ПЗ не узнает, кто к нему пришёл.
        if photo and text:
            try:
                await bot_obj.send_message(
                    chat_id=user_chat_id,
                    text=text,
                    entities=entities,
                    parse_mode=None,
                )
            except Exception:
                logger.debug("Не удалось отправить текст приветствия",
                             exc_info=True)
                return False
            return True
        return False
    return True


def _greeting_prompt_kb(topic_id: int, group_chat_id: int) -> InlineKeyboardMarkup:
    """Кнопки подтверждения отправки приветствия."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да — отправить",
                             callback_data=f"greet_yes_{topic_id}_{group_chat_id}",
                             style="success"),
        InlineKeyboardButton(text="🚫 Нет — не отправлять",
                             callback_data=f"greet_no_{topic_id}_{group_chat_id}",
                             style="danger"),
    ]])


GREETING_ASK_TEXT = (
    "💬 <b>Отправить приветствие?</b>\n\n"
    "У вас заготовлено приветствие для этого бота.\n"
    "Отправить его пользователю первым сообщением?"
)


async def _ask_admin_greeting(bot_obj: Bot, bot_id: int, topic_id: int,
                              group_chat_id: int, admin_user_id: int) -> bool:
    """Спрашивает админа, отправлять ли заготовленное приветствие.

    Раньше приветствие уходило ПЗ СРАЗУ после «✋ Я беру». Владелец просил
    подтверждение: текст может быть неуместен для конкретного обращения,
    а отменить отправленное сообщение уже нельзя.

    Вопрос появляется ТОЛЬКО если приветствие реально заведено — иначе нечего
    спрашивать (поведение как раньше: ничего не отправляем).

    False — приветствия нет или вопрос отправить не удалось.
    """
    if not admin_user_id:
        return False
    try:
        greeting = get_admin_greeting(bot_id, admin_user_id)
    except Exception:
        logger.debug("Не удалось прочитать приветствие админа", exc_info=True)
        return False
    if not greeting:
        return False

    has_content = bool(str(greeting.get("text") or "").strip()
                       or str(greeting.get("photo_id") or "").strip())
    if not has_content:
        return False

    try:
        await _send_with_gate(
            bot_obj, group_chat_id,
            lambda: bot_obj.send_message(
                chat_id=group_chat_id, message_thread_id=topic_id,
                text=GREETING_ASK_TEXT,
                reply_markup=_greeting_prompt_kb(topic_id, group_chat_id),
            ),
        )
    except Exception as e:
        logger.warning("Не удалось спросить про приветствие: %s", e)
        return False
    return True


async def _apply_greeting_decision(bot_obj: Bot, bot_id: int, admin_user_id: int,
                                   topic_id: int, group_chat_id: int,
                                   send: bool,
                                   callback_message: Any = None) -> tuple[bool, str]:
    """Применяет решение админа по приветствию.

    Возвращает ``(выполнено, текст-для-админа)``. Проверки здесь, а не в
    хендлере, чтобы их можно было протестировать: кнопки лежат в общем чате
    и доступны любому админу.
    """
    topic = get_topic_by_topic_id(bot_id, group_chat_id, topic_id)
    if not topic:
        return False, "❌ Обращение не найдено"

    # Отвечать может только тот, кто ведёт обращение. Если админ успел
    # смениться, старые кнопки не сработают.
    current_admin = int(topic.get("admin_user_id") or 0)
    if current_admin and current_admin != admin_user_id:
        return False, "❌ Это не твоё обращение"

    if not send:
        return True, "👌 Без приветствия"

    greeting = get_admin_greeting(bot_id, admin_user_id)
    if not greeting:
        return False, "⚠️ Приветствие уже удалено"

    sent = await _send_admin_greeting(
        bot_obj, bot_id, int(topic.get("user_chat_id") or 0), admin_user_id,
    )
    if not sent:
        return False, "⚠️ Не удалось отправить приветствие"
    return True, "✅ Приветствие отправлено"


async def _rename_topic(bot_obj: Bot, group_chat_id: int, topic_id: int,
                        name: str) -> None:
    """Переименовывает топик; ошибка не должна ломать сценарий."""
    try:
        await bot_obj.edit_forum_topic(
            chat_id=group_chat_id, message_thread_id=topic_id, name=name
        )
    except Exception:
        logger.debug("Не удалось переименовать топик %s", topic_id, exc_info=True)


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
        logger.debug(
            "Исключение проглочено",
            exc_info=True,
        )


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

    Раньше здесь была одна попытка ``answer_photo`` с HTML-разметкой, и при
    любой ошибке возвращался ``False`` — после чего приветствие уходило
    обычным текстом БЕЗ фото. Главная причина: в тексте приветствия живут
    теги ``<tg-emoji>`` (премиум-эмодзи), которые Telegram не понимает в
    HTML-режиме и отвечает ``Bad Request``.

    Поэтому отправляем тремя ступенями, каждая строже предыдущей:
      1. сущностями без parse_mode — разметка и премиум-эмодзи сохраняются;
      2. текстом без ``<tg-emoji>``-разметки — если HTML всё же не принят;
      3. фото совсем без подписи, а текст приветствия уходит отдельным
         сообщением следом: лучше текст без фото, чем молчаливое фото без текста.
    """
    data = await _cached_media_bytes(photo_id)
    if data is None:
        logger.warning("Не удалось скачать фото приветствия — отправляю текстом")
        return False

    photo = BufferedInputFile(data, filename="welcome.jpg")
    # Подпись к фото ограничена 1024 символами. Режем ДО конвертации в
    # сущности, иначе можно разорвать открывающий тег и получить битый текст.
    cap = (caption or "").strip()[:1024] or None

    # ── 1. Сущностями без parse_mode: Telegram принимает разметку «как есть» ──
    if cap:
        raw, entities = premium.markup_to_entities(cap)
        try:
            await message.answer_photo(photo=photo, caption=raw,
                                       caption_entities=entities or None,
                                       parse_mode=None, reply_markup=reply_markup)
            return True
        except TelegramBadRequest as e:
            logger.warning("Приветствие с фото не отправилось сущностями: %s", e)

        # ── 2. Тот же текст, но уже без тегов премиум-эмодзи ──
        plain = premium.EMOJI_TAG_RE.sub(r"\2", cap)
        try:
            await message.answer_photo(photo=photo, caption=plain,
                                       reply_markup=reply_markup)
            return True
        except TelegramBadRequest as e:
            logger.warning("Приветствие с фото не отправилось текстом: %s", e)

    # ── 3. Фото гарантированно уходит, текст — отдельным сообщением ──
    try:
        await message.answer_photo(photo=photo, reply_markup=reply_markup)
    except Exception as e:
        logger.warning("Не удалось отправить фото приветствия: %s", e)
        return False
    if cap:
        await _safe_answer(message, cap, reply_markup)
    return True


def _build_bot_commands() -> list[BotCommand]:
    """Команды дочернего бота — то самое меню по кнопке у поля ввода.

    Раньше действие «сменить админа» висело на reply-кнопке под полем ввода,
    и пользователи жаловались на случайные нажатия: сообщение отправлялось
    вместо текста ПЗ. Команда такого не делает — её нужно выбрать руками.

    Команда ``start`` добавлена тоже: без неё меню выглядит пустым, хотя /start
    — самая частая команда.
    """
    return [
        BotCommand(command="start", description="Перезапустить бота"),
        BotCommand(command="smena", description="Сменить админа"),
    ]


async def _set_bot_commands(bot_obj: Bot) -> None:
    """Публикует меню команд дочернего бота.

    Ошибка здесь не критична (например, у бота могут быть ограничения на
    команды), поэтому просто пишем в журнал и идём дальше — приветствие
    пользователю всё равно покажем.
    """
    try:
        await bot_obj.set_my_commands(_build_bot_commands())
    except Exception:
        logger.debug(
            "Не удалось настроить команды бота",
            exc_info=True,
        )


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


# Эмодзи, которые Telegram принимает в ``send_dice``. Другие он отвергает,
# поэтому незнакомый эмодзи подменяем стандартным кубиком.
_DICE_EMOJI = ("🎲", "🎯", "🏀", "⚽", "🎳", "🎰")

# Типы сообщений, которые боту переслать НЕЛЬЗЯ (Telegram не даёт пересоздать
# их через Bot API). Вместо самого сообщения получатель увидит понятную
# пометку: молчаливая потеря тут хуже всего — админ ответил, а ПЗ не понял,
# что вообще что-то было.
_NON_COPYABLE_NOTES = {
    "poll": "📊 Опрос — Telegram не позволяет боту переслать его.",
    "story": "📸 История — Telegram не даёт боту переслать её.",
    "game": "🎮 Игра — Telegram не даёт боту переслать её.",
    "paid_media": "💰 Платное медиа — бот не может его переслать.",
    "invoice": "🧾 Счёт — бот не может его переслать.",
}


def _content_type_of(source_msg: Message) -> str:
    """Строковый тип содержимого сообщения (``location``, ``poll``, …).

    Нужен именно строковый вид: ``Message.content_type`` в aiogram возвращает
    ``ContentType`` — это str-enum, у которого ``str()`` даёт
    ``ContentType.LOCATION``, а не ``location``. Из-за этого поиск по словарю
    молча не срабатывал бы.
    """
    ctype = source_msg.content_type
    return str(getattr(ctype, "value", ctype))


def _dice_emoji(source_msg: Message) -> str:
    """Эмодзи «кубика»; незнакомый Telegram не примет — берём стандартный."""
    dice = source_msg.dice
    emoji = dice.emoji if dice and dice.emoji else ""
    return emoji if emoji in _DICE_EMOJI else "🎲"


def _describe_message(source_msg: Message) -> str:
    """Короткое описание сообщения — для журнала и диагностики.

    Без него строка «сообщение не доставлено» бесполезна: непонятно, ЧТО
    именно потерялось. Пример: ``текст «привет, хочу заказ»`` или ``геолокация``.
    """
    if source_msg.photo:
        return "фото"
    if source_msg.video:
        return "видео"
    if source_msg.sticker:
        return "стикер"
    if source_msg.animation:
        return "GIF"
    if source_msg.voice:
        return "голосовое"
    if source_msg.video_note:
        return "видео-кружок"
    if source_msg.audio:
        return "аудио"
    if source_msg.document:
        name = (source_msg.document.file_name or "") if source_msg.document else ""
        return f"файл «{name}»" if name else "файл"
    if source_msg.location or source_msg.venue:
        return "геолокация"
    if source_msg.contact:
        return "контакт"
    if source_msg.dice:
        return f"кубик {_dice_emoji(source_msg)}"
    if source_msg.poll:
        return "опрос"

    text = (source_msg.text or source_msg.caption or "").strip().replace("\n", " ")
    if text:
        preview = text[:60] + ("…" if len(text) > 60 else "")
        return f"текст «{preview}»"
    return f"сообщение типа {_content_type_of(source_msg)}"


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

    # ── Типы, которых нет в _source_media ───────────────────────────────
    # Раньше они проваливались в проверку текста ниже и молча возвращали None:
    # жалоба «некоторые сообщения не доходят, и непонятно какие». Теперь их
    # либо пересылаем как есть (геолокация, контакт, кубик), либо честно
    # помечаем текстом, если Telegram пересоздать сообщение не даёт.
    if source_msg.location is not None:
        return await bot.send_location(
            **kwargs,
            latitude=source_msg.location.latitude,
            longitude=source_msg.location.longitude,
        )

    if source_msg.venue is not None:
        venue = source_msg.venue
        return await bot.send_venue(
            **kwargs,
            latitude=venue.location.latitude,
            longitude=venue.location.longitude,
            title=venue.title,
            address=venue.address,
        )

    if source_msg.contact is not None:
        contact = source_msg.contact
        return await bot.send_contact(
            **kwargs,
            phone_number=contact.phone_number,
            first_name=contact.first_name,
            last_name=contact.last_name or "",
        )

    if source_msg.dice is not None:
        return await bot.send_dice(**kwargs, emoji=_dice_emoji(source_msg))

    note = _NON_COPYABLE_NOTES.get(_content_type_of(source_msg))
    if note:
        logger.info("Сообщение типа %s боту переслать нельзя — отправляю пометку",
                    _content_type_of(source_msg))
        return await bot.send_message(**kwargs, text=note, **extra)

    text_kw = _text_kwargs(source_msg, native)
    if not text_kw["text"]:
        # Ни текста, ни известного медиа: тип, который бот пока не умеет
        # копировать. Раньше отсюда МОЛЧА возвращался None — сообщение
        # исчезало без единой строки в журнале. Теперь это видно в логе.
        logger.warning("Переслать не удалось (%s): тип %s бот не поддерживает",
                       _describe_message(source_msg), _content_type_of(source_msg))
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
    except Exception:
        # НЕ глотаем ошибку: вызывающий код (_send_to_topic_retry) сам решает,
        # повторить отправку на временном сбое или пересоздать удалённый топик.
        # Пишем на уровне DEBUG: удалённый топик — штатная ситуация, и её
        # обрабатывает вызывающий код, а не ошибка отправки.
        logger.debug("Не удалось отправить в топик %s", topic_id, exc_info=True)
        raise


async def _send_to_topic_retry(source_msg: Message, bot: Bot,
                               group_chat_id: int, topic_id: int,
                               reply_to: int | None = None,
                               bot_id: int = 0) -> tuple[Message | None, bool]:
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
        if _is_chat_gone(e):
            # Группы, в которой жили топики, больше нет. Пересоздавать топик
            # бессмысленно: снимаем привязку, чтобы бот не долбил мёртвый чат.
            await handle_dead_chat(bot_id, group_chat_id, _error_text(e))
            return None, False
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
        logger.warning("ПЗ %s заблокировал бота — %s не доставлено",
                       chat_id, _describe_message(source_msg))
        if bot_id:
            await _handle_user_blocked(bot, bot_id, chat_id)
        return None
    except Exception as e:
        logger.error("Не удалось отправить ПЗ %s: %s | не доставлено: %s",
                     chat_id, e, _describe_message(source_msg))
    return None


async def _handle_user_blocked(bot: Bot, bot_id: int, user_chat_id: int) -> None:
    """Юзер забанил бота: помечаем заблокированным, переименовываем и закрываем топик,
    уведомляем админа в этом же топике."""
    try:
        mark_user_blocked(bot_id, user_chat_id)
    except Exception:
        logger.debug(
            "Исключение проглочено",
            exc_info=True,
        )

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
        logger.debug(
            "Исключение проглочено",
            exc_info=True,
        )

    try:
        await bot.send_message(
            chat_id=g_id, message_thread_id=t_id,
            text=f"🚫 Пользователь <code>{user_chat_id}</code> забанил бота.\nТопик закрыт."
        )
    except Exception:
        logger.debug(
            "Исключение проглочено",
            exc_info=True,
        )

    try:
        await bot.close_forum_topic(chat_id=g_id, message_thread_id=t_id)
    except Exception:
        logger.debug(
            "Исключение проглочено",
            exc_info=True,
        )


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
    # Логгер этого бота: все его записи попадают в logs/bots/bot_<id>.log.
    bot_log = bot_logger(bot_id, __name__)

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
        bot_log.error("Ошибка обработчика: %s", event.exception,
                      exc_info=event.exception)
        return True

    child_dp.errors.register(_log_child_error)

    # Помечаем контекст журналирования: пока обрабатывается апдейт этого бота,
    # ВСЕ записи логов (наши и сторонних библиотек) относятся к нему и
    # пишутся в logs/bots/bot_<id>.log. Иначе в журнале бота не оказалось бы
    # ничего, а разбираться пришлось бы по общему файлу.
    @child_dp.update.outer_middleware()
    async def _bind_bot_context(handler, event, data):
        with bot_context(bot_id, f"@{bot_data.get('username') or ''}"):
            return await handler(event, data)

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

        # Инлайн-кнопки (ссылки) крепим ПРЯМО к приветствию. Reply-клавиатуры
        # больше нет: «сменить админа» живёт в меню команд бота (/smena),
        # поэтому случайно отправить команду вместо текста ПЗ нельзя.
        welcome_kb = _build_welcome_kb(fresh)

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
            add_stat(bot_id, "message_out")
            return

        if welcome_kb:
            if not await _safe_answer(message, welcome, welcome_kb):
                await _safe_answer(message, welcome)
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

        # Забанить может только админ этого бота: команда необратимо убирает ПЗ.
        if not _is_bot_admin(bot_id, msg_uid(message)):
            logger.warning("Забанен не-админ: %s в топике %s бота %s",
                           msg_uid(message), thread_id, bot_id)
            await _deny_not_admin(message)
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
            logger.debug(
                "Исключение проглочено",
                exc_info=True,
            )

        try:
            await bot_obj.close_forum_topic(
                chat_id=group_chat_id, message_thread_id=thread_id
            )
        except Exception:
            logger.debug(
                "Исключение проглочено",
                exc_info=True,
            )

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

        # Разбанить может только админ этого бота.
        if not _is_bot_admin(bot_id, msg_uid(message)):
            logger.warning("Разбан не-админом: %s в топике %s бота %s",
                           msg_uid(message), thread_id, bot_id)
            await _deny_not_admin(message)
            return

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
            logger.debug(
                "Исключение проглочено",
                exc_info=True,
            )

        # Открываем топик если был закрыт
        try:
            await bot_obj.reopen_forum_topic(
                chat_id=group_chat_id,
                message_thread_id=thread_id
            )
        except Exception:
            logger.debug(
                "Исключение проглочено",
                exc_info=True,
            )

        # Отправляем кнопку "Я беру"
        sent = await bot_obj.send_message(
            chat_id=group_chat_id,
            message_thread_id=thread_id,
            text=f"🔓 Пользователь <code>{user_chat_id}</code> разбанен.",
            reply_markup=_topic_action_kb(thread_id, group_chat_id),
        )
        # Шапка с кнопками закрепляется: иначе админ не найдёт, от чего
        # отказываться.
        await _pin_topic_action(bot_obj, bot_id, group_chat_id, thread_id, sent)

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

        # Отказаться может только админ, причём именно тот, кто ведёт ПЗ:
        # иначе любой участник чата «отбирал» бы обращение у коллеги.
        sender_id = msg_uid(message)
        current_admin = int(topic.get("admin_user_id") or 0)
        if not _is_bot_admin(bot_id, sender_id):
            logger.warning("Отказ не-админом: %s в топике %s бота %s",
                           sender_id, thread_id, bot_id)
            await _deny_not_admin(message)
            return
        if current_admin and current_admin != sender_id:
            await message.answer("⚠️ Это обращение ведёт другой админ. "
                                 "Отказаться можете только вы, если возьмёте его.")
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
            logger.debug(
                "Исключение проглочено",
                exc_info=True,
            )

        await bot_obj.send_message(
            chat_id=user_chat_id,
            text="⚠️ Ваш администратор отказался от вас.\nПодобрать нового?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Найти админа",
                                       callback_data=f"find_admin_{thread_id}_{group_chat_id}")]
            ])
        )
        await message.answer("✅ Пользователю отправлено уведомление.")

    # ═══════════════ Callback: «Отправить приветствие?» ═══════════════

    @child_dp.callback_query(F.data.regexp(r"^greet_(yes|no)_-?\d+_-?\d+$"))
    async def cb_greet_decision(callback: CallbackQuery) -> None:
        """Админ ответил, отправлять ли заготовленное приветствие.

        Фильтр строго ``^greet_(yes|no)_<топик>_<чат>$``: префиксы не должны
        пересекаться с другими кнопками, иначе нажатие попадёт в чужой
        обработчик (такой случай уже был с «refuse_»).
        """
        data = cb_data(callback)
        send = data.startswith("greet_yes_")
        ids = parse_topic_ids(data, 2, 2)
        if ids is None:
            await callback.answer("⚠️ Кнопка устарела — обновите сообщение",
                                  show_alert=True)
            return
        topic_id, group_chat_id = ids

        ok, note = await _apply_greeting_decision(
            bot_obj, bot_id, cb_uid(callback), topic_id, group_chat_id, send,
        )

        if not ok:
            await callback.answer(note, show_alert=True)
            return

        await callback.answer(note)
        # Кнопки снимаем: повторное нажатие не должно отправить приветствие
        # второй раз или переписать результат.
        await try_edit_answer(callback.message, note)

    # ═══════════════ Callback: "я беру" ═══════════════

    @child_dp.callback_query(F.data.regexp(r"^take_user_-?\d+_-?\d+$"))
    async def cb_take_user(callback: CallbackQuery) -> None:
        ids = parse_topic_ids(cb_data(callback), 2, 2)
        if ids is None:
            await callback.answer("⚠️ Кнопка устарела — обновите сообщение",
                                  show_alert=True)
            return
        topic_id, group_chat_id = ids

        # Запись топика нужна для приветствия: ПЗ отправляем именно ему.
        # Проверка обязательна: записи может уже не быть (ПЗ заблокировал бота,
        # топик удалили и пересоздали, кнопка осталась от старого сообщения).
        # Раньше здесь падало «'NoneType' object has no attribute 'get'»
        # на строке с topic.get("user_chat_id") ниже.
        topic = get_topic_by_topic_id(bot_id, group_chat_id, topic_id)
        if not topic:
            await callback.answer("❌ Обращение не найдено", show_alert=True)
            return

        me_id = cb_uid(callback)
        # Взять обращение может только админ этого бота (или владелец):
        # кнопка «✋ Я беру» лежит в общем чате и доступна всем участникам.
        if not _is_bot_admin(bot_id, me_id):
            logger.warning("Взятие ПЗ не-админом: %s в топике %s бота %s",
                           me_id, topic_id, bot_id)
            await callback.answer("⛔ Только админ этого бота может взять обращение",
                                  show_alert=True)
            return

        admin_tag = _bot_admin_tag(bot_id, me_id)
        # «owner:<id>» — владелец без записи в admins; учитываем это отдельно,
        # чтобы не засчитывать ему «действие админа» в статистике.
        is_registered_admin = bool(admin_tag) and not admin_tag.startswith("owner:")
        tag = admin_tag or str(me_id)
        if tag.startswith("owner:"):
            # У владельца нет тега админа — берём его ник, чтобы топик
            # назывался админским тегом, а не служебным «owner:123».
            if getattr(callback.from_user, "username", None):
                tag = f"@{cb_username(callback)}"
            elif getattr(callback.from_user, "first_name", None):
                tag = cb_firstname(callback)
            else:
                tag = str(me_id)
            logger.debug("Владелец %s взял ПЗ без тега админа, используем ник как тег",
                         me_id)

        # Назначаем и получаем инфу
        result = assign_admin_to_topic(bot_id, topic_id, group_chat_id, me_id, tag)
        if not result.get("ok"):
            # Строка топика исчезла между чтением и записью (например, ПЗ
            # заблокировал бота прямо в этот момент) — честно отказываем.
            await callback.answer("❌ Обращение уже недоступно", show_alert=True)
            return

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=topic_id, name=f"#{tag}"
            )
        except Exception as e:
            logger.warning("Не удалось переименовать топик: %s", e)

        if is_registered_admin:
            add_admin_message(bot_id, cb_uid(callback), "action")

        # Приветствие больше НЕ уходит автоматически: админ подтверждает
        # кнопкой (см. _ask_admin_greeting). Раньше текст улетал ПЗ сразу, и
        # отменить отправленное было нельзя.
        await _ask_admin_greeting(bot_obj, bot_id, topic_id, group_chat_id, me_id)

        # Только редактируем текст — НЕ удаляем инфу о юзере. Клавиатуру НЕ
        # снимаем целиком: исчезает лишь «✋ Я беру», а «🚫 Отказ» остаётся —
        # иначе админ, взявший обращение, не мог от него отказаться.
        try:
            if callback.message:
                rm = getattr(callback.message, "reply_markup", None)
                if rm:
                    original_text = getattr(callback.message, "html_text", None) \
                        or getattr(callback.message, "text", None) or ""
                    new_text = f"{original_text}\n\n✅ Взял: <b>#{tag}</b>"
                    await try_edit(callback.message, new_text,
                                    reply_markup=_topic_refuse_only_kb(topic_id, group_chat_id))
        except Exception:
            logger.debug(
                "Исключение проглочено",
                exc_info=True,
            )

        # Если это была смена админа — уведомляем
        if result["is_change"]:
            try:
                await bot_obj.send_message(
                    chat_id=group_chat_id,
                    message_thread_id=topic_id,
                    text=f"🔄 Админ сменился на <b>#{tag}</b>"
                )
            except Exception:
                logger.debug(
                    "Исключение проглочено",
                    exc_info=True,
                )

        await callback.answer(f"Ты взял пользователя. Тег: #{tag}")
# ═══════════════ Callback: «Отказ» от обращения ════════════════════

    @child_dp.callback_query(F.data.regexp(r"^refuse_-?\d+_-?\d+$"))
    async def cb_refuse(callback: CallbackQuery) -> None:
        """Админ отказался от обращения — спрашиваем, как об этом узнать ПЗ.

        Разница важна админу: «анонимно» — ПЗ ничего не узнает (выглядит как
        обычная смена админа), «сообщить» — ПЗ получит уведомление об отказе.
        В обоих случаях тема освобождается и нужен другой админ.

        Фильтр — строго ``^refuse_<число>_<число>$``. Прежний
        ``startswith("refuse_")`` перехватывал ещё и «refuse_q_…» / «refuse_cancel»,
        потому что этот хендлер объявлен раньше остальных: кнопки «Анонимно»,
        «Сообщить ПЗ» и «Отмена» попадали сюда и падали на int("q").
        """
        ids = parse_topic_ids(cb_data(callback), 1, 2)
        if ids is None:
            await callback.answer("⚠️ Кнопка устарела — обновите сообщение",
                                  show_alert=True)
            return
        topic_id, group_chat_id = ids

        topic = get_topic_by_topic_id(bot_id, group_chat_id, topic_id)
        if not topic:
            await callback.answer("❌ Обращение не найдено", show_alert=True)
            return

        # Отказаться может только тот, кто сейчас ведёт обращение.
        current_admin = int(topic.get("admin_user_id") or 0)
        if current_admin and current_admin != cb_uid(callback):
            await callback.answer("❌ Это не твоё обращение", show_alert=True)
            return

        # Вопрос уходит ОТДЕЛЬНЫМ сообщением — шапку ПЗ с кнопками
        # «✋ Я беру» / «🚫 Отказ» не переписываем (см. _ask_refuse_confirm).
        await _ask_refuse_confirm(bot_obj, topic_id, group_chat_id)
        await callback.answer()

    @child_dp.callback_query(F.data.regexp(r"^refuse_q_-?\d+_-?\d+_[01]$"))
    async def cb_refuse_confirm(callback: CallbackQuery) -> None:
        """Админ выбрал, уведомлять ли ПЗ об отказе."""
        ids = parse_topic_ids(cb_data(callback), 2, 3)
        if ids is None:
            await callback.answer("⚠️ Кнопка устарела — обновите сообщение",
                                  show_alert=True)
            return
        topic_id, group_chat_id, notify_flag = ids
        notify_pz = notify_flag == 1

        topic = get_topic_by_topic_id(bot_id, group_chat_id, topic_id)
        if not topic:
            await callback.answer("❌ Обращение не найдено", show_alert=True)
            return

        # Отказаться может только тот, кто сейчас ведёт обращение.
        # Проверка обязательна и ЗДЕСЬ: вопрос об отказе уходит отдельным
        # сообщением в общий топик, поэтому кнопки «Анонимно»/«Сообщить ПЗ»
        # видит ЛЮБОЙ админ чата. Без этой проверки чужой админ завершал бы
        # отказ за коллегу.
        current_admin = int(topic.get("admin_user_id") or 0)
        if current_admin and current_admin != cb_uid(callback):
            await callback.answer("❌ Это не твоё обращение", show_alert=True)
            return

        user_chat_id = int(topic["user_chat_id"])
        reset_topic_admin(bot_id, topic_id, group_chat_id)
        # Последним писал ПЗ, а не админ: иначе напоминалка сочла бы тему
        # отвеченной и не напомнила бы про неё новому админу.
        touch_topic_activity(bot_id, topic_id, group_chat_id, "in")
        add_admin_message(bot_id, cb_uid(callback), "action")

        # Тема освобождена в обоих случаях — на неё нужен другой админ.
        await _rename_topic(bot_obj, group_chat_id, topic_id, "🔄 смена админа")

        if notify_pz:
            try:
                await bot_obj.send_message(
                    chat_id=user_chat_id,
                    text=(
                        "😔 К сожалению, ваш админ отказался вести это "
                        "обращение.\n\nМы уже ищем другого администратора — "
                        "он скоро свяжется с вами."
                    ),
                )
            except Exception:
                logger.debug("Не удалось уведомить ПЗ об отказе", exc_info=True)
        # При анонимном отказе ПЗ не получает НИЧЕГО — видит только обычную
        # смену админа.

        try:
            sent = await bot_obj.send_message(
                chat_id=group_chat_id,
                message_thread_id=topic_id,
                text=(
                    "🚫 <b>Админ отказался от обращения</b>\n\n"
                    + ("Пользователь уведомлён об отказе."
                       if notify_pz else
                       "Пользователь не уведомлён (анонимный отказ).")
                    + "\nНужен другой админ."
                ),
                reply_markup=_topic_action_kb(topic_id, group_chat_id),
            )
        except Exception:
            logger.debug("Не удалось отправить отметку об отказе", exc_info=True)
        else:
            # Шапка с кнопками встаёт в закреп вместо старой: иначе админ
            # искал бы «Я беру» в переписке ПЗ, которая уже уехала вверх.
            await _pin_topic_action(bot_obj, bot_id, group_chat_id, topic_id, sent)

        await callback.answer("Отказ оформлен", show_alert=True)
        await try_edit_answer(
            callback.message,
            "🚫 Отказ оформлен, обращение освобождено.",
        )

    @child_dp.callback_query(F.data == "refuse_cancel")
    async def cb_refuse_cancel(callback: CallbackQuery) -> None:
        """Админ передумал отказываться.

        Правим ТОЛЬКО сообщение с вопросом об отказе: шапка ПЗ с кнопками
        «✋ Я беру» / «🚫 Отказ» при отказе не переписывается (см.
        ``_ask_refuse_confirm``), поэтому после отмены обе кнопки остаются на
        месте — обращение можно взять или отклонить позже в любой момент.

        Регресс: раньше вопрос подменял текст шапки, а здесь она затиралась
        на «Отказ отменён» вообще без клавиатуры — админ терял и инфу о ПЗ, и
        обе кнопки, вернуть их было нечем.
        """
        await callback.answer("Отменено")
        await try_edit_answer(callback.message, "👌 Отказ отменён.")


    # ═══════════════ Callback: "найти админа" ═══════════════

    @child_dp.callback_query(F.data.regexp(r"^find_admin_-?\d+_-?\d+$"))
    async def cb_find_admin(callback: CallbackQuery) -> None:
        ids = parse_topic_ids(cb_data(callback), 2, 2)
        if ids is None:
            await callback.answer("⚠️ Кнопка устарела — обновите сообщение",
                                  show_alert=True)
            return
        topic_id, group_chat_id = ids

        reset_topic_admin(bot_id, topic_id, group_chat_id)

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=topic_id, name="⏳ без админа"
            )
        except Exception:
            logger.debug(
                "Исключение проглочено",
                exc_info=True,
            )

        sent = await bot_obj.send_message(
            chat_id=group_chat_id, message_thread_id=topic_id,
            text="🔔 Пользователь запросил нового админа!",
            reply_markup=_topic_action_kb(topic_id, group_chat_id),
        )
        await _pin_topic_action(bot_obj, bot_id, group_chat_id, topic_id, sent)

        await try_edit(callback.message, "✅ Запрос отправлен.")
        await callback.answer()

    # ═══════════════ Callback: подтверждение смены ═══════════════

    @child_dp.callback_query(F.data.regexp(r"^confirm_change_(yes|no)_-?\d+_-?\d+$"))
    async def cb_confirm_change(callback: CallbackQuery) -> None:
        parts = cb_data(callback).split("_")
        answer = parts[2]
        ids = parse_topic_ids(cb_data(callback), 3, 2)
        if ids is None:
            await callback.answer("⚠️ Кнопка устарела — обновите сообщение",
                                  show_alert=True)
            return
        topic_id, group_chat_id = ids
        topic = get_topic_by_topic_id(bot_id, group_chat_id, topic_id)

        if answer == "yes":
            # Лимит смен в сутки: считаем здесь же, чтобы не пустить сверх лимита.
            if topic is None:
                await try_edit(callback.message, "❓ Обращение не найдено")
                await callback.answer("Обращение не найдено", show_alert=True)
                return

            if admin_changes_left(bot_id, topic["user_chat_id"]) == 0:
                await try_edit(
                    callback.message,
                    "⏳ Смен на сегодня не осталось — попробуй завтра.",
                )
                await callback.answer("Лимит смен исчерпан", show_alert=True)
                return

            log_admin_change(bot_id, topic["user_chat_id"])

            # Общий сценарий смены: сбросить админа, предупредить ПЗ,
            # переименовать топик и повесить кнопки «Я беру» / «Отказ».
            await _release_and_announce(
                bot_obj, bot_id, topic_id, group_chat_id,
                int(topic["user_chat_id"]),
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

    # ═══════════════ /smena в личке (было reply-кнопкой) ═════════════════

    @child_dp.message(Command("smena"), F.chat.type == ChatType.PRIVATE)
    async def cmd_smena_private(message: Message) -> None:
        """Пользователь в личке попросил сменить админа.

        Раньше это была reply-кнопка под полем ввода, и её регулярно отправляли
        случайно — вместе с текстом обращения. Теперь действие нужно выбрать
        руками из меню команд бота.

        Команда работает ровно как текст «сменить админа»: показывает остаток
        смен на сегодня и просит подтверждение. Раньше она сбрасывала админа
        сразу — без подтверждения, без проверки суточного лимита и без записи
        в журнал смен, из-за чего лимит можно было обходить бесконечно.
        """
        user_chat_id = msg_uid(message)
        topic = get_topic_by_user(bot_id, user_chat_id)
        if not topic:
            await message.answer(
                "🔄 <b>Сменить админа пока нельзя</b>\n\n"
                "У тебя ещё нет обращения в работе. Как только напишешь — "
                "появится кнопка, чтобы поменять админа.",
            )
            return

        await _ask_change_admin(bot_obj, bot_id, user_chat_id, topic)

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
                logger.debug(
                    "Исключение проглочено",
                    exc_info=True,
                )
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

        # Обработка «сменить админа» — тот же вход, что и у команды /smena.
        if message.text and message.text.strip().lower() == "сменить админа":
            topic = get_topic_by_user(bot_id, user_chat_id)
            if not topic:
                await message.answer(
                    "🔄 <b>Сменить админа пока нельзя</b>\n\n"
                    "У тебя ещё нет обращения в работе. Как только напишешь — "
                    "появится кнопка, чтобы поменять админа.",
                )
                return

            await _ask_change_admin(bot_obj, bot_id, user_chat_id, topic)
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
                            logger.debug(
                                "Исключение проглочено",
                                exc_info=True,
                            )

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
                                logger.debug(
                                    "Исключение проглочено",
                                    exc_info=True,
                                )
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
                header = await _send_with_gate(
                    bot_obj, group_chat_id,
                    lambda: bot_obj.send_message(
                        chat_id=group_chat_id, message_thread_id=new_topic_id,
                        text=header_text,
                        reply_markup=_topic_action_kb(new_topic_id, group_chat_id),
                    ),
                )
            except Exception as e:
                logger.warning("Не удалось отправить заголовок топика: %s", e)
            else:
                # Шапку нового ПЗ закрепляем сразу: в ней инфа об обращении и
                # кнопки «✋ Я беру» / «🚫 Отказ». Дальше она уедет вверх под
                # перепиской, и админ не найдёт, от чего отказываться.
                await _pin_topic_action(bot_obj, bot_id, group_chat_id,
                                        new_topic_id, header)

            sent, _ = await _send_to_topic_retry(message, bot_obj, group_chat_id,
                                                 new_topic_id, bot_id=bot_id)
            if sent:
                save_feedback_message(bot_id, new_topic_id, group_chat_id, user_chat_id,
                                       "in", sent.message_id, message.message_id)
                if message.text:
                    save_log_message(bot_id, user_chat_id, "in", message.text,
                                     msg_username(message) or "")
            else:
                # Первое сообщение ПЗ не доехало в только что созданный топик.
                # Без описания тут не понять, ЧТО потерялось: ПЗ «молчит», а
                # в журнале только техническая ошибка отправки.
                logger.warning(
                    "Первое сообщение ПЗ %s не доставлено в топик %s. Сообщение: %s",
                    user_chat_id, new_topic_id, _describe_message(message),
                )

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
                message, bot_obj, group_chat_id, topic_id, reply_to_group, bot_id=bot_id
            )

            if sent:
                save_feedback_message(bot_id, topic_id, group_chat_id, user_chat_id,
                                       "in", sent.message_id, message.message_id)
                # Текст сохраняем отдельно: Telegram не отдаёт историю, а по
                # логам пользователь шлёт техподдержку обращение.
                if message.text:
                    save_log_message(bot_id, user_chat_id, "in", message.text,
                                     msg_username(message) or "")
                # ПЗ написал — значит ответ админа снова ждут: помечаем
                # активность топика, чтобы напоминалка отсчитывала срок заново.
                touch_topic_activity(bot_id, topic_id, group_chat_id, "in")
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
                # Пишем, что именно не доехало: по строке «не удалось
                # доставить» без описания невозможно понять, что теряется.
                logger.warning(
                    "Не удалось доставить сообщение в топик %s (юзера %s), "
                    "но топик рабочий. Сообщение: %s",
                    topic_id, user_chat_id, _describe_message(message),
                )

    # ═══════════════ Закрытие/открытие топика ═══════════════
    #
    # Эти обработчики стоят ПЕРЕД group_topic_message не просто по порядку:
    # сервисные сообщения Telegram (forum_topic_closed и т.п.) приходят в том
    # же топике, с message_thread_id и БЕЗ from_user. Старый обработчик их не
    # отфильтровывал, поэтому служебное «Топик закрыт» уходило в ПЗ пользователю
    # как обычное сообщение. Здесь они обрабатываются по назначению, а
    # group_topic_message ниже добавлен защитной проверкой на from_user.

    @child_dp.message(
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id"),
        F.forum_topic_closed,
    )
    async def topic_closed(message: Message, thread_id: int) -> None:
        """Топик закрыт или удалён — убираем его из напоминалки.

        Жалоба: «топик удалили, ПЗ не пишет, новый не создаётся — а бот
        продолжает присылать "ПЗ без админа" по нему вечно».

        Проверить существование топика через Bot API нельзя (метода
        getForumTopic нет), поэтому единственный автоматический сигнал — это
        сервисное сообщение. Запись ПЗ при этом НЕ удаляется: топик просто
        помечается закрытым, и если админ откроет его снова, метка снимется
        (см. topic_reopened) и напоминания возобновятся.
        """
        changed = set_topic_closed(bot_id, thread_id, message.chat.id, True)
        if changed:
            logger.info(
                "Бот %s: топик %s закрыт/удалён — исключён из напоминалки",
                bot_id, thread_id,
            )

    @child_dp.message(
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id"),
        F.forum_topic_reopened,
    )
    async def topic_reopened(message: Message, thread_id: int) -> None:
        """Топик открыли снова — снимаем метку, напоминания возвращаются."""
        changed = set_topic_closed(bot_id, thread_id, message.chat.id, False)
        if changed:
            logger.info(
                "Бот %s: топик %s снова открыт — напоминалка возобновлена",
                bot_id, thread_id,
            )

    # ═══════════════ Сообщения из топика → юзеру ═══════════════

    @child_dp.message(
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id")
    )
    async def group_topic_message(message: Message, thread_id: int) -> None:
        # Служебные сообщения форума (закрытие/открытие/переименование топика)
        # не являются репликой админа: у них нет from_user, и пересылать их
        # пользователю бессмысленно. Разбираются выше отдельными хендлерами.
        if message.from_user is None:
            return
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
        else:
            # Ответ админа БЫЛ, даже если сообщение не дошло до ПЗ (у него
            # нерабочее время, он заблокировал бота, Telegram моргнул и т.п.).
            # Раньше в этом случае запись не появлялась, и напоминалка
            # приходила с «ПЗ без ответа!» на ПЗ, которому админ ответил.
            # Поэтому отмечаем активность топика независимо от доставки.
            # В строку добавлено, ЧТО именно не доехало: без этого «сообщение
            # не доставлено» не помогало понять, какие сообщения теряются.
            logger.info(
                "Бот %s: ответ админа в топик %s (%s) не доставлен ПЗ %s, "
                "но засчитан как ответ. Сообщение: %s",
                bot_id, thread_id, topic_web_link(group_chat_id, thread_id),
                user_chat_id, _describe_message(message),
            )

        # Отметка активности топика: последним написал АДМИН, значит ПЗ
        # отвечено и напоминалка «без ответа» по нему больше не придёт.
        touch_topic_activity(bot_id, thread_id, group_chat_id, "out")

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

            # Меню команд бота (/start, /smena). /smena — для ПЗ в личке:
            # раньше «сменить админа» была reply-кнопкой под полем ввода, и её
            # случайно отправляли вместо текста ПЗ — теперь действие нужно
            # выбрать руками.
            await _set_bot_commands(child_bot)

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
            # Регистрируем бота в очереди доставки: сообщения, адресованные
            # ему, не должны «висеть без дела», пока бот не поднят. При
            # остановке (ниже) регистрация снимается, и очередь снова ждёт
            # своего часа — так сообщения переживают перезапуск бота.
            get_outbox().register_bot(bot_id, child_bot)

            # Успешный запуск: сбрасываем счётчик перезапусков и флаг остановки.
            self._restart_attempts[bot_id] = 0
            self._stopping.discard(bot_id)
            # Бот поднялся — снимаем пометку «мёртвый», если она была раньше
            # (например, токен починили и добавили бота заново).
            try:
                clear_bot_dead(bot_id)
            except Exception:
                logger.debug(
                    "Исключение проглочено",
                    exc_info=True,
                )

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
                logger.debug(
                    "Исключение проглочено",
                    exc_info=True,
                )
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
                            fire_and_forget(notify_owner(
                                bot_id,
                                "⚠️ <b>Токен бота занят другим процессом</b>\n\n"
                                "У этого бота второй экземпляр получает сообщения "
                                "(например, копия на другом сервере или подключение к "
                                "другому сервису). Из-за этого ответы и приветствие "
                                "могут приходить не от нашей панели.\n\n"
                                "Останови второго «слушателя» — и бот сам заработает "
                                "нормально (перезапускать вручную не нужно).",
                            ), name=f"notify_token_busy_{bot_id}")
                        except RuntimeError:
                            logger.debug(
                                "Исключение проглочено",
                                exc_info=True,
                            )
                else:
                    logger.error(
                        "Дочерний бот %s аварийно завершился: %s\n%s",
                        bot_id, exc, exc.__traceback__,
                    )
                # Планируем автоперезапуск (не из колбэка — это sync).
                try:
                    fire_and_forget(self._restart_bot_later(bot_id),
                                    name=f"restart_bot_{bot_id}")
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
        # Бот выключается — снимаем регистрацию, чтобы воркер не слал в него
        # сообщения и не получал ошибки. В очереди они останутся и уйдут, когда
        # бот снова поднимется.
        get_outbox().unregister_bot(bot_id)

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
