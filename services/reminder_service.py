"""Фоновый сервис напоминалок.

Три задачи (первые две настраиваются владельцем в профиле → «Напоминалка»):

1) «авточек ответа админа» (mode='check_admin'):
   следит за ПЗ, у которых есть админ. Если админ не ответил за заданное время —
   шлёт в «чат админов» напоминание с тегом админа и ссылкой на топик:
       #тег ПЗ без ответа уже 1 час!
       https://t.me/c/.../...
   Если админов несколько — приходят отдельные сообщения по каждому тегу.

2) «напоминание про ПЗ» (mode='no_admin'):
   ждёт заданное время, пока ПЗ висит без админа, и напоминает списком ссылок:
       Данные ПЗ без админа уже 1 час!
       • бот — ссылка
       • бот — ссылка

3) «норма админов» (раздел «📊 Норма» в профиле):
   когда период подсчёта заканчивается, сообщает в «чат админов», кто не набрал
   норму, и прикладывает кнопку «📋 ПЗ без админа». Уведомление приходит один раз
   за период.

Повторные напоминания по одному и тому же топику не чаще одного раза в заданный
интервал (защита от «спама» при каждом сканировании).
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services import norms
from services.child_manager import get_main_bot, topic_web_link
from services.storage import (
    bot_display_name,
    get_all_enabled_reminders,
    get_all_norm_settings,
    get_all_topics_for_bot,
    get_bound_chat,
    get_last_admin_reply_at,
    get_norm_period_stats,
    get_reminder_quiet,
    get_reminder_tick,
    get_user_bots,
    set_norm_field,
    set_reminder_tick,
)

logger = logging.getLogger(__name__)

# Как часто сканировать состояние ПЗ (секунды).
SCAN_INTERVAL = 60

_UTC = timezone.utc
# Московское время (UTC+3) — тихие часы задаются именно в нём.
_MSK = timezone(timedelta(hours=3))


# ── Тихие часы: разбор и проверка ──────────────────────────────────────────

def _norm_time(raw: str) -> str | None:
    """'9:00' / '0900' / '9.00' / '9 00' → '09:00' (или None)."""
    match = re.match(r"^\s*(\d{1,2})[:.\s]?(\d{2})\s*$", (raw or "").strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return f"{hours:02d}:{minutes:02d}"


def _to_minutes(value: str) -> int | None:
    """'21:00' → 1260 (минут от полуночи) или None."""
    norm = _norm_time(value)
    if not norm:
        return None
    hours, minutes = norm.split(":")
    return int(hours) * 60 + int(minutes)


def parse_quiet_range(raw: str) -> tuple[str, str] | None:
    """Разбирает интервал тихих часов.

    Понимает: ``21:00-09:00``, ``с 21:00 по 9:00``, ``21.00 9.00``, ``2100-0900``.
    Возвращает пару («HH:MM», «HH:MM») или None, если разобрать не удалось.
    """
    text = (raw or "").strip().lower()
    for dash in ("—", "–", "−"):
        text = text.replace(dash, "-")
    found = re.findall(r"\d{1,2}[:.\s]?\d{2}", text)
    if len(found) < 2:
        return None
    start, end = _norm_time(found[0]), _norm_time(found[1])
    if not start or not end:
        return None
    return start, end


def is_quiet_now(owner_id: int, now: datetime | None = None) -> bool:
    """True, если сейчас тихие часы владельца — напоминания не отправляем.

    Интервал считается по МСК и может пересекать полночь (21:00 → 09:00).
    """
    settings = get_reminder_quiet(owner_id)
    if not settings["enabled"]:
        return False

    start, end = settings["from_time"], settings["to_time"]
    s, e = _to_minutes(start), _to_minutes(end)
    if s is None or e is None or s == e:
        return False

    current = (now or datetime.now(_UTC)).astimezone(_MSK)
    cur = current.hour * 60 + current.minute
    if s < e:
        return s <= cur < e
    # Интервал через полночь: 21:00 → 09:00.
    return cur >= s or cur < e


def quiet_hours_label(owner_id: int) -> str:
    """Строка-описание тихих часов для интерфейса."""
    settings = get_reminder_quiet(owner_id)
    if not settings["enabled"]:
        return "🔔 выключены"
    return f"🔕 с {settings['from_time']} до {settings['to_time']} (МСК)"


# ── Вспомогательные функции ────────────────────────────────────────────────

def _parse_ts(value: Any) -> datetime | None:
    """Разбирает UTC-строку БД ('YYYY-MM-DD HH:MM:SS') в aware datetime."""
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC)
    except ValueError:
        try:
            return datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=_UTC)
        except ValueError:
            return None


def _now_str() -> str:
    """Текущее UTC-время в формате БД."""
    return datetime.now(_UTC).strftime("%Y-%m-%d %H:%M:%S")


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and (n100 < 12 or n100 > 14):
        return few
    return many


def format_duration(seconds: int) -> str:
    """Человекочитаемое русское представление длительности."""
    seconds = max(1, int(seconds))
    if seconds % 86400 == 0:
        d = seconds // 86400
        return f"{d} {_plural(d, 'день', 'дня', 'дней')}"
    if seconds % 3600 == 0:
        h = seconds // 3600
        return f"{h} {_plural(h, 'час', 'часа', 'часов')}"
    if seconds % 60 == 0:
        m = seconds // 60
        return f"{m} {_plural(m, 'минута', 'минуты', 'минут')}"
    return f"{seconds} {_plural(seconds, 'секунда', 'секунды', 'секунд')}"


def _topic_key(bot_id: int, topic_id: int, group_chat_id: int) -> str:
    return f"{bot_id}:{topic_id}:{group_chat_id}"


def _should_send(reminder_id: int, topic_key: str, now: datetime, duration: int) -> bool:
    """True, если пора слать напоминание (нет записи или прошло >= duration)."""
    last_sent = get_reminder_tick(reminder_id, topic_key)
    if not last_sent:
        return True
    last_dt = _parse_ts(last_sent)
    if last_dt is None:
        return True
    return (now - last_dt).total_seconds() >= duration


async def _safe_send(bot: Bot, chat_id: int, text: str,
                     reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return True
    except Exception as e:
        logger.warning("Не удалось отправить напоминание в чат %s: %s", chat_id, e)
        return False
# ── Сервис ─────────────────────────────────────────────────────────────────

class ReminderService:
    """Фоновый цикл, сканирующий ПЗ и шлющий напоминания в «чат админов»."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="reminder_service")

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
        logger.info("Сервис напоминалок запущен")
        while True:
            try:
                await self._scan_once()
            except Exception as e:
                logger.exception("Ошибка в цикле напоминалок: %s", e)
            await asyncio.sleep(SCAN_INTERVAL)

    async def _scan_once(self) -> None:
        bot = get_main_bot()
        if bot is None:
            return
        now = datetime.now(_UTC)

        reminders = get_all_enabled_reminders()
        for r in reminders:
            try:
                await self._process_reminder(bot, r, now)
            except Exception as e:
                logger.exception("Ошибка обработки напоминалки %s: %s", r.get("id"), e)

        # Норма админов: раз в период сообщаем в «чат админов», кто не набрал.
        # Проверка не зависит от напоминалок, поэтому вызывается всегда.
        try:
            await self._check_norms(bot, now)
        except Exception as e:
            logger.exception("Ошибка проверки нормы админов: %s", e)

    # ── Норма админов: уведомление о недоборе ─────────────────────────────
    async def _check_norms(self, bot: Bot, now: datetime) -> None:
        """Раз в период шлём в «чат админов» список админов, не набравших норму.

        Повторов нет: метка отправленного периода хранится в настройках нормы
        (``last_notified``), поэтому в следующий раз уведомление придёт только
        за новый период.
        """
        for settings in get_all_norm_settings():
            owner_id = int(settings["owner_id"])
            due, label = norms.notification_due(settings, now)
            if not due:
                continue

            admin_chat = get_bound_chat(owner_id, "admin")
            if not admin_chat:
                continue

            start, end, _label = norms.period_bounds(
                now, settings["start_day"], settings["end_day"]
            )
            rows = get_norm_period_stats(owner_id, norms.to_db(start), norms.to_db(end))
            failed = [r for r in rows if not r["reached"]]

            # Помечаем период отправленным до отправки: даже если Telegram
            # недоступен, второй раз за этот период писать не будем.
            set_norm_field(owner_id, "last_notified", label)

            if not failed:
                await _safe_send(
                    bot, int(admin_chat),
                    f"🎉 <b>Норма за период {label} выполнена!</b>\n\n"
                    f"Все админы набрали норму <b>{settings['norm']}</b> сообщений. "
                    "Так держать!",
                )
                continue

            norm = int(settings["norm"])
            lines: list[str] = []
            for item in failed:
                admin = item["admin"]
                tag = str(admin.get("tag") or admin.get("user_id"))
                if not tag.startswith("#"):
                    tag = f"#{tag}"
                lines.append(f"• {tag} — <b>{item['period']}</b> / {norm}")

            text = (
                "⚠️ <b>Норма не набрана!</b>\n\n"
                f"📅 Период: <b>{label}</b>\n"
                f"🎯 Норма: <b>{norm}</b> сообщений\n"
                f"❌ Не набрали: <b>{len(failed)}</b> из <b>{len(rows)}</b>\n\n"
                + "\n".join(lines)
                + "\n\nСписок обращений без админа — по кнопке ниже."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 ПЗ без админа", callback_data="norm_noadmin",
                                      style="primary")]
            ])
            await _safe_send(bot, int(admin_chat), text, kb)

    async def _process_reminder(self, bot: Bot, reminder: dict, now: datetime) -> None:
        owner_id = reminder["owner_id"]

        # Тихие часы: ночью напоминания не шлём, чтобы не спамить админов.
        if is_quiet_now(owner_id, now):
            return

        admin_chat = get_bound_chat(owner_id, "admin")
        if not admin_chat:
            return
        duration = max(1, int(reminder["duration_seconds"] or 0))
        rid = int(reminder["id"])
        mode = reminder.get("mode")

        if mode == "no_admin":
            await self._check_no_admin(bot, owner_id, rid, admin_chat, duration, now)
        elif mode == "check_admin":
            await self._check_admin_reply(bot, owner_id, rid, admin_chat, duration, now)
