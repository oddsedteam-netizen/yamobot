import html
import logging
import time

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import (render_callback, safe_edit, cb_data, cb_uid,
                              try_edit_answer, try_edit)
from services.storage import (
    get_bot_by_id,
    get_user_bots,
    bot_display_name,
    get_child_users,
)
from services.child_manager import ChildManager

router = Router()
logger = logging.getLogger(__name__)


def _entities_to_dicts(entities) -> list[dict]:
    """Сериализуем сущности сообщения для передачи в рассылку
    (сохраняет premium-эмодзи и всё форматирование)."""
    result = []
    for e in entities or []:
        if getattr(e, "type", "") == "text_mention":
            # text_mention требует вложенный объект user — его нельзя
            # безопасно восстановить, поэтому пропускаем (просто текст)
            continue
        result.append(e.model_dump(exclude_none=True))
    return result


def _preview(text: str, limit: int = 200) -> str:
    if not text:
        return "— без текста —"
    shown = text[:limit]
    if len(text) > limit:
        shown += "..."
    return html.escape(shown)


def _fmt_duration(seconds: float) -> str:
    """Время в человеческом виде: «12 с», «3 мин 20 с», «1 ч 04 мин»."""
    seconds = max(0.0, float(seconds or 0))
    if seconds < 60:
        return f"{seconds:.0f} с" if seconds >= 1 else f"{seconds:.1f} с"
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes} мин {sec:02d} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


def _report_mailing(bots: int, total: int, sent: int, failed: int,
                    duration: float, reasons: dict, samples: list) -> None:
    """Сводка по рассылке в лог: по нему проще разбирать «куда делось»."""
    logger.info(
        "Рассылка: ботов=%d, получателей=%d, доставлено=%d, не доставлено=%d, "
        "время=%.1fс, причины=%s",
        bots, total, sent, failed, duration, reasons or {},
    )
    for sample in samples:
        logger.info("Рассылка: не доставлено — %s", sample)


class MailingFSM(StatesGroup):
    waiting_message = State()      # ждём текст/фото рассылки
    waiting_btn_name = State()     # название инлайн-кнопки
    waiting_btn_url = State()      # ссылка инлайн-кнопки
    waiting_btn_style = State()    # цвет инлайн-кнопки
    confirm = State()              # подтверждение рассылки


# Не больше трёх кнопок под сообщением (Telegram и так не любит «простыни»).
MAILING_MAX_BUTTONS = 3

# Цвета кнопок: Telegram Bot API 9.x — danger / primary / success.
BUTTON_STYLES: dict[str, str] = {
    "": "⚪ Без цвета",
    "primary": "🔵 Синий",
    "success": "🟢 Зелёный",
    "danger": "🔴 Красный",
}


def _build_buttons_markup(buttons: list[dict]) -> InlineKeyboardMarkup | None:
    """Собирает инлайн-клавиатуру рассылки из накопленных кнопок."""
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=b.get("text", "Кнопка"),
            url=b.get("url", ""),
            style=(b.get("style") or None),
        )] for b in buttons
    ])


def _buttons_summary(buttons: list[dict]) -> str:
    """Человекочитаемый список кнопок для превью в подтверждении."""
    if not buttons:
        return ""
    lines = ["\n🔗 <b>Кнопки под сообщением:</b>"]
    for i, b in enumerate(buttons, 1):
        color = BUTTON_STYLES.get(b.get("style") or "", "").split(" ", 1)[-1]
        lines.append(f"{i}. <b>{b['text']}</b> → <code>{b['url']}</code> ({color})")
    return "\n".join(lines)


def back_to_bot_kb(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}")]
        ]
    )


