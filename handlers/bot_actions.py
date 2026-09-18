from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from handlers._common import render_callback, safe_edit, cb_data, cb_uid, try_edit
from services.storage import (
    get_user_bots,
    get_bot_by_id,
    remove_user_bot,
    update_bot_field,
    bot_display_name,
    get_stats,
    get_antispam_mode,
    set_antispam_mode,
    set_bot_anonymous,
)
from services.child_manager import ChildManager
from handlers.my_bots import my_bots_kb

router = Router()


# Клавиатура, когда нужно вернуть юзера в главное меню (inline).
def main_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")]
    ])


def _single_bot_text(bot_info: dict, is_running: bool) -> str:
    """Текст карточки бота в меню бота."""
    name = bot_display_name(bot_info)
    status = "🟢 Работает" if is_running else "🔴 Остановлен"
    welcome = bot_info.get("welcome_text", "") or "— не задано —"
    # Показываем владельцу, что приветствие оформлено фото или статьёй.
    if str(bot_info.get("welcome_rich") or "").strip():
        welcome = "📰 [статья] " + welcome
    elif str(bot_info.get("welcome_photo") or "").strip():
        welcome = "🖼 [фото] " + welcome
    anon = "🟢 вкл" if bool(bot_info.get("anonymous_mode", 0)) else "⚪ выкл"

    return (
        f"🤖 <b>{name}</b>\n"
        f"🆔 <code>{bot_info['id']}</code>\n"
        f"Статус: {status}\n"
        f"🕶 Анонимный режим: {anon}\n\n"
        f"💬 Приветствие:\n{welcome}\n\n"
        f"Выбери действие:"
    )


def single_bot_kb(bot_id: int, is_running: bool, anon_mode: bool = False) -> InlineKeyboardMarkup:
    stop_text = "⛔ Остановить" if is_running else "▶️ Запустить"
    stop_data = f"action_stop_{bot_id}" if is_running else f"action_start_{bot_id}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📨 Рассылка", callback_data=f"mailing_{bot_id}", style="primary"),
                InlineKeyboardButton(text="🛡 Антиспам", callback_data=f"antispam_{bot_id}", style="primary"),
                InlineKeyboardButton(text="🕶 Аноним", callback_data=f"action_anon_{bot_id}", style="primary"),
            ],
            [InlineKeyboardButton(text=stop_text, callback_data=stop_data, style=("danger" if is_running else "success"))],
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data=f"stats_{bot_id}", style="primary"),
                InlineKeyboardButton(text="✏️ Редактор", callback_data=f"editor_{bot_id}", style="primary"),
                InlineKeyboardButton(text="📋 ПЗ", callback_data=f"pz_{bot_id}", style="primary"),
            ],
            [InlineKeyboardButton(text="⚙️ Конфиг", callback_data=f"cfg_menu_{bot_id}",
                                  style="primary")],
            [InlineKeyboardButton(text="🗑 Удалить бота", callback_data=f"action_delete_{bot_id}", style="danger")],
            [InlineKeyboardButton(text="⬅️ Назад к ботам", callback_data="my_bots", style="primary")],
        ]
    )


@router.callback_query(F.data.startswith("bot_"))
async def cb_single_bot(callback: CallbackQuery,
                        child_manager: ChildManager) -> None:
    bot_id = int(cb_data(callback).split("_", 1)[1])
    user_id = cb_uid(callback)

    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info is None:
        if callback.message:
            await try_edit(callback.message, "⚠️ Бот не найден.", reply_markup=main_inline_kb())
        await callback.answer()
        return

    running = child_manager.is_running(bot_id)
    text = _single_bot_text(bot_info, running)

    await render_callback(callback, text, single_bot_kb(bot_id, running, bool(bot_info.get("anonymous_mode", 0))))



