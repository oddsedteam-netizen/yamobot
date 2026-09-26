"""Тикеты поддержки (бывшие жалобы).

Пользователь сам создаёт тикет: выбирает категорию, описывает вопрос, может
приложить фото и (для тех. вопроса) логи. Тикет прилетает владельцу платформы
в личку одной карточкой, где сразу видны категория, ID отправителя, его
логи и текст. Ответить и закрыть тикет можно прямо оттуда.
"""

import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    MaybeInaccessibleMessage,
    MediaUnion,
    Message,
)

from handlers._common import (cb_data, cb_uid, event_bot, msg_firstname,
                              msg_uid, msg_username, render_callback)
from services.config import is_super_admin
from services.storage import (
    TICKET_CATEGORIES,
    TICKET_CATEGORY_TITLES,
    close_ticket,
    create_ticket,
    format_bot_errors,
    get_all_bots_flat,
    get_all_tickets,
    get_owner_bot_errors,
    get_ticket,
    get_user_bots,
    get_user_tickets,
    ticket_counts,
    ticket_photos,
)

logger = logging.getLogger(__name__)

router = Router()

MAX_TICKET_PHOTOS = 5


class TicketFSM(StatesGroup):
    category = State()   # выбирает категорию (или пишет свою)
    text = State()       # пишет текст обращения
    photos = State()     # прикладывает фото
    logs = State()       # решает, прикладывать ли логи
    answer = State()     # владелец пишет ответ


def _html(text: str) -> str:
    return escape(text or "", quote=False)


def _msg(target: MaybeInaccessibleMessage | None) -> Message | None:
    """Сообщение, с которым можно работать.

    ``callback.message`` — это ``MaybeInaccessibleMessage | None``: сообщения
    могло не быть (inline-кнопка без сообщения) или оно уже недоступно
    (``InaccessibleMessage`` — удалено/старое). В обоих случаях с объектом
    нельзя вызвать ``answer``/``edit_text``, поэтому возвращаем None.
    """
    return target if isinstance(target, Message) else None


async def _answer(source: CallbackQuery | Message,
                  target: MaybeInaccessibleMessage | None, text: str,
                  reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Отвечает на сообщение с фолбэком.

    Если сообщение недоступно (``InaccessibleMessage``), отправляем ботом
    напрямую в тот же чат — пользователь всё равно должен получить ответ.
    """
    msg = _msg(target)
    if msg is not None:
        await msg.answer(text, reply_markup=reply_markup)
        return
    chat_id = getattr(getattr(target, "chat", None), "id", None)
    if chat_id is None:
        logger.info("Некуда ответить: сообщение недоступно")
        return
    try:
        await event_bot(source).send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        logger.warning("Не удалось ответить в чат %s: %s", chat_id, e)


def _platform_owner() -> int | None:
    for row in get_all_bots_flat():
        if is_super_admin(int(row.get("owner_id") or 0)):
            return int(row["owner_id"])
    return None


def _ticket_line(ticket: dict) -> str:
    title = TICKET_CATEGORY_TITLES.get(ticket.get("category") or "other", "📦 Другое")
    who = ticket.get("username") or f"ID:{ticket.get('user_id')}"
    text = " ".join(str(ticket.get("text") or "").split())[:40]
    icon = "⚪" if ticket.get("status") == "closed" else "🟢"
    return f"{icon} #{ticket['id']} · {title} · {who} — {text or '—'}"


def tickets_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать тикет", callback_data="tk_new",
                              style="success")],
        [
            InlineKeyboardButton(text="🟢 Открытые", callback_data="tk_mine_open",
                                 style="primary"),
            InlineKeyboardButton(text="⚪ Закрытые", callback_data="tk_mine_closed",
                                 style="primary"),
        ],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")],
    ])


@router.callback_query(F.data == "tickets")
async def cb_tickets_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Меню тикетов пользователя."""
    await state.clear()
    owner_id = cb_uid(callback)
    text = (
        "🎫 <b>Тикеты</b>\n\n"
        "Обращения в поддержку: можно создать тикет и посмотреть свои "
        "открытые и закрытые обращения.\n\n"
        f"🟢 Открытых: <b>{len(get_user_tickets(owner_id, 'open'))}</b>\n"
        f"⚪ Закрытых: <b>{len(get_user_tickets(owner_id, 'closed'))}</b>"
    )
    await render_callback(callback, text, tickets_menu_kb())


@router.callback_query(F.data.startswith("tk_mine_"))
async def cb_tickets_mine(callback: CallbackQuery) -> None:
    """Открытые или закрытые тикеты пользователя."""
    status = "closed" if cb_data(callback).endswith("closed") else "open"
    items = get_user_tickets(cb_uid(callback), status)

    title = "⚪ <b>Закрытые тикеты</b>" if status == "closed" else "🟢 <b>Открытые тикеты</b>"
    text = f"{title}\n\n" + ("\n".join(_ticket_line(t) for t in items) or "Пока пусто.")
    await render_callback(callback, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К тикетам", callback_data="tickets", style="primary")]
    ]))


