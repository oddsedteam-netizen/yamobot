"""Публикация постов в ТГК (Telegram-канал) и отложенная отправка.

Раздел «📢 Мой ТГК»: пользователь добавляет YamoBot в свой
канал админом, бот публикует посты от лица канала.

Модуль содержит:
  • разбор даты/времени, которые вводит пользователь (день — «17.08», время —
    «15:00»), перевод МСК → UTC для хранения в БД;
  • сборку инлайн-кнопок поста из сохранённого JSON (с цветом кнопки, если он
    задан: primary/success/danger);
  • ``publish_channel_post`` — сама публикация (текст/фото + кнопки) с фолбэком
    HTML → сущности → простой текст и проверкой, не вырезал ли Telegram
    премиум-эмодзи (``premium_lost``);
  • ``ChannelService`` — фоновый цикл, который каждые ``SCAN_INTERVAL`` секунд
    проверяет отложенные посты и выкладывает те, чьё время пришло.

Просмотры постов Bot API не отдаёт — в статистике канала показываем
подписчиков и то, что бот знает сам (опубликованные/отложенные посты).
"""

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from services.child_manager import (check_premium_delivered, get_main_bot,
                                    send_with_gate)
from services.premium_emoji import markup_to_entities
from services.storage import (
    get_bound_channel,
    get_channel_owner,
    get_due_channel_posts,
    update_channel_post,
)

logger = logging.getLogger(__name__)

# Как часто проверять отложенные посты (секунды).
SCAN_INTERVAL = 30

_UTC = timezone.utc
# Московское время (UTC+3) — день и время поста пользователь вводит в МСК.
_MSK = timezone(timedelta(hours=3))

# Что говорим владельцу, если Telegram тихо вырезал премиум-эмодзи из поста.
PREMIUM_LOST_HINT = (
    "⚠️ <b>Премиум-эмодзи не прошли</b>\n"
    "Telegram принял пост, но заменил премиум-эмодзи обычными смайликами: "
    "право на премиум-эмодзи привязано к боту — оно покупается на Fragment "
    "вместе с юзернеймом для бота. Обычные эмодзи и разметка поста на месте."
)

_DB_FMT = "%Y-%m-%d %H:%M:%S"


# ── Разбор даты и времени ─────────────────────────────────────────────


def parse_time(raw: str) -> str | None:
    """«15:00» / «15.00» / «15 00» / «15» → «15:00» (или None)."""
    text = (raw or "").strip()
    match = re.match(r"^(\d{1,2})[:.\s]?(\d{2})?$", text)
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2) or 0)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return f"{hours:02d}:{minutes:02d}"


def parse_day(raw: str, now: datetime | None = None) -> date | None:
    """Разбирает день публикации.

    Понимает: ``сегодня``, ``завтра``, ``17.08``, ``17.08.2026``, ``17/08``,
    ``17-08``. Возвращает дату или None (в т.ч. для прошедших дат).
    """
    text = (raw or "").strip().lower().replace("ё", "е")
    today = (now or datetime.now(_MSK)).astimezone(_MSK).date()

    if text in ("сегодня", "today", "сейчас"):
        return today
    if text in ("завтра", "tomorrow"):
        return today + timedelta(days=1)

    match = re.match(r"^(\d{1,2})[.\-/\s](\d{1,2})(?:[.\-/\s](\d{2,4}))?$", text)
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    year_raw = match.group(3)
    if year_raw:
        year = int(year_raw)
        if year < 100:
            year += 2000
    else:
        year = today.year

    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    if parsed < today:
        # Без года и дата уже прошла — считаем, что имели в виду следующий год.
        if not year_raw:
            try:
                parsed = date(year + 1, month, day)
            except ValueError:
                return None
        if parsed < today:
            return None
    return parsed


def to_utc_str(moment_msk: datetime) -> str:
    """Переводит момент времени в МСК → строку UTC для БД."""
    return moment_msk.astimezone(_UTC).strftime(_DB_FMT)


def to_msk_str(utc_str: str | None) -> str:
    """UTC-строка из БД → «ДД.ММ.ГГГГ ЧЧ:ММ» по МСК."""
    if not utc_str:
        return "—"
    try:
        dt = datetime.strptime(str(utc_str)[:19], _DB_FMT).replace(tzinfo=_UTC)
    except ValueError:
        return str(utc_str)
    return dt.astimezone(_MSK).strftime("%d.%m.%Y %H:%M")


