"""Заглушки и сброс отсчёта в напоминалке.

Зачем этот модуль
----------------
Две реальные жалобы владельцев:

1. «Бот присылает список ПЗ без админа, там есть уже удалённый топик. Напишу
   номер — и пусть не напоминает». Здесь живёт кнопка «🔇 Заглушить»: бот
   нумерует список, владелец выбирает обращение и говорит, заглушить ли его
   навсегда или на сутки.
2. «Накопилось много напоминаний, я выключил и через время включил — бот
   вывалил всё разом». Здесь живёт «♻️ Сбросить отсчёт»: отметки переносятся
   в «сейчас», и все обращения снова начинают отсчёт с полного срока.

ПОЧЕМУ ОТДЕЛЬНЫЙ РОУТЕР (важно при правках)
------------------------------------------
Этот роутер подключается В ``handlers/__init__.py`` ДО ``antiraid_router``,
и это не случайно. В ``handlers/antiraid.py`` есть обработчик с широким
фильтром — «любое сообщение в группе» (``on_admin_chat_message``). По
правилам aiogram обработка останавливается на ПЕРВОМ подошедшем обработчике,
поэтому номер, написанный владельцем в «чате админов», съедался антирейдом и
до этого модуля не доходил. Ровно поэтому существующий сценарий «🗑 Удалить из
списка ПЗ» с вводом номера живёт в ``handlers/start.py`` — он подключён
самым первым. При переносе/переупорядочивании роутеров это ломается тихо:
кнопка работает, а номер не обрабатывается. Поэтому здесь номера-разрешаются
И кнопками, И вводом — даже если ввод перестанет доходить, сценарий останется
рабочим.

Доступ
------
Кнопки висят в «чате админов», где есть и другие админы. Менять настройки
владельца (заглушки, сброс) может только сам владелец или его со-владелец:
в группе владелец определяется через ``get_owner_by_admin_chat`` — тот же
приём, что в ``handlers/start.py``.
"""

import logging
from datetime import datetime, timedelta, timezone

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
    msg_uid,
    render_callback,
    try_edit_answer,
)
from services.reminder_service import current_entries as rs_current_entries
from services.reminder_service import topic_web_link
from services.storage import (
    add_reminder_mute,
    bot_display_name,
    delete_reminder_mute,
    get_bot_by_id,
    get_owner_by_admin_chat,
    get_reminder_mutes,
    get_reminders,
    is_coowner,
    purge_expired_mutes,
    reset_reminder_countdown,
)

logger = logging.getLogger(__name__)

router = Router()

# Московское время — сроки заглушек показываем в нём (как тихие часы).
_MSK = timezone(timedelta(hours=3))


class ReminderMuteFSM(StatesGroup):
    """Ожидание номера обращения для заглушки (кнопка «✍️ Написать номер»)."""
    waiting_number = State()


# ── Вспомогательное ────────────────────────────────────────────────────────

def _owner_of(callback: CallbackQuery) -> int:
    """Владелец, которому принадлежит чат с нажатой кнопкой.

    В личке это сам нажавший (он и есть владелец). В группе — владелец
    привязанного «чата админов»: идентификатор отправителя тут бесполезен,
    ведь в чате сидят все админы, а настройки принадлежат одному владельцу.
    """
    chat = callback.message.chat if callback.message else None
    if chat is not None and getattr(chat, "type", None) != "private":
        owner = get_owner_by_admin_chat(chat.id)
        if owner:
            return int(owner)
    return cb_uid(callback)


def _may_manage(callback: CallbackQuery, owner_id: int) -> bool:
    """Разрешено ли этому человеку менять настройки владельца.

    Отказ отвечаем сразу и коротко: в «чате админов» кнопку может нажать любой
    участник, и молчаливое «ничего не произошло» выглядело бы как баг.
    """
    if cb_uid(callback) == owner_id:
        return True
    if is_coowner(owner_id, cb_uid(callback)):
        return True
    logger.info(
        "Заглушка/сброс отклонены: пользователь %s не владелец и не со-владелец %s",
        cb_uid(callback), owner_id,
    )
    return False


