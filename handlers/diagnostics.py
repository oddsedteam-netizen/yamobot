"""Диагностика: что работает, что сломалось и что делать.

Зачем
----
Жалоба «ничего не работает» обычно шла без деталей: непонятно, мёртвый ли
токен, не привязан ли чат админов или бот не администратор в чате. Раздел
«🩺 Диагностика» проходит по всем ботам пользователя и по привязкам основного
бота и для каждой находки говорит, КАК её исправить.

Проверки
--------
* боты: запущены ли, не остановлены ли, были ли ошибки в журнале;
* привязки: чат работы и чат админов;
* права: является ли YamoBot администратором привязанного чата админов;
* очередь доставки: не застряли ли недоставленные сообщения;
* ПЗ: сколько обращений висит без админа;
* защита: антирейд / антинакрутка, не заблокирован ли пользователь.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from handlers._common import cb_uid, render_callback
from services.child_manager import ChildManager, get_main_bot
from services.storage import (
    bot_display_name,
    get_all_topics_for_bot,
    get_antiraid_settings,
    get_antinakrutka_settings,
    get_bound_chat,
    get_bot_errors,
    get_user_bots,
    is_registry_user_banned,
    outbox_counts,
)

logger = logging.getLogger(__name__)

router = Router()

OK = "ok"
WARN = "warn"
ERR = "err"

_MARKS = {OK: "✅", WARN: "⚠️", ERR: "❌"}


def _finding(level: str, title: str, fix: str) -> dict:
    """Одна находка: что не так и что с этим делать."""
    return {"level": level, "title": title, "fix": fix}


async def _check_admin_chat_rights(bot: Bot, chat_id: int) -> bool | None:
    """Есть ли у YamoBot права в чате админов.

    ``None`` — проверить не удалось (сеть, чат недоступен). Молчать об этом
    нельзя: пользователь решил бы, что всё в порядке.
    """
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except Exception:
        logger.debug("Не удалось проверить права в чате %s", chat_id, exc_info=True)
        return None
    return str(getattr(member, "status", "")) in ("administrator", "creator")


def _check_bindings(owner_id: int) -> list[dict]:
    """Проверка привязок чатов — самая частая причина «ничего не работает»."""
    found: list[dict] = []
    if not get_bound_chat(owner_id, "admin"):
        found.append(_finding(
            ERR, "Не привязан «чат админов»",
            "Открой профиль → «🔗 Привязать чаты» → «🛡 Чат админов». "
            "Без него не приходят новые ПЗ и напоминалки.",
        ))
    if not get_bound_chat(owner_id, "work"):
        found.append(_finding(
            WARN, "Не привязан «чат работы»",
            "Открой профиль → «🔗 Привязать чаты» → «💼 Чат работы». "
            "Тогда обращения будут уходить в твою группу.",
        ))
    return found


def _check_bots(bots: list[dict], running: set[int]) -> list[dict]:
    """Проверка состояния ботов: работают, не остановлены, были ли ошибки."""
    found: list[dict] = []
    offline = errored = no_admin = 0

    for row in bots:
        bot_id = int(row["id"])
        name = bot_display_name(row)
        if bot_id not in running:
            offline += 1
        if row.get("stopped"):
            found.append(_finding(
                WARN, f"Бот «{name}» остановлен",
                "Включи его в списке ботов — иначе он не отвечает на ПЗ.",
            ))
        if get_bot_errors(bot_id, limit=1):
            errored += 1
        no_admin += sum(
            1 for t in get_all_topics_for_bot(bot_id)
            if not t.get("admin_user_id")
        )

    if offline:
        found.append(_finding(
            ERR if offline == len(bots) else WARN,
            f"Не работает ботов: {offline} из {len(bots)}",
            "Нажми в профиле «🔄 Полный перезапуск». Если не помогло — "
            "проверь токен бота и журнал ботов.",
        ))
    if errored:
        found.append(_finding(
            WARN, f"В ботах были ошибки: {errored}",
            "Посмотреть можно в «🗂 Логи ботов» — там видно, что сломалось.",
        ))
    if no_admin:
        found.append(_finding(
            WARN, f"Обращений без админа: {no_admin}",
            "Их нужно взять в чате админов кнопкой «✋ Я беру». "
            "Включи «⏰ Напоминалка», чтобы напоминать автоматически.",
        ))
    return found


def _check_protection(owner_id: int) -> list[dict]:
    """Проверка защиты: антирейд, антинакрутка, блокировка."""
    found: list[dict] = []

    if is_registry_user_banned(owner_id):
        found.append(_finding(
            ERR, "Твой аккаунт заблокирован",
            "Обратись в поддержку: разблокировка делается вручную.",
        ))

    antiraid = get_antiraid_settings(owner_id)
    if antiraid.get("enabled") and antiraid.get("triggered"):
        found.append(_finding(
            WARN, "Антирейд сработал — чат админов заблокирован",
            "Чат админов закрыт от новых участников. Если срабатывание ложное "
            "— выключи «🛡 Антирейд» в профиле.",
        ))

    nakrutka = get_antinakrutka_settings(owner_id)
    if int(nakrutka.get("enabled", 1)) and nakrutka.get("triggered"):
        found.append(_finding(
            WARN, "Антинакрутка активна — новые ПЗ не доставляются",
            "Так бот защищается от наплыва фейковых ПЗ. Сними защиту: "
            "профиль → «🛡 Защита» → «🚨 Антинакрутка».",
        ))
    return found


def _check_outbox() -> list[dict]:
    """Проверка очереди доставки: не застряли ли сообщения."""
    if not outbox_counts().get("failed"):
        return []
    return [_finding(
        WARN, f"Не доставлено сообщений: {outbox_counts()['failed']}",
        "Обычно это чаты, куда бот писать не может. Проверь, что чаты живы "
        "и YamoBot — администратор.",
    )]

async def collect_diagnostics(owner_id: int, bot: Bot | None,
                             running: set[int]) -> list[dict]:
    """Собирает все находки по ботам пользователя."""
    bots = get_user_bots(owner_id)
    if not bots:
        return [_finding(
            WARN, "У тебя пока нет ни одного бота",
            "Добавь бота через «➕ Добавить бота» — иначе проверять нечего.",
        )]

    found = _check_bindings(owner_id)

    admin_chat = get_bound_chat(owner_id, "admin")
    if admin_chat and bot is not None:
        rights = await _check_admin_chat_rights(bot, int(admin_chat))
        if rights is False:
            found.append(_finding(
                ERR, "YamoBot не администратор в чате админов",
                "Выдай права: «Управление чатом» → «Администраторы» → "
                "YamoBot → «Назначить администратором».",
            ))

    found += _check_bots(bots, running)
    found += _check_outbox()
    found += _check_protection(owner_id)

    if not found:
        found.append(_finding(
            OK, "Всё в порядке",
            "Боты работают, чаты привязаны, накопленных ошибок нет.",
        ))
    return found


def _payload(findings: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопки экрана диагностики."""
    errors = [f for f in findings if f["level"] == ERR]
    warns = [f for f in findings if f["level"] == WARN]

    lines = [
        "🩺 <b>Диагностика</b>\n\n"
        f"❌ Проблем: <b>{len(errors)}</b> · "
        f"⚠️ Предупреждений: <b>{len(warns)}</b>",
    ]

    for item in findings:
        mark = _MARKS.get(item["level"], "•")
        lines.append(f"\n{mark} <b>{item['title']}</b>")
        if item["level"] != OK:
            # Главная ценность экрана — не «что сломалось», а «что делать».
            lines.append(f"    👉 {item['fix']}")

    if not errors and not warns:
        lines.append("\n\nПроблем не найдено 👍")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить заново",
                              callback_data="user_diagnostics", style="primary")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show",
                              style="primary")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data == "user_diagnostics")
async def cb_user_diagnostics(callback: CallbackQuery,
                              child_manager: ChildManager) -> None:
    """Диагностика по всем ботам пользователя.

    ``child_manager`` внедряется диспетчером (``app.py`` кладёт его в
    ``dp["child_manager"]``). Раньше здесь стояло ``callback.bot.get(...)`` —
    но ``callback.bot`` это объект ``Bot``, а не словарь диспетчера: у него
    нет метода ``.get``, и нажатие на кнопку роняло обработчик с
    ``AttributeError``. См. остальные хендлеры (profile.py, my_bots.py).
    """
    user_id = cb_uid(callback)
    running: set[int] = set()
    if isinstance(child_manager, ChildManager):
        running = {int(b["id"]) for b in get_user_bots(user_id)
                   if child_manager.is_running(int(b["id"]))}

    findings = await collect_diagnostics(user_id, get_main_bot(), running)
    text, kb = _payload(findings)
    await render_callback(callback, text, kb)