# ═══════════════ Создание тикета ═══════════════

def _category_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"tk_cat_{key}", style="primary")]
        for key, title in TICKET_CATEGORIES
    ]
    rows.append([InlineKeyboardButton(text="⬅️ К тикетам", callback_data="tickets",
                                      style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "tk_new")
async def cb_ticket_new(callback: CallbackQuery, state: FSMContext) -> None:
    """Создание тикета: спрашиваем категорию."""
    await state.set_state(TicketFSM.category)
    # Запоминаем, кто создаёт тикет: ID берём из нажавшего кнопку (пользователя),
    # а не из сообщения бота с этой кнопкой — там from_user был бы сам бот.
    await state.update_data(tk_user_id=cb_uid(callback))
    await render_callback(callback, "🎫 <b>Новый тикет</b>\n\nВыбери категорию:",
                          _category_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("tk_cat_"))
async def cb_ticket_category(callback: CallbackQuery, state: FSMContext) -> None:
    """Категория выбрана (для «Другое» спрашиваем свою)."""
    key = cb_data(callback).split("_")[-1]

    if key == "other":
        await state.set_state(TicketFSM.category)
        await _answer(
            callback, callback.message,
            "✏️ <b>Напиши категорию</b>\n\nОдним словом или короткой фразой."
        )
        return

    title = TICKET_CATEGORY_TITLES.get(key, key)
    await state.update_data(tk_category=key, tk_category_title=title)
    await state.set_state(TicketFSM.text)
    await render_callback(
        callback,
        f"🎫 <b>Категория: {title}</b>\n\n"
        "✏️ Опиши проблему или вопрос подробно: что случилось и что "
        "ожидалось.\n\nПиши текст следующим сообщением 👇",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="tickets", style="primary")]
        ]),
    )
    await callback.answer()


@router.message(TicketFSM.category)
async def fsm_ticket_category(message: Message, state: FSMContext) -> None:
    """Своя категория для варианта «Другое»."""
    title = (message.text or "").strip()[:32]
    if not title:
        await message.answer("❌ Напиши категорию одним словом.")
        return

    await state.update_data(tk_category="other", tk_category_title=title or "Другое")
    await state.set_state(TicketFSM.text)
    await message.answer(
        f"🎫 <b>Категория: {title}</b>\n\n✏️ Опиши вопрос подробно 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="tickets", style="primary")]
        ]),
    )


@router.message(TicketFSM.text)
async def fsm_ticket_text(message: Message, state: FSMContext) -> None:
    """Текст обращения получен — спрашиваем фото."""
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Текст пустой. Напиши его ещё раз.")
        return

    await state.update_data(tk_user_id=msg_uid(message), tk_username=msg_username(message),
                            tk_first_name=msg_firstname(message),
                            tk_text=text, tk_photos=[])
    await state.set_state(TicketFSM.photos)
    await message.answer(
        "📎 <b>Прикрепи фото</b> (по желанию)\n\n"
        f"Можно прислать до {MAX_TICKET_PHOTOS} фото. Отправь «—» или нажми "
        "кнопку, если фото не нужны.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📎 Без фото", callback_data="tk_nophotos",
                                  style="success")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="tickets", style="primary")],
        ]),
    )


