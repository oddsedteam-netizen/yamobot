from aiogram import Bot, Router, F
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers.start import main_menu_kb
from services.storage import add_user_bot, bot_display_name
from services.child_manager import ChildManager

router = Router()


class AddBotFSM(StatesGroup):
    waiting_for_token = State()


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]
        ]
    )


async def verify_token(token: str) -> dict | None:
    try:
        tmp_bot = Bot(token=token)
        me = await tmp_bot.get_me()
        info = {
            "token": token,
            "id": me.id,
            "username": me.username or "",
            "first_name": me.first_name or "",
            "welcome_text": "",
            "stopped": False,
        }
        await tmp_bot.session.close()
        return info
    except TelegramUnauthorizedError:
        return None
    except Exception:
        return None


@router.callback_query(F.data == "add_bot")
async def cb_add_bot(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddBotFSM.waiting_for_token)
    text = (
        "➕ <b>Добавить бота</b>\n\n"
        "Отправь мне <b>токен</b> бота от @BotFather.\n\n"
        "Пример:\n<code>1234567890:AAExampleToken</code>"
    )
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=back_kb())
        except Exception:
            await callback.message.answer(text, reply_markup=back_kb())
    await callback.answer()


@router.message(AddBotFSM.waiting_for_token)
async def fsm_receive_token(message: Message, state: FSMContext,
                            child_manager: ChildManager) -> None:
    token = message.text.strip() if message.text else ""

    if ":" not in token or len(token) < 30:
        await message.answer(
            "❌ Неверный формат токена.\n\n"
            "Токен выглядит так:\n"
            "<code>1234567890:AAExampleToken</code>\n\n"
            "Попробуй ещё раз или нажми «Назад».",
            reply_markup=back_kb()
        )
        return

    wait_msg = await message.answer("⏳ Проверяю токен...")

    info = await verify_token(token)

    if info is None:
        await wait_msg.edit_text(
            "❌ <b>Токен недействителен.</b>\n\n"
            "Проверь правильность и попробуй снова.",
            reply_markup=back_kb()
        )
        return

    add_user_bot(message.from_user.id, info)
    await state.clear()

    # Запускаем дочернего бота
    started = await child_manager.start_child(info)
    status = "🟢 Бот запущен" if started else "⚠️ Бот сохранён, но не удалось запустить"

    name = bot_display_name(info)
    await wait_msg.edit_text(
        f"✅ <b>Бот подключён!</b>\n\n"
        f"🤖 {name}\n"
        f"🆔 <code>{info['id']}</code>\n"
        f"{status}\n\n"
        f"Бот появился в «Мои боты».",
        reply_markup=main_menu_kb()
    )