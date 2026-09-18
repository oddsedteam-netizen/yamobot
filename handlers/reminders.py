<<<<<<< HEAD
"""Интерфейс «Напоминалки» в профиле владельца.

Две кнопки:
  • «Авточек ответа админа»  — бот следит, ответил ли админ на ПЗ за заданное время;
  • «Напоминание про ПЗ»     — бот ждёт заданное время, пока ПЗ без админа.

Если «чат админов» не привязан — выдаём инструкцию и кнопку привязки.
"""

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import cb_data, cb_uid, msg_uid, render_callback
from services.reminder_service import (format_duration, parse_quiet_range,
                                       quiet_hours_label)
from services.storage import (
    add_reminder,
    delete_reminder,
    get_bound_chat,
    get_reminders,
    set_reminder_enabled,
    set_reminder_quiet,
)

router = Router()

# ── Режимы ──────────────────────────────────────────────────────────────────

MODE_CHECK_ADMIN = "check_admin"
MODE_NO_ADMIN = "no_admin"

MODE_LABELS = {
    MODE_CHECK_ADMIN: "✅ Авточек ответа админа",
    MODE_NO_ADMIN: "⏳ Напоминание про ПЗ",
}

# Допустимый диапазон длительности: от 30 секунд до 30 дней.
MIN_SECONDS = 30
MAX_SECONDS = 30 * 24 * 60 * 60


class ReminderFSM(StatesGroup):
    waiting_time = State()
    waiting_quiet = State()


REMINDER_MENU_TEXT = (
    "⏰ <b>Напоминалка</b>\n\n"
    "Автоматические напоминания приходят в твой <b>чат админов</b>. "
    "Здесь два режима:\n\n"
    "<b>1) ✅ Авточек ответа админа</b>\n"
    "Бот следит, ответил ли админ на ПЗ. Если ответа нет указанное время — "
    "в чат админов приходит напоминание с тегом админа и ссылкой на топик:\n"
    "  <code>#тег ПЗ без ответа уже (время)! (ссылка на топик)</code>\n"
    "Если админов несколько — приходят отдельные сообщения по каждому тегу "
    "(списком).\n\n"
    "<b>2) ⏳ Напоминание про ПЗ</b>\n"
    "Бот ждёт указанное время, пока ПЗ висит без админа, и напоминает в чате "
    "админов:\n"
    "  <code>Данные ПЗ без админа уже (время)! (ссылки на топики)</code>\n\n"
    "🕒 <b>Настройка времени</b>\n"
    "Тихие часы: в этот интервал (по МСК) напоминания <b>не отправляются</b>, "
    "чтобы не спамить админов, когда они спят.\n"
    "По стандарту: <b>с 21:00 до 09:00</b>.\n\n"
    "Выбери режим ниже 👇"
)


def _mode_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Авточек ответа админа",
                              callback_data="reminder_mode_check_admin", style="primary")],
        [InlineKeyboardButton(text="⏳ Напоминание про ПЗ",
                              callback_data="reminder_mode_no_admin", style="primary")],
        [InlineKeyboardButton(text="🕒 Настройка времени",
                              callback_data="reminder_quiet", style="primary")],
        [InlineKeyboardButton(text="📋 Мои напоминалки", callback_data="reminder_list", style="primary")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")],
    ])


def _bind_required_text() -> str:
    return (
        "⚠️ <b>Чат админов не подключён.</b>\n\n"
        "Для работы напоминалки нужен <b>чат админов</b> — туда будут приходить "
        "напоминания.\n\n"
        "Как подключить:\n"
        "1️⃣ Добавь <b>YamoBot</b> в групповой чат, где состоят админы.\n"
        "2️⃣ Нажми кнопку «🛡 Привязать чат админов» ниже.\n"
        "3️⃣ Дождись подтверждения и вернись в напоминалку."
    )


def _bind_required_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Привязать чат админов", callback_data="bind_admin", style="primary")],
        [InlineKeyboardButton(text="⬅️ Напоминалка", callback_data="reminder_menu", style="primary")],
    ])


# ── Разбор времени ──────────────────────────────────────────────────────────

_UNITS = {
    "сек": 1, "секунд": 1, "секунда": 1, "секунды": 1, "с": 1,
    "мин": 60, "минут": 60, "минута": 60, "минуты": 60, "минуту": 60, "м": 60,
    "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
    "день": 86400, "дня": 86400, "дней": 86400, "дн": 86400, "д": 86400,
    "сутки": 86400, "суток": 86400,
}


