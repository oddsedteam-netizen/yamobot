"""«👤 Профиль+» — обзор всех пользователей и ботов платформы.

Зачем этот модуль
-----------------
Раньше в админ-панели была кнопка «👤 Владелец бота»: она открывала список
БОТОВ, а по боту — профиль его владельца. Этого мало: посмотреть профиль
человека, у которого ботов ещё нет, или который не владелец, а просто
админ/ПЗ в чужом боте, было невозможно вообще.

Теперь кнопка называется «👤 Профиль+» и открывает развилку из двух списков:

* **🤖 Боты** — все подключённые боты; по боту открывается полный профиль
  его владельца: настройки, привязки, его боты;
* **👥 ВЛД** — все люди, заведённые в системе; по человеку — его карточка.

Почему два списка, а не один
---------------------------
Список людей и список ботов отвечают на разные вопросы. «Кто этот человек
и что у него настроено» — вопрос про ВЛД. «У этого бота кто владелец» —
вопрос про боты. Один общий список в сотни строк читать невозможно.

Доступ
------
Только владелец платформы (``is_super_admin``): здесь видны ID всех
пользователей, их привязанные чаты и состав ботов — данные, которые
пользователям показывать нельзя.

Осторожность с действиями
--------------------------
Кнопки этого раздела меняют состояние чужого человека (отвязка чата,
удаление бота, бан). Поэтому удаление подтверждается отдельным экраном, а
опасные кнопки помечены красным: это не то, что делают случайно.
"""

import logging

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from handlers._common import (
    cb_data,
    cb_uid,
    html_escape,
    render_callback,
)
from services.config import is_super_admin
from services.storage import (
    bot_display_name,
    get_all_bots_flat,
    get_all_users_registry,
    get_bound_chat,
    get_user_bots,
    get_user_registry,
    remove_user_bot,
    set_bound_chat,
    set_registry_user_blocked,
)

logger = logging.getLogger(__name__)

router = Router()

# Сколько строк на страницу: люди копятся быстро, а Telegram не любит
# клавиатуры длиннее примерно сотни кнопок.
PAGE_SIZE = 15

BACK_TO_ADMIN = "profile_admin"
BACK_TO_LIST = "prfplus_root"


def _deny(callback: CallbackQuery) -> bool:
    """Отвечает отказом, если нажавший — не супер-админ."""
    if is_super_admin(cb_uid(callback)):
        return False
    callback.answer("⛔ Доступ запрещён", show_alert=True)
    return True


def _page_of(data: str) -> int:
    """Номер страницы из ``<префикс>_<N>`` (1 — первая)."""
    tail = data.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() and int(tail) > 0 else 1


def _pager(prefix: str, page: int, total: int,
           back_data: str) -> list[list[InlineKeyboardButton]]:
    """Ряд листания и возврат назад."""
    rows: list[list[InlineKeyboardButton]] = []
    if total > 1:
        prev_p = page - 1 if page > 1 else total
        next_p = page + 1 if page < total else 1
        rows.append([
            InlineKeyboardButton(text="◀️", callback_data=f"{prefix}_{prev_p}",
                                 style="primary"),
            InlineKeyboardButton(text=f"📄 {page}/{total}",
                                 callback_data=f"{prefix}_{page}", style="primary"),
            InlineKeyboardButton(text="▶️", callback_data=f"{prefix}_{next_p}",
                                 style="primary"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_data,
                                      style="primary")])
    return rows


def _user_name(user_id: int, row: dict | None = None) -> str:
    """Как подписать человека: юзернейм, иначе имя, иначе ID."""
    row = row or get_user_registry(user_id) or {}
    name = row.get("username") or row.get("first_name") or ""
    return f"@{html_escape(str(name))}" if name else f"ID:{user_id}"


def _bots_line(bots: list[dict], with_state: bool = False) -> str:
    """Список ботов многострочкой (для карточек)."""
    if not bots:
        return "   — нет —"
    return "\n".join(
        f"   • {bot_display_name(b)} <code>{b['id']}</code>"
        + ("" if with_state or b.get("stopped") else " — 🟢 работает")
        for b in bots
    )


# ═══════════════ Развилка ═══════════════


