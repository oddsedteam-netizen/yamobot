"""Раздел «🔔 Мои уведомления»: от каких ботов получать уведомления.

Зачем
-----
Реальная жалоба владельцев: в «чат админов» прилетают уведомления о новых ПЗ
и напоминания по ботам поддержки и анкетниц, а в чате админов их обсуждать
некому. Раздел выключает уведомления ПО БОТУ — у кого-то лишним окажется
один бот, а у кого-то половина.

Что можно выключить
------------------
* «🔔 Уведомлять о новом ПЗ» — сообщение «🆕 Новый ПЗ» в чат админов;
* «⏰ Напоминания» — «ПЗ без ответа» и «ПЗ без админа».

Что остаётся всегда: антинакрутка, смена админа, норма админов, защита — это
разовые тревоги, молчать о них нельзя.
"""

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from handlers._common import cb_data, cb_uid, render_callback
from services.storage import (
    bot_display_name,
    get_notify_settings_map,
    get_user_bots,
    set_bot_notify_field,
)

router = Router()

# Что переключаем и как это выглядит в кнопке.
_FIELDS = ("notify_new_pz", "notify_reminders")


def _fit(text: str, limit: int = 40) -> str:
    """Обрезает подпись кнопки: Telegram режет текст длиннее 64 символов."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def notify_payload(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура раздела «🔔 Мои уведомления»."""
    bots = get_user_bots(owner_id)
    if not bots:
        return (
            "🔔 <b>Мои уведомления</b>\n\n"
            "У тебя пока нет ботов — настраивать нечего.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show",
                                      style="primary"),
            ]]),
        )

    settings = get_notify_settings_map(owner_id, [int(b["id"]) for b in bots])

    lines = [
        "🔔 <b>Мои уведомления</b>\n",
        "От каких ботов присылать уведомления в «чат админов».",
        "Выключил — бот не пишет про новые ПЗ и напоминания по этому боту.\n",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for bot in bots:
        bot_id = int(bot["id"])
        st = settings.get(bot_id, {})
        title = _fit(bot_display_name(bot))

        pz_on = st.get("notify_new_pz", True)
        rem_on = st.get("notify_reminders", True)
        lines.append(
            f"\n🤖 <b>{bot_display_name(bot)}</b>\n"
            f"   🆕 новые ПЗ: {'🔔 присылать' if pz_on else '🔕 молчать'}\n"
            f"   ⏰ напоминания: {'🔔 присылать' if rem_on else '🔕 молчать'}"
        )
        rows.append([
            InlineKeyboardButton(
                text=f"{'🔔' if pz_on else '🔕'} ПЗ · {title}",
                callback_data=f"notify_toggle_pz_{bot_id}",
                # Зелёный = уведомления идут, серый (без цвета) = выключены.
                style="success" if pz_on else "primary",
            ),
            InlineKeyboardButton(
                text=f"{'⏰' if rem_on else '🔕'} Напом. · {title}",
                callback_data=f"notify_toggle_rem_{bot_id}",
                style="primary" if rem_on else "primary",
            ),
        ])

    rows.append([InlineKeyboardButton(text="⬅️ Профиль",
                                      callback_data="profile_show",
                                      style="primary")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "profile_notify")
async def cb_profile_notify(callback: CallbackQuery) -> None:
    """Открывает «🔔 Мои уведомления»."""
    text, kb = notify_payload(cb_uid(callback))
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^notify_toggle_(pz|rem)_\d+$"))
async def cb_notify_toggle(callback: CallbackQuery) -> None:
    """Переключает один вид уведомлений по одному боту."""
    kind, raw_bot_id = cb_data(callback).split("_")[2:4]
    bot_id = int(raw_bot_id)
    field = "notify_new_pz" if kind == "pz" else "notify_reminders"
    owner_id = cb_uid(callback)

    current = get_notify_settings_map(owner_id, [bot_id]).get(bot_id, {}).get(
        field, True)
    set_bot_notify_field(owner_id, bot_id, field, not current)

    what = "уведомления о новых ПЗ" if kind == "pz" else "напоминания"
    if current:
        await callback.answer(f"🔕 {what.capitalize()} выключены")
    else:
        await callback.answer(f"🔔 {what.capitalize()} включены")

    text, kb = notify_payload(owner_id)
    await render_callback(callback, text, kb)