def _match_unit(word: str) -> int:
    if word in _UNITS:
        return _UNITS[word]
    for unit in ("минут", "минута", "минуту", "час", "часа", "часов",
                 "день", "дня", "дней", "суток", "сутки", "секунд", "секунда"):
        if word.startswith(unit):
            return _UNITS[unit]
    if word.startswith("д"):
        return 86400
    if word.startswith("ч"):
        return 3600
    if word.startswith("м"):
        return 60
    if word.startswith("с"):
        return 1
    return 0


def parse_duration(raw: str) -> int | None:
    """Разбирает формулировки вида: 5м, 5 минут, 2 часа, 3 дня, 10ч, 1 день.

    Возвращает количество секунд или None, если разобрать не удалось.
    """
    text = (raw or "").strip().lower().replace(".", "")
    parts = re.findall(r"(\d+)\s*([а-яёa-z]+)", text)
    if not parts:
        return None
    total = 0
    for num_str, unit in parts:
        mult = _match_unit(unit)
        if mult <= 0:
            return None
        total += int(num_str) * mult
    if total <= 0:
        return None
    return total
# ── Список напоминалок ─────────────────────────────────────────────────────

def _ask_quiet_text(user_id: int) -> str:
    """Подсказка про формат времени + пример."""
    return (
        "🕒 <b>Тихие часы напоминалок</b>\n\n"
        "Укажи, <b>с какого по какое время</b> не присылать уведомления "
        "из режима напоминалки.\n"
        "<b>Время указывай по МСК!</b>\n\n"
        "✍️ Напиши так: <code>21:00-09:00</code>\n\n"
        "📌 <b>Пример:</b>\n"
        "<code>22:30-08:00</code> — напоминания молчат с 22:30 до 8 утра.\n\n"
        "Понимаю и другие варианты: <code>с 21:00 по 9:00</code>, "
        "<code>2100-0900</code>, <code>21.00 9.00</code>.\n"
        "Можно указать и днём: <code>13:00-15:00</code>."
    )


def _reminders_payload(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    reminders = get_reminders(user_id)
    if not reminders:
        text = (
            "📋 <b>Мои напоминалки</b>\n\n"
            "Пока пусто. Нажми «➕ Добавить напоминалку», чтобы настроить "
            "авточек ответа админа или напоминание про ПЗ."
        )
    else:
        lines = ["📋 <b>Мои напоминалки</b>\n"]
        for r in reminders:
            status = "🟢 вкл" if r.get("enabled") else "⚪ выкл"
            label = MODE_LABELS.get(r.get("mode", ""), r.get("mode", "?"))
            dur = format_duration(r.get("duration_seconds") or 0)
            lines.append(f"{status} | {label} — <b>{dur}</b>")
        lines.append("\nСписок обновляется: пересоздай напоминалку при изменении.")
        text = "\n".join(lines)

    kb_rows: list[list[InlineKeyboardButton]] = []
    for r in reminders:
        rid = r["id"]
        toggle_text = "⏸ Выкл" if r.get("enabled") else "▶️ Вкл"
        kb_rows.append([
            InlineKeyboardButton(text=toggle_text, callback_data=f"reminder_toggle_{rid}", style="primary"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"reminder_del_{rid}", style="danger"),
        ])
    kb_rows.append([InlineKeyboardButton(text="🕒 Настройка времени",
                                         callback_data="reminder_quiet", style="primary")])
    kb_rows.append([InlineKeyboardButton(text="➕ Добавить напоминалку",
                                         callback_data="reminder_menu", style="primary")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


def _ask_time_text(mode: str) -> str:
    return (
        f"⏰ <b>{MODE_LABELS.get(mode, mode)}</b>\n\n"
        "Укажи, через сколько времени должно приходить напоминание.\n\n"
        "Понимаю любые привычные формулировки, например:\n"
        "<code>5м, 5 минут, 30м, 2 часа, 12ч, 3 дня, 1д, 45 сек</code>\n\n"
        "Можно вместе: <code>1 час 30 минут</code>.\n"
        "Диапазон: от 30 секунд до 30 дней."
    )


# ── Обработчики ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "reminder_menu")
async def cb_reminder_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await render_callback(callback, REMINDER_MENU_TEXT, _mode_kb(cb_uid(callback)))


@router.callback_query(F.data == "reminder_list")
async def cb_reminder_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = _reminders_payload(cb_uid(callback))
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^reminder_mode_(check_admin|no_admin)$"))
async def cb_reminder_mode(callback: CallbackQuery, state: FSMContext) -> None:
    data = cb_data(callback)
    # Режим извлекаем по префиксу, а не rsplit: "reminder_mode_check_admin" -> "check_admin"
    mode = data[len("reminder_mode_"):] if data.startswith("reminder_mode_") else ""
    user_id = cb_uid(callback)
    admin_chat = get_bound_chat(user_id, "admin")
    if not admin_chat:
        await render_callback(callback, _bind_required_text(), _bind_required_kb())
        return

    await state.set_state(ReminderFSM.waiting_time)
    await state.update_data(mode=mode)
    await render_callback(
        callback,
        _ask_time_text(mode),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="reminder_menu", style="primary")]
        ]),
    )