def _cancel_mailing_kb() -> InlineKeyboardMarkup:
    """Кнопка отмены рассылки на любом шаге оформления."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить рассылку", callback_data="mailing_btns_cancel",
                                  style="primary")]
        ]
    )


async def _show_mailing_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Экран подтверждения: кому уйдёт, что именно и какие кнопки снизу."""
    data = await state.get_data()
    bot_ids = data.get("mailing_bot_ids", []) or []
    buttons = data.get("mailing_buttons", []) or []
    text_raw = str(data.get("mailing_text") or "")
    media_type = str(data.get("mailing_media_type") or "")

    total_users = sum(len(get_child_users(bid, only_active=True)) for bid in bot_ids)
    media_info = f"\n📎 Медиа: <code>{media_type}</code>" if media_type else ""
    cancel_data = f"bot_{bot_ids[0]}" if len(bot_ids) == 1 else "select_all"

    body = (
        f"📨 <b>Подтверди рассылку</b>\n\n"
        f"🤖 Ботов: <b>{len(bot_ids)}</b>\n"
        f"👥 Получателей: <b>{total_users}</b>"
        f"{media_info}\n\n"
        f"💬 Текст:\n{_preview(text_raw)}"
        f"{_buttons_summary(buttons)}"
    )

    await state.set_state(MailingFSM.confirm)
    await render_callback(
        callback,
        body,
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Отправить ({total_users})",
                                  callback_data="mailing_confirm", style="success")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_data, style="primary")],
        ]),
    )


# ═══════════════ Рассылка для одного бота ═══════════════

@router.callback_query(F.data.regexp(r"^mailing_\d+$"))
async def cb_mailing_start(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(cb_data(callback).split("_", 1)[1])
    user_id = cb_uid(callback)

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    users = get_child_users(bot_id, only_active=True)
    name = bot_display_name(bot_info)

    if not users:
        await safe_edit(
            callback.message,
            f"📨 <b>Рассылка — {name}</b>\n\n"
            f"❌ У бота нет активных пользователей.\n"
            f"Пользователи появятся, когда нажмут /start у дочернего бота.",
            back_to_bot_kb(bot_id),
        )
        await callback.answer()
        return

    await state.set_state(MailingFSM.waiting_message)
    await state.update_data(
        mailing_bot_ids=[bot_id],
        mailing_mode="single"
    )

    text = (
        f"📨 <b>Рассылка — {name}</b>\n\n"
        f"👥 Активных пользователей: <b>{len(users)}</b>\n\n"
        f"Отправь <b>сообщение для рассылки</b>.\n\n"
        f"Поддерживается:\n"
        f"• текст (HTML)\n"
        f"• фото с подписью\n"
        f"• видео с подписью\n"
        f"• GIF\n"
        f"• документ\n"
        f"• стикер\n"
        f"• премиум-эмодзи\n\n"
        f"<i>Дальше спрошу, нужны ли инлайн-кнопки со ссылками (до "
        f"{MAILING_MAX_BUTTONS}), и покажу подтверждение перед отправкой.</i>"
    )

    await render_callback(callback, text, back_to_bot_kb(bot_id))


# ═══════════════ Рассылка для всех ботов ═══════════════

@router.callback_query(F.data == "all_mailing")
async def cb_all_mailing_start(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = cb_uid(callback)
    # Рассылка идёт ТОЛЬКО в ботов категории «стандарт». Боты-анкетницы
    # не участвуют в рассылках.
    bots = [
        b for b in get_user_bots(user_id)
        if not b.get("stopped") and (b.get("bot_type") or "standard") == "standard"
    ]

    if not bots:
        await safe_edit(
            callback.message,
            "📨 <b>Рассылка — только стандарт-боты</b>\n\n"
            "⚠️ Нет запущенных ботов категории «стандарт» для рассылки.\n"
            "Остановленные и боты-анкетницы не участвуют в рассылке.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="select_all", style="primary")]
            ]),
        )
        await callback.answer()
        return

    bot_ids = [b["id"] for b in bots]
    total_users = 0

    for bid in bot_ids:
        users = get_child_users(bid, only_active=True)
        total_users += len(users)

    if total_users == 0:
        await safe_edit(
            callback.message,
            "📨 <b>Рассылка — только стандарт-боты</b>\n\n"
            "❌ Ни у одного стандарт-бота нет активных пользователей.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="select_all", style="primary")]
            ]),
        )
        await callback.answer()
        return

    await state.set_state(MailingFSM.waiting_message)
    await state.update_data(
        mailing_bot_ids=bot_ids,
        mailing_mode="all"
    )

    text = (
        f"📨 <b>Рассылка — только стандарт-боты</b>\n\n"
        f"🤖 Ботов: <b>{len(bots)}</b>\n"
        f"👥 Всего активных пользователей: <b>{total_users}</b>\n\n"
        f"<i>Боты-анкетницы пропускаются.</i>\n\n"
        f"Отправь <b>сообщение для рассылки</b>.\n\n"
        f"Поддерживается:\n"
        f"• текст, фото, видео, GIF, документ, стикер\n"
        f"• премиум-эмодзи\n\n"
        f"<i>Дальше спрошу, нужны ли инлайн-кнопки со ссылками (до "
        f"{MAILING_MAX_BUTTONS}), и покажу подтверждение перед отправкой.</i>"
    )

    if callback.message:
        await try_edit_answer(callback.message, text,
                              InlineKeyboardMarkup(inline_keyboard=[
                                  [InlineKeyboardButton(text="❌ Отмена", callback_data="select_all", style="primary")]
                              ]))
    await callback.answer()