@router.callback_query(F.data.startswith("action_stop_"))
async def cb_stop_bot(callback: CallbackQuery,
                      child_manager: ChildManager) -> None:
    bot_id = int(cb_data(callback).split("_")[-1])
    user_id = cb_uid(callback)

    await child_manager.stop_child(bot_id)
    update_bot_field(user_id, bot_id, "stopped", 1)
    await callback.answer("⛔ Бот остановлен")

    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info:
        text = _single_bot_text(bot_info, False)
        await safe_edit(callback.message, text, single_bot_kb(bot_id, False, bool(bot_info.get("anonymous_mode", 0))))


@router.callback_query(F.data.startswith("action_start_"))
async def cb_start_bot(callback: CallbackQuery,
                       child_manager: ChildManager) -> None:
    bot_id = int(cb_data(callback).split("_")[-1])
    user_id = cb_uid(callback)

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    update_bot_field(user_id, bot_id, "stopped", 0)
    started = await child_manager.start_child(bot_info)

    if started:
        await callback.answer("▶️ Бот запущен")
    else:
        await callback.answer("⚠️ Не удалось запустить")

    if callback.message:
        running = child_manager.is_running(bot_id)
        text = _single_bot_text(bot_info, running)
        await safe_edit(callback.message, text, single_bot_kb(bot_id, running, bool(bot_info.get("anonymous_mode", 0))))


# ═══════════════ Анонимный режим ═══════════════

ANON_DESCRIPTION = (
    "🕶 <b>Анонимный режим</b>\n\n"
    "При включении этого режима бот скрывает пользователей:\n"
    "• юзеры исчезают из списков и поиска ПЗ;\n"
    "• в топиках вместо имени пишется «Новое сообщение 🕶»;\n"
    "• списки и пагинация ПЗ бота скрываются.\n\n"
    "⚠️ <b>Важно:</b> выключить анонимный режим после включения "
    "будет НЕЛЬЗЯ. Если передумал — просто нажми «Отмена»."
)


@router.callback_query(F.data.startswith("action_anon_"))
async def cb_anon_info(callback: CallbackQuery) -> None:
    """Показывает описание анонимного режима и просит подтвердить включение."""
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    user_id = cb_uid(callback)

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    if bool(bot_info.get("anonymous_mode", 0)):
        await callback.answer("🕶 Анонимный режим уже включён", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Включай", callback_data=f"anon_confirm_{bot_id}", style="success"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"anon_cancel_{bot_id}"),
        ],
    ])
    await render_callback(callback, ANON_DESCRIPTION, kb)


@router.callback_query(F.data.startswith("anon_confirm_"))
async def cb_anon_confirm(callback: CallbackQuery,
                          child_manager: ChildManager) -> None:
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    user_id = cb_uid(callback)

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    # Анонимный режим включается навсегда: 0 → 1. Выключить больше нельзя.
    set_bot_anonymous(user_id, bot_id, True)

    # Перезапускаем дочернего бота, чтобы он сразу подхватил новый режим.
    if child_manager.is_running(bot_id):
        await child_manager.restart_child(bot_info)

    bot_info = get_bot_by_id(user_id, bot_id) or bot_info
    running = child_manager.is_running(bot_id)
    text = _single_bot_text(bot_info, running)
    await safe_edit(callback.message, text, single_bot_kb(bot_id, running, True))
    await callback.answer("🕶 Анонимный режим включён")


@router.callback_query(F.data.startswith("anon_cancel_"))
async def cb_anon_cancel(callback: CallbackQuery,
                         child_manager: ChildManager) -> None:
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    user_id = cb_uid(callback)

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    running = child_manager.is_running(bot_id)
    text = _single_bot_text(bot_info, running)
    await safe_edit(callback.message, text, single_bot_kb(bot_id, running, False))
    await callback.answer("❌ Отменено")



# ═══════════════ Антиспам ═══════════════

