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
from handlers.person_card import person_card_text
from handlers.yid import show_yid_card
from services.storage import (
    get_all_users_registry,
    get_user_banned_bots,
    get_user_by_username,
    get_user_registry,
    get_yid_owner,
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


# ═══════════════ Поиск человека по ID (YID, Telegram ID, @username) ══════════
#
# Зачем
# ----
# Владелец попросил искать человека в админ-панели «по айди — айди телеграм
# и YID, если есть». Раньше понимался только номер YID, а настоящий Telegram
# ID (тот, что пишут в уведомлениях о бане) найти было нельзя.
#
# Разбор по форме строки:
#   «Y104» / «104»       — внутренний номер;
#   «123456789» (≥6 цифр) — Telegram ID: у YID числа трёхзначные, а у
#                           Telegram ID — всегда длинные;
#   «@ivan» / «ivan»     — юзернейм.


def _find_user(raw: str) -> tuple[dict | None, str]:
    """Человек по строке запроса. Возвращает ``(запись, как искали)``."""
    text = (raw or "").strip()
    if not text:
        return None, ""

    # Сначала номер с буквой: «Y104» содержит и букву, и цифры, поэтому
    # проверка «это username?» и «это цифры?» его не поймала бы.
    if text.upper().startswith("Y"):
        number = _parse_yid(text)
        if number is not None:
            owner = get_yid_owner(number)
            return (get_user_registry(int(owner)), "yid") if owner else (None, "yid")

    if text.lstrip("@").isalpha() or (text.startswith("@") and
                                      text.lstrip("@").replace("_", "").isalnum()):
        return get_user_by_username(text), "username"

    digits = text.lstrip("@").strip()
    if digits.isdigit():
        number = int(digits)
        # Длинное число — это Telegram ID, короткое — YID. Сначала пробуем
        # ID: номер YID может совпасть с ID какого-то бота, и тогда человек
        # и бот «склеились» бы в одну карточку.
        if len(digits) >= 6:
            registry = get_user_registry(number)
            if registry:
                return registry, "tgid"
        owner = get_yid_owner(number)
        if owner:
            return get_user_registry(int(owner)), "yid"
        # Короткое число могло быть ID — проверяем и его.
        return get_user_registry(number), "tgid" if len(digits) >= 6 else "yid"

    return None, ""


def _query_help() -> str:
    """Подсказка формата для экрана поиска."""
    return (
        "🔎 <b>Найти человека по ID</b>\n\n"
        "Напиши любое из трёх — понимаю все:\n"
        "• <b>Telegram ID</b> — <code>123456789</code>\n"
        "• <b>YID</b> — <code>Y104</code> или <code>104</code>\n"
        "• <b>@username</b> — <code>@ivan</code>\n\n"
        "Найду и покажу полную карточку: боты, чаты, YID, бан."
    )


# ═══════════════ Поиск по номеру ═══════════════

def _user_card(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка человека: полная, как в «Профиль+».

    Раньше здесь был свой, более бедный вариант (имя, ID, YID, счётчики без
    чатов), из-за чего поиск по YID и поиск в «Профиль+» показывали разное.
    Теперь обе точки входа ведут в один рендер — ``person_card_text``.
    """
    text = person_card_text(user_id)

    return text, InlineKeyboardMarkup(inline_keyboard=[
        # Разбан в ПЗ — по жалобе владельца («забанил бота, разбанил, а
        # сообщения не доходят»): бан хранится по ботам, в карточке без
        # отдельной кнопки его снять было негде.
        ([InlineKeyboardButton(
            text="🔓 Снять бан в ПЗ",
            callback_data=f"prfplus_pzunban_{user_id}", style="success")]
         if get_user_banned_bots(user_id) else []),
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
    """Запрашивает ID для поиска: Telegram ID, YID или @username."""
    if _deny(callback):
        return
    await state.set_state(YidAdminFSM.waiting_number)
    await render_callback(
        callback,
        _query_help(),
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Список YID", callback_data="yid_list",
                                 style="primary"),
        ]]),
    )


@router.message(YidAdminFSM.waiting_number)
async def fsm_yid_find(message: Message, state: FSMContext) -> None:
    """Ищет человека по Telegram ID, YID или @username.

    Раньше понимался только номер YID, поэтому по настоящему Telegram ID —
    тому, что написан в уведомлении о бане, — найти человека было нельзя
    (жалоба владельца).
    """
    if not is_super_admin(msg_uid(message)):
        await state.clear()
        return

    query = (message.text or "").strip()
    registry, _kind = _find_user(query)
    if registry is None:
        # Показываем ту же подсказку, а не «не понял номер»: формат теперь
        # не один, и человеку надо видеть все варианты.
        await message.answer(
            f"🤷 <b>Не нашёл</b> по запросу <code>{html_escape(query)}</code>\n\n"
            + _query_help()
        )
        return

    user_id = int(registry["user_id"])
    await state.clear()
    text, kb = _user_card(user_id)
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