# ═══════════════ Получение сообщения для рассылки ═══════════════

@router.message(MailingFSM.waiting_message)
async def fsm_mailing_message(message: Message, state: FSMContext) -> None:
    media_type = ""
    media_id = ""
    raw_text = ""
    entities: list[dict] = []

    if message.photo:
        media_type = "photo"
        media_id = message.photo[-1].file_id
        raw_text = message.caption or ""
        entities = _entities_to_dicts(message.caption_entities)
    elif message.video:
        media_type = "video"
        media_id = message.video.file_id
        raw_text = message.caption or ""
        entities = _entities_to_dicts(message.caption_entities)
    elif message.animation:
        media_type = "animation"
        media_id = message.animation.file_id
        raw_text = message.caption or ""
        entities = _entities_to_dicts(message.caption_entities)
    elif message.document:
        media_type = "document"
        media_id = message.document.file_id
        raw_text = message.caption or ""
        entities = _entities_to_dicts(message.caption_entities)
    elif message.sticker:
        media_type = "sticker"
        media_id = message.sticker.file_id
        raw_text = ""
        entities = []
    else:
        raw_text = message.text or ""
        entities = _entities_to_dicts(message.entities)

    if not raw_text and not media_id:
        await message.answer("❌ Пустое сообщение. Отправь текст или медиа.")
        return

    await state.update_data(
        mailing_text=raw_text,
        mailing_entities=entities,
        mailing_media_type=media_type,
        mailing_media_id=media_id,
        mailing_buttons=[],
    )

    # Спрашиваем, нужны ли кнопки под сообщением.
    await message.answer(
        "✅ <b>Сообщение принято.</b>\n\n"
        "Добавить под сообщение <b>инлайн-кнопки со ссылками</b>?\n"
        f"Можно до <b>{MAILING_MAX_BUTTONS}</b> штук — для каждой спросим "
        "название, ссылку и цвет.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Да, добавить кнопки", callback_data="mailing_btns_yes",
                                  style="success")],
            [InlineKeyboardButton(text="✖️ Нет, без кнопок", callback_data="mailing_btns_no",
                                  style="primary")],
        ]),
    )


# ═══════════════ Инлайн-кнопки под рассылкой ═══════════════

@router.callback_query(F.data == "mailing_btns_yes")
async def cb_mailing_btns_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MailingFSM.waiting_btn_name)
    await state.update_data(mailing_btn_index=1)
    await render_callback(
        callback,
        f"🔗 <b>Кнопка 1 из {MAILING_MAX_BUTTONS}</b>\n\n"
        "Напиши <b>название кнопки</b> — как её увидит получатель.\n"
        "Пример: <code>Перейти в канал</code>",
        _cancel_mailing_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "mailing_btns_no")
async def cb_mailing_btns_no(callback: CallbackQuery, state: FSMContext) -> None:
    """Без кнопок — сразу к подтверждению рассылки."""
    await callback.answer()
    await _show_mailing_confirm(callback, state)


