"""Раздел профиля «🕐 Время работы».

Если ПЗ пишет боту в нерабочее время, бот сам отвечает ему: «бот работает
с … до …, если кто-то из админов свободен — обязательно напишет». Текст
ответа настраивается (можно приложить фото и премиум-эмодзи), время работы
тоже редактируется. По умолчанию — 09:00–21:00.
"""

import json
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
)

from handlers._common import render_callback, cb_uid, cb_data, msg_uid
from services.storage import (
    DEFAULT_WORK_MESSAGE,
    get_work_hours,
    set_work_hours_enabled,
    set_work_hours_message,
    set_work_hours_time,
)

router = Router()
logger = logging.getLogger(__name__)


class WorkHoursFSM(StatesGroup):
    waiting_start = State()
    waiting_end = State()
    waiting_message = State()


def _preview_message(text: str, start: str, end: str, limit: int = 300) -> str:
    """Показываем текст ответа так, как его увидит ПЗ (со вставленным временем)."""
    raw = text or DEFAULT_WORK_MESSAGE
    filled = raw.replace("{start}", start).replace("{end}", end)
    if len(filled) > limit:
        filled = filled[:limit] + "…"
    return filled.replace("<", "&lt;").replace(">", "&gt;")


def _hours_text(owner_id: int) -> str:
    s = get_work_hours(owner_id)
    enabled = bool(s.get("enabled"))
    start = s.get("start", "09:00")
    end = s.get("end", "21:00")
    status = "🟢 включено" if enabled else "⚪ выключено"
    photo_note = "\n📎 К сообщению прикреплено фото." if s.get("msg_photo") else ""

    return (
        "🕐 <b>Время работы</b>\n\n"
        "Если пользователь напишет боту <b>вне рабочего времени</b>, бот сам "
        "ответит ему, что сейчас не работает, и предупредит: свободный админ "
        "обязательно ответит позже. Само обращение при этом никуда не "
        "теряется — оно попадает в топик как обычно.\n\n"
        f"📊 Сейчас: <b>{status}</b>, работа с <b>{start}</b> до <b>{end}</b>."
        f"{photo_note}\n\n"
        "💬 <b>Что увидит пользователь:</b>\n"
        f"<blockquote>{_preview_message(s.get('msg_text', ''), start, end)}</blockquote>\n\n"
        "🔧 Что можно изменить: включение/выключение, само время работы и текст "
        "ответа (вместе с фото и премиум-эмодзи)."
    )


def _hours_kb(owner_id: int) -> InlineKeyboardMarkup:
    s = get_work_hours(owner_id)
    enabled = bool(s.get("enabled"))
    toggle = InlineKeyboardButton(
        text="🔴 Выключить" if enabled else "🟢 Включить",
        callback_data=f"wh_toggle_{0 if enabled else 1}",
        style="danger" if enabled else "success",
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [toggle],
        [InlineKeyboardButton(
            text=f"🕐 Время работы (сейчас {s.get('start')}–{s.get('end')})",
            callback_data="wh_time", style="primary")],
        [InlineKeyboardButton(text="💬 Текст ответа", callback_data="wh_message", style="primary")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show", style="primary")],
    ])


def _parse_time(raw: str) -> str | None:
    """«9:30» / «09.30» → «09:30»; None, если это не время."""
    raw = (raw or "").strip().replace(".", ":").replace(",", ":")
    if not raw:
        return None
    parts = raw.split(":")
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


@router.callback_query(F.data == "wh_time")
async def cb_wh_time(callback: CallbackQuery, state: FSMContext) -> None:
    """Спрашивает время начала работы."""
    await state.set_state(WorkHoursFSM.waiting_start)
    await render_callback(
        callback,
        "🕐 <b>Когда бот начинает работать?</b>\n\n"
        "Напиши время в формате <code>ЧЧ:ММ</code>, например <code>09:00</code> "
        "или <code>9:30</code>.\n\n"
        "💡 Ночная смена (например 22:00–08:00) тоже работает: бот поймёт, "
        "что время идёт через полночь.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="work_hours", style="primary")]
        ]),
    )
    await callback.answer()


