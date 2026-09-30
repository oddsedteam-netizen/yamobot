"""Раздел «🆔 YID» — внутренний номер и личная статистика админа.

О чём раздел
------------
Показывает, кто пользователь **как админ**: короткий номер Y100+, его личные
показатели по каждому боту, где он админ, и список этих ботов с возможностью
отвязаться. Раздел доступен всем, кто его открыл.

Чего здесь намеренно нет
------------------------
Статистики чужих ботов: сколько у бота обращений, сколько у него юзеров.
Админ не должен видеть цифры по чужим данным — поэтому считается только то,
что сделал сам пользователь (см. ``services.db.yid``).

Из этого раздела доступно завести приветствие админа: бот отправляет его ПЗ
автоматически, когда админ берёт обращение.
"""

import json
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

from handlers._common import (cb_data, cb_uid, html_escape, msg_uid,
                              render_callback)
from services.storage import (
    bot_display_name,
    delete_admin_greeting,
    get_admin_greeting,
    get_bot_by_id_any_owner,
    remove_admin,
    set_admin_greeting,
)
from services.db.yid import admin_bots_of, yid_card

logger = logging.getLogger(__name__)

router = Router()


# Пользователь Y100… кнопки записи не использует, но состояние нужно, чтобы
# отличать «присылаю приветствие» от обычного сообщения в личку.
class GreetingFSM(StatesGroup):
    waiting_text = State()


def _bot_title(bot: dict) -> str:
    """Сырое имя бота — для текста кнопок и всплывашек Telegram.

    Там разметка НЕ парсится, поэтому экранировать нельзя: иначе админ
    увидел бы «&amp;» вместо «&». Для HTML-текстов есть ``_bot_title_html``.
    """
    if not bot:
        return "бот"
    return bot_display_name(bot)


def _bot_title_html(bot: dict) -> str:
    """Имя бота, безопасное для вставки в HTML-разметку.

    Имя задаёт владелец в @BotFather, там бывают «<», «>» и «&». В HTML-режиме
    такой символ ломает разметку целиком, и Telegram отвечает
    «can't parse entities» — экран не показывается вообще.
    """
    return html_escape(_bot_title(bot))


# ═══════════════ Карточка YID ═══════════════════════════════════════════