# ── Режим «напоминание про ПЗ» (без админа) ─────────────────────────────
    async def _check_no_admin(self, bot: Bot, owner_id: int, rid: int,
                              admin_chat: int, duration: int, now: datetime) -> None:
        overdue: list[tuple[str, str]] = []  # (bot_name, link)
        keys: list[str] = []
        for b in get_user_bots(owner_id):
            bot_name = bot_display_name(b) if b else f"bot_{b.get('id')}"
            for t in get_all_topics_for_bot(b["id"]):
                if t.get("admin_user_id"):
                    continue
                key = _topic_key(b["id"], t["topic_id"], t["group_chat_id"])
                created = _parse_ts(t.get("created_at"))
                if not created:
                    continue
                if (now - created).total_seconds() < duration:
                    continue
                if not _should_send(rid, key, now, duration):
                    continue
                overdue.append((bot_name, topic_web_link(t["group_chat_id"], t["topic_id"])))
                keys.append(key)

        if not overdue:
            return

        lines = [f"• {name} — {link}" for name, link in overdue]
        text = (
            f"⏳ <b>Данные ПЗ без админа уже {format_duration(duration)}!</b>\n\n"
            + "\n".join(lines)
        )
        if await _safe_send(bot, admin_chat, text):
            now_str = _now_str()
            for key in keys:
                set_reminder_tick(rid, key, now_str)
    # ── Режим «авточек ответа админа» (админ есть, но не ответил) ───────────
    async def _check_admin_reply(self, bot: Bot, owner_id: int, rid: int,
                                 admin_chat: int, duration: int, now: datetime) -> None:
        # Собираем просроченные ПЗ, сгруппировав по тегу админа.
        # («бот может отправлять списком, если админов несколько»)
        by_tag: dict[str, list[tuple[str, str]]] = {}  # tag -> [(bot_name, link)]
        keys_to_tick: list[str] = []
        for b in get_user_bots(owner_id):
            bot_name = bot_display_name(b) if b else f"bot_{b.get('id')}"
            for t in get_all_topics_for_bot(b["id"]):
                if not t.get("admin_user_id"):
                    continue
                key = _topic_key(b["id"], t["topic_id"], t["group_chat_id"])
                last_reply = get_last_admin_reply_at(b["id"], t["topic_id"], t["group_chat_id"])
                ref = _parse_ts(last_reply) if last_reply else _parse_ts(t.get("created_at"))
                if not ref:
                    continue
                if (now - ref).total_seconds() < duration:
                    continue
                if not _should_send(rid, key, now, duration):
                    continue
                tag = (t.get("admin_tag") or "").strip() or f"ID:{t.get('admin_user_id')}"
                if tag.startswith("#"):
                    tag = tag[1:]
                by_tag.setdefault(tag, []).append(
                    (bot_name, topic_web_link(t["group_chat_id"], t["topic_id"]))
                )
                keys_to_tick.append(key)

        if not by_tag:
            return

        now_str = _now_str()
        sent_ok = True
        for tag, entries in by_tag.items():
            lines = [f"• {name} — {link}" for name, link in entries]
            text = (
                f"#{tag} <b>ПЗ без ответа уже {format_duration(duration)}!</b>\n\n"
                + "\n".join(lines)
            )
            if not await _safe_send(bot, admin_chat, text):
                sent_ok = False

        if sent_ok:
            for key in keys_to_tick:
                set_reminder_tick(rid, key, now_str)
