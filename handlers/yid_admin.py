"""Поиск по YID и учёт YID в админ-панели.

Зачем этот модуль
-----------------
Владельцу платформы нужен быстрый ответ на два вопроса:

1. «Кто это за человек по номеру Y104?» — по номеру находят профиль.
2. «Кому номер уже выдан, а кому нет?» — список тех, кто ещё ни разу не
   открывал раздел YID.

Почему отдельный файл, а не ``handlers/profile.py``
--------------------------------------------------
Админ-панель в профиле — это уже большой раздел на две тысячи строк. Поиск
по YID — отдельная задача со своим состоянием, своими экранами и своей
логикой доступа; держать её там же значит искать её среди остального.

Доступ
------
Только владелец платформы (``is_super_admin``). Поиск по номеру выдаёт
чужому человеку имя, юзернейм и список ботов — это данные, которые не должны
быть видны всем подряд.
"""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import (
    cb_data,
    cb_uid,
    html_escape,
    msg_uid,
    render_callback,
)
from services.config import is_super_admin
from handlers.yid import show_yid_card
from services.storage import (
    bot_display_name,
    get_admins_all,
    get_all_users_registry,
    get_user_bots,
    get_user_registry,
    get_yid,
    get_yid_owner,
    utc_to_msk,
)

logger = logging.getLogger(__name__)

router = Router()

# Сколько человек показываем на странице списка.
PAGE_SIZE = 15


class YidAdminFSM(StatesGroup):
    """Ожидание номера YID для поиска."""
    waiting_number = State()


def _deny(callback: CallbackQuery) -> bool:
    """Отвечает отказом и говорит, что доступ закрыт."""
    if is_super_admin(cb_uid(callback)):
        return False
    callback.answer("⛔ Доступ запрещён", show_alert=True)
    return True


def _parse_yid(raw: str) -> int | None:
    """Понимает «Y104», «y104» и просто «104»."""
    text = (raw or "").strip().upper().lstrip("Y").strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


# ═══════════════ Поиск по номеру ═══════════════

