"""Команды модерации для «чата админов» (главный бот YamoBot).

Здесь нет топиков — наказания накладываются на пользователей дочерних ботов
владельца. Цель определяется:
  • реплаем на любое сообщение в чате админов (по ссылке на топик, ID,
    @username или имени, найденному в тексте переписки);
  • либо явным указанием юзера в аргументах (ID / @username / имя).

Доступные команды:
  /бан [время|навсегда] [цель]        — бан (по умолчанию навсегда)
  /пред или /варн [цель]              — предупреждение (до порога из /предред)
  /предред N бан|мут [время]          — порог предов до наказания + само наказание
  /мут [время] [цель]                 — запрет писать на время
  /стата неделя|день|месяц            — статистика админов за период
"""

import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from services.config import is_super_admin
from services.storage import (
    get_owner_by_admin_chat,
    get_user_bots,
    get_owner_users,
    set_user_ban,
    set_user_mute,
    add_user_warn,
    reset_user_warns,
    clear_user_restriction_for_owner,
    clear_user_mute_for_owner,
    reset_user_warns_for_owner,
    add_admin_chat_moderator,
    remove_admin_chat_moderator,
    get_admin_chat_moderators,
    is_admin_chat_moderator,
    get_warn_settings,
    set_warn_settings,
    get_topic_by_topic_id,
    get_admins_all,
    get_admin_message_stats,
    get_admin_active_topics,
    _expire_time,
)

router = Router()

_MARK = r"[/\.!]"
_BAN_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*(?:бан|ban)\b\s*(.*)$")
_WARN_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*(?:варн|warn|пред|prew|pred)\b\s*(.*)$")
_PREDRED_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*предред\b\s*(.*)$")
_MUTE_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*(?:мут|мьют|mute|mut)\b\s*(.*)$")
_STATA_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*стата\b\s*(.+)$")
_MODER_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*модер\b\s*(.*)$")
_RAZZHAL_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*разжаловать\b\s*(.*)$")
_MODERS_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*модеры\b\s*(.*)$")
_RAZBAN_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*разбан\b\s*(.*)$")
_RAZMUT_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*размут\b\s*(.*)$")
_SNYAT_WARN_RE = re.compile(rf"(?i)^\s*{_MARK}+\s*(?:снять\s+варн|снять\s+пред|снятьварн)\b\s*(.*)$")

_UNIT_COEF = {
    "м": 1, "мин": 1, "min": 1, "m": 1,
    "ч": 60, "час": 60, "часов": 60, "h": 60,
    "д": 1440, "день": 1440, "дня": 1440, "дней": 1440, "дн": 1440, "d": 1440,
    "нед": 10080, "неделю": 10080, "неделя": 10080, "w": 10080,
    "мес": 43200, "месяц": 43200, "месяцев": 43200,
}

_PERIOD_KEYS = {
    "день": "day", "дн": "day", "day": "day",
    "неделя": "week", "нед": "week", "week": "week",
    "месяц": "month", "мес": "month", "month": "month",
    "всего": "total", "все": "total", "всё": "total", "total": "total",
}

_PERIOD_LABELS = {"day": "день", "week": "неделю", "month": "месяц", "total": "всё время"}

_TME_LINK_RE = re.compile(r"t\.me/c/(\d+)/(\d+)")


class _TargetUser:
    __slots__ = ("bot_id", "chat_id", "username", "first_name")

    def __init__(self, bot_id: int, chat_id: int, username: str = "", first_name: str = ""):
        self.bot_id = bot_id
        self.chat_id = chat_id
        self.username = username
        self.first_name = first_name

    def display(self) -> str:
        if self.username:
            return f"@{self.username}"
        if self.first_name:
            return self.first_name
        return f"<code>{self.chat_id}</code>"