def msk_moment(day: date, time_str: str) -> datetime:
    """Момент публикации в МСК по дню и времени."""
    hours, minutes = time_str.split(":")
    return datetime(day.year, day.month, day.day, int(hours), int(minutes),
                    tzinfo=_MSK)
# ── Кнопки поста ──────────────────────────────────────────────────────


# Цвета инлайн-кнопок (Telegram Bot API 9.0: primary/success/danger).
BUTTON_STYLES = ("primary", "success", "danger")

# Подписи цветов для интерфейса: что видит владелец при выборе/просмотре.
BUTTON_STYLE_LABELS = {
    "primary": "🔵 синий",
    "success": "🟢 зелёный",
    "danger": "🔴 красный",
    "": "⬜ без цвета",
}


def safe_button_style(value: Any) -> str:
    """Валидный цвет кнопки (пусто — цвет не задан или некорректен)."""
    style = str(value or "").strip().lower()
    return style if style in BUTTON_STYLES else ""


def parse_buttons(raw: Any) -> list[dict]:
    """Разбирает сохранённые кнопки поста (JSON-строка или список).

    Цвет кнопки (``style``) сохраняем: Telegram показывает его в канале,
    поэтому он должен переживать запись в БД и чтение обратно.
    """
    if isinstance(raw, list):
        data = raw
    else:
        try:
            data = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(data, list):
        return []
    result: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        url = str(item.get("url", "")).strip()
        if not text or not url.startswith(("http://", "https://", "tg://")):
            continue
        button = {"text": text[:64], "url": url}
        style = safe_button_style(item.get("style"))
        if style:
            button["style"] = style
        result.append(button)
    return result


def build_buttons(raw: Any) -> InlineKeyboardMarkup | None:
    """Инлайн-клавиатура поста из сохранённых кнопок (по одной в ряд)."""
    buttons = parse_buttons(raw)
    if not buttons:
        return None
    rows = [
        [InlineKeyboardButton(text=b["text"], url=b["url"],
                              style=safe_button_style(b.get("style")) or None)]
        for b in buttons
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows[:50])


def dump_buttons(buttons: list[dict]) -> str:
    """Сериализует кнопки поста для БД."""
    return json.dumps(parse_buttons(buttons), ensure_ascii=False)


def post_link(channel: dict | None, message_id: int) -> str:
    """Ссылка на вышедший пост (по username канала, иначе по ID)."""
    if not channel or not message_id:
        return ""
    username = str(channel.get("username") or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}/{message_id}"
    channel_id = int(channel.get("channel_id") or 0)
    if channel_id:
        return f"https://t.me/c/{abs(channel_id)}/{message_id}"
    return ""


# ── Публикация ────────────────────────────────────────────────────────


