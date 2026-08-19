from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.storage import get_coowners, add_coowner, remove_coowner
from handlers.start import main_menu_kb

router = Router()


class CoownerFSM(StatesGroup):
    waiting_add = State()
    waiting_remove = State()


def coowners_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить совладельца", callback_data="co_add")],
            [InlineKeyboardButton(text="🗑 Удалить совладельца", callback_data="co_remove")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )


@router.callback_query(F.data == "coowners")
async def cb_coowners(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user_id = callback.from_user.id

    coowners = get_coowners(user_id)

    if coowners:
        lines = []
        for co in coowners:
            uname = f"@{co['username']}" if co['username'] else f"ID:{co['coowner_id']}"
            lines.append(f"  👤 {uname}")
        co_list = "\n".join(lines)
        text = (
            f"👥 <b>Совладельцы</b> ({len(coowners)})\n\n"
            f"{co_list}\n\n"
            f"Совладельцы видят все твои боты и могут "
            f"просматривать статистику."
        )
    else:
        text = (
            "👥 <b>Совладельцы</b>\n\n"
            "У тебя пока нет совладельцев.\n\n"
            "Совладельцы смогут видеть все твои боты "
            "и их статистику."
        )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=coowners_kb())
        except Exception:
            await callback.message.answer(text, reply_markup=coowners_kb())
    await callback.answer()


# ═══════════════ Добавить совладельца ═══════════════

@router.callback_query(F.data == "co_add")
async def cb_co_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CoownerFSM.waiting_add)

    text = (
        "➕ <b>Добавить совладельца</b>\n\n"
        "Отправь <b>user ID</b> и <b>username</b> через пробел.\n\n"
        "Формат:\n<code>123456789 @username</code>\n\n"
        "User ID можно узнать через @userinfobot"
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="coowners")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(CoownerFSM.waiting_add)
async def fsm_co_add(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=1)

    if not parts:
        await message.answer("❌ Отправь user ID и username.")
        return

    try:
        coowner_id = int(parts[0])
    except ValueError:
        await message.answer(
            "❌ Первый аргумент должен быть числовым user ID.\n\n"
            "Формат: <code>123456789 @username</code>"
        )
        return

    username = parts[1].strip().lstrip("@") if len(parts) > 1 else ""

    if coowner_id == user_id:
        await message.answer("❌ Нельзя добавить себя.")
        await state.clear()
        return

    success = add_coowner(user_id, coowner_id, username)
    await state.clear()

    if success:
        uname = f"@{username}" if username else f"ID:{coowner_id}"
        await message.answer(
            f"✅ <b>Совладелец добавлен!</b>\n\n"
            f"👤 {uname}\n"
            f"🆔 <code>{coowner_id}</code>\n\n"
            f"Теперь он видит все твои боты и статистику.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👥 Совладельцы", callback_data="coowners")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="back_main")],
            ])
        )
    else:
        await message.answer(
            "⚠️ Этот пользователь уже является совладельцем.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👥 Совладельцы", callback_data="coowners")]
            ])
        )


# ═══════════════ Удалить совладельца ═══════════════

@router.callback_query(F.data == "co_remove")
async def cb_co_remove(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    coowners = get_coowners(user_id)

    if not coowners:
        await callback.answer("Нет совладельцев")
        return

    await state.set_state(CoownerFSM.waiting_remove)

    lines = []
    for co in coowners:
        uname = f"@{co['username']}" if co['username'] else f"ID:{co['coowner_id']}"
        lines.append(f"  {uname} — ID: <code>{co['coowner_id']}</code>")

    co_list = "\n".join(lines)

    text = (
        f"🗑 <b>Удалить совладельца</b>\n\n"
        f"Текущие совладельцы:\n{co_list}\n\n"
        f"Отправь <b>user ID</b> совладельца для удаления."
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="coowners")]
                ])
            )
        except Exception:
            await callback.message.answer(text)
    await callback.answer()


@router.message(CoownerFSM.waiting_remove)
async def fsm_co_remove(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    raw = (message.text or "").strip()

    try:
        coowner_id = int(raw)
    except ValueError:
        await message.answer("❌ Отправь числовой user ID.")
        return

    removed = remove_coowner(user_id, coowner_id)
    await state.clear()

    if removed:
        await message.answer(
            f"✅ <b>Совладелец удалён!</b>\n\n"
            f"🆔 <code>{coowner_id}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👥 Совладельцы", callback_data="coowners")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="back_main")],
            ])
        )
    else:
        await message.answer(
            "⚠️ Совладелец не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👥 Совладельцы", callback_data="coowners")]
            ])
        )