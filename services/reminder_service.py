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

Что прикручено сверху (жалобы владельцев)
----------------------------------------
* Нумерация списка + кнопки «🔇 Заглушить» и «♻️ Сбросить отсчёт»: бот
  присылал одно и то же «ПЗ без админа» по топику, который удалили, — и
  владелец не мог этому помешать. Подробности — в handlers/reminder_mutes.py.
* Заглушенные топики не попадают ни в уведомления, ни в «Диагностику».
* Закрытые/удалённые топики (метка ``closed_at``) исключены выборками БД.
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
from services.delivery import enqueue
from services.guards import supervised_task
from services.storage import (
    bot_display_name,
    get_all_enabled_reminders,
    get_all_norm_settings,
    get_bound_chat,
    get_muted_topic_keys,
    get_norm_period_stats,
    get_reminder_quiet,
    get_reminder_tick,
    get_reminders,
    get_topics_waiting_admin,
    get_topics_without_admin,
    get_user_bots,
    purge_expired_mutes,
    set_norm_field,
    set_reminder_tick,
)

logger = logging.getLogger(__name__)

# Как часто сканировать состояние ПЗ (секунды).
SCAN_INTERVAL = 60

_UTC = timezone.utc
# Московское время (UTC+3) — тихие часы задаются именно в нём.
_MSK = timezone(timedelta(hours=3))

# Формат UTC-строки в базе (так же, как CURRENT_TIMESTAMP в SQLite).
_DB_FMT = "%Y-%m-%d %H:%M:%S"


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


# ═══════════════ Диагностика ══════════════════════════════════════════
# Главная претензия к напоминалке была не «ошибка», а «не работает» — и
# непонятно почему. Поэтому собираем честный отчёт: что настроено, что
# готово к отправке и что именно мешает прийти уведомлению.

REASON_OK = "ok"
REASON_NO_CHAT = "no_admin_chat"
REASON_QUIET = "quiet_hours"
REASON_OFF = "disabled"
REASON_NO_TOPICS = "no_topics"
REASON_BOT_DOWN = "bot_unavailable"

REASON_TEXTS = {
    REASON_OK: "✅ Всё настроено, напоминания приходят",
    REASON_NO_CHAT: "⚠️ Не привязан «чат админов» — слать напоминание некуда",
    REASON_QUIET: "🔕 Сейчас тихие часы — напоминания молчат",
    REASON_OFF: "⏸ Напоминание выключено",
    REASON_NO_TOPICS: "ℹ️ Пока нет ПЗ, которые ждут ответа админа",
    REASON_BOT_DOWN: "⚠️ Бот недоступен — проверь токен и журнал",
}


# Свои подписи режимов: handlers/reminders.py их тоже определяет, но
# сервис не должен зависеть от хендлеров (слой выше).
_MODE_LABELS = {
    "check_admin": "✅ Авточек ответа админа",
    "no_admin": "⏳ Напоминание про ПЗ",
}


def _due_topics(owner_id: int, mode: str, duration: int,
                now: datetime) -> list[dict]:
    """ПЗ, по которым эта напоминалка должна сработать прямо сейчас."""
    cutoff = (now - timedelta(seconds=duration)).strftime(_DB_FMT)
    finder = get_topics_waiting_admin if mode == "check_admin" else get_topics_without_admin

    # Список собираем ЗДЕСЬ, внутри функции: если держать его снаружи цикла,
    # то для второй напоминалки туда попали бы ещё и ПЗ первой — и «количество
    # ожидающих» показывалось бы неверно.
    #
    # Заглушенные топики исключаем ЗДЕСЬ, а не только при отправке: иначе в
    # «Диагностике» показывалось бы «ждут: 12», хотя бот молчал бы по всем 12.
    muted = get_muted_topic_keys(owner_id)
    topics: list[dict] = []
    for b in get_user_bots(owner_id):
        for t in finder(b["id"], cutoff):
            if _is_muted(muted, b["id"], t["topic_id"], t["group_chat_id"]):
                continue
            topics.append(t)
    return topics