def _plain_text(text: str | None) -> str:
    """Убирает теги разметки — нужен для последнего фолбэка при публикации."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


async def publish_channel_post(bot: Bot, post: dict) -> tuple[bool, int, str, bool]:
    """Публикует пост в канал.

    Возвращает (ok, message_id, error_text, premium_lost). Текст поста хранится
    в HTML — значит, жирный/курсив/подчёркнутый/спойлер и премиум-эмодзи
    сохраняются.

    Если Telegram не принимает HTML (в тексте владельца попался символ, ломающий
    разметку), повторяем отправку «сырым» текстом с сущностями, собранными из
    тегов ``<tg-emoji>`` — так пост не теряется и премиум-эмодзи не пропадают.

    ``premium_lost`` = True, если разметка есть, а Telegram тихо вырезал
    премиум-эмодзи: сообщение ушло обычными смайликами (право на премиум-эмодзи
    привязано к боту и покупается на Fragment — со стороны кода это не лечится,
    но владельцу об этом нужно сказать).
    """
    channel_id = int(post.get("channel_id") or 0)
    if not channel_id:
        return False, 0, "неизвестный канал", False

    text = post.get("text") or ""
    photo = str(post.get("photo") or "").strip()
    markup = build_buttons(post.get("buttons"))

    if not text and not photo:
        return False, 0, "пустой пост", False

    async def _send_html():
        if photo:
            return await bot.send_photo(
                chat_id=channel_id, photo=photo,
                caption=text or None, reply_markup=markup,
            )
        return await bot.send_message(
            chat_id=channel_id, text=text, reply_markup=markup,
        )

    async def _send_entities():
        """Отправка без HTML: текст + сущности (включая custom_emoji)."""
        raw_text, entities = markup_to_entities(text)
        if photo:
            return await bot.send_photo(
                chat_id=channel_id, photo=photo, caption=raw_text or None,
                caption_entities=entities or None, parse_mode=None,
                reply_markup=markup,
            )
        return await bot.send_message(
            chat_id=channel_id, text=raw_text, entities=entities or None,
            parse_mode=None, reply_markup=markup,
        )

    async def _send_plain():
        """Последний шанс: простой текст без разметки — пост не теряем."""
        plain = _plain_text(text)
        if not plain and not photo:
            raise ValueError("пустой текст")
        if photo:
            return await bot.send_photo(
                chat_id=channel_id, photo=photo, caption=plain or None,
                parse_mode=None, reply_markup=markup,
            )
        return await bot.send_message(
            chat_id=channel_id, text=plain, parse_mode=None, reply_markup=markup,
        )

    try:
        message = await send_with_gate(bot, channel_id, _send_html)
    except Exception as e:
        logger.warning(
            "HTML-разметка не прошла (%s) — повторяю пост сущностями", e
        )
        try:
            message = await send_with_gate(bot, channel_id, _send_entities)
        except Exception as e2:
            logger.warning(
                "Отправка сущностями не удалась (%s) — отправляю простым текстом",
                e2,
            )
            try:
                message = await send_with_gate(bot, channel_id, _send_plain)
            except Exception as e3:
                logger.warning("Не удалось опубликовать пост в канал %s: %s",
                               channel_id, e3)
                return False, 0, str(e3), False

    # Telegram мог принять запрос, но вырезать премиум-эмодзи (тихий сбой).
    lost = check_premium_delivered(message, text, getattr(bot, "id", None))
    return True, int(getattr(message, "message_id", 0) or 0), "", lost


def channel_owner(channel_id: int) -> int | None:
    """Публичная обёртка: чей это канал (для обработчиков и сервисов)."""
    return get_channel_owner(channel_id)


def premium_emoji_lost(sent: Message | None, text: str,
                       bot_id: int | None = None) -> bool:
    """Публичная обёртка: Telegram принял сообщение, но вырезал премиум-эмодзи.

    Нужна обработчикам: превью поста показываем владельцу в личке, и если
    эмодзи не прошли — честно об этом пишем (правку делает только Telegram).
    """
    return check_premium_delivered(sent, text, bot_id)


# ── Фоновый сервис отложенных постов ──────────────────────────────────


class ChannelService:
    """Фоновый цикл: вовремя выкладывает отложенные посты каналов."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="channel_service")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        logger.info("Сервис отложенных постов ТГК запущен")
        while True:
            try:
                await self._scan_once()
            except Exception as e:
                logger.exception("Ошибка в цикле отложенных постов: %s", e)
            await asyncio.sleep(SCAN_INTERVAL)

    async def _scan_once(self) -> None:
        bot = get_main_bot()
        if bot is None:
            return
        now_str = datetime.now(_UTC).strftime(_DB_FMT)
        for post in get_due_channel_posts(now_str):
            try:
                await self._publish_due(bot, post)
            except Exception as e:
                logger.exception("Ошибка публикации отложенного поста %s: %s",
                                 post.get("id"), e)

    async def _publish_due(self, bot: Bot, post: dict) -> None:
        post_id = int(post["id"])
        owner_id = int(post["owner_id"])
        ok, message_id, error, premium_lost = await publish_channel_post(bot, post)

        if ok:
            update_channel_post(post_id, status="published", message_id=message_id)
        else:
            # Помечаем пост «неудачным», чтобы цикл не пытался слать его вечно,
            # и сообщаем владельцу, что нужно исправить.
            update_channel_post(post_id, status="failed")

        channel = get_bound_channel(owner_id)
        title = (channel or {}).get("title") or "канал"
        if ok:
            link = post_link(channel, message_id)
            text = (
                f"✅ <b>Пост опубликован</b>\n\n"
                f"📢 Канал: <b>{title}</b>\n"
                f"🕐 {to_msk_str(post.get('publish_at'))} (МСК)"
            )
            if link:
                text += f"\n🔗 {link}"
            if premium_lost:
                text += "\n\n" + PREMIUM_LOST_HINT
        else:
            text = (
                f"⚠️ <b>Не удалось опубликовать пост</b>\n\n"
                f"📢 Канал: <b>{title}</b>\n"
                f"❌ Причина: <code>{error[:200]}</code>\n\n"
                f"Проверь права бота в канале и выложи пост заново."
            )

        try:
            await send_with_gate(
                bot, owner_id, lambda: bot.send_message(chat_id=owner_id, text=text)
            )
        except Exception as e:
            logger.warning("Не удалось уведомить владельца %s о посте: %s", owner_id, e)