@router.callback_query(F.data == "profile_plus")
async def cb_profile_plus(callback: CallbackQuery) -> None:
    """«👤 Профиль+» — выбор: смотреть по ботам или по людям."""
    if _deny(callback):
        return

    bots = get_all_bots_flat()
    users = get_all_users_registry()

    text = (
        "👤 <b>Профили</b>\n\n"
        "С кого начать?\n\n"
        f"🤖 <b>Ботов</b> — <b>{len(bots)}</b>\n"
        "   по боту откроется полный профиль его владельца\n"
        f"👥 <b>ВЛД</b> — <b>{len(users)}</b>\n"
        "   все люди, заведённые в системе"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Боты", callback_data="prfplus_bots",
                              style="success")],
        [InlineKeyboardButton(text="👥 ВЛД", callback_data="prfplus_users",
                              style="success")],
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data=BACK_TO_ADMIN,
                              style="primary")],
    ])
    await render_callback(callback, text, kb)


@router.callback_query(F.data == BACK_TO_LIST)
async def cb_prfplus_root(callback: CallbackQuery) -> None:
    """Возврат к развилке «Профиль+»."""
    await cb_profile_plus(callback)


# ═══════════════ Список ботов ═══════════════


def _bots_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Список всех ботов; по боту — профиль его владельца."""
    bots = get_all_bots_flat()
    if not bots:
        return (
            "🤖 <b>Боты</b>\n\nВ системе пока нет ни одного бота.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ Назад", callback_data=BACK_TO_LIST,
                                     style="primary"),
            ]]),
        )

    total = max(1, (len(bots) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total)
    window = bots[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    text = [f"🤖 <b>Боты</b> ({len(bots)}) — стр. {page}/{total}\n"]
    rows: list[list[InlineKeyboardButton]] = []
    for b in window:
        bot_id = int(b["id"])
        owner_id = int(b.get("owner_id") or 0)
        state = "" if b.get("stopped") else " 🟢"
        text.append(
            f"\n🤖 <b>{bot_display_name(b)}</b>{state} <code>{bot_id}</code>"
            + (f"\n   👤 владелец: {_user_name(owner_id)}" if owner_id else "")
        )
        rows.append([InlineKeyboardButton(
            text=bot_display_name(b)[:40],
            callback_data=f"prfplus_owner_of_{bot_id}",
            style="primary")])
    rows.extend(_pager("prfplus_bots_p", page, total, BACK_TO_LIST))
    return "\n".join(text), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(r"^prfplus_bots(_p_(\d+))?$"))
async def cb_prfplus_bots(callback: CallbackQuery) -> None:
    """Список ботов."""
    if _deny(callback):
        return
    text, kb = _bots_page(_page_of(cb_data(callback)))
    await render_callback(callback, text, kb)


# ═══════════════ Список людей (ВЛД) ═══════════════


def _users_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Список всех людей; по человеку — его карточка."""
    users = get_all_users_registry()
    if not users:
        return (
            "👥 <b>ВЛД</b>\n\nВ системе пока никого нет.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ Назад", callback_data=BACK_TO_LIST,
                                     style="primary"),
            ]]),
        )

    total = max(1, (len(users) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total)
    window = users[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    text = [f"👥 <b>ВЛД</b> ({len(users)}) — стр. {page}/{total}\n"]
    rows: list[list[InlineKeyboardButton]] = []
    for u in window:
        uid = int(u["user_id"])
        banned = "🚫 " if u.get("blocked") else ""
        text.append(
            f"\n{banned}<b>{_user_name(uid, u)}</b> <code>{uid}</code>"
            f"\n   🤖 ботов: <b>{len(get_user_bots(uid))}</b>"
        )
        rows.append([InlineKeyboardButton(
            text=f"{banned}{_user_name(uid, u)}"[:40],
            callback_data=f"prfplus_user_{uid}",
            style="primary")])
    rows.extend(_pager("prfplus_users_p", page, total, BACK_TO_LIST))
    return "\n".join(text), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(r"^prfplus_users(_p_(\d+))?$"))
async def cb_prfplus_users(callback: CallbackQuery) -> None:
    """Список людей."""
    if _deny(callback):
        return
    text, kb = _users_page(_page_of(cb_data(callback)))
    await render_callback(callback, text, kb)


# ═══════════════ Карточка человека ═══════════════


def _person_text(uid: int) -> str:
    """Карточка человека: ID, дата, бан, боты."""
    row = get_user_registry(uid) or {}
    bots = get_user_bots(uid)
    return (
        f"👤 <b>{_user_name(uid, row)}</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📛 Имя: {html_escape(str(row.get('first_name') or '—'))}\n"
        f"📅 В системе с: {str(row.get('created_at') or '—')[:19]}\n"
        f"📌 Забанен: {'да' if row.get('blocked') else 'нет'}\n\n"
        f"🤖 <b>Ботов: {len(bots)}</b>\n{_bots_line(bots)}"
    )


@router.callback_query(F.data.regexp(r"^prfplus_user_(\d+)$"))
async def cb_prfplus_user(callback: CallbackQuery) -> None:
    """Карточка человека из списка ВЛД."""
    if _deny(callback):
        return

    uid = int(cb_data(callback).rsplit("_", 1)[-1])
    row = get_user_registry(uid)
    if not row:
        await callback.answer("❌ Человек не найден", show_alert=True)
        return

    bots = get_user_bots(uid)
    rows: list[list[InlineKeyboardButton]] = []
    # Разбан — только если человек действительно забанен: кнопка «разбанить»
    # у живого человека вводит в заблуждение.
    if row.get("blocked"):
        rows.append([InlineKeyboardButton(text="✅ Разбанить",
                                          callback_data=f"prfplus_unban_{uid}",
                                          style="success")])
    if bots:
        rows.append([InlineKeyboardButton(text="🗑 Удалить всех ботов",
                                          callback_data=f"prfplus_delbots_{uid}",
                                          style="danger")])
    rows.append([InlineKeyboardButton(text="⬅️ К списку",
                                      callback_data="prfplus_users",
                                      style="primary")])
    await render_callback(callback, _person_text(uid),
                          InlineKeyboardMarkup(inline_keyboard=rows))


# ═══════════════ Полный профиль владельца бота ═══════════════


def _owner_text(owner_id: int) -> str:
    """Полный профиль владельца: привязки, боты, статус."""
    row = get_user_registry(owner_id) or {}
    bots = get_user_bots(owner_id)
    work = get_bound_chat(owner_id, "work")
    admin = get_bound_chat(owner_id, "admin")

    def _chat(value: int | None) -> str:
        return f"<code>{value}</code>" if value else "не привязан"

    return (
        f"👤 <b>Профиль {_user_name(owner_id, row)}</b>\n\n"
        f"🆔 ID: <code>{owner_id}</code>\n"
        f"📛 Имя: {html_escape(str(row.get('first_name') or '—'))}\n"
        f"📅 В системе с: {str(row.get('created_at') or '—')[:19]}\n"
        f"📌 Забанен: {'да' if row.get('blocked') else 'нет'}\n\n"
        f"💼 Чат работы: {_chat(work)}\n"
        f"🛡 Чат админов: {_chat(admin)}\n\n"
        f"🤖 <b>Ботов: {len(bots)}</b>\n{_bots_line(bots, with_state=True)}"
    )


def _owner_kb(owner_id: int) -> InlineKeyboardMarkup:
    """Кнопки полного профиля владельца.

    Отвязка чатов — по одной кнопке на чат: у человека два РАЗНЫХ чата (работы
    и админов), и одна кнопка «отвязать всё» сносила бы оба молча.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for kind, title in (("work", "💼 Чат работы"), ("admin", "🛡 Чат админов")):
        if get_bound_chat(owner_id, kind):
            rows.append([InlineKeyboardButton(
                text=f"🔓 Отвязать {title}",
                callback_data=f"prfplus_unbind_{owner_id}_{kind}",
                style="danger")])
    if get_user_bots(owner_id):
        rows.append([InlineKeyboardButton(text="🗑 Удалить ботов",
                                          callback_data=f"prfplus_delbots_{owner_id}",
                                          style="danger")])
    rows.append([InlineKeyboardButton(text="⬅️ К списку ботов",
                                      callback_data="prfplus_bots",
                                      style="primary")])
    rows.append([InlineKeyboardButton(text="⬅️ Админ-панель",
                                      callback_data=BACK_TO_ADMIN,
                                      style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(r"^prfplus_owner_of_(\d+)$"))
async def cb_prfplus_owner_of(callback: CallbackQuery) -> None:
    """По боту — полный профиль его владельца."""
    if _deny(callback):
        return

    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    bot = next((b for b in get_all_bots_flat() if int(b["id"]) == bot_id), None)
    if not bot:
        await callback.answer("❌ Бот не найден", show_alert=True)
        return

    owner_id = int(bot.get("owner_id") or 0)
    if not owner_id:
        await callback.answer("⚠️ У бота не записан владелец", show_alert=True)
        return

    await render_callback(callback, _owner_text(owner_id), _owner_kb(owner_id))


# ═══════════════ Действия ═══════════════


@router.callback_query(F.data.regexp(r"^prfplus_unbind_(\d+)_(work|admin)$"))
async def cb_prfplus_unbind(callback: CallbackQuery) -> None:
    """Отвязать ОДИН чат — ровно тот, что нажат."""
    if _deny(callback):
        return

    parts = cb_data(callback).split("_")
    owner_id, kind = int(parts[2]), parts[3]
    if not get_bound_chat(owner_id, kind):
        await callback.answer("ℹ️ Этот чат уже не привязан", show_alert=True)
    else:
        set_bound_chat(owner_id, kind, None)
        title = "работы" if kind == "work" else "админов"
        await callback.answer(f"🔓 Чат {title} отвязан")

    await render_callback(callback, _owner_text(owner_id),
                          _owner_kb(owner_id))


@router.callback_query(F.data.regexp(r"^prfplus_delbots_(\d+)$"))
async def cb_prfplus_delbots(callback: CallbackQuery) -> None:
    """Удаление ботов — всегда через подтверждение."""
    if _deny(callback):
        return

    owner_id = int(cb_data(callback).rsplit("_", 1)[-1])
    bots = get_user_bots(owner_id)
    if not bots:
        await callback.answer("ℹ️ У него нет ботов", show_alert=True)
        return

    text = (
        "🗑 <b>Удалить все боты?</b>\n\n"
        f"Владелец: <b>{_user_name(owner_id)}</b>\n"
        f"Всего ботов: <b>{len(bots)}</b>\n\n"
        "Вместе с ними удалятся ПЗ, сообщения и настройки этих ботов. "
        "Действие необратимое."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить",
                              callback_data=f"prfplus_delbots_yes_{owner_id}",
                              style="danger")],
        [InlineKeyboardButton(text="❌ Отмена",
                              callback_data=f"prfplus_back_owner_{owner_id}",
                              style="primary")],
    ])
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^prfplus_back_owner_(\d+)$"))
async def cb_prfplus_back_owner(callback: CallbackQuery) -> None:
    """Отмена удаления — вернуться к профилю владельца."""
    if _deny(callback):
        return
    owner_id = int(cb_data(callback).rsplit("_", 1)[-1])
    await render_callback(callback, _owner_text(owner_id), _owner_kb(owner_id))


@router.callback_query(F.data.regexp(r"^prfplus_delbots_yes_(\d+)$"))
async def cb_prfplus_delbots_yes(callback: CallbackQuery) -> None:
    """Подтверждение удаления: удаляем боты и возвращаемся в список людей."""
    if _deny(callback):
        return

    owner_id = int(cb_data(callback).rsplit("_", 1)[-1])
    removed = 0
    for b in get_user_bots(owner_id):
        if remove_user_bot(owner_id, int(b["id"])):
            removed += 1

    await callback.answer(f"🗑 Удалено ботов: {removed}")
    text, kb = _users_page(1)
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^prfplus_unban_(\d+)$"))
async def cb_prfplus_unban(callback: CallbackQuery) -> None:
    """Снять бан с человека."""
    if _deny(callback):
        return

    uid = int(cb_data(callback).rsplit("_", 1)[-1])
    set_registry_user_blocked(uid, False)
    await callback.answer("✅ Разбанен")

    bots = get_user_bots(uid)
    rows: list[list[InlineKeyboardButton]] = []
    if bots:
        rows.append([InlineKeyboardButton(text="🗑 Удалить всех ботов",
                                          callback_data=f"prfplus_delbots_{uid}",
                                          style="danger")])
    rows.append([InlineKeyboardButton(text="⬅️ К списку",
                                      callback_data="prfplus_users",
                                      style="primary")])
    await render_callback(callback, _person_text(uid),
                          InlineKeyboardMarkup(inline_keyboard=rows))