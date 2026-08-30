import html
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

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


class MailingFSM(StatesGroup):
    waiting_message = State()
    confirm = State()


def back_to_bot_kb(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}")]
        ]
    )


# ═══════════════ Рассылка для одного бота ═══════════════

@router.callback_query(F.data.regexp(r"^mailing_\d+$"))
async def cb_mailing_start(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    users = get_child_users(bot_id, only_active=True)
    name = bot_display_name(bot_info)

    if not users:
        if callback.message:
            try:
                await callback.message.edit_text(
                    f"📨 <b>Рассылка — {name}</b>\n\n"
                    f"❌ У бота нет активных пользователей.\n"
                    f"Пользователи появятся, когда нажмут /start у дочернего бота.",
                    reply_markup=back_to_bot_kb(bot_id)
                )
            except Exception:
                pass
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
        f"• премиум-эмодзи"
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=back_to_bot_kb(bot_id))
        except Exception:
            await callback.message.answer(text, reply_markup=back_to_bot_kb(bot_id))
    await callback.answer()


# ═══════════════ Рассылка для всех ботов ═══════════════

@router.callback_query(F.data == "all_mailing")
async def cb_all_mailing_start(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    bots = [b for b in get_user_bots(user_id) if not b.get("stopped")]

    if not bots:
        if callback.message:
            try:
                await callback.message.edit_text(
                    "📨 <b>Рассылка для всех ботов</b>\n\n"
                    "⚠️ Нет запущенных ботов.\n"
                    "Остановленные боты не участвуют в рассылке.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="⬅️ Назад", callback_data="select_all")]
                        ]
                    )
                )
            except Exception:
                pass
        await callback.answer()
        return

    bot_ids = [b["id"] for b in bots]
    total_users = 0

    for bid in bot_ids:
        users = get_child_users(bid, only_active=True)
        total_users += len(users)

    if total_users == 0:
        if callback.message:
            try:
                await callback.message.edit_text(
                    "📨 <b>Рассылка для всех ботов</b>\n\n"
                    "❌ Ни у одного бота нет активных пользователей.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="⬅️ Назад", callback_data="select_all")]
                        ]
                    )
                )
            except Exception:
                pass
        await callback.answer()
        return

    await state.set_state(MailingFSM.waiting_message)
    await state.update_data(
        mailing_bot_ids=bot_ids,
        mailing_mode="all"
    )

    text = (
        f"📨 <b>Рассылка для ВСЕХ ботов</b>\n\n"
        f"🤖 Ботов: <b>{len(bots)}</b>\n"
        f"👥 Всего активных пользователей: <b>{total_users}</b>\n\n"
        f"Отправь <b>сообщение для рассылки</b>.\n\n"
        f"Поддерживается:\n"
        f"• текст, фото, видео, GIF, документ, стикер\n"
        f"• премиум-эмодзи"
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="❌ Отмена", callback_data="select_all")]
                    ]
                )
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


# ═══════════════ Получение сообщения для рассылки ═══════════════

@router.message(MailingFSM.waiting_message)
async def fsm_mailing_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    bot_ids = data.get("mailing_bot_ids", [])

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
        mailing_media_id=media_id
    )
    await state.set_state(MailingFSM.confirm)

    total_users = 0
    for bid in bot_ids:
        users = get_child_users(bid, only_active=True)
        total_users += len(users)

    preview = _preview(raw_text)
    media_info = f"\n📎 Медиа: {media_type}" if media_type else ""
    cancel_data = f"bot_{bot_ids[0]}" if len(bot_ids) == 1 else "select_all"

    await message.answer(
        f"📨 <b>Подтверди рассылку</b>\n\n"
        f"🤖 Ботов: <b>{len(bot_ids)}</b>\n"
        f"👥 Получателей: <b>{total_users}</b>"
        f"{media_info}\n\n"
        f"💬 Текст:\n{preview}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Отправить", callback_data="mailing_confirm")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_data)],
            ]
        )
    )


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
    await state.clear()

    if not callback.message:
        await callback.answer()
        return

    status_msg = await callback.message.edit_text("📨 Рассылка запущена... ⏳")
    await callback.answer()

    grand_sent = 0
    grand_failed = 0
    grand_total = 0

    for bot_id in bot_ids:
        if not child_manager.is_running(bot_id):
            continue

        async def progress_cb(sent, failed, total, current, _bot_id=bot_id):
            try:
                pct = int(current / total * 100) if total else 0
                await status_msg.edit_text(
                    f"📨 Рассылка...\n\n"
                    f"🤖 Бот: <code>{_bot_id}</code>\n"
                    f"📊 {pct}% ({current}/{total})\n"
                    f"✅ {sent}  ❌ {failed}"
                )
            except Exception:
                pass

        result = await child_manager.send_mailing(
            bot_id=bot_id,
            text=mailing_text,
            media_type=media_type,
            media_id=media_id,
            entities=mailing_entities,
            progress_callback=progress_cb
        )

        grand_sent += result["sent"]
        grand_failed += result["failed"]
        grand_total += result["total"]

    back_data = f"bot_{bot_ids[0]}" if len(bot_ids) == 1 else "select_all"

    await status_msg.edit_text(
        f"📨 <b>Рассылка завершена!</b>\n\n"
        f"🤖 Ботов: <b>{len(bot_ids)}</b>\n"
        f"👥 Всего получателей: <b>{grand_total}</b>\n\n"
        f"✅ Доставлено: <b>{grand_sent}</b>\n"
        f"❌ Не доставлено: <b>{grand_failed}</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_data)],
            ]
        )
    )