def reminder_diagnostics(owner_id: int) -> dict:
    """Собирает отчёт «почему напоминалка молчит» по напоминалкам владельца.

    Возвращает словарь с ключами:
      ``bot_ready``    — основной бот на связи;
      ``admin_chat``   — привязан ли чат админов;
      ``quiet``        — тихие часы включены и активны прямо сейчас;
      ``reminders``    — список напоминалок с их состоянием;
      ``pending``      — сколько ПЗ сейчас ждут уведомления;
      ``reason``       — machine-readable причина молчания;
      ``reason_text``  — то же человеческим языком.
    """
    now = datetime.now(_UTC)
    reminders = get_reminders(owner_id)

    pending_total = 0
    prepared: list[dict] = []
    for r in reminders:
        duration = max(1, int(r.get("duration_seconds") or 0))
        rid = int(r["id"])
        mode = r.get("mode") or ""

        due_here = 0
        for t in _due_topics(owner_id, mode, duration, now):
            key = _topic_key(t["bot_id"], t["topic_id"], t["group_chat_id"])
            if _should_send(rid, key, now, duration):
                due_here += 1
        pending_total += due_here

        prepared.append({
            "id": rid,
            "mode": mode,
            "label": _MODE_LABELS.get(mode, mode or "?"),
            "duration": format_duration(duration),
            "enabled": bool(r.get("enabled")),
            "due": due_here,
        })

    admin_chat = get_bound_chat(owner_id, "admin")
    quiet = is_quiet_now(owner_id, now)
    bot_ready = get_main_bot() is not None

    if not bot_ready:
        reason = REASON_BOT_DOWN
    elif not any(p["enabled"] for p in prepared):
        reason = REASON_OFF
    elif not admin_chat:
        reason = REASON_NO_CHAT
    elif quiet:
        reason = REASON_QUIET
    elif pending_total == 0:
        reason = REASON_NO_TOPICS
    else:
        reason = REASON_OK

    return {
        "bot_ready": bot_ready,
        "admin_chat": admin_chat,
        "quiet": quiet,
        "quiet_label": quiet_hours_label(owner_id),
        "reminders": prepared,
        "pending": pending_total,
        # Заглушенные ПЗ в «ожидающих» не входят (их отсекает _due_topics),
        # но показать их количество полезно: если «ждут: 0», владелец должен
        # понимать, что это из-за заглушек, а не из-за отсутствия ПЗ.
        "muted": len(get_muted_topic_keys(owner_id)),
        "reason": reason,
        "reason_text": REASON_TEXTS[reason],
    }


def _topic_key(bot_id: int, topic_id: int, group_chat_id: int) -> str:
    return f"{bot_id}:{topic_id}:{group_chat_id}"


# Сколько позиций показываем в одном уведомлении. Telegram не любит
# простыни из сотни ссылок, да и владельцу такой список не нужен — хвост
# всё равно можно заглушить кнопкой «🔇 Заглушить».
MAX_LIST_ITEMS = 30


def _is_muted(muted: set[str], bot_id: int, topic_id: int, group_chat_id: int) -> bool:
    """Заглушён ли топик (ключи загружены ОДНОЙ выборкой на скан — см. вызов)."""
    return _topic_key(bot_id, topic_id, group_chat_id) in muted


def _build_entries(rows: list[tuple[str, int, int, int]]) -> list[dict]:
    """Готовит нумерованные позиции для уведомления.

    ``rows`` — ``(имя бота, bot_id, topic_id, group_chat_id)``. Нумерация нужна
    для кнопки «🔇 Заглушить»: владелец должен однозначно указать, какое
    именно обращение заглушить, поэтому порядок в тексте и в кнопках должен
    совпадать. Номер кладём и в текст, и в callback_data.
    """
    entries: list[dict] = []
    for i, (bot_name, bot_id, topic_id, group_chat_id) in enumerate(rows[:MAX_LIST_ITEMS], 1):
        entries.append({
            "n": i,
            "bot_name": bot_name,
            "bot_id": bot_id,
            "topic_id": topic_id,
            "group_chat_id": group_chat_id,
            "link": topic_web_link(group_chat_id, topic_id),
        })
    return entries