@router.message(MailingFSM.waiting_btn_name)
async def fsm_mailing_btn_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    index = int(data.get("mailing_btn_index", 1))
    name = (message.text or "").strip()

    if not name:
        await message.answer("❌ Название не может быть пустым. Напиши его ещё раз.")
        return
    if len(name) > 64:
        name = name[:64]
        await message.answer("⚠️ Название обрезано до 64 символов (Telegram не примет больше).")

    await state.update_data(mailing_btn_name=name)
    await state.set_state(MailingFSM.waiting_btn_url)
    await message.answer(
        f"🔗 <b>Кнопка {index}: «{name}»</b>\n\n"
        "Теперь пришли <b>ссылку</b> для кнопки.\n"
        "Можно полной (<code>https://t.me/…</code>), короткой "
        "(<code>t.me/…</code>) или как <code>@username</code> — бот сам "
        "приведёт её к нужному виду."
    )


@router.message(MailingFSM.waiting_btn_url)
async def fsm_mailing_btn_url(message: Message, state: FSMContext) -> None:
    from handlers._common import normalize_link

    data = await state.get_data()
    index = int(data.get("mailing_btn_index", 1))
    name = str(data.get("mailing_btn_name") or "Кнопка")

    link = normalize_link(message.text or "")
    if not link:
        await message.answer(
            "❌ Это не похоже на ссылку.\n\n"
            "Пришли ещё раз: <code>https://t.me/канал</code>, "
            "<code>t.me/канал</code> или <code>@канал</code>."
        )
        return

    await state.update_data(mailing_btn_url=link)
    await state.set_state(MailingFSM.waiting_btn_style)

    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"mailing_style_{code or 'none'}",
                              style=(code or None))]
        for code, label in BUTTON_STYLES.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Отменить рассылку",
                                      callback_data="mailing_btns_cancel", style="primary")])
    await message.answer(
        f"🎨 <b>Кнопка {index}: «{name}»</b>\n\n"
        f"Ссылка: <code>{link}</code>\n\n"
        "Какой цвет кнопки?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("mailing_style_"))
async def cb_mailing_style(callback: CallbackQuery, state: FSMContext) -> None:
    code = cb_data(callback).split("mailing_style_", 1)[1]
    style = "" if code == "none" else code
    if style not in BUTTON_STYLES:
        await callback.answer("⚠️ Неизвестный цвет", show_alert=True)
        return

    data = await state.get_data()
    name = str(data.get("mailing_btn_name") or "")
    url = str(data.get("mailing_btn_url") or "")
    if not name or not url:
        await callback.answer("⚠️ Данные кнопки потеряны — начни рассылку заново",
                              show_alert=True)
        await state.clear()
        return

    buttons = list(data.get("mailing_buttons") or [])
    buttons.append({"text": name, "url": url, "style": style})
    await state.update_data(
        mailing_buttons=buttons,
        mailing_btn_index=len(buttons) + 1,
    )
    await callback.answer(f"✅ Кнопка «{name}» добавлена")

    if len(buttons) >= MAILING_MAX_BUTTONS:
        await _show_mailing_confirm(callback, state)
        return

    await render_callback(
        callback,
        f"🔗 <b>Добавлено кнопок: {len(buttons)} из {MAILING_MAX_BUTTONS}</b>\n"
        + _buttons_summary(buttons)
        + "\n\nДобавить ещё одну кнопку?",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Да, ещё одну", callback_data="mailing_more_yes",
                                  style="success")],
            [InlineKeyboardButton(text="✅ Хватит, к подтверждению",
                                  callback_data="mailing_more_no", style="primary")],
        ]),
    )


