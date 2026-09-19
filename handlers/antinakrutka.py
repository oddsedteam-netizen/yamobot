"""Антинакрутка ПЗ — защита и её настройки (кнопка «🚨 Антинакрутка» в профиле).

Защита от наплыва фейковых «новых ПЗ»: если за заданное окно времени приходит
слишком много новых ПЗ, бот:

  • запоминает статистику на момент срабатывания и присылает владельцу
    уведомление о возможной накрутке — «засчитывать ли наплыв?»;
  • вторым сообщением спрашивает: «снимаю защиту?»;
  • пока защита активна — <b>не создаёт</b> новые ПЗ и <b>не присылает</b>
    уведомления в «чат админов», а пользователям пишет, что бот находится
    в режиме защиты от спама и сообщения временно не доходят.

Решения владельца:
  • «✅ Сохранить (не накрутка)» / «🚫 Это накрутка» — что делать со статистикой;
  • «✅ Да, снять защиту» — топики и уведомления снова работают;
  • «❌ Нет, оставить защиту» — защита остаётся, снять её можно кнопкой
    «🔄 Сбросить защиту» в этом же экране.

Настройки — по владельцу и действуют на всех его ботов.
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

from handlers._common import (cb_data, cb_uid, msg_uid, render_callback,
                              try_edit, try_edit_answer)
from services.child_manager import (apply_antinakrutka_decision,
                                    lift_antinakrutka,
                                    notify_antinakrutka_release)
from services.storage import (
    get_antinakrutka_settings,
    set_antinakrutka_field,
)

logger = logging.getLogger(__name__)

router = Router()


class AntiNakrutkaFSM(StatesGroup):
    waiting_count = State()
    waiting_window = State()


def antinakrutka_kb(owner_id: int, settings: dict) -> InlineKeyboardMarkup:
    """Клавиатура настроек антинакрутки."""
    enabled = bool(int(settings.get("enabled", 1)))
    toggle = (
        InlineKeyboardButton(text="🔴 Выключить защиту", callback_data="an_disable",
                             style="danger")
        if enabled else
        InlineKeyboardButton(text="🟢 Включить защиту", callback_data="an_enable",
                             style="success")
    )
    rows = [
        [toggle],
        [InlineKeyboardButton(text="🔢 Сколько ПЗ для срабатывания",
                              callback_data="an_count", style="primary")],
        [InlineKeyboardButton(text="⏱ За сколько минут",
                              callback_data="an_window", style="primary")],
    ]
    if settings["triggered"]:
        rows.append([InlineKeyboardButton(text="🔄 Сбросить защиту",
                                          callback_data="an_reset",
                                          style="danger")])
    rows.append([InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def antinakrutka_text(owner_id: int) -> str:
    """Текст экрана настроек антинакрутки."""
    s = get_antinakrutka_settings(owner_id)
    enabled = bool(int(s.get("enabled", 1)))

    if not enabled:
        status = "🔴 <b>защита выключена</b> — бот не следит за наплывом ПЗ"
    elif s["triggered"]:
        status = (
            "🚨 <b>ЗАЩИТА АКТИВНА</b>\n"
            "  • новые ПЗ не создаются;\n"
            "  • уведомления в «чат админов» не приходят;\n"
            "  • пользователям пишется, что бот в режиме защиты от спама;\n"
            "  • снять защиту — кнопкой «🔄 Сбросить защиту» ниже"
        )
    else:
        status = "🟢 следит за новыми ПЗ"
    return (
        "🚨 <b>Антинакрутка ПЗ</b>\n\n"
        "Защита от наплыва фейковых «новых ПЗ»: если за короткое время "
        "приходит слишком много ПЗ, бот запоминает статистику, спрашивает, "
        "засчитывать ли наплыв, и включает режим защиты — новые ПЗ не "
        "создаются, уведомления в «чат админов» не приходят.\n\n"
        "🔌 Включить или выключить защиту можно кнопкой ниже — при выключенной "
        "защите бот просто не следит за наплывом.\n\n"
        "📊 <u>Настройки:</u>\n"
        f"  • Срабатывание: <b>{s['count']}</b> ПЗ за <b>{s['window_minutes']}</b> мин\n"
        f"  • Статус: {status}\n\n"
        "Что настроить?"
    )


# ═══════════════ Включение и выключение защиты ═══════════════

@router.callback_query(F.data == "an_enable")
async def cb_an_enable(callback: CallbackQuery) -> None:
    owner_id = cb_uid(callback)
    set_antinakrutka_field(owner_id, "enabled", 1)
    await callback.answer("🟢 Защита включена")
    await render_callback(callback, antinakrutka_text(owner_id),
                          antinakrutka_kb(owner_id, get_antinakrutka_settings(owner_id)))


@router.callback_query(F.data == "an_disable")
async def cb_an_disable(callback: CallbackQuery) -> None:
    owner_id = cb_uid(callback)
    set_antinakrutka_field(owner_id, "enabled", 0)
    await callback.answer("🔴 Защита выключена")
    await render_callback(callback, antinakrutka_text(owner_id),
                          antinakrutka_kb(owner_id, get_antinakrutka_settings(owner_id)))


# ═══════════════ Открытие настроек из профиля ═══════════════

@router.callback_query(F.data == "antinakrutka")
async def cb_antinakrutka(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    owner_id = cb_uid(callback)
    await render_callback(callback, antinakrutka_text(owner_id),
                          antinakrutka_kb(owner_id, get_antinakrutka_settings(owner_id)))


# ═══════════════ Сколько ПЗ (порог) ═══════════════

@router.callback_query(F.data == "an_count")
async def cb_an_count(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AntiNakrutkaFSM.waiting_count)
    s = get_antinakrutka_settings(cb_uid(callback))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="antinakrutka")]
    ])
    if callback.message:
        await try_edit_answer(
            callback.message,
            "🔢 <b>Сколько ПЗ за окно считать накруткой?</b>\n\n"
            f"Сейчас: <b>{s['count']}</b>\n\n"
            "Напиши число (например, <code>10</code>).",
            reply_markup=kb,
        )
    await callback.answer()


@router.message(AntiNakrutkaFSM.waiting_count)
async def fsm_an_count(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await message.answer("❌ Напиши целое число больше 0.")
        return
    owner_id = msg_uid(message)
    set_antinakrutka_field(owner_id, "count", int(raw))
    await state.clear()
    await message.answer(
        f"✅ Порог обновлён: <b>{int(raw)}</b> ПЗ.",
        reply_markup=antinakrutka_kb(owner_id, get_antinakrutka_settings(owner_id)),
    )


# ═══════════════ Окно времени (минуты) ═══════════════

@router.callback_query(F.data == "an_window")
async def cb_an_window(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AntiNakrutkaFSM.waiting_window)
    s = get_antinakrutka_settings(cb_uid(callback))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="antinakrutka")]
    ])
    if callback.message:
        await try_edit_answer(
            callback.message,
            "⏱ <b>За сколько минут?</b>\n\n"
            f"Сейчас: <b>{s['window_minutes']}</b> мин\n\n"
            "Напиши число минут (например, <code>5</code>).",
            reply_markup=kb,
        )
    await callback.answer()


@router.message(AntiNakrutkaFSM.waiting_window)
async def fsm_an_window(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await message.answer("❌ Напиши целое число минут больше 0.")
        return
    owner_id = msg_uid(message)
    set_antinakrutka_field(owner_id, "window_minutes", int(raw))
    await state.clear()
    await message.answer(
        f"✅ Окно обновлено: <b>{int(raw)}</b> мин.",
        reply_markup=antinakrutka_kb(owner_id, get_antinakrutka_settings(owner_id)),
    )


# ═══════════════ Сброс защиты (кнопка в профиле) ═══════════════

@router.callback_query(F.data == "an_reset")
async def cb_an_reset(callback: CallbackQuery) -> None:
    owner_id = cb_uid(callback)
    await lift_antinakrutka(owner_id)
    text = (
        "✅ <b>Защита снята!</b>\n\n"
        "🛡 Бот снова создаёт топики ПЗ и присылает уведомления о новых ПЗ "
        "в «чат админов».\n"
        "📊 Статистика не тронута (решение по наплыву уже принято)."
    )
    await render_callback(callback, f"{text}\n\n{antinakrutka_text(owner_id)}",
                          antinakrutka_kb(owner_id, get_antinakrutka_settings(owner_id)))


# ═══════════════ Решение владельца по наплыву (из уведомления) ═══════════════

@router.callback_query(F.data.startswith("an_keep_"))
async def cb_an_keep(callback: CallbackQuery) -> None:
    await _apply_decision(callback, keep_stats=True)


@router.callback_query(F.data.startswith("an_drop_"))
async def cb_an_drop(callback: CallbackQuery) -> None:
    await _apply_decision(callback, keep_stats=False)


async def _apply_decision(callback: CallbackQuery, keep_stats: bool) -> None:
    owner_id = int(cb_data(callback).rsplit("_", 1)[-1] or 0)
    if cb_uid(callback) != owner_id:
        await callback.answer("⛔ Это уведомление не для вас.", show_alert=True)
        return

    apply_antinakrutka_decision(owner_id, keep_stats=keep_stats)
    if keep_stats:
        text = (
            "✅ <b>Наплыв ПЗ засчитан.</b>\n\n"
            "📊 Статистика оставлена как есть — наплыв признан реальными "
            "обращениями."
        )
    else:
        text = (
            "🚫 <b>Накрутка учтена.</b>\n\n"
            "📊 Статистика откатана к моменту срабатывания защиты — "
            "накрученные ПЗ в неё не попали."
        )

    if callback.message:
        await try_edit(callback.message, text, reply_markup=None)
    await callback.answer("Готово")

    # Вторым шагом всегда спрашиваем: снимать ли защиту?
    await notify_antinakrutka_release(owner_id)


# ═══════════════ Снятие защиты (после вопроса) ═══════════════

@router.callback_query(F.data.startswith("an_release_yes_"))
async def cb_an_release_yes(callback: CallbackQuery) -> None:
    owner_id = int(cb_data(callback).rsplit("_", 1)[-1] or 0)
    if cb_uid(callback) != owner_id:
        await callback.answer("⛔ Это уведомление не для вас.", show_alert=True)
        return

    await lift_antinakrutka(owner_id)
    if callback.message:
        await try_edit(
            callback.message,
            "✅ <b>Защита снята.</b>\n\n"
            "Бот снова создаёт топики ПЗ и присылает уведомления о новых ПЗ "
            "в «чат админов».",
            reply_markup=None,
        )
    await callback.answer("Защита снята")


@router.callback_query(F.data.startswith("an_release_no_"))
async def cb_an_release_no(callback: CallbackQuery) -> None:
    owner_id = int(cb_data(callback).rsplit("_", 1)[-1] or 0)
    if cb_uid(callback) != owner_id:
        await callback.answer("⛔ Это уведомление не для вас.", show_alert=True)
        return

    if callback.message:
        await try_edit(
            callback.message,
            "🛡 <b>Защита остаётся включённой.</b>\n\n"
            "Новые ПЗ не создаются, уведомления в «чат админов» не приходят.\n\n"
            "Когда захочешь снять — <b>Профиль → 🚨 Антинакрутка → "
            "🔄 Сбросить защиту</b>.",
            reply_markup=None,
        )
    await callback.answer("Защита оставлена")