def _list_kb(rid: int, entries: list[dict], with_mute: bool = True) -> InlineKeyboardMarkup:
    """Клавиатура под списком ПЗ: заглушить выбранное / сбросить отсчёт."""
    rows: list[list[InlineKeyboardButton]] = []
    if with_mute and entries:
        rows.append([InlineKeyboardButton(
            text="🔇 Заглушить", callback_data=f"rm_mute_menu:{rid}",
            style="primary",
        )])
    rows.append([InlineKeyboardButton(
        text="♻️ Сбросить отсчёт", callback_data=f"rm_reset_ask:{rid}", style="primary",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_entries(entries: list[dict]) -> str:
    """Текстовый блок «1. бот — ссылка» по пронумерованным позициям."""
    return "\n".join(f"{e['n']}. {e['bot_name']} — {e['link']}" for e in entries)


def current_entries(owner_id: int, reminder_id: int) -> list[dict]:
    """Позиции, по которым напоминалка напомнила бы ПРЯМО СЕЙЧАС.

    Нужна кнопке «🔇 Заглушить» в уведомлении: по нажатию показываем
    владельцу АКТУАЛЬНЫЙ список, а не полагаемся на текст старого сообщения
    (кнопка может быть нажата через сутки).

    Здесь НЕТ проверки «уже слали» (``_should_send``), и это важно: уведомление
    только что отправилось и тик по каждому топику уже проставлен. С таким
    фильтром список был бы пустым сразу после отправки, и кнопка «Заглушить»
    не работала бы никогда. Владелец заглушает ПЗ из уведомления — значит,
    показываем ровно те ПЗ, которые в нём были: просроченные, не заглушенные
    и не закрытые.
    """
    reminder = next(
        (r for r in get_reminders(owner_id) if int(r["id"]) == int(reminder_id)), None
    )
    if not reminder:
        return []

    mode = reminder.get("mode") or ""
    duration = max(1, int(reminder.get("duration_seconds") or 0))
    now = datetime.now(_UTC)
    cutoff = (now - timedelta(seconds=duration)).strftime(_DB_FMT)
    muted = get_muted_topic_keys(owner_id)

    finder = get_topics_waiting_admin if mode == "check_admin" else get_topics_without_admin
    rows: list[tuple[str, int, int, int]] = []
    for b in get_user_bots(owner_id):
        bot_name = bot_display_name(b) if b else f"bot_{b.get('id')}"
        for t in finder(b["id"], cutoff):
            if _is_muted(muted, b["id"], t["topic_id"], t["group_chat_id"]):
                continue
            if mode == "check_admin" and not t.get("admin_user_id"):
                continue
            rows.append((bot_name, b["id"], t["topic_id"], t["group_chat_id"]))
    return _build_entries(rows)


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
    """Отправляет напоминание, а при сбое кладёт его в очередь доставки.

    Раньше здесь был единственный ``send_message`` и тихий возврат False: если
    Telegram в этот момент дал flood-control или сеть моргнула, напоминание
    просто пропадало, а владелец думал, что напоминалка «не работает».

    Теперь поведение такое:

    * отправилось сразу — вернули True (обычный путь, задержки нет);
    * не отправилось — кладём в ``outbox`` (сообщение переживёт перезапуск и
      уйдёт позже), возвращаем True, чтобы метка «уже отправлено» НЕ
      ставилась: если очередь потеряет, топик попадёт в напоминание снова.

    ``bot_id`` = 0: напоминания шлёт основной бот.
    """
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return True
    except Exception as e:
        logger.warning("Не удалось отправить напоминание в чат %s: %s", chat_id, e)
        try:
            enqueue(0, chat_id, text,
                    kind="message",
                    **({"reply_markup": reply_markup.model_dump()} if reply_markup else {}))
            logger.info("Напоминание в чат %s поставлено в очередь доставки", chat_id)
            # Считаем успехом: сообщение не потеряно, тик можно ставить.
            return True
        except Exception as queue_error:
            logger.error(
                "Не удалось даже поставить напоминание в очередь: %s", queue_error,
            )
            return False
# ── Сервис ─────────────────────────────────────────────────────────────────

class ReminderService:
    """Фоновый цикл, сканирующий ПЗ и шлющий напоминания в «чат админов»."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Запускает сканер под присмотром (services/guards.py).

        Раньше это был обычный ``create_task``: любое непойманное исключение
        в цикле убивало задачу навсегда, и напоминалки просто переставали
        работать — без единой записи в журнале. Теперь цикл перезапускается
        сам и пишет в журнал причину.
        """
        if self._task is None or self._task.done():
            self._task = supervised_task(
                lambda: self._run(), "reminder_service", log=logger,
            )

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
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка в цикле напоминалок")
            await asyncio.sleep(SCAN_INTERVAL)

    async def _scan_once(self) -> None:
        bot = get_main_bot()
        if bot is None:
            return
        now = datetime.now(_UTC)

        # Раз в круг подчищаем истёкшие заглушки: без этого таблица росла бы
        # бесконечно. Ошибку НЕ глотаем молча — пишем в журнал, но сканирование
        # продолжаем: чистка не должна ломать напоминания.
        try:
            expired = purge_expired_mutes()
            if expired:
                logger.info("Напоминалка: убрано истёкших заглушек — %d", expired)
        except Exception as e:
            logger.error("Не удалось вычистить истёкшие заглушки: %s", e)

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

            # Тихие часы здесь тоже соблюдаем: уведомление о недоборе нормы —
            # то же самое напоминание, что и авточек, и ночью оно так же
            # неуместно. Раньше эта проверка была только в напоминалках, из-за
            # чего ночью прилетало уведомление, которое днём не пришло бы.
            if is_quiet_now(owner_id, now):
                continue

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

            if not failed:
                ok = await _safe_send(
                    bot, int(admin_chat),
                    f"🎉 <b>Норма за период {label} выполнена!</b>\n\n"
                    f"Все админы набрали норму <b>{settings['norm']}</b> сообщений. "
                    "Так держать!",
                )
                # Метку ставим ТОЛЬКО после успешной отправки. Раньше она
                # писалась до отправки, и при любом сбое Telegram уведомление
                # о недоборе терялось навсегда — метка уже стояла.
                if ok:
                    set_norm_field(owner_id, "last_notified", label)
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
            if await _safe_send(bot, int(admin_chat), text, kb):
                set_norm_field(owner_id, "last_notified", label)

    async def _process_reminder(self, bot: Bot, reminder: dict, now: datetime) -> None:
        owner_id = reminder["owner_id"]

        # Тихие часы: ночью напоминания не шлём, чтобы не спамить админов.
        if is_quiet_now(owner_id, now):
            logger.debug("Напоминалка %s молчит: тихие часы", reminder.get("id"))
            return

        admin_chat = get_bound_chat(owner_id, "admin")
        if not admin_chat:
            # Раньше был молчаливый return — владелец не понимал, почему
            # напоминания не приходят. Теперь это видно в журнале.
            logger.info(
                "Напоминалка %s владельца %s пропущена: не привязан «чат админов»",
                reminder.get("id"), owner_id,
            )
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
        rows: list[tuple[str, int, int, int]] = []  # (bot_name, bot_id, topic, chat)
        keys: list[str] = []
        # Граница «с какого момента ПЗ считается просроченным».
        cutoff = (now - timedelta(seconds=duration)).strftime(_DB_FMT)
        # Заглушки грузим ОДИН раз на этот проход, а не внутри цикла по ботам.
        muted = get_muted_topic_keys(owner_id)

        for b in get_user_bots(owner_id):
            bot_name = bot_display_name(b) if b else f"bot_{b.get('id')}"
            # Только ПЗ без админа, где последним писал ПЗ. Раньше брались все
            # топики бота, и старые заброшенные ПЗ напоминались бесконечно.
            for t in get_topics_without_admin(b["id"], cutoff):
                if t.get("admin_user_id"):
                    continue
                # Заглушённые пропускаем молча: владелец сам сказал «не
                # напоминай про это ПЗ». Тик тоже НЕ пишем — тогда после снятия
                # заглушки напоминание придёт сразу, а не через полный срок.
                if _is_muted(muted, b["id"], t["topic_id"], t["group_chat_id"]):
                    continue
                key = _topic_key(b["id"], t["topic_id"], t["group_chat_id"])
                if not _should_send(rid, key, now, duration):
                    continue
                rows.append((bot_name, b["id"], t["topic_id"], t["group_chat_id"]))
                keys.append(key)

        if not rows:
            return

        # Нумерованный список: номера нужны кнопке «🔇 Заглушить» (владелец
        # указывает номер обращения) — они же в callback_data.
        entries = _build_entries(rows)
        text = (
            f"⏳ <b>Данные ПЗ без админа уже {format_duration(duration)}!</b>\n\n"
            + _format_entries(entries)
        )
        if len(rows) > len(entries):
            text += f"\n\n<i>…и ещё {len(rows) - len(entries)}. Показаны первые {MAX_LIST_ITEMS}.</i>"
        if await _safe_send(bot, admin_chat, text, _list_kb(rid, entries)):
            now_str = _now_str()
            for key in keys:
                set_reminder_tick(rid, key, now_str)

    # ── Режим «авточек ответа админа» (админ есть, но не ответил) ───────────
    async def _check_admin_reply(self, bot: Bot, owner_id: int, rid: int,
                                 admin_chat: int, duration: int, now: datetime) -> None:
        # Собираем просроченные ПЗ, сгруппировав по тегу админа.
        # («бот может отправлять списком, если админов несколько»)
        by_tag: dict[str, list[tuple[str, int, int, int]]] = {}  # tag -> строки
        keys_by_tag: dict[str, list[str]] = {}  # tag -> [topic_key]
        cutoff = (now - timedelta(seconds=duration)).strftime(_DB_FMT)
        # Заглушки грузим ОДИН раз на этот проход.
        muted = get_muted_topic_keys(owner_id)

        for b in get_user_bots(owner_id):
            bot_name = bot_display_name(b) if b else f"bot_{b.get('id')}"
            for t in get_topics_waiting_admin(b["id"], cutoff):
                if not t.get("admin_user_id"):
                    continue
                # Заглушка работает и здесь: владелец мог заглушить ПЗ, где
                # админ уже назначен, но молчит (тот же «вечный» сценарий).
                if _is_muted(muted, b["id"], t["topic_id"], t["group_chat_id"]):
                    continue
                key = _topic_key(b["id"], t["topic_id"], t["group_chat_id"])
                if not _should_send(rid, key, now, duration):
                    continue
                tag = (t.get("admin_tag") or "").strip() or f"ID:{t.get('admin_user_id')}"
                if tag.startswith("#"):
                    tag = tag[1:]
                by_tag.setdefault(tag, []).append(
                    (bot_name, b["id"], t["topic_id"], t["group_chat_id"])
                )
                keys_by_tag.setdefault(tag, []).append(key)

        if not by_tag:
            return

        now_str = _now_str()
        for tag, rows in by_tag.items():
            # Нумерация — своя для каждого тега: сообщения по тегам идут
            # отдельными сообщениями, и номера в них должны совпадать со
            # своими кнопками «🔇 Заглушить».
            entries = _build_entries(rows)
            text = (
                f"#{tag} <b>ПЗ без ответа уже {format_duration(duration)}!</b>\n\n"
                + _format_entries(entries)
            )
            if len(rows) > len(entries):
                text += f"\n\n<i>…и ещё {len(rows) - len(entries)}. Показаны первые {MAX_LIST_ITEMS}.</i>"
            # Тик пишем ТОЛЬКО для того тега, который реально отправлен.
            # Раньше тики писались лишь когда удалось отправить ВСЕ сообщения,
            # и уже отправленные повторялись каждые 60 секунд.
            if await _safe_send(bot, admin_chat, text, _list_kb(rid, entries)):
                for key in keys_by_tag.get(tag, []):
                    set_reminder_tick(rid, key, now_str)
            else:
                logger.info(
                    "Напоминание для тега %s не отправлено — вернёмся к нему "
                    "на следующем круге", tag,
                )