def parse_duration(raw: str):
    s = (raw or "").strip().lower()
    if not s:
        return None
    if s in ("0", "навсегда", "перманент", "бессрочно", "forever", "perm", "permanent"):
        return 0
    # Время всегда с единицей (30м, 1ч, 2д) — голое число это ID юзера.
    m = re.match(r"(\d+)\s*([a-zа-я]+)", s)
    if not m:
        return None
    num = int(m.group(1))
    if num <= 0:
        return None
    unit = m.group(2)
    coef = _UNIT_COEF.get(unit)
    if coef is None:
        return None
    return num * coef


def format_duration(minutes) -> str:
    if minutes is None or minutes <= 0:
        return "навсегда"
    if minutes < 60:
        return f"{minutes} мин."
    if minutes % 1440 == 0:
        return f"{minutes // 1440} дн."
    if minutes % 60 == 0:
        return f"{minutes // 60} ч."
    return f"{minutes} мин."


def _strip_link(text: str) -> str:
    return _TME_LINK_RE.sub("", text or "")


def _target_tokens(text: str) -> tuple[set[int], set[str], list[str]]:
    ids: set[int] = set()
    usernames: set[str] = set()
    names: list[str] = []
    for tok in re.split(r"\s+", text or ""):
        tok = tok.strip(".,;:()[]|")
        if not tok:
            continue
        if re.fullmatch(r"\d{3,}", tok):
            ids.add(int(tok))
        elif re.match(r"^@[\w_]+$", tok):
            usernames.add(tok[1:].lower())
        else:
            names.append(tok.lower())
    return ids, usernames, names


def _resolve_from_link(owner_id: int, text: str) -> list[_TargetUser] | None:
    m = _TME_LINK_RE.search(text or "")
    if not m:
        return None
    chat_part = int(m.group(1))
    topic_id = int(m.group(2))
    group_chat_id = int("-100" + str(chat_part))
    for b in get_user_bots(owner_id):
        topic = get_topic_by_topic_id(b["id"], group_chat_id, topic_id)
        if topic:
            return [_TargetUser(int(b["id"]), int(topic["user_chat_id"]))]
    return None