def _deny(callback: CallbackQuery) -> None:
    """Единый ответ на попытку чужого нажать кнопку."""
    try:
        callback.answer("⛔ Это доступно только владельцу", show_alert=True)
    except Exception as e:
        logger.debug("Не удалось ответить на колбэк: %s", e)


def _topic_label(owner_id: int, bot_id: int, topic_id: int, group_chat_id: int) -> str:
    """Человеческое имя топика для подписи в заглушках.

    Если бот или топик уже не нашлись (например, топик удалили после
    заглушки) — показываем технический ключ. Это честнее, чем пустая строка:
    владелец должен видеть, ЧТО именно он снимает заглушку с.
    """
    bot = get_bot_by_id(owner_id, bot_id)
    name = bot_display_name(bot) if bot else f"bot {bot_id}"
    link = topic_web_link(group_chat_id, topic_id)
    return f"{name} — {link}"


def _mute_pick_kb(entries: list[dict]) -> InlineKeyboardMarkup:
    """Клавиатура выбора обращения: кнопки-номера + отмена.

    Номера кодируются прямо в ``callback_data`` (bot_id/topic_id/chat_id), а не
    хранятся в состоянии FSM. Причина: уведомление с кнопками лежит в чате и
    может быть нажато спустя сутки или после перезапуска бота — состояние
    FSM к тому моменту уже не существовало бы, и кнопка работала бы «через раз».
    """
    rows: list[list[InlineKeyboardButton]] = []
    for e in entries:
        rows.append([InlineKeyboardButton(
            text=f"{e['n']}. {e['bot_name']}"[:60],
            callback_data=f"rm_mute:{e['bot_id']}:{e['topic_id']}:{e['group_chat_id']}",
            style="primary",
        )])
    rows.append([InlineKeyboardButton(text="✍️ Написать номер", callback_data="rm_mute_type",
                                      style="primary")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="rm_mute_close",
                                      style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Меню выбора обращения ───────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^rm_mute_menu:\d+$"))
async def cb_mute_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """«🔇 Заглушить» в уведомлении — показываем список для выбора."""
    await state.clear()
    owner_id = _owner_of(callback)
    if not _may_manage(callback, owner_id):
        await _deny(callback)
        return

    rid = int(cb_data(callback).rsplit(":", 1)[-1])
    entries = rs_current_entries(owner_id, rid)
    if not entries:
        text = (
            "🔇 <b>Заглушать нечего</b>\n\n"
            "Сейчас нет обращений, по которым пришло бы напоминание. "
            "Возможно, они уже отработали — тогда ничего делать не нужно."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"rm_mute_menu:{rid}",
                                  style="primary")],
        ])
        await render_callback(callback, text, kb)
        return

    lines = "\n".join(
        f"{e['n']}. {e['bot_name']} — {e['link']}" for e in entries
    )
    # Список кладём в состояние только для варианта «написать номер»: сами
    # кнопки несут id прямо в callback_data и от состояния не зависят.
    await state.update_data(rm_entries=entries)
    text = (
        "🔇 <b>Заглушить обращение</b>\n\n"
        "Выбери номер обращения, которое больше не нужно напоминать.\n"
        "Заглушенный ПЗ не попадёт в уведомления, пока его не снимут "
        "(навсегда) или не пройдут сутки.\n\n"
        f"{lines}"
    )
    await render_callback(callback, text, _mute_pick_kb(entries))