@router.message(ReminderFSM.waiting_time)
async def fsm_reminder_time(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    mode = data.get("mode")
    if mode not in MODE_LABELS:
        # Не должно случаться, но на всякий случай даём понять, что пошло не так.
        await state.clear()
        await message.answer(
            "⚠️ Что-то пошло не так: режим напоминалки не выбран.\n"
            "Открой заново «⏰ Напоминалку» и выбери режим.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏰ Напоминалка", callback_data="reminder_menu", style="primary")]
            ]),
        )
        return

    seconds = parse_duration(message.text or "")
    if seconds is None or seconds < MIN_SECONDS or seconds > MAX_SECONDS:
        await message.answer(
            "❌ Не понял время. Напиши в формате:\n"
            "<code>5м · 5 минут · 30м · 2 часа · 12ч · 3 дня · 1д · 45 сек</code>\n\n"
            "Диапазон: от 30 секунд до 30 дней."
        )
        return

    await state.clear()
    add_reminder(msg_uid(message), mode, seconds)

    label = MODE_LABELS[mode]
    await message.answer(
        f"✅ <b>Напоминалка создана!</b>\n\n"
        f"{label}\n"
        f"Напоминание будет приходить в чат админов каждые "
        f"<b>{format_duration(seconds)}</b>, пока условие держится."
    )
    text, kb = _reminders_payload(msg_uid(message))
    await message.answer(text, reply_markup=kb)


# ── Тихие часы: экран настройки ────────────────────────────────────────────