# Фото, отправленные без подписи, ждут своей очереди: основной бот должен
# переслать их владельцу. Файл берём по file_id главного бота.
_pending_photos: dict[int, list[str]] = {}


async def _ask_logs(state: FSMContext, source: CallbackQuery | Message,
                    target: MaybeInaccessibleMessage | None) -> None:
    """Спрашиваем, прикладывать ли логи (только для тех. вопроса)."""
    await state.set_state(TicketFSM.logs)
    await _answer(
        source, target,
        "🗂 <b>Приложить логи?</b>\n\n"
        "Логи — это ошибки, которые происходили при работе твоих ботов. "
        "Они помогут разобраться быстрее.\n\n"
        "Отправлять можно не чаще раза в 5 минут.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, приложить", callback_data="tk_logs_yes",
                                     style="success"),
                InlineKeyboardButton(text="❌ Нет", callback_data="tk_logs_no",
                                     style="primary"),
            ]
        ]),
    )


@router.callback_query(F.data == "tk_nophotos")
async def cb_ticket_no_photos(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь закончил прикреплять фото (или пропустил шаг)."""
    data = await state.get_data()
    # ВАЖНО: список фото НЕ затираем — иначе прикреплённые фото терялись бы
    # при нажатии «Готово». ID отправителя тоже не берём из сообщения бота.
    if not data.get("tk_user_id"):
        await state.update_data(tk_user_id=cb_uid(callback))
    _pending_photos.pop(cb_uid(callback), None)

    if (data.get("tk_category") or "") == "tech":
        await _ask_logs(state, callback, callback.message)
    else:
        await _create_ticket_and_notify(callback, state, callback.message)
    await callback.answer()


@router.message(TicketFSM.photos)
async def fsm_ticket_photos(message: Message, state: FSMContext) -> None:
    """Собираем фото тикета."""
    photos: list[str] = list((await state.get_data()).get("tk_photos") or [])

    if message.photo:
        if len(photos) >= MAX_TICKET_PHOTOS:
            await message.answer(f"⚠️ Больше {MAX_TICKET_PHOTOS} фото не прикрепить.")
            return
        photos.append(message.photo[-1].file_id)
        await state.update_data(tk_photos=photos)
        await message.answer(
            f"📎 Фото {len(photos)}/{MAX_TICKET_PHOTOS}. Добавить ещё или закончить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Готово", callback_data="tk_nophotos",
                                      style="success")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="tickets",
                                      style="primary")],
            ]),
        )
        return

    if (message.text or "").strip() in ("—", "-", "нет", "без фото", "пропустить"):
        await state.update_data(tk_photos=photos)
        if ((await state.get_data()).get("tk_category") or "") == "tech":
            await _ask_logs(state, message, message)
        else:
            await _create_ticket_and_notify(message, state, message)
        return

    # Обычный текст — считаем его продолжением описания.
    data = await state.get_data()
    await state.update_data(tk_text=f"{data.get('tk_text', '')}\n{message.text}".strip())
    await message.answer("➕ Дописал в текст тикета. Прикрепи фото или нажми «Готово».",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="✅ Готово", callback_data="tk_nophotos",
                                                   style="success")]
                         ]))


# ═══════════════ Создание тикета и отправка владельцу ═══════════════

async def _create_ticket_and_notify(source: CallbackQuery | Message,
                                    state: FSMContext,
                                    target: MaybeInaccessibleMessage | None) -> None:
    """Создаёт тикет и отправляет его владельцу платформы в личку.

    ID отправителя берём из состояния, а не из ``target``: в этот момент
    ``target`` — это сообщение самого бота (например, карточка в ЛС
    владельца), и ID оттуда указывал бы на бота, а не на пользователя.
    """
    data = await state.get_data()
    user_id = int(data.get("tk_user_id") or 0)
    if not user_id:
        logger.info("Тикет без ID отправителя — пропускаю")
        return
    bot = event_bot(source)
    message = _msg(target)

    title = data.get("tk_category_title") or TICKET_CATEGORY_TITLES.get(
        data.get("tk_category") or "other", "Другое")

    # Имя и username берём из состояния (настоящий пользователь), а из
    # сообщения — только как запасной вариант.
    username = data.get("tk_username") or (
        msg_username(message) if message is not None else "")
    first_name = data.get("tk_first_name") or (
        msg_firstname(message) if message is not None else "")

    ticket_id = create_ticket(
        user_id,
        data.get("tk_category") or "other",
        data.get("tk_text") or "",
        list(data.get("tk_photos") or []),
        bool(data.get("tk_has_logs")),
        username=username,
        first_name=first_name,
    )
    await state.clear()

    await _answer(
        source, target,
        f"✅ <b>Тикет #{ticket_id} создан</b>\n\n"
        "Ответ придёт сюда, в личку. Пока тикет открыт, его можно посмотреть "
        "в разделе «🎫 Тикеты».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎫 Мои тикеты", callback_data="tickets",
                                  style="primary")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")],
        ]),
    )

    owner_id = _platform_owner()
    if not owner_id:
        return

    header = (
        f"🎫 <b>Новый тикет #{ticket_id}</b>\n\n"
        f"🗂 Категория: <b>{_html(title)}</b>\n"
        f"👤 {first_name or '—'}"
        + (f" (@{username})" if username else "")
        + f"\n🆔 <code>{user_id}</code>\n"
        f"🤖 Ботов у него: <b>{len(get_user_bots(user_id))}</b>\n"
        f"📎 Фото: <b>{len(data.get('tk_photos') or [])}</b>\n"
        f"🗂 Ошибок в логах: <b>{len(get_owner_bot_errors(user_id))}</b>\n\n"
        "Нажми «Открыть», чтобы увидеть текст, логи и фото."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📨 Открыть", callback_data=f"tickets_view_{ticket_id}",
                             style="success"),
    ]])

    try:
        await bot.send_message(owner_id, header, reply_markup=kb)
    except Exception as e:
        logger.warning("Не удалось отправить тикет владельцу: %s", e)
        return

    # Больше ничего не шлём: полное содержимое (текст, логи, фото) придёт
    # только после нажатия «Открыть» — иначе тикет падает в личку простынёй.


@router.callback_query(F.data == "tk_logs_yes")
async def cb_ticket_logs_yes(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь решил приложить логи."""
    await state.update_data(tk_has_logs=True)
    await _create_ticket_and_notify(callback, state, callback.message)
    await callback.answer("Логи прикреплены")


@router.callback_query(F.data == "tk_logs_no")
async def cb_ticket_logs_no(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь отказался от логов."""
    await state.update_data(tk_has_logs=False)
    await _create_ticket_and_notify(callback, state, callback.message)
    await callback.answer()


# ═══════════════ Админ-панель: тикеты ═══════════════

def tickets_admin_kb() -> InlineKeyboardMarkup:
    counts = ticket_counts()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🟢 Открытые ({counts.get('open', 0)})",
                              callback_data="tickets_admin_open", style="primary")],
        [InlineKeyboardButton(text=f"⚪ Закрытые ({counts.get('closed', 0)})",
                              callback_data="tickets_admin_closed", style="primary")],
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="profile_admin",
                              style="primary")],
    ])


def _ticket_card(ticket: dict, with_logs: bool = True) -> str:
    """Карточка тикета: категория, отправитель, боты, логи и текст."""
    user_id = int(ticket["user_id"])
    title = TICKET_CATEGORY_TITLES.get(ticket.get("category") or "other", "Другое")
    card = (
        f"🎫 <b>Тикет #{ticket['id']}</b>\n\n"
        f"🗂 Категория: <b>{_html(title)}</b>\n"
        f"🆔 Отправитель: <code>{user_id}</code>\n"
        f"🤖 Ботов у него: <b>{len(get_user_bots(user_id))}</b>\n\n"
        f"💬 <b>Вопрос:</b>\n<blockquote>{_html(ticket.get('text') or '—')}</blockquote>"
    )
    if with_logs:
        # Логи здесь — это ошибки работы ботов, а не переписка.
        errors = get_owner_bot_errors(user_id) if ticket.get("has_logs") else []
        logs = format_bot_errors(errors) if errors else "— ошибок не зафиксировано —"
        card += f"\n\n🗂 <b>Логи (ошибки ботов):</b>\n<blockquote>{_html(logs)}</blockquote>"
    card += f"\n\n📎 Фото: <b>{len(ticket_photos(ticket))}</b>"
    return card


@router.callback_query(F.data == "tickets_admin")
async def cb_tickets_admin(callback: CallbackQuery) -> None:
    """Список тикетов для владельца платформы."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await render_callback(
        callback,
        "🎫 <b>Тикеты</b>\n\nОбращения пользователей: открытые, закрытые и без ответа.",
        tickets_admin_kb(),
    )


@router.callback_query(F.data.startswith("tickets_admin_"))
async def cb_tickets_admin_list(callback: CallbackQuery) -> None:
    """Открытые / закрытые тикеты."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    status = "closed" if cb_data(callback).endswith("closed") else "open"
    items = get_all_tickets(status)
    if not items:
        await render_callback(callback, "🎫 <b>Пусто</b>\n\nЗдесь пока нет тикетов.",
                              tickets_admin_kb())
        return

    rows = [
        [InlineKeyboardButton(
            text=f"#{t['id']} · {TICKET_CATEGORY_TITLES.get(t.get('category') or 'other', 'Другое')}"
                 f" · {t.get('username') or t['user_id']}"[:40],
            callback_data=f"tickets_view_{t['id']}",
            style="success" if status == "open" else "primary",
        )]
        for t in items
    ]
    title = "⚪ <b>Закрытые тикеты</b>" if status == "closed" else "🟢 <b>Открытые тикеты</b>"
    await render_callback(
        callback, f"{title}\n\nВсего: <b>{len(items)}</b>",
        InlineKeyboardMarkup(inline_keyboard=rows + [
            [InlineKeyboardButton(text="⬅️ К тикетам", callback_data="tickets_admin",
                                  style="primary")]
        ]),
    )


@router.callback_query(F.data.regexp(r"^tickets_view_\d+$"))
async def cb_ticket_view(callback: CallbackQuery) -> None:
    """Карточка тикета для владельца: ответить и закрыть."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    ticket_id = int(cb_data(callback).rsplit("_", 1)[-1])
    ticket = get_ticket(ticket_id)
    if not ticket:
        await callback.answer("⚠️ Тикет не найден", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✍️ Ответить",
                                  callback_data=f"tickets_answer_{ticket_id}",
                                  style="success"),
            InlineKeyboardButton(text="🔒 Закрыть тикет",
                                  callback_data=f"tickets_close_{ticket_id}", style="danger"),
        ],
        [InlineKeyboardButton(text="⬅️ К тикетам", callback_data="tickets_admin",
                              style="primary")],
    ])

    photos = ticket_photos(ticket)
    if photos:
        me = getattr(callback, "bot", None)
        me_id = getattr(me, "id", None)
        # Фото лежат в основном боте: отправлять их может только он же.
        # Если вдруг чат получателя совпал с ID бота, Telegram вернёт ошибку
        # «bot can't send messages to the bot» — такие случаи просто пропускаем.
        if me_id is None or int(me_id) != cb_uid(callback):
            try:
                media: list[MediaUnion] = [
                    InputMediaPhoto(media=file_id) for file_id in photos
                ]
                await event_bot(callback).send_media_group(cb_uid(callback), media)
            except Exception as e:
                logger.info("Не удалось показать фото тикета #%s: %s", ticket_id, e)
        else:
            logger.info("Пропускаю фото тикета #%s: получатель совпадает с ботом",
                        ticket_id)

    await render_callback(callback, _ticket_card(ticket), kb, force_answer=True)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^tickets_answer_\d+$"))
