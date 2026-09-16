"""Фоновый сервис напоминалок.

Два режима (настраиваются владельцем в профиле → «Напоминалка»):

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

Повторные напоминания по одному и тому же топику не чаще одного раза в заданный
интервал (защита от «спама» при каждом сканировании).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot

from services.child_manager import get_main_bot, topic_web_link
from services.storage import (
    bot_display_name,
    get_all_enabled_reminders,
    get_all_topics_for_bot,
    get_bound_chat,
    get_last_admin_reply_at,
    get_reminder_tick,
    get_user_bots,
    set_reminder_tick,
)

logger = logging.getLogger(__name__)

# Как часто сканировать состояние ПЗ (секунды).
SCAN_INTERVAL = 60

_UTC = timezone.utc


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


async def _safe_send(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id=chat_id, text=text)
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
        reminders = get_all_enabled_reminders()
        if not reminders:
            return
        bot = get_main_bot()
        if bot is None:
            return
        now = datetime.now(_UTC)
        for r in reminders:
            try:
                await self._process_reminder(bot, r, now)
            except Exception as e:
                logger.exception("Ошибка обработки напоминалки %s: %s", r.get("id"), e)

    async def _process_reminder(self, bot: Bot, reminder: dict, now: datetime) -> None:
        owner_id = reminder["owner_id"]
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