def _user_card(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка человека по его номеру YID."""
    yid = get_yid(user_id)
    registry = get_user_registry(user_id) or {}
    bots = get_user_bots(user_id)
    admins = get_admins_all(user_id)

    name = registry.get("username") or registry.get("first_name") or "—"
    username = registry.get("username") or ""
    registered = (registry.get("created_at") or "—")[:19].replace("T", " ")

    lines = [
        f"🆔 <b>{('Y' + str(yid)) if yid else 'номер не выдан'}</b>\n",
        f"👤 Имя: <b>{html_escape(str(name))}</b>",
        f"🔗 Юзернейм: {'@' + html_escape(str(username)) if username else '—'}",
        f"🆔 ID: <code>{user_id}</code>",
        f"📅 В системе с: <b>{utc_to_msk(registered)[:10]}</b>",
        f"🤖 Ботов: <b>{len(bots)}</b>",
        f"👥 Админов у владельцев: <b>{len(admins)}</b>",
    ]
    if bots:
        lines.append("")
        for b in bots[:5]:
            lines.append(f"  • {html_escape(bot_display_name(b))}")

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Профили", callback_data="profiles_list",
                              style="primary")],
        # «👤 Профиль+» — открыть карточку YID этого человека в самом боте.
        # Раньше здесь была только кнопка списка профилей: из результата
        # поиска нельзя было перейти к карточке самого человека.
        [InlineKeyboardButton(text="👤 Профиль+",
                              callback_data=f"yid_show_card_{user_id}",
                              style="success")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="yid_list",
                              style="primary")],
    ])


@router.callback_query(F.data.regexp(r"^yid_show_card_(\d+)$"))
async def cb_yid_show_card(callback: CallbackQuery, state: FSMContext) -> None:
    """«👤 Профиль+» из поиска по YID: открыть карточку YID человека.

    Тот же экран, что и по кнопке «🆔 YID» в профиле, только для найденного
    по номеру человека. Своей карточки в разделе YID у него может не быть
    (номер выдаётся при первом открытии), поэтому рисуем её здесь.
    """
    if _deny(callback):
        return
    target = int(cb_data(callback).split("yid_show_card_", 1)[1])
    await state.clear()
    text, kb = show_yid_card(target)
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "yid_find")
async def cb_yid_find(callback: CallbackQuery, state: FSMContext) -> None:
    """Запрашивает номер для поиска."""
    if _deny(callback):
        return
    await state.set_state(YidAdminFSM.waiting_number)
    await render_callback(
        callback,
        "🔎 <b>Найти по YID</b>\n\n"
        "Напиши номер — можно с буквой или без: <code>Y104</code> или "
        "<code>104</code>.",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Список YID", callback_data="yid_list",
                                 style="primary"),
        ]]),
    )


@router.message(YidAdminFSM.waiting_number)
async def fsm_yid_find(message: Message, state: FSMContext) -> None:
    """Ищет человека по номеру YID."""
    if not is_super_admin(msg_uid(message)):
        await state.clear()
        return

    number = _parse_yid(message.text or "")
    if number is None:
        await message.answer(
            "❌ Не понял номер.\n\nНапиши так: <code>Y104</code> или "
            "<code>104</code>.")
        return

    user_id = get_yid_owner(number)
    await state.clear()

    if not user_id:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Список YID", callback_data="yid_list",
                                 style="primary"),
        ]])
        await message.answer(f"🤷 <b>Y{number}</b> никому не выдан.", reply_markup=kb)
        return

    text, kb = _user_card(int(user_id))
    await message.answer(text, reply_markup=kb)
# ═══════════════ Список: кто получил номер, а кто нет ═══════════════

def _split_by_yid() -> tuple[list[dict], list[dict]]:
    """Разделяет реестр на «с номером» и «без номера»."""
    with_num: list[dict] = []
    without: list[dict] = []
    for u in get_all_users_registry():
        with_num.append(u) if int(u.get("yid") or 0) > 0 else without.append(u)
    with_num.sort(key=lambda r: int(r.get("yid") or 0))
    return with_num, without


def _pager(prefix: str, page: int, total: int) -> list[list[InlineKeyboardButton]]:
    """Строка листания (пустая, если страница одна)."""
    if total <= 1:
        return []
    prev_page = page - 1 if page > 1 else total
    next_page = page + 1 if page < total else 1
    return [[
        InlineKeyboardButton(text="◀️", callback_data=f"{prefix}_{prev_page}",
                             style="primary"),
        InlineKeyboardButton(text=f"📄 {page}/{total}",
                             callback_data=f"{prefix}_{page}", style="primary"),
        InlineKeyboardButton(text="▶️", callback_data=f"{prefix}_{next_page}",
                             style="primary"),
    ]]


def _page_of(data: str, prefix: str) -> int:
    tail = data.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() and int(tail) > 0 else 1


def _page(items: list[dict], page: int) -> tuple[list[dict], int]:
    total = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total))
    return items[(page - 1) * PAGE_SIZE:page * PAGE_SIZE], total


def _fit_name(text: str, limit: int = 50) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _users_kb(rows: list[dict], total: int, page: int,
              prefix: str) -> list[list[InlineKeyboardButton]]:
    kb_rows: list[list[InlineKeyboardButton]] = []
    for u in rows:
        name = u.get("username") or u.get("first_name") or str(u["user_id"])
        kb_rows.append([InlineKeyboardButton(
            text=_fit_name(f"{name} · Y{u.get('yid') or '—'}"),
            callback_data=f"profile_view_{u['user_id']}", style="primary")])
    kb_rows.extend(_pager(prefix, page, total))
    kb_rows.append([InlineKeyboardButton(text="⬅️ YID",
                                         callback_data="yid_list",
                                         style="primary")])
    return kb_rows


def _note(page: int, total: int) -> str:
    return f"\n\n📄 Страница <b>{page}</b> из <b>{total}</b>" if total > 1 else ""


@router.callback_query(F.data == "yid_list")
async def cb_yid_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Меню YID: поиск и два списка."""
    if _deny(callback):
        return
    await state.clear()
    with_num, without = _split_by_yid()

    text = (
        "🆔 <b>YID: кто получил номер</b>\n\n"
        f"✅ С номером: <b>{len(with_num)}</b>\n"
        f"⏳ Без номера: <b>{len(without)}</b>\n\n"
        "Номер выдаётся, когда человек открывает «🆔 YID»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Найти по номеру",
                              callback_data="yid_find", style="success")],
        [InlineKeyboardButton(text=f"✅ С номером ({len(with_num)})",
                              callback_data="yid_list_with",
                              style="primary")],
        [InlineKeyboardButton(text=f"⏳ Без номера ({len(without)})",
                              callback_data="yid_list_without",
                              style="primary")],
        [InlineKeyboardButton(text="⬅️ Админ-панель",
                              callback_data="profile_admin", style="primary")],
    ])
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^yid_list_with(_p_\d+)?$"))
async def cb_yid_list_with(callback: CallbackQuery) -> None:
    """Список тех, кому номер уже выдан."""
    if _deny(callback):
        return
    with_num, _ = _split_by_yid()
    if not with_num:
        await render_callback(
            callback, "✅ <b>С номером</b>\n\nПока таких нет.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ YID", callback_data="yid_list",
                                     style="primary"),
            ]]))
        return

    page = _page_of(cb_data(callback), "yid_list_with_p")
    rows, total = _page(with_num, page)
    await render_callback(
        callback,
        f"✅ <b>Номер выдан</b> ({len(with_num)}){_note(page, total)}",
        InlineKeyboardMarkup(inline_keyboard=_users_kb(
            rows, total, page, "yid_list_with_p")),
    )


@router.callback_query(F.data.regexp(r"^yid_list_without(_p_\d+)?$"))
async def cb_yid_list_without(callback: CallbackQuery) -> None:
    """Список тех, кому номер ещё не выдан."""
    if _deny(callback):
        return
    _with_num, without = _split_by_yid()
    if not without:
        await render_callback(
            callback, "✅ <b>Без номера</b>\n\nТаких нет — у всех есть YID.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ YID", callback_data="yid_list",
                                     style="primary"),
            ]]))
        return

    page = _page_of(cb_data(callback), "yid_list_without_p")
    rows, total = _page(without, page)
    await render_callback(
        callback,
        f"⏳ <b>Номер ещё не выдан</b> ({len(without)}){_note(page, total)}",
        InlineKeyboardMarkup(inline_keyboard=_users_kb(
            rows, total, page, "yid_list_without_p")),
    )