async def cb_ticket_answer_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Владелец пишет ответ — ждём текст."""
    ticket_id = int(cb_data(callback).rsplit("_", 1)[-1])
    ticket = get_ticket(ticket_id)
    if not ticket:
        await callback.answer("⚠️ Тикет не найден", show_alert=True)
        return

    await state.set_state(TicketFSM.answer)
    await state.update_data(tk_reply_id=ticket_id)
    await render_callback(
        callback,
        f"✍️ <b>Ответ на тикет #{ticket_id}</b>\n\n"
        f"Отправитель: <code>{ticket['user_id']}</code>\n\n"
        "Напиши ответ — он уйдёт пользователю в личку.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"tickets_view_{ticket_id}",
                                  style="primary")]
        ]),
    )
    await callback.answer()


@router.message(TicketFSM.answer)
async def fsm_ticket_answer(message: Message, state: FSMContext) -> None:
    """Отправляем ответ пользователю."""
    data = await state.get_data()
    ticket_id = int(data.get("tk_reply_id") or 0)
    ticket = get_ticket(ticket_id)
    if not ticket:
        await state.clear()
        await message.answer("⚠️ Тикет не найден")
        return

    answer = (message.text or "").strip()
    if not answer:
        await message.answer("❌ Ответ пустой. Напиши его ещё раз.")
        return

    await state.clear()
    target_id = int(ticket["user_id"])
    bot = event_bot(message)
    if bot.id == target_id:
        await message.answer(
            "⚠️ Этот тикет создан самим ботом — ответить в него нельзя."
        )
        return

    try:
        await bot.send_message(
            target_id,
            f"💬 <b>Ответ по тикету #{ticket_id}</b>\n\n{_html(answer)}",
        )
    except Exception as e:
        logger.warning("Не удалось отправить ответ по тикету #%s: %s", ticket_id, e)
        await message.answer(f"⚠️ Не удалось отправить ответ: {e}")
        return

    await message.answer(
        f"✅ Ответ отправлен по тикету #{ticket_id}.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔒 Закрыть тикет",
                                  callback_data=f"tickets_close_{ticket_id}", style="danger")],
            [InlineKeyboardButton(text="⬅️ К тикету", callback_data=f"tickets_view_{ticket_id}",
                                  style="primary")],
        ]),
    )


@router.callback_query(F.data.regexp(r"^tickets_close_\d+$"))
async def cb_ticket_close(callback: CallbackQuery) -> None:
    """Закрывает тикет и уведомляет об этом пользователя."""
    ticket_id = int(cb_data(callback).rsplit("_", 1)[-1])
    ticket = get_ticket(ticket_id)
    if not ticket:
        await callback.answer("⚠️ Тикет не найден", show_alert=True)
        return

    close_ticket(ticket_id)

    try:
        await event_bot(callback).send_message(
            int(ticket["user_id"]),
            f"🔒 <b>Тикет #{ticket_id} закрыт</b>\n\n"
            "Обращение отработано. Если вопрос остался — создай новый тикет, "
            "мы на связи 🤍",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить о закрытии тикета: %s", e)

    await render_callback(
        callback,
        f"🔒 Тикет #{ticket_id} закрыт. Пользователь уведомлён в личку.",
        tickets_admin_kb(),
    )
    await callback.answer("Закрыт")


@router.callback_query(F.data.regexp(r"^tickets_from_user_\d+$"))
async def cb_ticket_from_user(callback: CallbackQuery) -> None:
    """Вход в тикеты пользователя из сообщения с отправленными логами."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    user_id = int(cb_data(callback).rsplit("_", 1)[-1])
    items = get_user_tickets(user_id, "open")
    if not items:
        await callback.answer("🎫 Открытых тикетов нет", show_alert=True)
        return
    await render_callback(
        callback,
        f"🎫 <b>Тикеты пользователя {user_id}</b>\n\n" +
        "\n".join(_ticket_line(t) for t in items),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Открыть #{items[0]['id']}",
                                  callback_data=f"tickets_view_{items[0]['id']}",
                                  style="success")],
            [InlineKeyboardButton(text="⬅️ К тикетам", callback_data="tickets_admin",
                                  style="primary")],
        ]),
    )