@router.callback_query(F.data == "mailing_more_yes")
async def cb_mailing_more_yes(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    buttons = data.get("mailing_buttons") or []
    index = min(int(data.get("mailing_btn_index", len(buttons) + 1)), MAILING_MAX_BUTTONS)
    await state.set_state(MailingFSM.waiting_btn_name)
    await state.update_data(mailing_btn_index=index)
    await render_callback(
        callback,
        f"🔗 <b>Кнопка {index} из {MAILING_MAX_BUTTONS}</b>\n\n"
        "Напиши <b>название</b> новой кнопки.",
        _cancel_mailing_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "mailing_more_no")
async def cb_mailing_more_no(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_mailing_confirm(callback, state)


@router.callback_query(F.data == "mailing_btns_cancel")
async def cb_mailing_btns_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена рассылки на любом шаге оформления."""
    await state.clear()
    await callback.answer("❌ Рассылка отменена", show_alert=True)
    await render_callback(callback, "❌ <b>Рассылка отменена.</b>", _cancel_mailing_kb())


# ═══════════════ Подтверждение и отправка ═══════════════

@router.callback_query(F.data == "mailing_confirm", MailingFSM.confirm)
async def cb_mailing_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    child_manager: ChildManager
) -> None:
    data = await state.get_data()
    bot_ids = data.get("mailing_bot_ids", [])
    mailing_text = data.get("mailing_text", "")
    media_type = data.get("mailing_media_type", "")
    media_id = data.get("mailing_media_id", "")
    mailing_entities = data.get("mailing_entities", []) or []
    mailing_buttons = data.get("mailing_buttons", []) or []
    await state.clear()

    if not callback.message:
        await callback.answer()
        return

    await try_edit(callback.message, "📨 Рассылка запущена... ⏳")
    status_msg = callback.message
    await callback.answer()

    reply_markup = _build_buttons_markup(mailing_buttons)

    grand_sent = 0
    grand_failed = 0
    grand_total = 0
    reasons: dict[str, int] = {}
    samples: list[str] = []
    started_at = time.monotonic()

    for bot_id in bot_ids:
        if not child_manager.is_running(bot_id):
            reasons["бот остановлен"] = reasons.get("бот остановлен", 0) + 1
            continue

        async def progress_cb(sent, failed, total, current, _bot_id=bot_id):
            try:
                pct = int(current / total * 100) if total else 0
                await try_edit(
                    status_msg,
                    f"📨 Рассылка...\n\n"
                    f"🤖 Бот: <code>{_bot_id}</code>\n"
                    f"📊 {pct}% ({current}/{total})\n"
                    f"✅ {sent}  ❌ {failed}",
                )
            except Exception:
                pass

        result = await child_manager.send_mailing(
            bot_id=bot_id,
            text=mailing_text,
            media_type=media_type,
            media_id=media_id,
            entities=mailing_entities,
            progress_callback=progress_cb,
            reply_markup=reply_markup,
        )

        grand_sent += result["sent"]
        grand_failed += result["failed"]
        grand_total += result["total"]
        for reason, count in (result.get("reasons") or {}).items():
            reasons[reason] = reasons.get(reason, 0) + count
        for sample in (result.get("samples") or []):
            if len(samples) < 5:
                samples.append(sample)

    duration = time.monotonic() - started_at
    _report_mailing(len(bot_ids), grand_total, grand_sent, grand_failed,
                    duration, reasons, samples)

    back_data = f"bot_{bot_ids[0]}" if len(bot_ids) == 1 else "select_all"

    lines = [
        "📨 <b>Рассылка завершена!</b>",
        "",
        f"🤖 Ботов: <b>{len(bot_ids)}</b>",
        f"👥 Получателей: <b>{grand_total}</b>",
        "",
        f"✅ Доставлено: <b>{grand_sent}</b>",
        f"❌ Не доставлено: <b>{grand_failed}</b>",
        f"⏱ Заняло: <b>{_fmt_duration(duration)}</b>",
    ]

    if reasons:
        lines += ["", "📉 <b>Почему не дошло:</b>"]
        lines += [f"  • {reason} — <b>{count}</b>" for reason, count in reasons.items()]

    if samples:
        lines += ["", "🔎 <b>Примеры:</b>"] + [f"  {s}" for s in samples]

    if grand_total and grand_sent == 0:
        lines += [
            "",
            "⚠️ <b>Не дошло никому.</b> Чаще всего это лимит Telegram на "
            "рассылки: попробуй позже или разбей список на части. Если бот "
            "остановлен — сначала запусти его.",
        ]

    await try_edit(
        status_msg,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_data, style="primary")],
            ]
        ),
    )