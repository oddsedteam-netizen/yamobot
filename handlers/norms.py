"""Раздел «📊 Норма» в профиле владельца.

Показывает норму на период, кто из админов её набрал, а кто нет, и рейтинг
админов по активности за период. Здесь же настраиваются сама норма, дни
подсчёта и уведомления о недоборе.

Кнопка «📋 ПЗ без админа» открывает тот же список, что и в сводке /стата
(см. handlers/start.py) — чтобы владелец сразу видел, кому можно отдать ПЗ.
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

from handlers._common import render_callback, cb_data, cb_uid, msg_uid
from handlers.start import (_noadmin_payload, _resolve_stats_owner,
                            collect_noadmin_entries)
from services import norms
from services.norms import parse_days_range, period_title
from services.storage import (
    get_norm_period_stats,
    get_norm_settings,
    set_norm_field,
)

logger = logging.getLogger(__name__)

router = Router()

# Сколько админов показывать на одной странице рейтинга.
PAGE_SIZE = 10
MAX_NORM = 100_000


class NormFSM(StatesGroup):
    waiting_value = State()
    waiting_days = State()


# ═══════════════ Экран нормы ═══════════════

def _norm_rows(owner_id: int) -> tuple[dict, list[dict], str]:
    """Настройки, рейтинг за текущий период и его метка."""
    settings = get_norm_settings(owner_id)
    start, end, label = norms.period_bounds(
        start_day=settings["start_day"], end_day=settings["end_day"]
    )
    rows = get_norm_period_stats(owner_id, norms.to_db(start), norms.to_db(end))
    return settings, rows, label


def norm_payload(owner_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура экрана «📊 Норма»."""
    settings, rows, _label = _norm_rows(owner_id)
    norm = settings["norm"]
    reached = sum(1 for r in rows if r["reached"])
    failed = len(rows) - reached if norm else 0

    norm_line = f"<b>{norm}</b> сообщений" if norm else "не задана"
    notify_line = "🔔 включены" if settings["notify_enabled"] else "🔕 выключены"

    total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)

    lines = [
        "📊 <b>Норма админов</b>",
        "",
        f"📅 Период подсчёта: <b>{period_title(settings)}</b>",
        f"🎯 Норма за период: {norm_line}",
        f"Уведомления о недоборе: {notify_line}",
        "",
        f"👥 Всего админов: <b>{len(rows)}</b>",
    ]
    if norm:
        lines.append(f"✅ Набрали норму: <b>{reached}</b>")
        lines.append(f"❌ Не набрали: <b>{failed}</b>")

    lines.append("")
    if not rows:
        lines.append("🏆 Рейтинг появится, когда в боте будут админы.")
    else:
        lines.append(f"🏆 <b>Рейтинг за период</b> (стр. {page + 1}/{total_pages})")
        page_rows = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        offset = page * PAGE_SIZE
        lines.extend(norms.rating_lines(page_rows, offset=offset))

    rows_kb: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✏️ Норма в неделю", callback_data="norm_value",
                              style="primary")],
        [InlineKeyboardButton(text="📅 Первый и последний день подсчёта",
                              callback_data="norm_days", style="primary")],
        [InlineKeyboardButton(
            text="🔕 Выключить уведомления" if settings["notify_enabled"]
            else "🔔 Включить уведомления",
            callback_data="norm_notify",
            style="danger" if settings["notify_enabled"] else "success",
        )],
        [InlineKeyboardButton(text="📋 ПЗ без админа", callback_data="norm_noadmin",
                              style="primary")],
    ]

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"norm_page_{page - 1}",
                                            style="primary"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}",
                                        callback_data="norm_page_current"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"norm_page_{page + 1}",
                                            style="primary"))
        rows_kb.append(nav)

    rows_kb.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="norm",
                                         style="primary"),
                    InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows_kb)