def antispam_kb(bot_id: int, current_mode: str) -> InlineKeyboardMarkup:
    modes = {
        "off": "⚪ Выключен",
        "auto": "🟢 Авто (предупреждение + бан за спам)",
        "manual": "🟡 Ручной (1 сообщ/мин)",
    }

    rows = []
    for mode, label in modes.items():
        prefix = "✅ " if mode == current_mode else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{prefix}{label}",
                callback_data=f"setantispam_{bot_id}_{mode}", style="primary"
            )
        ])

    rows.append([
        InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}")
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("antispam_"))
async def cb_antispam(callback: CallbackQuery) -> None:
    bot_id = int(cb_data(callback).split("_", 1)[1])

    current = get_antispam_mode(bot_id)

    text = (
        "🛡 <b>Антиспам</b>\n\n"
        "<b>Авто:</b> бот выдает варн за 5 стикеров, при повторных 5 стикерах — навсегда банит юзера, закрывая тему с названием «🚫 бан спам»\n"
        "<b>Ручной:</b> жесткое ограничение 1 сообщение в минуту для всех в ЛС\n"
        "<b>Выключен:</b> без ограничений\n\n"
        f"Текущий режим: <b>{current}</b>\n\n"
        "Выбери режим:"
    )

    await render_callback(callback, text, antispam_kb(bot_id, current))


@router.callback_query(F.data.startswith("setantispam_"))
async def cb_set_antispam(callback: CallbackQuery,
                          child_manager: ChildManager) -> None:
    parts = cb_data(callback).split("_")
    bot_id = int(parts[1])
    mode = parts[2]
    user_id = cb_uid(callback)

    set_antispam_mode(user_id, bot_id, mode)

    # Перезапуск дочерки чтобы подхватить новый режим
    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info and child_manager.is_running(bot_id):
        await child_manager.restart_child(bot_info)

    mode_names = {"off": "Выключен", "auto": "Авто", "manual": "Ручной"}
    await callback.answer(f"🛡 Режим: {mode_names.get(mode, mode)}")

    text = (
        "🛡 <b>Антиспам</b>\n\n"
        f"Режим изменён на: <b>{mode_names.get(mode, mode)}</b>\n\n"
        "Выбери режим:"
    )

    await safe_edit(callback.message, text, antispam_kb(bot_id, mode))


# ═══════════════ Статистика ═══════════════

@router.callback_query(F.data.startswith("stats_"))
async def cb_stats(callback: CallbackQuery) -> None:
    bot_id = int(cb_data(callback).split("_", 1)[1])
    user_id = cb_uid(callback)

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    s = get_stats(bot_id)
    name = bot_display_name(bot_info)

    text = (
        f"📊 <b>Статистика — {name}</b>\n\n"
        f"👥 Всего пользователей: <b>{s['users_total']}</b>\n"
        f"🚫 Заблокировали бота: <b>{s['users_blocked']}</b>\n"
        f"✅ Активных: <b>{s['users_active']}</b>\n\n"
        f"📩 Получено сообщений: <b>{s['messages_in']}</b>\n"
        f"📤 Отправлено сообщений: <b>{s['messages_out']}</b>\n\n"
        f"📨 Рассылок всего: <b>{s['mailings_count']}</b>\n"
        f"  ├ Доставлено: <b>{s['mailings_sent']}</b>\n"
        f"  └ Не доставлено: <b>{s['mailings_failed']}</b>\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"stats_{bot_id}", style="primary")],
        [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}")],
    ])

    await render_callback(callback, text, kb)


# ═══════════════ Удаление ═══════════════

@router.callback_query(F.data.startswith("action_delete_"))
async def cb_delete_bot(callback: CallbackQuery,
                        child_manager: ChildManager) -> None:
    bot_id = int(cb_data(callback).split("_")[-1])
    user_id = cb_uid(callback)

    await child_manager.stop_child(bot_id)
    removed = remove_user_bot(user_id, bot_id)

    if removed:
        text = "🗑 <b>Бот удалён.</b>"
    else:
        text = "⚠️ Бот не найден."

    bots = get_user_bots(user_id)
    if bots:
        text += f"\n\nОсталось ботов: {len(bots)}"
        kb = my_bots_kb(user_id, child_manager)
    else:
        text += "\n\nУ тебя больше нет подключённых ботов."
        kb = main_inline_kb()

    await render_callback(callback, text, kb)