from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from handlers._common import render_callback
from handlers.profile import admin_kb
from services.config import OWNER_ID, is_super_admin
from services.storage import create_complaint, get_complaints, get_complaint, set_complaint_status

router = Router()


class ComplaintFSM(StatesGroup):
    waiting_screenshot = State()
    waiting_comment = State()


CATEGORIES = {"spam": "📢 Спам", "abuse": "😡 Оскорбления",
              "rules": "⚠️ Нарушение правил", "other": "❓ Другое"}


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_main")]
    ])


def categories_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=CATEGORIES[c], callback_data=f"comp_cat_{c}")]
            for c in CATEGORIES]
    rows.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def complaints_admin_kb(complaints: list[dict]) -> InlineKeyboardMarkup:
    statuses = {"new": "🆕 Новая", "accepted": "✅ Принята", "rejected": "❌ Отклонена"}
    rows = []
    for c in complaints:
        label = c.get("category") or c.get("user_username") or f"#{c['id']}"
        st = statuses.get(c["status"], c["status"])
        rows.append([InlineKeyboardButton(text=f"#{c['id']} — {label} ({st})",
                                          callback_data=f"comp_view_{c['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="profile_admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_complaint(message: Message, state: FSMContext) -> None:
    await state.set_state(ComplaintFSM.waiting_screenshot)
    await message.answer("⚠️ <b>Жалоба</b>\n\nВыбери категорию жалобы:", reply_markup=categories_kb())


@router.callback_query(F.data.startswith("comp_cat_"))
async def cb_choose_category(callback: CallbackQuery, state: FSMContext) -> None:
    code = callback.data.split("_")[-1]
    label = CATEGORIES.get(code, code)
    await state.update_data(category=label)
    await state.set_state(ComplaintFSM.waiting_screenshot)
    try:
        await callback.message.edit_text(f"⚠️ Категория: <b>{label}</b>\n\n📎 Отправь скриншот (фото).",
                                         reply_markup=_cancel_kb())
    except Exception:
        await callback.message.answer(f"⚠️ Категория: <b>{label}</b>\n\n📎 Отправь скриншот (фото).")
    await callback.answer()


@router.message(ComplaintFSM.waiting_screenshot)
async def fsm_screenshot(message: Message, state: FSMContext) -> None:
    if not message.photo:
        await message.answer("❌ Отправь именно фото (скриншот).")
        return
    await state.update_data(screenshot_id=message.photo[-1].file_id)
    await state.set_state(ComplaintFSM.waiting_comment)
    await message.answer("💬 Теперь напиши комментарий к жалобе.")


@router.message(ComplaintFSM.waiting_comment)
async def fsm_comment(message: Message, state: FSMContext) -> None:
    comment = (message.text or "").strip()
    if not comment:
        await message.answer("❌ Комментарий не может быть пустым.")
        return
    data = await state.get_data()
    complaint_id = create_complaint(message.from_user.id, message.from_user.username or "",
                                    data.get("category", "Другое"),
                                    data.get("screenshot_id", ""), comment)
    await state.clear()
    await message.answer(f"✅ <b>Жалоба #{complaint_id} отправлена.</b>\n\nМы рассмотрим её в ближайшее время.")
    await _notify_admin(message.bot, complaint_id, data.get("category", ""), comment)
async def _notify_admin(bot, complaint_id: int, category: str, comment: str) -> None:
    if not OWNER_ID:
        return
    try:
        await bot.send_message(
            OWNER_ID,
            f"🆕 <b>Новая жалоба #{complaint_id}</b>\n"
            f"Категория: <b>{category}</b>\nКомментарий: {comment}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Открыть жалобу",
                                     callback_data=f"comp_view_{complaint_id}")]
            ]),
        )
    except Exception:
        pass


async def _notify_user(bot, user_id: int, complaint_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, f"ℹ️ <b>Ваша жалоба #{complaint_id}</b>\n{text}")
    except Exception:
        pass


@router.callback_query(F.data == "complaints_admin")
async def cb_complaints_admin(callback: CallbackQuery) -> None:
    if not is_super_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    complaints = get_complaints()
    if not complaints:
        await render_callback(callback, "📨 <b>Жалобы</b>\n\nПока нет жалоб.", admin_kb())
        return
    await render_callback(callback, f"📨 <b>Жалобы</b> ({len(complaints)})",
                          complaints_admin_kb(complaints))


@router.callback_query(F.data.startswith("comp_view_"))
async def cb_complaint_view(callback: CallbackQuery) -> None:
    if not is_super_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    cid = int(callback.data.split("_")[-1])
    c = get_complaint(cid)
    if not c:
        await callback.answer("Жалоба не найдена")
        return
    txt = (f"📨 <b>Жалоба #{c['id']}</b>\n\n"
           f"👤 Податель: <code>{c['user_id']}</code>\n"
           f"📂 Категория: {c['category'] or '—'}\n"
           f"💬 Комментарий: {c['comment'] or '—'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"comp_acc_{cid}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"comp_rej_{cid}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="complaints_admin")],
    ])
    if c.get("screenshot_id"):
        await callback.message.answer_photo(c["screenshot_id"], caption=txt, reply_markup=kb)
    else:
        await callback.message.answer(txt, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("comp_acc_"))
async def cb_accept(callback: CallbackQuery) -> None:
    if not is_super_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    cid = int(callback.data.split("_")[-1])
    c = get_complaint(cid)
    if c:
        set_complaint_status(cid, "accepted")
        await _notify_user(callback.bot, c["user_id"], cid, "✅ Ваша жалоба принята.")
    await callback.answer("Принято")


@router.callback_query(F.data.startswith("comp_rej_"))
async def cb_reject(callback: CallbackQuery) -> None:
    if not is_super_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    cid = int(callback.data.split("_")[-1])
    c = get_complaint(cid)
    if c:
        set_complaint_status(cid, "rejected")
        await _notify_user(callback.bot, c["user_id"], cid, "❌ Ваша жалоба отклонена.")
    await callback.answer("Отклонено")