def _dedupe(targets: list[_TargetUser]) -> list[_TargetUser]:
    seen = set()
    out = []
    for t in targets:
        key = (t.bot_id, t.chat_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def resolve_targets(owner_id: int, args_text: str, reply_text: str) -> list[_TargetUser]:
    from_link = _resolve_from_link(owner_id, reply_text)
    if from_link:
        return from_link

    ids, usernames, names = set(), set(), []
    if args_text.strip():
        a_ids, a_un, a_nm = _target_tokens(args_text)
        ids.update(a_ids)
        usernames.update(a_un)
        names.extend(a_nm)
    else:
        r_ids, r_un, r_nm = _target_tokens(_strip_link(reply_text))
        ids.update(r_ids)
        usernames.update(r_un)
        names.extend(r_nm)

    if not ids and not usernames and not names:
        return []

    rows = get_owner_users(owner_id)
    found: list[_TargetUser] = []
    for r in rows:
        chat_id = int(r["chat_id"])
        username = (r["username"] or "").lower()
        first_name = r["first_name"] or ""

        hit = False
        if ids and chat_id in ids:
            hit = True
        elif usernames and username and username in usernames:
            hit = True
        else:
            for nm in names:
                if nm and (nm in first_name.lower() or first_name.lower() in nm):
                    hit = True
                    break
        if hit:
            found.append(_TargetUser(int(r["bot_id"]), chat_id, r["username"] or "", first_name))

    return _dedupe(found)


def _resolve_owner(message: Message) -> int | None:
    if message.chat and message.chat.type != ChatType.PRIVATE:
        own = get_owner_by_admin_chat(message.chat.id)
        if own:
            return own
        return None
    return message.from_user.id


def _is_moderator(owner_id: int, user_id: int) -> bool:
    """Модератор чата: владелец, супер-админ или назначенный модер (/модер)."""
    if owner_id == user_id:
        return True
    if is_super_admin(user_id):
        return True
    if is_admin_chat_moderator(owner_id, user_id):
        return True
    return False


def _is_owner(owner_id: int, user_id: int) -> bool:
    return owner_id == user_id


def _resolve_target_user(owner_id: int, text: str, reply_text: str) -> dict | None:
    """Из аргументов/реплая получает одного целевого юзера (для назначения/снятия прав)."""
    targets = resolve_targets(owner_id, text, reply_text)
    if not targets:
        return None
    # Берём первого найденного по ID/имени/ссылке.
    return {"chat_id": targets[0].chat_id, "username": targets[0].username,
            "first_name": targets[0].first_name}


def _usage(msg: str) -> str:
    return (
        f"⚠️ <b>Неверное использование</b>\n{msg}\n"
        f"Работает по реплаю на сообщение или с указанием юзера (ID/@имя)."
    )


def _actor_name(from_user) -> str:
    if getattr(from_user, "username", None):
        return f"@{from_user.username}"
    return from_user.first_name or str(from_user.id)
# ═══════════════════════════════════════════════════════════
#  /предред — настройка порога предов и наказания
# ═══════════════════════════════════════════════════════════

@router.message(F.text.regexp(_PREDRED_RE))
async def cmd_pred_red(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None or not _is_moderator(owner_id, message.from_user.id):
        await message.answer("❌ Команда работает в привязанном «чате админов».")
        return

    args = (_PREDRED_RE.match(message.text or "").group(1) or "").split()
    if len(args) < 2:
        await message.answer(
            _usage("Формат: <code>/предред N бан|мут [время]</code>\n"
                   "Пример: <code>/предред 3 мут 1ч</code>")
        )
        return

    try:
        max_warns = int(args[0])
    except ValueError:
        await message.answer("❌ <code>N</code> должно быть числом (кол-во предов).")
        return
    if max_warns <= 0:
        await message.answer("❌ Кол-во предов должно быть больше нуля.")
        return

    ptype_raw = args[1].strip().lower()
    if ptype_raw in ("бан", "ban"):
        ptype = "ban"
    elif ptype_raw in ("мут", "mute", "mut", "мьють"):
        ptype = "mute"
    else:
        await message.answer("❌ Наказание: <code>бан</code> или <code>мут</code>.")
        return

    duration = 60
    if len(args) >= 3:
        parsed = parse_duration(args[2])
        if parsed is None and args[2].strip().isdigit():
            parsed = int(args[2].strip())  # голое число = минуты
        if parsed is None or parsed <= 0:
            await message.answer("❌ Не удалось разобрать время наказания.")
            return
        duration = parsed

    set_warn_settings(owner_id, max_warns, ptype, duration)

    punish_label = "🚫 бан" if ptype == "ban" else f"🔇 мут на {format_duration(duration)}"
    await message.answer(
        f"✅ <b>Настройки предов обновлены</b>\n\n"
        f"Порог: <b>{max_warns} пред.</b>\n"
        f"Наказание при достижении порога: {punish_label}"
    )


# ═══════════════════════════════════════════════════════════
#  /пред и /варн
# ═══════════════════════════════════════════════════════════

@router.message(F.text.regexp(_WARN_RE))
async def cmd_warn(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None or not _is_moderator(owner_id, message.from_user.id):
        await message.answer("❌ Команда работает в привязанном «чате админов».")
        return

    args_text = _WARN_RE.match(message.text or "").group(1) or ""
    reply_text = message.reply_to_message.text if message.reply_to_message else ""
    targets = resolve_targets(owner_id, args_text, reply_text)
    if not targets:
        await message.answer(_usage("Не удалось найти пользователя. Укажи ID, @имя или сделай реплей."))
        return

    settings = get_warn_settings(owner_id)
    lines = [f"👤 <b>{_actor_name(message.from_user)}</b> выдал пред:"]
    for t in targets:
        count = add_user_warn(t.bot_id, t.chat_id)
        if count >= settings["max_warns"]:
            reset_user_warns(t.bot_id, t.chat_id)
            if settings["punish_type"] == "ban":
                set_user_ban(t.bot_id, t.chat_id, None)
                act = "🚫 заблокирован навсегда"
            else:
                until = _expire_time(settings["punish_duration"])
                set_user_mute(t.bot_id, t.chat_id, until)
                act = f"🔇 замьючен на {format_duration(settings['punish_duration'])}"
            lines.append(f"  • {t.display()} — достигнут порог <b>{settings['max_warns']}</b>: {act}")
        else:
            lines.append(f"  • {t.display()} — пред <b>{count}/{settings['max_warns']}</b>")
    await message.answer("\n".join(lines))
# ═══════════════════════════════════════════════════════════
#  /бан
# ═══════════════════════════════════════════════════════════

@router.message(F.text.regexp(_BAN_RE))
async def cmd_ban(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None or not _is_moderator(owner_id, message.from_user.id):
        await message.answer("❌ Команда работает в привязанном «чате админов».")
        return

    args_text = _BAN_RE.match(message.text or "").group(1) or ""
    reply_text = message.reply_to_message.text if message.reply_to_message else ""

    duration = None
    target_tokens = []
    for tok in args_text.split():
        d = parse_duration(tok)
        if d is not None and duration is None:
            duration = d
        else:
            target_tokens.append(tok)
    target_text = " ".join(target_tokens)

    targets = resolve_targets(owner_id, target_text, reply_text)
    if not targets:
        await message.answer(_usage("Не удалось найти пользователя. Укажи ID, @имя или сделай реплей."))
        return

    until = _expire_time(duration)
    for t in targets:
        set_user_ban(t.bot_id, t.chat_id, until)

    label = "навсегда" if duration is None or duration == 0 else format_duration(duration)
    lines = [f"🚫 <b>{_actor_name(message.from_user)}</b> забанил пользователя на {label}:"]
    for t in targets:
        lines.append(f"  • {t.display()}")
    await message.answer("\n".join(lines))


# ═══════════════════════════════════════════════════════════
#  /мут
# ═══════════════════════════════════════════════════════════

@router.message(F.text.regexp(_MUTE_RE))
async def cmd_mute(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None or not _is_moderator(owner_id, message.from_user.id):
        await message.answer("❌ Команда работает в привязанном «чате админов».")
        return

    args_text = _MUTE_RE.match(message.text or "").group(1) or ""
    reply_text = message.reply_to_message.text if message.reply_to_message else ""

    duration = None
    target_tokens = []
    for tok in args_text.split():
        d = parse_duration(tok)
        if d is not None and duration is None:
            duration = d
        else:
            target_tokens.append(tok)
    target_text = " ".join(target_tokens)

    if duration is None:
        await message.answer(_usage("Формат: <code>/мут 30м</code> или <code>/мут 1ч</code>."))
        return

    targets = resolve_targets(owner_id, target_text, reply_text)
    if not targets:
        await message.answer(_usage("Не удалось найти пользователя. Укажи ID, @имя или сделай реплей."))
        return

    until = _expire_time(duration)
    for t in targets:
        set_user_mute(t.bot_id, t.chat_id, until)

    lines = [f"🔇 <b>{_actor_name(message.from_user)}</b> замьютил пользователя на {format_duration(duration)}:"]
    for t in targets:
        lines.append(f"  • {t.display()}")
    await message.answer("\n".join(lines))
# ═══════════════════════════════════════════════════════════
#  /стата <период> — статистика админов
# ═══════════════════════════════════════════════════════════

@router.message(F.text.regexp(_STATA_RE))
async def cmd_stata_period(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None:
        await message.answer("❌ Команда работает в привязанном «чате админов».")
        return

    arg = (_STATA_RE.match(message.text or "").group(1) or "").strip().lower()
    period = _PERIOD_KEYS.get(arg)
    if not period:
        await message.answer(
            _usage("Доступные периоды: <code>неделя</code>, <code>день</code>, <code>месяц</code>.")
        )
        return

    admins = get_admins_all(owner_id)
    if not admins:
        await message.answer("👥 Нет админов.")
        return

    lines = [
        f"📊 <b>Статистика админов за {_PERIOD_LABELS[period]}</b>",
        f"👤 Админов: <b>{len(admins)}</b>",
        "",
    ]
    for a in admins:
        stats = get_admin_message_stats(owner_id, a["user_id"])
        topics = get_admin_active_topics(owner_id, a["user_id"])
        msgs = stats.get(period, 0)
        uname = f"@{a['username']}" if a.get("username") else f"ID:{a['user_id']}"
        lines.append(
            f"• <b>#{a['tag']}</b> ({uname})\n"
            f"    ✉️ Сообщений: <b>{msgs}</b>\n"
            f"    📋 ПЗ за админом: <b>{topics}</b>"
        )

# ═══════════════════════════════════════════════════════════
#  /разбан, /размут, /снять варн
# ═══════════════════════════════════════════════════════════

@router.message(F.text.regexp(_RAZBAN_RE))
async def cmd_unban_global(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None or not _is_moderator(owner_id, message.from_user.id):
        await message.answer("❌ Команда работает в привязанном «чате админов».")
        return

    args_text = _RAZBAN_RE.match(message.text or "").group(1) or ""
    reply_text = message.reply_to_message.text if message.reply_to_message else ""
    targets = resolve_targets(owner_id, args_text, reply_text)
    if not targets:
        await message.answer(_usage("Не удалось найти пользователя. Укажи ID, @имя или сделай реплей."))
        return

    for t in _dedupe(targets):
        clear_user_restriction_for_owner(owner_id, t.chat_id)

    lines = [f"✅ <b>{_actor_name(message.from_user)}</b> снял бан:"]
    for t in _dedupe(targets):
        lines.append(f"  • {t.display()}")
    await message.answer("\n".join(lines))


@router.message(F.text.regexp(_RAZMUT_RE))
async def cmd_unmute_global(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None or not _is_moderator(owner_id, message.from_user.id):
        await message.answer("❌ Команда работает в привязанном «чате админов».")
        return

    args_text = _RAZMUT_RE.match(message.text or "").group(1) or ""
    reply_text = message.reply_to_message.text if message.reply_to_message else ""
    targets = resolve_targets(owner_id, args_text, reply_text)
    if not targets:
        await message.answer(_usage("Не удалось найти пользователя. Укажи ID, @имя или сделай реплей."))
        return

    for t in _dedupe(targets):
        clear_user_mute_for_owner(owner_id, t.chat_id)

    lines = [f"✅ <b>{_actor_name(message.from_user)}</b> снял мут:"]
    for t in _dedupe(targets):
        lines.append(f"  • {t.display()}")
    await message.answer("\n".join(lines))
@router.message(F.text.regexp(_SNYAT_WARN_RE))
async def cmd_unwarn_global(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None or not _is_moderator(owner_id, message.from_user.id):
        await message.answer("❌ Команда работает в привязанном «чате админов».")
        return

    args_text = _SNYAT_WARN_RE.match(message.text or "").group(1) or ""
    reply_text = message.reply_to_message.text if message.reply_to_message else ""
    targets = resolve_targets(owner_id, args_text, reply_text)
    if not targets:
        await message.answer(_usage("Не удалось найти пользователя. Укажи ID, @имя или сделай реплей."))
        return

    for t in _dedupe(targets):
        reset_user_warns_for_owner(owner_id, t.chat_id)

    lines = [f"✅ <b>{_actor_name(message.from_user)}</b> снял пред(ы):"]
    for t in _dedupe(targets):
        lines.append(f"  • {t.display()}")
    await message.answer("\n".join(lines))
# ═══════════════════════════════════════════════════════════
#  /модер, /разжаловать, /модеры — права модераторов чата
# ═══════════════════════════════════════════════════════════

@router.message(F.text.regexp(_MODER_RE))
async def cmd_add_moder(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None or not _is_owner(owner_id, message.from_user.id):
        await message.answer("❌ Только владелец чата может назначать модераторов.")
        return

    args_text = _MODER_RE.match(message.text or "").group(1) or ""
    reply_text = message.reply_to_message.text if message.reply_to_message else ""

    target = _resolve_target_user(owner_id, args_text, reply_text)
    if not target and message.reply_to_message and message.reply_to_message.from_user:
        target = {"chat_id": message.reply_to_message.from_user.id,
                  "username": message.reply_to_message.from_user.username or "",
                  "first_name": message.reply_to_message.from_user.first_name or ""}
    if not target:
        await message.answer(_usage("Не удалось найти пользователя. Сделай реплей или укажи ID/@имя."))
        return

    chat_id = target["chat_id"]
    if _is_owner(owner_id, chat_id):
        await message.answer("❌ Владелец и так имеет все права.")
        return
    if is_admin_chat_moderator(owner_id, chat_id):
        await message.answer("⚠️ Этот пользователь уже модератор.")
        return

    add_admin_chat_moderator(owner_id, chat_id, target["username"], target["first_name"])
    await message.answer(
        f"✅ <b>{_actor_name(message.from_user)}</b> назначил модератора:\n"
        f"  • <code>{chat_id}</code>"
    )
@router.message(F.text.regexp(_RAZZHAL_RE))
async def cmd_remove_moder(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None or not _is_owner(owner_id, message.from_user.id):
        await message.answer("❌ Только владелец чата может разжаловать модератора.")
        return

    args_text = _RAZZHAL_RE.match(message.text or "").group(1) or ""
    reply_text = message.reply_to_message.text if message.reply_to_message else ""
    target = _resolve_target_user(owner_id, args_text, reply_text)

    if not target and message.reply_to_message and message.reply_to_message.from_user:
        target = {"chat_id": message.reply_to_message.from_user.id,
                  "username": message.reply_to_message.from_user.username or "",
                  "first_name": message.reply_to_message.from_user.first_name or ""}
    if not target:
        await message.answer(_usage("Не удалось найти пользователя. Сделай реплей или укажи ID/@имя."))
        return

    chat_id = target["chat_id"]
    if _is_owner(owner_id, chat_id):
        await message.answer("❌ Владельца нельзя разжаловать.")
        return

    removed = remove_admin_chat_moderator(owner_id, chat_id)
    if removed:
        await message.answer(
            f"✅ <b>{_actor_name(message.from_user)}</b> разжаловал модератора:\n"
            f"  • <code>{chat_id}</code>"
        )
    else:
        await message.answer("⚠️ Этот пользователь не является модератором.")


@router.message(F.text.regexp(_MODERS_RE))
async def cmd_list_moders(message: Message) -> None:
    owner_id = _resolve_owner(message)
    if owner_id is None or not _is_moderator(owner_id, message.from_user.id):
        await message.answer("❌ Команда работает в привязанном «чате админов».")
        return

    moders = get_admin_chat_moderators(owner_id)
    lines = [f"🛡 <b>Модераторы чата</b>", ""]
    if moders:
        for m in moders:
            label = f"@{m['username']}" if m.get("username") else f"ID:{m['user_id']}"
            lines.append(f"  • <code>{m['user_id']}</code> — {label}")
    else:
        lines.append("  — нет")
    lines.append("")
    lines.append(f"👑 Владелец: <code>{owner_id}</code>")
    await message.answer("\n".join(lines))
    await message.answer("\n".join(lines))