def _quiet_payload(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура экрана «Настройка времени»."""
    from services.storage import get_reminder_quiet

    s = get_reminder_quiet(user_id)
    status = quiet_hours_label(user_id)
    toggle_text = ("🔔 Включить тихие часы" if not s["enabled"]
                   else "🔕 Выключить тихие часы")
    text = (
        "🕒 <b>Настройка времени напоминалок</b>\n\n"
        "В тихие часы напоминания <b>не приходят</b> в «чат админов» — "
        "чтобы не спамить, когда админы спят.\n\n"
        f"📊 Сейчас: <b>{status}</b>\n"
        f"  • с: <code>{s['from_time']}</code>\n"
        f"  • по: <code>{s['to_time']}</code>\n"
        "  • время по <b>МСК</b>\n\n"
        f"Пример формата: <code>{s['from_time']}-{s['to_time']}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить время", callback_data="reminder_quiet_edit",
                              style="primary")],
        [InlineKeyboardButton(text=toggle_text, callback_data="reminder_quiet_toggle",
                              style=("danger" if s["enabled"] else "success"))],
        [InlineKeyboardButton(text="⬅️ Напоминалка", callback_data="reminder_menu", style="primary")],
    ])
    return text, kb


@router.callback_query(F.data == "reminder_quiet")
async def cb_reminder_quiet(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = _quiet_payload(cb_uid(callback))
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "reminder_quiet_edit")
async def cb_reminder_quiet_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ReminderFSM.waiting_quiet)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="reminder_quiet", style="primary")]
    ])
    await render_callback(callback, _ask_quiet_text(cb_uid(callback)), kb)


@router.callback_query(F.data == "reminder_quiet_toggle")
async def cb_reminder_quiet_toggle(callback: CallbackQuery) -> None:
    from services.storage import get_reminder_quiet

    user_id = cb_uid(callback)
    current = get_reminder_quiet(user_id)
    set_reminder_quiet(user_id, enabled=not bool(current["enabled"]))
    await callback.answer("🔔 Тихие часы выключены" if current["enabled"]
                          else "🔕 Тихие часы включены")
    text, kb = _quiet_payload(user_id)
    await render_callback(callback, text, kb)


@router.message(ReminderFSM.waiting_quiet)
async def fsm_reminder_quiet(message: Message, state: FSMContext) -> None:
    parsed = parse_quiet_range(message.text or "")
    if parsed is None:
        await message.answer(
            "❌ Не понял интервал. Напиши в формате <code>21:00-09:00</code> "
            "(время по МСК).\n\n"
            "📌 Пример: <code>22:30-08:00</code>"
        )
        return

    from_time, to_time = parsed
    if from_time == to_time:
        await message.answer(
            "❌ Начало и конец совпадают — так тихие часы работать не будут.\n"
            "Напиши, например: <code>21:00-09:00</code>"
        )
        return

    user_id = msg_uid(message)
    await state.clear()
    set_reminder_quiet(user_id, from_time=from_time, to_time=to_time, enabled=True)

    await message.answer(
        "✅ <b>Тихие часы обновлены!</b>\n\n"
        f"🔕 С <b>{from_time}</b> до <b>{to_time}</b> (МСК) напоминания "
        "не будут приходить в «чат админов».\n"
        "⏰ В остальное время напоминания работают как обычно."
    )
    text, kb = _quiet_payload(user_id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.regexp(r"^reminder_toggle_\d+$"))
async def cb_reminder_toggle(callback: CallbackQuery) -> None:
    rid = int(cb_data(callback).rsplit("_", 1)[-1])
    reminders = get_reminders(cb_uid(callback))
    target = next((r for r in reminders if r["id"] == rid), None)
    if target is None:
        await callback.answer("❌ Напоминалка не найдена", show_alert=True)
        return
    new_state = not target.get("enabled")
    set_reminder_enabled(rid, new_state)
    await callback.answer("✅ Включено" if new_state else "⏸ Выключено")
    text, kb = _reminders_payload(cb_uid(callback))
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^reminder_del_\d+$"))
async def cb_reminder_del(callback: CallbackQuery) -> None:
    rid = int(cb_data(callback).rsplit("_", 1)[-1])
    delete_reminder(rid)
    await callback.answer("🗑 Удалено")
    text, kb = _reminders_payload(cb_uid(callback))
=======
"""Интерфейс «Напоминалки» в профиле владельца.

Две кнопки:
  • «Авточек ответа админа»  — бот следит, ответил ли админ на ПЗ за заданное время;
  • «Напоминание про ПЗ»     — бот ждёт заданное время, пока ПЗ без админа.

Если «чат админов» не привязан — выдаём инструкцию и кнопку привязки.
"""

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import cb_data, cb_uid, msg_uid, render_callback
from services.reminder_service import format_duration
from services.storage import (
    add_reminder,
    delete_reminder,
    get_bound_chat,
    get_reminders,
    set_reminder_enabled,
)

router = Router()

# ── Режимы ──────────────────────────────────────────────────────────────────

MODE_CHECK_ADMIN = "check_admin"
MODE_NO_ADMIN = "no_admin"

MODE_LABELS = {
    MODE_CHECK_ADMIN: "✅ Авточек ответа админа",
    MODE_NO_ADMIN: "⏳ Напоминание про ПЗ",
}

# Допустимый диапазон длительности: от 30 секунд до 30 дней.
MIN_SECONDS = 30
MAX_SECONDS = 30 * 24 * 60 * 60


class ReminderFSM(StatesGroup):
    waiting_time = State()


REMINDER_MENU_TEXT = (
    "⏰ <b>Напоминалка</b>\n\n"
    "Автоматические напоминания приходят в твой <b>чат админов</b>. "
    "Здесь два режима:\n\n"
    "<b>1) ✅ Авточек ответа админа</b>\n"
    "Бот следит, ответил ли админ на ПЗ. Если ответа нет указанное время — "
    "в чат админов приходит напоминание с тегом админа и ссылкой на топик:\n"
    "  <code>#тег ПЗ без ответа уже (время)! (ссылка на топик)</code>\n"
    "Если админов несколько — приходят отдельные сообщения по каждому тегу "
    "(списком).\n\n"
    "<b>2) ⏳ Напоминание про ПЗ</b>\n"
    "Бот ждёт указанное время, пока ПЗ висит без админа, и напоминает в чате "
    "админов:\n"
    "  <code>Данные ПЗ без админа уже (время)! (ссылки на топики)</code>\n\n"
    "Выбери режим ниже 👇"
)


def _mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Авточек ответа админа",
                              callback_data="reminder_mode_check_admin")],
        [InlineKeyboardButton(text="⏳ Напоминание про ПЗ",
                              callback_data="reminder_mode_no_admin")],
        [InlineKeyboardButton(text="📋 Мои напоминалки", callback_data="reminder_list")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")],
    ])


def _bind_required_text() -> str:
    return (
        "⚠️ <b>Чат админов не подключён.</b>\n\n"
        "Для работы напоминалки нужен <b>чат админов</b> — туда будут приходить "
        "напоминания.\n\n"
        "Как подключить:\n"
        "1️⃣ Добавь <b>YamoBot</b> в групповой чат, где состоят админы.\n"
        "2️⃣ Нажми кнопку «🛡 Привязать чат админов» ниже.\n"
        "3️⃣ Дождись подтверждения и вернись в напоминалку."
    )


def _bind_required_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Привязать чат админов", callback_data="bind_admin")],
        [InlineKeyboardButton(text="⬅️ Напоминалка", callback_data="reminder_menu")],
    ])


# ── Разбор времени ──────────────────────────────────────────────────────────

_UNITS = {
    "сек": 1, "секунд": 1, "секунда": 1, "секунды": 1, "с": 1,
    "мин": 60, "минут": 60, "минута": 60, "минуты": 60, "минуту": 60, "м": 60,
    "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
    "день": 86400, "дня": 86400, "дней": 86400, "дн": 86400, "д": 86400,
    "сутки": 86400, "суток": 86400,
}


def _match_unit(word: str) -> int:
    if word in _UNITS:
        return _UNITS[word]
    for unit in ("минут", "минута", "минуту", "час", "часа", "часов",
                 "день", "дня", "дней", "суток", "сутки", "секунд", "секунда"):
        if word.startswith(unit):
            return _UNITS[unit]
    if word.startswith("д"):
        return 86400
    if word.startswith("ч"):
        return 3600
    if word.startswith("м"):
        return 60
    if word.startswith("с"):
        return 1
    return 0


def parse_duration(raw: str) -> int | None:
    """Разбирает формулировки вида: 5м, 5 минут, 2 часа, 3 дня, 10ч, 1 день.

    Возвращает количество секунд или None, если разобрать не удалось.
    """
    text = (raw or "").strip().lower().replace(".", "")
    parts = re.findall(r"(\d+)\s*([а-яёa-z]+)", text)
    if not parts:
        return None
    total = 0
    for num_str, unit in parts:
        mult = _match_unit(unit)
        if mult <= 0:
            return None
        total += int(num_str) * mult
    if total <= 0:
        return None
    return total
# ── Список напоминалок ─────────────────────────────────────────────────────

def _reminders_payload(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    reminders = get_reminders(user_id)
    if not reminders:
        text = (
            "📋 <b>Мои напоминалки</b>\n\n"
            "Пока пусто. Нажми «➕ Добавить напоминалку», чтобы настроить "
            "авточек ответа админа или напоминание про ПЗ."
        )
    else:
        lines = ["📋 <b>Мои напоминалки</b>\n"]
        for r in reminders:
            status = "🟢 вкл" if r.get("enabled") else "⚪ выкл"
            label = MODE_LABELS.get(r.get("mode", ""), r.get("mode", "?"))
            dur = format_duration(r.get("duration_seconds") or 0)
            lines.append(f"{status} | {label} — <b>{dur}</b>")
        lines.append("\nСписок обновляется: пересоздай напоминалку при изменении.")
        text = "\n".join(lines)

    kb_rows: list[list[InlineKeyboardButton]] = []
    for r in reminders:
        rid = r["id"]
        toggle_text = "⏸ Выкл" if r.get("enabled") else "▶️ Вкл"
        kb_rows.append([
            InlineKeyboardButton(text=toggle_text, callback_data=f"reminder_toggle_{rid}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"reminder_del_{rid}"),
        ])
    kb_rows.append([InlineKeyboardButton(text="➕ Добавить напоминалку",
                                         callback_data="reminder_menu")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


def _ask_time_text(mode: str) -> str:
    return (
        f"⏰ <b>{MODE_LABELS.get(mode, mode)}</b>\n\n"
        "Укажи, через сколько времени должно приходить напоминание.\n\n"
        "Понимаю любые привычные формулировки, например:\n"
        "<code>5м, 5 минут, 30м, 2 часа, 12ч, 3 дня, 1д, 45 сек</code>\n\n"
        "Можно вместе: <code>1 час 30 минут</code>.\n"
        "Диапазон: от 30 секунд до 30 дней."
    )


# ── Обработчики ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "reminder_menu")
async def cb_reminder_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await render_callback(callback, REMINDER_MENU_TEXT, _mode_kb())


@router.callback_query(F.data == "reminder_list")
async def cb_reminder_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = _reminders_payload(cb_uid(callback))
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^reminder_mode_(check_admin|no_admin)$"))
async def cb_reminder_mode(callback: CallbackQuery, state: FSMContext) -> None:
    data = cb_data(callback)
    # Режим извлекаем по префиксу, а не rsplit: "reminder_mode_check_admin" -> "check_admin"
    mode = data[len("reminder_mode_"):] if data.startswith("reminder_mode_") else ""
    user_id = cb_uid(callback)
    admin_chat = get_bound_chat(user_id, "admin")
    if not admin_chat:
        await render_callback(callback, _bind_required_text(), _bind_required_kb())
        return

    await state.set_state(ReminderFSM.waiting_time)
    await state.update_data(mode=mode)
    await render_callback(
        callback,
        _ask_time_text(mode),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="reminder_menu")]
        ]),
    )


@router.message(ReminderFSM.waiting_time)
async def fsm_reminder_time(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    mode = data.get("mode")
    if mode not in MODE_LABELS:
        # Не должно случаться, но на всякий случай даём понять, что пошло не так.
        await state.clear()
        await message.answer(
            "⚠️ Что-то пошло не так: режим напоминалки не выбран.\n"
            "Открой заново «⏰ Напоминалку» и выбери режим.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏰ Напоминалка", callback_data="reminder_menu")]
            ]),
        )
        return

    seconds = parse_duration(message.text or "")
    if seconds is None or seconds < MIN_SECONDS or seconds > MAX_SECONDS:
        await message.answer(
            "❌ Не понял время. Напиши в формате:\n"
            "<code>5м · 5 минут · 30м · 2 часа · 12ч · 3 дня · 1д · 45 сек</code>\n\n"
            "Диапазон: от 30 секунд до 30 дней."
        )
        return

    await state.clear()
    add_reminder(msg_uid(message), mode, seconds)

    label = MODE_LABELS[mode]
    await message.answer(
        f"✅ <b>Напоминалка создана!</b>\n\n"
        f"{label}\n"
        f"Напоминание будет приходить в чат админов каждые "
        f"<b>{format_duration(seconds)}</b>, пока условие держится."
    )
    text, kb = _reminders_payload(msg_uid(message))
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.regexp(r"^reminder_toggle_\d+$"))
async def cb_reminder_toggle(callback: CallbackQuery) -> None:
    rid = int(cb_data(callback).rsplit("_", 1)[-1])
    reminders = get_reminders(cb_uid(callback))
    target = next((r for r in reminders if r["id"] == rid), None)
    if target is None:
        await callback.answer("❌ Напоминалка не найдена", show_alert=True)
        return
    new_state = not target.get("enabled")
    set_reminder_enabled(rid, new_state)
    await callback.answer("✅ Включено" if new_state else "⏸ Выключено")
    text, kb = _reminders_payload(cb_uid(callback))
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^reminder_del_\d+$"))
async def cb_reminder_del(callback: CallbackQuery) -> None:
    rid = int(cb_data(callback).rsplit("_", 1)[-1])
    delete_reminder(rid)
    await callback.answer("🗑 Удалено")
    text, kb = _reminders_payload(cb_uid(callback))
>>>>>>> a26fa0ca2db5328dc4044ff97611847506600e74
    await render_callback(callback, text, kb)