@router.message(WorkHoursFSM.waiting_start)
async def fsm_wh_start(message: Message, state: FSMContext) -> None:
    value = _parse_time(message.text or "")
    if value is None:
        await message.answer("❌ Не похоже на время. Напиши в формате <code>09:00</code>.")
        return

    await state.update_data(wh_start=value)
    await state.set_state(WorkHoursFSM.waiting_end)
    await message.answer(
        "🕐 <b>А когда бот заканчивает работать?</b>\n\n"
        "Так же в формате <code>ЧЧ:ММ</code>.",
    )


@router.message(WorkHoursFSM.waiting_end)
async def fsm_wh_end(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    start = str(data.get("wh_start") or "09:00")
    value = _parse_time(message.text or "")
    if value is None:
        await message.answer("❌ Не похоже на время. Напиши в формате <code>21:00</code>.")
        return

    owner_id = msg_uid(message)
    set_work_hours_time(owner_id, start, value)
    set_work_hours_enabled(owner_id, True)
    await state.clear()
    await message.answer(
        f"✅ Сохранено: бот работает с <b>{start}</b> до <b>{value}</b>, "
        "ограничение включено.",
        reply_markup=_hours_kb(owner_id),
    )


@router.callback_query(F.data == "wh_message")
async def cb_wh_message(callback: CallbackQuery, state: FSMContext) -> None:
    """Редактор текста ответа (с фото и премиум-эмодзи)."""
    owner_id = cb_uid(callback)
    s = get_work_hours(owner_id)
    await state.set_state(WorkHoursFSM.waiting_message)
    await render_callback(
        callback,
        "💬 <b>Текст ответа в нерабочее время</b>\n\n"
        "Пришли новый текст — можно с фото (фото придёт подписью) и "
        "премиум-эмодзи.\n\n"
        "В тексте работают подстановки <code>{start}</code> и <code>{end}</code> — "
        "бот сам подставит твоё время работы.\n\n"
        f"<i>Сейчас: {_preview_message(s.get('msg_text', ''), s.get('start', '09:00'), s.get('end', '21:00'))}</i>",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="work_hours", style="primary")]
        ]),
    )
    await callback.answer()


@router.message(WorkHoursFSM.waiting_message)
async def fsm_wh_message(message: Message, state: FSMContext) -> None:
    owner_id = msg_uid(message)
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("❌ Текст не может быть пустым. Пришли текст или фото с подписью.")
        return

    photo = message.photo[-1].file_id if message.photo else ""
    entities_source = message.caption_entities if message.photo else message.entities
    entities = [e.model_dump(exclude_none=True) for e in (entities_source or [])]
    # text_mention требует вложенный объект user — такие просто пропускаем.
    entities = [e for e in entities if e.get("type") != "text_mention"]

    set_work_hours_message(owner_id, text, photo, json.dumps(entities, ensure_ascii=False))
    await state.clear()

    s = get_work_hours(owner_id)
    await message.answer(
        "✅ Сохранено. Вот что увидит пользователь в нерабочее время:\n\n"
        f"<blockquote>{_preview_message(s.get('msg_text', ''), s.get('start', '09:00'), s.get('end', '21:00'))}</blockquote>"
        + ("\n\n📎 С фото." if s.get("msg_photo") else ""),
        reply_markup=_hours_kb(owner_id),
    )


def build_entities(raw: str) -> list[MessageEntity] | None:
    """Восстанавливает сущности сообщения (премиум-эмодзи) для ответа ПЗ."""
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return None
    result: list[MessageEntity] = []
    for item in data or []:
        try:
            result.append(MessageEntity(**item))
        except Exception:
            continue
    return result or None


@router.callback_query(F.data == "work_hours")
async def cb_work_hours(callback: CallbackQuery, state: FSMContext) -> None:
    """Экран «🕐 Время работы»: объяснение + настройки."""
    await state.clear()
    owner_id = cb_uid(callback)
    await render_callback(callback, _hours_text(owner_id), _hours_kb(owner_id))


@router.callback_query(F.data.regexp(r"^wh_toggle_[01]$"))
async def cb_wh_toggle(callback: CallbackQuery) -> None:
    """Включение/выключение ограничения по времени."""
    owner_id = cb_uid(callback)
    enabled = cb_data(callback).endswith("1")
    set_work_hours_enabled(owner_id, enabled)
    await callback.answer("🟢 Включено" if enabled else "🔴 Выключено")
    await render_callback(callback, _hours_text(owner_id), _hours_kb(owner_id))