@router.callback_query(F.data == "norm")
async def cb_norm(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = norm_payload(cb_uid(callback), 0)
    await render_callback(callback, text, kb)


@router.callback_query(F.data.startswith("norm_page_"))
async def cb_norm_page(callback: CallbackQuery) -> None:
    raw = cb_data(callback).rsplit("_", 1)[-1]
    if raw == "current":
        await callback.answer()
        return
    try:
        page = int(raw)
    except ValueError:
        await callback.answer()
        return
    text, kb = norm_payload(cb_uid(callback), page)
    await render_callback(callback, text, kb)


# ═══════════════ Настройка нормы ═══════════════

@router.callback_query(F.data == "norm_value")
async def cb_norm_value(callback: CallbackQuery, state: FSMContext) -> None:
    settings = get_norm_settings(cb_uid(callback))
    await state.set_state(NormFSM.waiting_value)
    current = f"<b>{settings['norm']}</b>" if settings["norm"] else "не задана"
    text = (
        "✏️ <b>Норма в неделю</b>\n\n"
        f"Сейчас: {current}\n\n"
        "Напиши, сколько сообщений админ должен набрать за период "
        "(например, <code>500</code>).\n"
        "Отправь <code>0</code>, чтобы выключить норму."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="norm", style="primary")]
    ])
    await render_callback(callback, text, kb)


@router.message(NormFSM.waiting_value)
async def fsm_norm_value(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("❌ Нужно число. Например: <code>500</code> (0 — выключить).")
        return
    if value < 0 or value > MAX_NORM:
        await message.answer(f"❌ Норма должна быть числом от 0 до {MAX_NORM}.")
        return

    user_id = msg_uid(message)
    set_norm_field(user_id, "norm", value)
    await state.clear()

    if value:
        head = f"✅ Норма установлена: <b>{value}</b> сообщений за период."
    else:
        head = "✅ Норма выключена."
    text, kb = norm_payload(user_id, 0)
    await message.answer(f"{head}\n\n{text}", reply_markup=kb)


@router.callback_query(F.data == "norm_days")
async def cb_norm_days(callback: CallbackQuery, state: FSMContext) -> None:
    settings = get_norm_settings(cb_uid(callback))
    await state.set_state(NormFSM.waiting_days)
    text = (
        "📅 <b>Первый и последний день подсчёта</b>\n\n"
        f"Сейчас: <b>{norms.weekday_name(settings['start_day'])}</b> — "
        f"<b>{norms.weekday_name(settings['end_day'])}</b>\n\n"
        "Напиши, с какого по какой день недели считать норму. По умолчанию "
        "с понедельника по пятницу.\n\n"
        "Примеры: <code>пн-пт</code>, <code>с понедельника по пятницу</code>, "
        "<code>1-5</code>, <code>вт сб</code>."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="norm", style="primary")]
    ])
    await render_callback(callback, text, kb)


@router.message(NormFSM.waiting_days)
async def fsm_norm_days(message: Message, state: FSMContext) -> None:
    parsed = parse_days_range(message.text or "")
    if not parsed:
        await message.answer(
            "❌ Не понял дни недели.\n\n"
            "Напиши так: <code>пн-пт</code>, <code>с понедельника по пятницу</code> "
            "или <code>1-5</code> (1 — понедельник, 7 — воскресенье)."
        )
        return

    start_day, end_day = parsed
    user_id = msg_uid(message)
    set_norm_field(user_id, "start_day", start_day)
    set_norm_field(user_id, "end_day", end_day)
    await state.clear()

    text, kb = norm_payload(user_id, 0)
    await message.answer(
        f"✅ Период подсчёта: <b>{norms.weekday_name(start_day)}</b> — "
        f"<b>{norms.weekday_name(end_day)}</b>.\n\n{text}",
        reply_markup=kb,
    )


@router.callback_query(F.data == "norm_notify")
async def cb_norm_notify(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    settings = get_norm_settings(user_id)
    enabled = not bool(settings["notify_enabled"])
    set_norm_field(user_id, "notify_enabled", 1 if enabled else 0)

    await callback.answer("🔔 Уведомления включены" if enabled
                          else "🔕 Уведомления выключены")
    text, kb = norm_payload(user_id, 0)
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "norm_noadmin")
async def cb_norm_noadmin(callback: CallbackQuery, state: FSMContext) -> None:
    """Список ПЗ без админа — тот же, что в сводке /стата."""
    await state.clear()
    chat = callback.message.chat if callback.message else None
    owner_id = _resolve_stats_owner(chat, cb_uid(callback))
    entries = collect_noadmin_entries(owner_id)
    await state.update_data(stats_no_admin=entries)
    text, kb = _noadmin_payload(entries)
    await render_callback(callback, text, kb)