def _card_payload(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопки карточки YID."""
    card = yid_card(user_id)
    label = f"Y{card['yid']}" if card["yid"] else "—"
    registered = (card["registered_at"] or "—")[:19].replace("T", " ")

    name_line = f"👤 Имя: <b>{html_escape(card['first_name']) or '—'}</b>"
    if card["username"]:
        name_line += f" (@{html_escape(card['username'])})"

    lines = [
        f"🆔 <b>Твой YID: {label}</b>\n",
        name_line,
        f"📅 В системе с: <b>{registered}</b>\n",
        "📊 <b>Твои показатели как админа</b>",
        f"  🤝 Взял обращений: <b>{card['total_pz_taken']}</b>",
        f"  💬 Ответов: <b>{card['total_replies']}</b>",
        f"  📋 Сейчас ведёшь: <b>{card['total_pz_current']}</b>",
    ]

    bots = card["bots"]
    if bots:
        lines.append("\n<b>Боты, где ты админ:</b>")
        for row in bots:
            lines.append(
                f"\n  🤖 <b>{_bot_title_html(row)}</b>"
                + (" 👑" if row.get("is_owner") else "")
                + f"\n     🤝 взял: <b>{row['pz_taken']}</b>"
                + f" · 💬 ответов: <b>{row['replies']}</b>"
                + f" · 📋 ведёшь: <b>{row['pz_current']}</b>"
            )
            since = (row.get("since") or "")[:10]
            if since:
                lines.append(f"     📅 админ с: {since}")
    else:
        lines.append(
            "\nℹ️ <b>Ты пока не админ ни в одном боте.</b>\n"
            "Статистика появится, когда владелец добавит тебя в админы."
        )

    lines.append(
        "\nℹ️ Здесь только твои цифры. Общая статистика ботов тебе не показывается."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Я — админ",
                              callback_data="yid_my_admins", style="primary")],
        [InlineKeyboardButton(text="💬 Моё приветствие",
                              callback_data="yid_greeting_bots", style="primary")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show",
                              style="primary")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data == "yid_card")
async def cb_yid_card(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = _card_payload(cb_uid(callback))
    await render_callback(callback, text, kb)

# ═══════════════ «Я — админ» ═════════════════════════════════════════════

@router.callback_query(F.data == "yid_my_admins")
async def cb_yid_my_admins(callback: CallbackQuery) -> None:
    """Список ботов, где пользователь числится админом, с отвязкой."""
    user_id = cb_uid(callback)
    bots = admin_bots_of(user_id)

    if not bots:
        await render_callback(
            callback,
            "👤 <b>Я — админ</b>\n\n"
            "Ты не числишься админом ни в одном боте.\n\n"
            "Когда владелец добавит тебя в админы, они появятся здесь.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ YID", callback_data="yid_card",
                                      style="primary"),
            ]]),
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    text = ["👤 <b>Я — админ</b>\n",
            f"Ты админ в <b>{len(bots)}</b> ботах.\n"]
    for bot in bots:
        title = _bot_title(bot)
        title_html = _bot_title_html(bot)
        if bot.get("is_owner"):
            text.append(f"🤖 <b>{title_html}</b> 👑 <i>(твой бот)</i>")
            rows.append([InlineKeyboardButton(
                text=f"💬 {title}",
                callback_data=f"yid_greeting_pick_{bot['id']}", style="primary")])
        else:
            text.append(f"🤖 <b>{title_html}</b>")
            rows.append([InlineKeyboardButton(
                text=f"🚫 Отвязать: {title}",
                callback_data=f"yid_unbind_{bot['id']}", style="danger")])

    text.append("\n<i>Отвязаться можно от ботов, которые тебе не принадлежат.</i>")
    rows.append([InlineKeyboardButton(text="⬅️ YID", callback_data="yid_card",
                                      style="primary")])
    await render_callback(callback, "\n".join(text),
                          InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^yid_unbind_\d+$"))
async def cb_yid_unbind(callback: CallbackQuery) -> None:
    """Отвязывает пользователя от чужого бота."""
    user_id = cb_uid(callback)
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])

    bot = get_bot_by_id_any_owner(bot_id)
    if bot is None:
        await callback.answer("❌ Бот не найден", show_alert=True)
        return

    owner_id = int(bot.get("owner_id") or 0)
    if owner_id == user_id:
        await callback.answer("👑 Это твой бот — отвязаться нельзя",
                              show_alert=True)
        return

    removed = remove_admin(owner_id, user_id)
    if not removed:
        # Запись могла остаться легаси (owner_id = 0).
        removed = remove_admin(0, user_id)

    if not removed:
        await callback.answer("Ты и не был админом этого бота", show_alert=True)
    else:
        await callback.answer(f"🚫 Отвязан от {_bot_title(bot)}")

    text, kb = _card_payload(user_id)
    await render_callback(callback, text, kb)


# ═══════════════ «Моё приветствие» ══════════════════════════════════════

def _greeting_bots_payload(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Выбор бота, для которого задаётся приветствие."""
    bots = admin_bots_of(user_id)
    if not bots:
        return (
            "💬 <b>Моё приветствие</b>\n\n"
            "Приветствие можно завести только в ботах, где ты админ.\n"
            "Пока таких ботов нет.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ YID", callback_data="yid_card",
                                      style="primary"),
            ]]),
        )

    lines = [
        "💬 <b>Моё приветствие</b>\n",
        "Выбери бота — бот отправит ПЗ это приветствие, когда ты возьмёшь "
        "обращение в нём.\n",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for bot in bots:
        title = _bot_title(bot)
        current = get_admin_greeting(int(bot["id"]), user_id)
        if current:
            # Показываем, что именно сохранено: раньше фото терялось при
            # сохранении, и админ не видел, что оно пропало.
            marks = ["✅ уже есть"]
            if current.get("photo_id"):
                marks.append("📷 с фото")
            if not str(current.get("text") or "").strip():
                marks.append("без текста")
            mark = ", ".join(marks)
        else:
            mark = "— не задано"
        lines.append(f"🤖 <b>{_bot_title_html(bot)}</b> — {mark}")
        row = [InlineKeyboardButton(
            text=f"🤖 {title}",
            callback_data=f"yid_greeting_pick_{bot['id']}", style="primary",
        )]
        if current:
            row.append(InlineKeyboardButton(
                text="🗑 Убрать", callback_data=f"yid_greeting_del_{bot['id']}",
                style="danger"))
        rows.append(row)

    rows.append([InlineKeyboardButton(text="⬅️ YID", callback_data="yid_card",
                                      style="primary")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "yid_greeting_bots")
async def cb_yid_greeting_bots(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = _greeting_bots_payload(cb_uid(callback))
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^yid_greeting_pick_\d+$"))
async def cb_yid_greeting_pick(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбрали бота — просим прислать само приветствие."""
    user_id = cb_uid(callback)
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    bot = get_bot_by_id_any_owner(bot_id)
    if bot is None:
        await callback.answer("❌ Бот не найден", show_alert=True)
        return

    await state.set_state(GreetingFSM.waiting_text)
    await state.update_data(bot_id=bot_id)

    current = get_admin_greeting(bot_id, user_id)
    hint = "Пришли текст приветствия — можно с фото и премиум-эмодзи."
    if current:
        # Текст экранируем: приветствие пишет сам админ, там бывают «<», «&»
        # и просто длинные строки. Сырой текст в HTML ломал экран целиком:
        # Telegram отвечал «can't parse entities» на edit и на повтор при
        # отправке — кнопка «умирала» без объяснений.
        saved = str(current.get("text") or "").strip()
        hint += "\n\nСейчас сохранено: " + (
            html_escape(saved[:300]) if saved else "(только фото)"
        )

    await render_callback(
        callback,
        f"💬 <b>Приветствие для «{_bot_title_html(bot)}»</b>\n\n{hint}\n\n"
        "📌 Это <b>дополнительное</b> первое сообщение для ПЗ: ты потом "
        "сможешь написать ему своими словами.",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Отмена",
                                  callback_data="yid_greeting_bots",
                                  style="primary"),
        ]]),
    )


@router.message(GreetingFSM.waiting_text)
async def fsm_greeting(message: Message, state: FSMContext) -> None:
    """Сохраняет присланное приветствие админа."""
    user_id = msg_uid(message)
    data = await state.get_data()
    bot_id = int(data.get("bot_id") or 0)
    if not bot_id:
        await state.clear()
        return

    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities
    # Сущности сохраняем как есть: так доезжают премиум-эмодзи и разметка,
    # которые в HTML Telegram вырезает.
    text_entities = json.dumps(
        [e.model_dump() for e in entities] if entities else [],
        ensure_ascii=False,
    )
    # Фото админ мог приложить к тексту или прислать вообще без подписи.
    # Раньше photo_id здесь просто не передавался, поэтому админский
    # автоответ приходил всегда текстом: file_id молча терялся, и при
    # взятии ПЗ фото не уходило (см. _send_admin_greeting).
    photo_id = message.photo[-1].file_id if message.photo else ""
    if not text.strip() and not photo_id:
        logger.warning(
            "Приветствие админа: пустое сообщение (content_type=%s)",
            getattr(message, "content_type", "?"),
        )
        await message.answer(
            "❌ Не увидел ни текста, ни фото.\n\n"
            "Пришли приветствие <b>текстом</b> или <b>фото с подписью</b>."
        )
        return

    set_admin_greeting(bot_id, user_id, text=text.strip(), photo_id=photo_id,
                       text_entities=text_entities)

    bot = get_bot_by_id_any_owner(bot_id)
    await state.clear()
    what = "🖼 Фото с подписью" if photo_id and text.strip() else (
        "🖼 Фото без подписи" if photo_id else "💬 Текст"
    )
    await message.answer(
        f"✅ <b>Приветствие сохранено!</b> ({what})\n\n"
        f"🤖 Бот: <b>{_bot_title_html(bot) if bot else bot_id}</b>\n\n"
        "Теперь оно уйдёт ПЗ автоматически, когда ты возьмёшь обращение."
    )
    text_card, kb = _card_payload(user_id)
    await message.answer(text_card, reply_markup=kb)


@router.callback_query(F.data.regexp(r"^yid_greeting_del_\d+$"))
async def cb_yid_greeting_delete(callback: CallbackQuery) -> None:
    """Убирает сохранённое приветствие."""
    user_id = cb_uid(callback)
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    if delete_admin_greeting(bot_id, user_id):
        await callback.answer("🗑 Приветствие удалено")
    else:
        await callback.answer("Приветствия и не было", show_alert=True)
    text, kb = _greeting_bots_payload(user_id)
    await render_callback(callback, text, kb)
