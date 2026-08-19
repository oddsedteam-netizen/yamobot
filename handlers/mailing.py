import asyncio
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

from services.storage import get_bot_by_id, bot_display_name
from services.child_manager import ChildManager

router = Router()
logger = logging.getLogger(__name__)


class MailingFSM(StatesGroup):
    waiting_chat_ids = State()
    waiting_message = State()
    confirm = State()


def back_to_bot_kb(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}")]
    ])


@router.callback_query(F.data.startswith("mailing_"))
async def cb_mailing_start(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    await state.set_state(MailingFSM.waiting_chat_ids)
    await state.update_data(mailing_bot_id=bot_id)

    name = bot_display_name(bot_info)
    text = (
        f"📨 <b>Рассылка — {name}</b>\n\n"
        f"Отправь список chat_id через запятую или каждый с новой строки.\n\n"
        f"Пример:\n<code>123456789\n987654321</code>\n\n"
        f"Чтобы узнать chat_id, пользователи могут написать /start дочернему боту."
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=back_to_bot_kb(bot_id))
        except Exception:
            await callback.message.answer(text, reply_markup=back_to_bot_kb(bot_id))
    await callback.answer()


@router.message(MailingFSM.waiting_chat_ids)
async def fsm_chat_ids(message: Message, state: FSMContext) -> None:
    raw = message.text or ""
    # Парсим chat_id
    ids = []
    for part in raw.replace(",", "\n").split("\n"):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))

    if not ids:
        await message.answer(
            "❌ Не удалось распознать chat_id.\n"
            "Отправь числа через запятую или каждый с новой строки."
        )
        return

    data = await state.get_data()
    bot_id = data.get("mailing_bot_id")

    await state.update_data(chat_ids=ids)
    await state.set_state(MailingFSM.waiting_message)

    await message.answer(
        f"✅ Получено <b>{len(ids)}</b> chat_id.\n\n"
        f"Теперь отправь <b>текст рассылки</b>.\n"
        f"Поддерживается HTML и премиум-эмодзи.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"bot_{bot_id}")]
        ])
    )


@router.message(MailingFSM.waiting_message)
async def fsm_mailing_message(message: Message, state: FSMContext) -> None:
    mailing_text = message.html_text or message.text or ""

    if not mailing_text.strip():
        await message.answer("❌ Текст не может быть пустым.")
        return

    data = await state.get_data()
    bot_id = data.get("mailing_bot_id")
    chat_ids = data.get("chat_ids", [])

    await state.update_data(mailing_text=mailing_text)
    await state.set_state(MailingFSM.confirm)

    await message.answer(
        f"📨 <b>Подтверди рассылку</b>\n\n"
        f"Получателей: <b>{len(chat_ids)}</b>\n\n"
        f"Текст:\n{mailing_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить", callback_data="mailing_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"bot_{bot_id}")],
        ])
    )


@router.callback_query(F.data == "mailing_confirm", MailingFSM.confirm)
async def cb_mailing_confirm(callback: CallbackQuery, state: FSMContext,
                             child_manager: ChildManager) -> None:
    data = await state.get_data()
    bot_id = data.get("mailing_bot_id")
    chat_ids = data.get("chat_ids", [])
    mailing_text = data.get("mailing_text", "")
    await state.clear()

    if not child_manager.is_running(bot_id):
        if callback.message:
            await callback.message.edit_text(
                "⚠️ Бот не запущен. Сначала запусти его.",
                reply_markup=back_to_bot_kb(bot_id)
            )
        await callback.answer()
        return

    await callback.answer("📨 Рассылка запущена...")

    # Отправляем
    success = 0
    fail = 0

    status_msg = await callback.message.edit_text("📨 Рассылка в процессе... 0%")

    for i, chat_id in enumerate(chat_ids):
        sent = await child_manager.send_message(bot_id, chat_id, mailing_text)
        if sent:
            success += 1
        else:
            fail += 1

        # Обновляем прогресс каждые 5 сообщений
        if (i + 1) % 5 == 0 or (i + 1) == len(chat_ids):
            pct = int((i + 1) / len(chat_ids) * 100)
            try:
                await status_msg.edit_text(
                    f"📨 Рассылка... {pct}%\n"
                    f"✅ {success}  ❌ {fail}"
                )
            except Exception:
                pass

        # Задержка чтобы не словить rate limit
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"📨 <b>Рассылка завершена!</b>\n\n"
        f"✅ Доставлено: {success}\n"
        f"❌ Ошибок: {fail}\n"
        f"📊 Всего: {len(chat_ids)}",
        reply_markup=back_to_bot_kb(bot_id)
    )