@router.callback_query(F.data.regexp(r"^rm_mute:\d+:-?\d+:-?\d+$"))
async def cb_mute_choose(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбрано обращение — спрашиваем, заглушить ли навсегда."""
    await state.clear()
    owner_id = _owner_of(callback)
    if not _may_manage(callback, owner_id):
        await _deny(callback)
        return

    _, bot_id, topic_id, chat_id = cb_data(callback).split(":")
    label = _topic_label(owner_id, int(bot_id), int(topic_id), int(chat_id))
    text = (
        f"🔇 <b>{label}</b>\n\n"
        "Заглушить это обращение?\n\n"
        "♾️ <b>Навсегда</b> — не напоминать больше никогда, пока не снимешь "
        "вручную в «⏰ Напоминалка» → «🔇 Заглушённые».\n"
        "⏱ <b>На сутки</b> — напоминания вернутся завтра."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="♾️ Навсегда", style="danger",
                                 callback_data=f"rm_mute_f:{bot_id}:{topic_id}:{chat_id}"),
            InlineKeyboardButton(text="⏱ На сутки", style="success",
                                 callback_data=f"rm_mute_d:{bot_id}:{topic_id}:{chat_id}"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="rm_mute_close",
                              style="primary")],
    ])
    await render_callback(callback, text, kb)


def _parse_mute_callback(data: str) -> tuple[int, int, int, bool] | None:
    """Разбирает ``callback_data`` кнопок заглушки.

    Формат: ``rm_mute_f:<bot>:<topic>:<chat>`` — «навсегда»,
    ``rm_mute_d:<bot>:<topic>:<chat>`` — «на сутки».
    Возвращает ``(bot_id, topic_id, group_chat_id, forever)`` либо ``None``,
    если кнопка битая.

    Регресс, ради которого функция выделена: раньше здесь было
    ``forever = ":f:" in data``. После «rm_mute» идёт «_», а не «:», поэтому
    условие было ВСЕГДА ложным — кнопка «♾️ Навсегда» глушила на сутки.
    """
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] not in ("rm_mute_f", "rm_mute_d"):
        return None
    try:
        bot_id, topic_id, group_chat_id = (int(part) for part in parts[1:])
    except ValueError:
        return None
    return bot_id, topic_id, group_chat_id, parts[0] == "rm_mute_f"


@router.callback_query(F.data.regexp(r"^rm_mute_[fd]:\d+:-?\d+:-?\d+$"))
async def cb_mute_apply(callback: CallbackQuery) -> None:
    """Заглушка выбрана — сохраняем."""
    owner_id = _owner_of(callback)
    if not _may_manage(callback, owner_id):
        await _deny(callback)
        return

    parsed = _parse_mute_callback(cb_data(callback))
    if parsed is None:
        await callback.answer("⚠️ Кнопка устарела — открой список заново",
                              show_alert=True)
        return

    bot_id, topic_id, chat_id, forever = parsed
    add_reminder_mute(owner_id, bot_id, topic_id, chat_id, forever)
    label = _topic_label(owner_id, bot_id, topic_id, chat_id)

    logger.info(
        "Владелец %s заглушил %s (%s)", owner_id, label,
        "навсегда" if forever else "на сутки",
    )
    text = (
        f"🔇 <b>Заглушено: {label}</b>\n\n"
        + ("Это обращение больше не попадёт в напоминания — вообще. "
           "Снять заглушку можно в «⏰ Напоминалка» → «🔇 Заглушённые»."
           if forever else
           "По этому обращению напоминаний не будет сутки. "
           "Потом напоминания вернутся сами.")
    )
    await render_callback(callback, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список заглушённых", callback_data="rm_muted",
                              style="primary")],
    ]))


@router.callback_query(F.data == "rm_mute_close")
async def cb_mute_close(callback: CallbackQuery) -> None:
    """Отмена — убираем кнопки с сообщения."""
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    await render_callback(callback, "Отменено.", kb)


# ── Список заглушённых ──────────────────────────────────────────────────────

def _muted_payload(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «🔇 Заглушённые»: список и кнопки снятия."""
    mutes = get_reminder_mutes(owner_id)
    if not mutes:
        text = (
            "🔇 <b>Заглушённые</b>\n\n"
            "Пока пусто — ты не заглушал ни одного обращения.\n\n"
            "Заглушка нужна, когда бот напоминает про ПЗ, которое уже неактуально: "
            "например, топик удалили, а ПЗ больше никто не пишет."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Мои напоминалки", callback_data="reminder_list",
                                  style="primary")],
        ])
        return text, kb

    lines = []
    for m in mutes:
        forever = not m.get("expires_at")
        label = _topic_label(owner_id, int(m["bot_id"]), int(m["topic_id"]),
                             int(m["group_chat_id"]))
        mark = "♾️ навсегда" if forever else f"⏱ до {_msk(m['expires_at'])}"
        lines.append(f"{label}\n    <i>{mark}</i>")

    text = (
        f"🔇 <b>Заглушённые ({len(mutes)})</b>\n\n"
        "По этим обращениям напоминания не приходят. "
        "Сними заглушку, если обращение снова нужно напоминать.\n\n"
        + "\n".join(lines)
    )
    rows: list[list[InlineKeyboardButton]] = []
    for m in mutes:
        rows.append([InlineKeyboardButton(
            text="♾️ Снять заглушку",
            callback_data=f"rm_unmute:{int(m['id'])}",
            style="success",
        )])
    rows.append([
        InlineKeyboardButton(text="🧹 Убрать истёкшие", callback_data="rm_mute_purge",
                             style="primary"),
        InlineKeyboardButton(text="⬅️ Мои напоминалки", callback_data="reminder_list",
                             style="primary"),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _msk(raw: str) -> str:
    """'2026-09-30 12:00:00' (UTC) → '30.09 15:00' (МСК)."""
    try:
        dt = datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(raw or "?")
    return dt.astimezone(_MSK).strftime("%d.%m %H:%M")


@router.callback_query(F.data == "rm_muted")
async def cb_muted_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Экран «🔇 Заглушённые»."""
    await state.clear()
    owner_id = _owner_of(callback)
    if not _may_manage(callback, owner_id):
        await _deny(callback)
        return
    text, kb = _muted_payload(owner_id)
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^rm_unmute:\d+$"))
async def cb_unmute(callback: CallbackQuery) -> None:
    """Снять конкретную заглушку."""
    owner_id = _owner_of(callback)
    if not _may_manage(callback, owner_id):
        await _deny(callback)
        return
    mute_id = int(cb_data(callback).rsplit(":", 1)[-1])
    if delete_reminder_mute(mute_id, owner_id):
        await callback.answer("🔊 Заглушка снята")
    else:
        # Заглушки уже нет: возможно, истекла и была вычищена, либо id чужой.
        # Молча сообщаем честно, без «успеха», которого не было.
        await callback.answer("⚠️ Заглушка уже не найдена", show_alert=True)
    text, kb = _muted_payload(owner_id)
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "rm_mute_purge")
async def cb_mute_purge(callback: CallbackQuery) -> None:
    """Убрать истёкшие заглушки — они и так молчали, но занимают место."""
    owner_id = _owner_of(callback)
    if not _may_manage(callback, owner_id):
        await _deny(callback)
        return
    removed = purge_expired_mutes()
    await callback.answer(f"🧹 Убрано: {removed}")
    text, kb = _muted_payload(owner_id)
    await render_callback(callback, text, kb)


# ── Сброс отсчёта ───────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^rm_reset_ask:\d+$"))
async def cb_reset_ask(callback: CallbackQuery) -> None:
    """«♻️ Сбросить отсчёт» — предупреждение с честным описанием последствий."""
    rid = int(cb_data(callback).rsplit(":", 1)[-1])
    owner_id = _owner_of(callback)
    if not _may_manage(callback, owner_id):
        await _deny(callback)
        return

    reminder = next((r for r in get_reminders(owner_id) if int(r["id"]) == rid), None)
    if not reminder:
        await callback.answer("❌ Напоминалка не найдена", show_alert=True)
        return

    text = (
        "♻️ <b>Сбросить отсчёт?</b>\n\n"
        "Все обращения начнут отсчёт заново — с полного срока этой напоминалки.\n\n"
        "Сейчас ничего не придёт: те обращения, которые уже «просрочены», "
        "помолчат ещё один срок, а потом напомнят по одному, а не все сразу.\n\n"
        "<i>Это полезно, если напоминаний накопилось много: после сброса бот "
        "не вывалит их в чат разом.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сбросить", callback_data=f"rm_reset_yes:{rid}",
                              style="success")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="reminder_list",
                              style="primary")],
    ])
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^rm_reset_yes:\d+$"))
async def cb_reset_yes(callback: CallbackQuery) -> None:
    """Собственно сброс отсчёта."""
    rid = int(cb_data(callback).rsplit(":", 1)[-1])
    owner_id = _owner_of(callback)
    if not _may_manage(callback, owner_id):
        await _deny(callback)
        return

    # Повторная проверка «напоминалка принадлежит этому владельцу»: id приходит
    # из callback_data, доверять ему нельзя — иначе участник «чата админов»
    # сбросил бы отсчёт чужому владельцу.
    if not any(int(r["id"]) == rid for r in get_reminders(owner_id)):
        await callback.answer("❌ Напоминалка не найдена", show_alert=True)
        return

    refreshed, stale = reset_reminder_countdown(owner_id, rid)
    logger.info(
        "Владелец %s сбросил отсчёт напоминалки %s: обновлено %d, убрано мусорных %d",
        owner_id, rid, refreshed, stale,
    )
    text = (
        "♻️ <b>Отсчёт сброшен</b>\n\n"
        f"Обновлено отметок: <b>{refreshed}</b>.\n"
        "Теперь ни одно обращение не придёт мгновенно — все ждут полный срок "
        "заново, и напоминания пойдут по одному.\n\n"
        + (f"🧹 Заодно убрано старых отметок по несуществующим топикам: <b>{stale}</b>."
           if stale else "")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список заглушённых", callback_data="rm_muted",
                              style="primary")],
        [InlineKeyboardButton(text="⬅️ Мои напоминалки", callback_data="reminder_list",
                              style="primary")],
    ])
    await render_callback(callback, text, kb)


# ── Ввод номера вручную ─────────────────────────────────────────────────────
#
# Это ДОПОЛНИТЕЛЬНЫЙ способ выбрать обращение. Основной — кнопки с номерами:
# они не зависят от порядка роутеров. Ввод нужен, когда список длинный.
#
# Роутер подключён ДО antiraid (см. докстринг модуля): иначе номер, написанный
# в «чате админов», перехватит обработчик антирейда с широким фильтром.

@router.callback_query(F.data == "rm_mute_type")
async def cb_mute_type(callback: CallbackQuery, state: FSMContext) -> None:
    """Переход к вводу номера (кнопка «✍️ Написать номер»)."""
    owner_id = _owner_of(callback)
    if not _may_manage(callback, owner_id):
        await _deny(callback)
        return
    entries = (await state.get_data()).get("rm_entries") or []
    if not entries:
        await callback.answer("⚠️ Список устарел — нажми «🔇 Заглушить» заново",
                              show_alert=True)
        return
    await state.set_state(ReminderMuteFSM.waiting_number)
    text = (
        "✍️ <b>Напиши номер обращения</b>\n\n"
        f"В списке {len(entries)} позиций. Напиши номер (1–{len(entries)})."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="rm_mute_close",
                              style="primary")],
    ])
    await try_edit_answer(callback.message, text, reply_markup=kb)


@router.message(ReminderMuteFSM.waiting_number)
async def fsm_mute_number(message: Message, state: FSMContext) -> None:
    """Приняли номер — дальше как при нажатии кнопки с этим номером."""
    entries = (await state.get_data()).get("rm_entries") or []
    if not entries:
        await state.clear()
        await message.answer("⚠️ Список устарел. Нажми «🔇 Заглушить» в уведомлении заново.")
        return

    try:
        n = int((message.text or "").strip())
    except ValueError:
        await message.answer(f"❌ Номер должен быть числом (1–{len(entries)}).")
        return

    if n < 1 or n > len(entries):
        await message.answer(f"❌ Номер вне диапазона (1–{len(entries)}). Попробуй ещё раз.")
        return

    entry = entries[n - 1]
    await state.clear()

    bot_id = int(entry["bot_id"])
    topic_id = int(entry["topic_id"])
    chat_id = int(entry["group_chat_id"])
    label = _topic_label(msg_uid(message), bot_id, topic_id, chat_id)
    text = (
        f"🔇 <b>{label}</b>\n\n"
        "Заглушить это обращение?\n\n"
        "♾️ <b>Навсегда</b> — не напоминать больше никогда.\n"
        "⏱ <b>На сутки</b> — напоминания вернутся завтра."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="♾️ Навсегда", style="danger",
                             callback_data=f"rm_mute_f:{bot_id}:{topic_id}:{chat_id}"),
        InlineKeyboardButton(text="⏱ На сутки", style="success",
                             callback_data=f"rm_mute_d:{bot_id}:{topic_id}:{chat_id}"),
    ]])
    await message.answer(text, reply_markup=kb)