from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.storage import get_bot_by_id, update_bot_field, bot_display_name
from services.child_manager import ChildManager

router = Router()


class EditorFSM(StatesGroup):
    waiting_welcome_text = State()


def editor_kb(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="💬 Изменить приветствие",
                callback_data=f"edit_welcome_{bot_id}"
            )],
            [InlineKeyboardButton(
                text="⬅️ Назад к боту",
                callback_data=f"bot_{bot_id}"
            )],
        ]
    )


@router.callback_query(F.data.startswith("editor_"))
async def cb_editor(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    bot_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    name = bot_display_name(bot_info)
    welcome = bot_info.get("welcome_text", "") or "— не задано —"

    text = (
        f"✏️ <b>Редактор — {name}</b>\n\n"
        f"💬 Текущее приветствие:\n{welcome}\n\n"
        f"Выбери что изменить:"
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=editor_kb(bot_id))
        except Exception:
            await callback.message.answer(text, reply_markup=editor_kb(bot_id))
    await callback.answer()


@router.callback_query(F.data.startswith("edit_welcome_"))
async def cb_edit_welcome(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split("_")[-1])

    await state.set_state(EditorFSM.waiting_welcome_text)
    await state.update_data(editing_bot_id=bot_id)

    text = (
        "💬 <b>Новое приветствие</b>\n\n"
        "Отправь текст, который дочерний бот будет показывать "
        "при /start.\n\n"
        "Поддерживается HTML-разметка и премиум-эмодзи.\n\n"
        "Пример:\n"
        "<code>👋 Привет! Добро пожаловать!</code>"
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"editor_{bot_id}"
                    )]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(EditorFSM.waiting_welcome_text)
async def fsm_welcome_text(message: Message, state: FSMContext,
                           child_manager: ChildManager) -> None:
    data = await state.get_data()
    bot_id = data.get("editing_bot_id")
    user_id = message.from_user.id

    if not bot_id:
        await state.clear()
        await message.answer("⚠️ Ошибка. Попробуй снова.")
        return

    # Берём текст (с entities для премиум-эмодзи)
    new_welcome = message.html_text or message.text or ""

    if not new_welcome.strip():
        await message.answer("❌ Текст не может быть пустым. Попробуй ещё раз.")
        return

    # Сохраняем
    update_bot_field(user_id, bot_id, "welcome_text", new_welcome)
    await state.clear()

    # Перезапускаем дочернего бота чтобы он подхватил новое приветствие
    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info and child_manager.is_running(bot_id):
        await child_manager.restart_child(bot_info)
        status = "🟢 Бот перезапущен с новым приветствием"
    else:
        status = "💾 Сохранено (бот остановлен, изменения применятся при запуске)"

    name = bot_display_name(bot_info) if bot_info else f"bot_{bot_id}"

    await message.answer(
        f"✅ <b>Приветствие обновлено!</b>\n\n"
        f"🤖 {name}\n"
        f"{status}\n\n"
        f"💬 Новый текст:\n{new_welcome}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактор", callback_data=f"editor_{bot_id}")],
            [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
        ])
    )