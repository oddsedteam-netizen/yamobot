"""Норма админов: период подсчёта, рейтинг и уведомление о недоборе.

Помощник для раздела «📊 Норма» в профиле и для фоновой проверки в
``services/reminder_service.py``.

Дни недели нумеруются как в ``datetime.weekday()``: 0 — понедельник,
6 — воскресенье. Период считается по московскому времени (МСК), а в БД время
хранится в UTC (``CURRENT_TIMESTAMP`` SQLite), поэтому границы переводим в
UTC-строки формата «YYYY-MM-DD HH:MM:SS».
"""

import re
from datetime import datetime, timedelta, timezone

# Московское время (UTC+3) — в нём задаются дни и считаются периоды.
MSK = timezone(timedelta(hours=3))
_UTC = timezone.utc

WEEKDAY_NAMES = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]
WEEKDAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_EN_SHORT = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Во сколько часов (МСК) последнего дня периода отправляем уведомление.
NOTIFY_HOUR = 20

# Алиасы для разбора дней недели: «понедельник», «пон», «пн», «1», «mon»…
_WEEKDAY_ALIASES: dict[str, int] = {}
for _index, _name in enumerate(WEEKDAY_NAMES):
    _low = _name.lower()
    _WEEKDAY_ALIASES[_low] = _index
    _WEEKDAY_ALIASES[_low[:3]] = _index
    _WEEKDAY_ALIASES[WEEKDAY_SHORT[_index]] = _index
    _WEEKDAY_ALIASES[_EN_SHORT[_index]] = _index
    _WEEKDAY_ALIASES[str(_index + 1)] = _index
# Частые сокращения и опечатки.
_WEEKDAY_ALIASES.update({
    "вос": 6, "воскрес": 6, "вскр": 6,
    "чет": 3, "пят": 4, "суб": 5, "втор": 1, "сред": 2,
})

# Разбор по первым трём буквам — чтобы понимать падежи:
# «понедельника», «пятницу», «субботу», «среды» и т.п.
_WEEKDAY_PREFIXES: dict[str, int] = {}
for _index, _name in enumerate(WEEKDAY_NAMES):
    _WEEKDAY_PREFIXES[_name.lower()[:3]] = _index


def _weekday_from_token(token: str) -> int | None:
    """День недели по слову: «пн», «понедельника», «пятницу», «1», «mon»."""
    word = (token or "").strip().lower()
    if len(word) < 2:
        return None
    if word in _WEEKDAY_ALIASES:
        return _WEEKDAY_ALIASES[word]
    if len(word) >= 3 and word[:3] in _WEEKDAY_PREFIXES:
        return _WEEKDAY_PREFIXES[word[:3]]
    return None


def weekday_name(day: int) -> str:
    """Название дня недели по индексу (0 — понедельник)."""
    return WEEKDAY_NAMES[min(6, max(0, int(day)))]


def parse_days_range(raw: str) -> tuple[int, int] | None:
    """Разбирает «первый и последний день подсчёта».

    Понимает: ``пн-пт``, ``с понедельника по пятницу``, ``1-5``, ``пн пт``,
    ``mon-fri``. Возвращает пару (start_day, end_day) или None.
    """
    text = (raw or "").strip().lower()
    for dash in ("—", "–", "−", "…"):
        text = text.replace(dash, "-")
    if not text:
        return None

    # Числа: «1-5», «1 5», «с 1 по 5» (1 — понедельник, 7 — воскресенье).
    numbers = re.findall(r"(?<!\d)([1-7])(?!\d)", text)
    if len(numbers) >= 2:
        return int(numbers[0]) - 1, int(numbers[1]) - 1

    tokens = [t for t in re.split(r"[\s,\-]+", text) if t]
    found = [d for d in (_weekday_from_token(t) for t in tokens) if d is not None]
    if len(found) >= 2:
        return found[0], found[1]
    return None


def period_bounds(now: datetime | None = None, start_day: int = 0,
                  end_day: int = 4) -> tuple[datetime, datetime, str]:
    """Границы последнего периода подсчёта: (start_utc, end_utc, label).

    ``start`` — 00:00 МСК первого дня периода, ``end`` — 23:59:59 МСК последнего
    дня. Если сейчас идёт следующий период (например, суббота при периоде
    Пн–Пт), вернутся границы уже завершившегося периода — по ним и считаем
    «норму за прошедшую неделю».
    """
    now_utc = now or datetime.now(_UTC)
    now_msk = now_utc.astimezone(MSK)

    days_back = (now_msk.weekday() - int(start_day)) % 7
    start_msk = (now_msk - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    length = (int(end_day) - int(start_day)) % 7
    end_msk = (start_msk + timedelta(days=length)).replace(
        hour=23, minute=59, second=59, microsecond=0
    )

    label = f"{start_msk.strftime('%d.%m')}–{end_msk.strftime('%d.%m')}"
    return start_msk.astimezone(_UTC), end_msk.astimezone(_UTC), label


def period_title(settings: dict, now: datetime | None = None) -> str:
    """Человеческое описание периода: «пн–пт, 18.08–22.08»."""
    _start, _end, label = period_bounds(now, settings.get("start_day", 0),
                                        settings.get("end_day", 4))
    first = WEEKDAY_SHORT[int(settings.get("start_day", 0))]
    last = WEEKDAY_SHORT[int(settings.get("end_day", 4))]
    return f"{first}–{last}, {label}"


def to_db(ts: datetime) -> str:
    """Переводит момент времени в строку для сравнения с created_at (UTC)."""
    return ts.astimezone(_UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def notification_due(settings: dict, now: datetime | None = None) -> tuple[bool, str]:
    """Пора ли отправить уведомление о недоборе нормы за период.

    True — если норма задана, уведомления разрешены, наступил вечер последнего
    дня периода (или период уже закончился) и по этому периоду уведомление ещё
    не отправлялось. Возвращает (due, label периода).
    """
    if not int(settings.get("norm") or 0):
        return False, ""
    if not int(settings.get("notify_enabled") or 0):
        return False, ""

    now_utc = now or datetime.now(_UTC)
    start_day = int(settings.get("start_day", 0))
    end_day = int(settings.get("end_day", 4))
    _start, end, label = period_bounds(now_utc, start_day, end_day)

    if str(settings.get("last_notified") or "") == label:
        return False, label

    now_msk = now_utc.astimezone(MSK)
    finished = now_utc > end
    last_evening = now_msk.weekday() == end_day and now_msk.hour >= NOTIFY_HOUR
    return (finished or last_evening), label


def rating_lines(rows: list[dict], limit: int | None = None,
                 offset: int = 0) -> list[str]:
    """Строки рейтинга: место, тег, сообщения за период и общая статистика.

    ``offset`` — сколько позиций уже показано (для страниц рейтинга).
    """
    lines: list[str] = []
    for place, item in enumerate(rows, start=offset + 1):
        if limit is not None and place - offset > limit:
            break
        admin = item["admin"]
        tag = str(admin.get("tag") or admin.get("user_id"))
        if not tag.startswith("#"):
            tag = f"#{tag}"
        stats = item["stats"]
        mark = "✅" if item["reached"] else "⏳"
        lines.append(
            f"{mark} <b>{place}.</b> {tag} — <b>{item['period']}</b> сообщ.\n"
            f"     📅 день/нед/мес/всего: {stats['day']} / {stats['week']} / "
            f"{stats['month']} / {stats['total']}  •  📋 ПЗ: {item['active_topics']}"
        )
    return lines

