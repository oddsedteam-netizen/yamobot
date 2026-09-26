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

from handlers._common import render_callback, safe_edit, cb_data, cb_uid, try_edit, msg_uid
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
    get_bound_chat,
    get_feedback_chat,
    clear_feedback_chat,
    get_cat_ask_settings,
    set_cat_ask_enabled,
    set_cat_ask_categories,
    get_cat_custom,
    add_custom_category,
    remove_custom_category,
    toggle_custom_category,
    get_admin_change_settings,
    set_admin_change_enabled,
    set_admin_change_limit,
    DEFAULT_ADMIN_CHANGE_LIMIT,
    MAX_ADMIN_CHANGE_LIMIT,
    MAX_CUSTOM_CATEGORIES,
    DEFAULT_PZ_CATEGORIES,
)
from services.child_manager import ChildManager
from handlers.my_bots import my_bots_kb

router = Router()
logger = logging.getLogger(__name__)


class BotActionsFSM(StatesGroup):
    waiting_custom_cat = State()   # ввод названия своей категории
    waiting_change_limit = State()  # ввод лимита смен админа


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
    ask_on, _ask_cats = get_cat_ask_settings(bot_info["id"])
    cat_ask = "🟢 вкл" if ask_on else "⚪ выкл"

    return (
        f"🤖 <b>{name}</b>\n"
        f"🆔 <code>{bot_info['id']}</code>\n"
        f"Статус: {status}\n"
        f"🕶 Анонимный режим: {anon}\n"
        f"🏷 Уточнение категории: {cat_ask}\n\n"
        f"💬 Приветствие:\n{welcome}\n\n"
        f"Выбери действие:"
    )


def single_bot_kb(bot_id: int, is_running: bool, anon_mode: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура карточки бота.

    Раскладка «сверху вниз»: сверху самые частые и крупные кнопки
    (рассылка, редактор, статистика), ниже мелкие парами (антиспам/аноним,
    ПЗ/конфиг), в самом конце — опасные действия (остановка, удаление).
    Кнопка на всю ширину = «крупная», две в ряд = «мелкие».
    """
    stop_text = "⛔ Остановить" if is_running else "▶️ Запустить"
    stop_data = f"action_stop_{bot_id}" if is_running else f"action_start_{bot_id}"
    stop_style = "danger" if is_running else "success"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            # ── Крупные кнопки ──
            [InlineKeyboardButton(text="📨 Рассылка", callback_data=f"mailing_{bot_id}")],
            [InlineKeyboardButton(text="✏️ Редактор", callback_data=f"editor_{bot_id}",
                                  style="success")],
            [
                InlineKeyboardButton(text="🛡 Антиспам", callback_data=f"antispam_{bot_id}",
                                     style="success"),
                InlineKeyboardButton(text="🕶 Аноним", callback_data=f"action_anon_{bot_id}",
                                     style="primary"),
            ],
            [InlineKeyboardButton(text="📊 Статистика", callback_data=f"stats_{bot_id}",
                                  style="success")],
            [
                InlineKeyboardButton(text="📋 ПЗ", callback_data=f"pz_{bot_id}"),
                InlineKeyboardButton(text="⚙️ Конфиг", callback_data=f"cfg_menu_{bot_id}"),
            ],
            [InlineKeyboardButton(text="🏷 Уточнение категории", callback_data=f"catask_{bot_id}",
                                  style="success")],
            [InlineKeyboardButton(text="🔄 Смена админа", callback_data=f"admchg_{bot_id}",
                                  style="primary")],
            [InlineKeyboardButton(text="🔗 Перепривязка", callback_data=f"rebind_{bot_id}",
                                  style="primary")],
            [InlineKeyboardButton(text=stop_text, callback_data=stop_data, style=stop_style)],
            [InlineKeyboardButton(text="🗑 Удалить бота", callback_data=f"action_delete_{bot_id}",
                                  style="danger")],
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


# ═══════════════ Уточнение категории ПЗ ═══════════════

CAT_ASK_TEXT = (
    "🏷 <b>Уточнение категории</b>\n\n"
    "Настройка помогает YamoBot понять, какая категория нужна юзеру.\n\n"
    "<b>Как это работает:</b>\n"
    "1️⃣ Юзер пишет боту первое сообщение (после приветствия).\n"
    "2️⃣ Если категории в сообщении нет — например, он пишет просто «привет» — "
    "бот присылает ему уточнение с кнопками: какая категория админов нужна "
    "(поддержка, универсал или общение).\n"
    "3️⃣ Как только юзер выбрал категорию, бот присылает в «чат админов» "
    "уведомление о ПЗ вместе с категорией.\n\n"
    "Если юзер назвал категорию сразу («привет, #поддержка» или просто "
    "«поддержка») — уточнение не показывается, уведомление уходит сразу.\n\n"
    "Категории можно настроить: отключи те, которые боту не нужны "
    "(например, если бот только про общение — поддержку можно выключить)."
)


def cat_ask_text(enabled: bool, categories: list[str]) -> str:
    """Текст экрана «Уточнение категории»: описание + статус и категории."""
    status = "🟢 включено" if enabled else "⚪ выключено"
    cats_line = ", ".join(f"#{c}" for c in categories) or "—"
    return (
        f"{CAT_ASK_TEXT}\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Категории: {cats_line}"
    )


def cat_ask_kb(bot_id: int, enabled: bool) -> InlineKeyboardMarkup:
    """Кнопки экрана «Уточнение категории»: настроить / вкл-выкл / назад."""
    if enabled:
        toggle = InlineKeyboardButton(text="🔴 Выключить",
                                      callback_data=f"catask_toggle_{bot_id}",
                                      style="danger")
    else:
        toggle = InlineKeyboardButton(text="🟢 Включить",
                                      callback_data=f"catask_toggle_{bot_id}",
                                      style="success")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настроить категории",
                              callback_data=f"catask_cats_{bot_id}", style="primary")],
        [toggle],
        [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}",
                              style="primary")],
    ])


CAT_ASK_NO_CHAT_TEXT = (
    "⚠️ <b>Чат админов не привязан</b>\n\n"
    "Уточнение категории не сможет включиться: уведомления о ПЗ приходят именно "
    "в <b>чат админов</b>, и без него категорию просто некуда отправлять.\n\n"
    "Сначала привяжи чат админов, потом вернись сюда и включи функцию."
)


def _cat_ask_no_chat_kb(bot_id: int) -> InlineKeyboardMarkup:
    """Клавиатура, когда для включения нужно привязать «чат админов»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Привязать чат админов", callback_data="bind_admin",
                              style="primary")],
        [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}",
                              style="primary")],
    ])


def cat_ask_catalog(categories: list[str]) -> list[str]:
    """Список для экрана настройки: стандартные категории + сохранённые.

    Стандартные показываем всегда — иначе выключенную категорию нельзя было бы
    включить обратно.
    """
    catalog = list(DEFAULT_PZ_CATEGORIES)
    for name in categories:
        if name not in catalog:
            catalog.append(name)
    return catalog


def cat_ask_list_kb(bot_id: int, catalog: list[str],
                    categories: list[str]) -> InlineKeyboardMarkup:
    """Кнопки выбора категорий: ✅ — предлагаем ПЗ, ⚪ — не предлагаем."""
    rows: list[list[InlineKeyboardButton]] = []
    for index, name in enumerate(catalog):
        mark = "✅" if name in categories else "⚪"
        rows.append([
            InlineKeyboardButton(text=f"{mark} #{name}",
                                 callback_data=f"catask_cat_{bot_id}_{index}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Свои категории",
                                      callback_data=f"catown_list_{bot_id}",
                                      style="success")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"catask_{bot_id}",
                                      style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═══════════════ Свои категории (до 3): добавить / выключить / удалить ═══════════════

CUSTOM_CATS_TEXT = (
    "➕ <b>Свои категории</b>\n\n"
    f"Можно добавить до <b>{MAX_CUSTOM_CATEGORIES}</b> своих категорий к "
    "стандартным. Выключенные (<b>⚪</b>) бот ПЗ не предлагает, удалённые "
    "исчезают совсем.\n\n"
    "Кнопки в сообщении ПЗ выстраиваются компактно, поэтому длинные названия "
    "не растягивают сообщение."
)


def custom_cats_kb(bot_id: int, items: list[dict]) -> InlineKeyboardMarkup:
    """Список своих категорий с переключателем и удалением."""
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(items):
        name = item["name"]
        mark = "🟢" if item.get("active") else "⚪"
        rows.append([
            InlineKeyboardButton(text=f"{mark} #{name}",
                                 callback_data=f"catown_toggle_{bot_id}_{index}",
                                 style="success" if item.get("active") else None),
            InlineKeyboardButton(text="🗑", callback_data=f"catown_del_{bot_id}_{index}",
                                 style="danger"),
        ])
    if len(items) < MAX_CUSTOM_CATEGORIES:
        rows.append([InlineKeyboardButton(text="➕ Добавить категорию",
                                          callback_data=f"catown_add_{bot_id}",
                                          style="success")])
    rows.append([InlineKeyboardButton(text="⬅️ К стандартным",
                                      callback_data=f"catask_cats_{bot_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _custom_cats_text(items: list[dict]) -> str:
    text = CUSTOM_CATS_TEXT
    if not items:
        return text + "\n\nПока своих категорий нет."
    text += "\n\n" + "\n".join(
        f"  • <b>#{item['name']}</b> — {'включена' if item['active'] else 'выключена'}"
        for item in items
    )
    return text


def _safe_int(value: str | None, default: int = 0) -> int:
    """Мягкое приведение к int: битая кнопка не должна ронять бота."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@router.callback_query(F.data.startswith("catown_"))
async def cb_catown(callback: CallbackQuery, state: FSMContext) -> None:
    """Свои категории: список, добавление, вкл/выкл, удаление.

    Разбор callback_data строго по месту: у «catown_list_12» номер бота стоит
    в конце, а у «catown_toggle_12_0» — на втором месте, поэтому берём его
    по позиции действия, а не «как попалось».
    """
    data = cb_data(callback)
    user_id = cb_uid(callback)

    # ВНИМАНИЕ: разделять по «_» нельзя — в имени действия есть подчёркивание
    # («catown_list_12» → ['catown', 'list', '12']). Поэтому действие узнаём по
    # префиксу, а номер бота всегда берём с конца строки.
    for action in ("catown_list", "catown_back", "catown_add",
                   "catown_toggle", "catown_del"):
        if data.startswith(f"{action}_"):
            break
    else:
        return

    if action in ("catown_add", "catown_back", "catown_list"):
        bot_id = _safe_int(data.rsplit("_", 1)[-1])
        if not get_bot_by_id(user_id, bot_id):
            await callback.answer("⚠️ Бот не найден", show_alert=True)
            return

        if action == "catown_add":
            if len(get_cat_custom(user_id, bot_id)) >= MAX_CUSTOM_CATEGORIES:
                await callback.answer(
                    f"⚠️ Уже добавлено {MAX_CUSTOM_CATEGORIES} категорий — это лимит",
                    show_alert=True,
                )
                return
            await state.set_state(BotActionsFSM.waiting_custom_cat)
            await state.update_data(catown_bot_id=bot_id)
            await render_callback(
                callback,
                "➕ <b>Новая категория</b>\n\n"
                "Напиши <b>название</b> — одним словом или короткой фразой.\n\n"
                f"Добавить можно не больше {MAX_CUSTOM_CATEGORIES} своих категорий.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена",
                                          callback_data=f"catown_back_{bot_id}",
                                          style="primary")]
                ]),
            )
            await callback.answer()
            return

        if action == "catown_back":
            _enabled, base = get_cat_ask_settings(bot_id)
            await render_callback(callback, CAT_ASK_LIST_TEXT,
                                  cat_ask_list_kb(bot_id, cat_ask_catalog(base), base))
            return

        items = get_cat_custom(user_id, bot_id)
        await render_callback(callback, _custom_cats_text(items),
                              custom_cats_kb(bot_id, items))
        return

    # Действия с индексом: catown_toggle_<bot_id>_<index>, catown_del_<bot_id>_<index>
    tail = data[len(action) + 1:].split("_")
    bot_id = _safe_int(tail[0] if tail else None)
    if not get_bot_by_id(user_id, bot_id):
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    items = get_cat_custom(user_id, bot_id)
    index = _safe_int(tail[1] if len(tail) > 1 else None, -1)
    if not (0 <= index < len(items)):
        await callback.answer("⚠️ Категория не найдена", show_alert=True)
        return

    if action == "catown_toggle":
        toggle_custom_category(user_id, bot_id, items[index]["name"])
        items = get_cat_custom(user_id, bot_id)
        await callback.answer(
            "🟢 Включено" if items[index]["active"] else "⚪ Выключено"
        )
    elif action == "catown_del":
        remove_custom_category(user_id, bot_id, items[index]["name"])
        items = get_cat_custom(user_id, bot_id)
        await callback.answer("🗑 Категория удалена")

    await render_callback(callback, _custom_cats_text(items), custom_cats_kb(bot_id, items))


@router.message(BotActionsFSM.waiting_custom_cat)
async def fsm_custom_cat(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = msg_uid(message)
    bot_id = int(data.get("catown_bot_id", 0))

    if not get_bot_by_id(user_id, bot_id):
        await state.clear()
        await message.answer("⚠️ Бот не найден")
        return

    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название не может быть пустым. Напиши его ещё раз.")
        return

    _ok, answer = add_custom_category(user_id, bot_id, name)
    await state.clear()

    items = get_cat_custom(user_id, bot_id)
    await message.answer(f"{answer}\n\n{_custom_cats_text(items)}",
                         reply_markup=custom_cats_kb(bot_id, items))


CAT_ASK_LIST_TEXT = (
    "⚙️ <b>Категории для уточнения</b>\n\n"
    "Нажми на категорию, чтобы включить или выключить её. "
    "<b>✅</b> — бот предлагает её юзеру, <b>⚪</b> — не предлагает.\n\n"
    "Например, если бот только про общение — выключи «поддержку», и её не будет "
    "среди кнопок уточнения.\n\n"
    "⚠️ Хотя бы одна категория должна остаться включённой."
)


@router.callback_query(F.data.regexp(r"^catask_\d+$"))
async def cb_cat_ask(callback: CallbackQuery) -> None:
    """Экран «🏷 Уточнение категории»."""
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    user_id = cb_uid(callback)

    if not get_bot_by_id(user_id, bot_id):
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    enabled, categories = get_cat_ask_settings(bot_id)
    await render_callback(callback, cat_ask_text(enabled, categories),
                          cat_ask_kb(bot_id, enabled))


@router.callback_query(F.data.startswith("catask_toggle_"))
async def cb_cat_ask_toggle(callback: CallbackQuery) -> None:
    """Включает/выключает уточнение категории (включение требует чат админов)."""
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    user_id = cb_uid(callback)

    if not get_bot_by_id(user_id, bot_id):
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    enabled, categories = get_cat_ask_settings(bot_id)

    if not enabled and not get_bound_chat(user_id, "admin"):
        # Включать некуда: уведомления о ПЗ уходят в «чат админов».
        await render_callback(callback, CAT_ASK_NO_CHAT_TEXT,
                              _cat_ask_no_chat_kb(bot_id), force_answer=True)
        return

    set_cat_ask_enabled(user_id, bot_id, not enabled)
    enabled, categories = get_cat_ask_settings(bot_id)
    await callback.answer("🟢 Уточнение включено" if enabled else "🔴 Уточнение выключено")
    await safe_edit(callback.message, cat_ask_text(enabled, categories),
                    cat_ask_kb(bot_id, enabled))


@router.callback_query(F.data.startswith("catask_cats_"))
async def cb_cat_ask_cats(callback: CallbackQuery) -> None:
    """Экран настройки категорий: какие предлагать ПЗ, а какие нет."""
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    user_id = cb_uid(callback)

    if not get_bot_by_id(user_id, bot_id):
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    _enabled, categories = get_cat_ask_settings(bot_id)
    catalog = cat_ask_catalog(categories)
    await render_callback(callback, CAT_ASK_LIST_TEXT,
                          cat_ask_list_kb(bot_id, catalog, categories))


@router.callback_query(F.data.regexp(r"^catask_cat_\d+_\d+$"))
async def cb_cat_ask_cat_toggle(callback: CallbackQuery) -> None:
    """Переключает одну категорию в настройке уточнения."""
    parts = cb_data(callback).split("_")
    bot_id = int(parts[2])
    index = int(parts[3])
    user_id = cb_uid(callback)

    if not get_bot_by_id(user_id, bot_id):
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    _enabled, categories = get_cat_ask_settings(bot_id)
    catalog = cat_ask_catalog(categories)
    if index >= len(catalog):
        await callback.answer("⚠️ Категория не найдена", show_alert=True)
        return

    name = catalog[index]
    if name in categories:
        if len(categories) <= 1:
            await callback.answer(
                "⚠️ Нужна хотя бы одна категория — иначе спросить у ПЗ будет нечего.",
                show_alert=True,
            )
            return
        categories = [c for c in categories if c != name]
        await callback.answer(f"⚪ #{name} выключена")
    else:
        # Храним в порядке справочника, чтобы список не «прыгал».
        wanted = set(categories) | {name}
        categories = [c for c in catalog if c in wanted]
        await callback.answer(f"✅ #{name} включена")

    set_cat_ask_categories(user_id, bot_id, categories)
    _enabled, categories = get_cat_ask_settings(bot_id)
    await safe_edit(callback.message, CAT_ASK_LIST_TEXT,
                    cat_ask_list_kb(bot_id, catalog, categories))


# ═══════════════ Смена админа: лимит смен для ПЗ в сутки ═══════════════

def _adm_change_text(bot_id: int) -> str:
    enabled, limit = get_admin_change_settings(bot_id)
    status = "🟢 включено" if enabled else "⚪ выключено"
    return (
        "🔄 <b>Смена админа</b>\n\n"
        "Если юзеру не ответил админ, он может попросить заменить его — кнопкой "
        "«сменить админа» или командой в боте. Так можно бесконечно дёргать "
        "админов, поэтому бот ставит <b>ограничение на количество смен в "
        "сутки</b>.\n\n"
        f"📊 По умолчанию разрешено <b>{DEFAULT_ADMIN_CHANGE_LIMIT} смен</b> в "
        "сутки, число можно изменить. Когда лимит исчерпан, ПЗ покажет, что "
        "смены на сегодня закончились, и счётчик обнулится через сутки.\n\n"
        f"📌 Сейчас: <b>{status}</b>"
        + (f", лимит — <b>{limit}</b> смен в сутки.\n\n" if enabled else ".\n\n")
        + "Когда выключено — смены не ограничены вовсе."
    )


def _adm_change_kb(bot_id: int) -> InlineKeyboardMarkup:
    enabled, limit = get_admin_change_settings(bot_id)
    toggle = InlineKeyboardButton(
        text="🔴 Выключить" if enabled else "🟢 Включить",
        callback_data=f"admchg_set_{bot_id}_{0 if enabled else 1}",
        style="danger" if enabled else "success",
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [toggle],
        [InlineKeyboardButton(text=f"🔢 Сколько смен в сутки (сейчас {limit})",
                              callback_data=f"admchg_num_{bot_id}", style="primary")],
        [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}",
                              style="primary")],
    ])


@router.callback_query(F.data.regexp(r"^admchg_\d+$"))
async def cb_adm_change(callback: CallbackQuery) -> None:
    """Экран «🔄 Смена админа»: объяснение + вкл/выкл + количество."""
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    if not get_bot_by_id(cb_uid(callback), bot_id):
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return
    await render_callback(callback, _adm_change_text(bot_id), _adm_change_kb(bot_id))


@router.callback_query(F.data.regexp(r"^admchg_set_\d+_[01]$"))
async def cb_adm_change_toggle(callback: CallbackQuery) -> None:
    """Включает/выключает ограничение смен админа."""
    data = cb_data(callback)
    bot_id = int(data.split("_")[2])
    enabled = data.endswith("_1")
    user_id = cb_uid(callback)

    if not get_bot_by_id(user_id, bot_id):
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    set_admin_change_enabled(user_id, bot_id, enabled)
    await callback.answer("🟢 Включено" if enabled else "🔴 Выключено")
    await render_callback(callback, _adm_change_text(bot_id), _adm_change_kb(bot_id))


@router.callback_query(F.data.regexp(r"^admchg_num_\d+$"))
async def cb_adm_change_num(callback: CallbackQuery, state: FSMContext) -> None:
    """Просит новое количество смен в сутки."""
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    if not get_bot_by_id(cb_uid(callback), bot_id):
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    await state.set_state(BotActionsFSM.waiting_change_limit)
    await state.update_data(admchg_bot_id=bot_id)
    await render_callback(
        callback,
        "🔢 <b>Сколько смен разрешить в сутки?</b>\n\n"
        "Напиши число от <b>1</b> до "
        f"<b>{MAX_ADMIN_CHANGE_LIMIT}</b>.\n\n"
        f"Например: <code>{DEFAULT_ADMIN_CHANGE_LIMIT}</code> — это значение "
        "по умолчанию.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admchg_{bot_id}",
                                  style="primary")]
        ]),
    )
    await callback.answer()


@router.message(BotActionsFSM.waiting_change_limit)
async def fsm_adm_change_limit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = msg_uid(message)
    bot_id = int(data.get("admchg_bot_id", 0))

    if not get_bot_by_id(user_id, bot_id):
        await state.clear()
        await message.answer("⚠️ Бот не найден")
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("❌ Нужно число. Напиши его ещё раз.")
        return

    value = int(raw)
    if not (1 <= value <= MAX_ADMIN_CHANGE_LIMIT):
        await message.answer(
            f"❌ Число должно быть от 1 до {MAX_ADMIN_CHANGE_LIMIT}. Напиши ещё раз."
        )
        return

    set_admin_change_limit(user_id, bot_id, value)
    set_admin_change_enabled(user_id, bot_id, True)
    await state.clear()
    await message.answer(
        f"✅ Сохранено: <b>{value}</b> смен в сутки, ограничение включено.",
        reply_markup=_adm_change_kb(bot_id),
    )


# ═══════════════ Перепривязка к другому рабочему чату ═══════════════

REBIND_TEXT = (
    "🔗 <b>Перепривязка к чату</b>\n\n"
    "Один бот обслуживает <b>один рабочий чат</b> с темами. Если чат нужно "
    "поменять — бот сейчас пишет «🚫 Этот бот уже привязан к другому чату», "
    "хотя старый чат тебе больше не нужен.\n\n"
    "<b>Что произойдёт:</b>\n"
    "• бот отвяжется от текущего рабочего чата;\n"
    "• постарается выйти из него, чтобы больше не писать туда;\n"
    "• бот перезапустится и сможет подключиться к новому чату.\n\n"
    "<b>Что НЕ произойдёт:</b>\n"
    "• история ПЗ, пользователи и статистика останутся на месте;\n"
    "• другие боты и их чаты не затрагиваются.\n\n"
    "⚠️ <b>Важно:</b> после перепривязки добавь бота в новый чат с "
    "<b>включёнными темами</b> — он подключится сам. До этого момента бот "
    "не сможет создавать топики."
)


@router.callback_query(F.data.regexp(r"^rebind_\d+$"))
async def cb_rebind_info(callback: CallbackQuery) -> None:
    """Экран «🔗 Перепривязка»: описание + подтверждение."""
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    user_id = cb_uid(callback)

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    current = get_feedback_chat(bot_id)
    current_line = (f"Сейчас привязан к: <code>{current}</code>"
                    if current else "Сейчас бот ни к какому чату не привязан.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Перепривязать", callback_data=f"rebind_confirm_{bot_id}",
                              style="danger")],
        [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}",
                              style="primary")],
    ])
    await render_callback(callback, f"{current_line}\n\n{REBIND_TEXT}", kb)


@router.callback_query(F.data.startswith("rebind_confirm_"))
async def cb_rebind_confirm(callback: CallbackQuery,
                            child_manager: ChildManager) -> None:
    """Отвязывает бота от рабочего чата (и выходит из него)."""
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    user_id = cb_uid(callback)

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    old_chat = get_feedback_chat(bot_id)
    was_running = child_manager.is_running(bot_id)
    left = False

    if old_chat:
        # Выходим из старого чата, чтобы бот не слал туда ПЗ. Если чат уже
        # удалён или бота выгнали — это не помеха: привязку снимаем в любом случае.
        child_bot = child_manager.get_bot(bot_id)
        if child_bot is not None:
            try:
                await child_bot.leave_chat(int(old_chat))
                left = True
            except Exception as e:
                logger.info("Не удалось выйти из чата %s: %s", old_chat, e)
        clear_feedback_chat(bot_id)

    # Перезапускаем, чтобы дочерний бот подхватил «чистое» состояние и
    # подключился к новому чату при следующем добавлении.
    if was_running:
        await child_manager.restart_child(bot_info)

    if old_chat:
        result = (
            f"✅ <b>Бот отвязан от чата</b> <code>{old_chat}</code>."
            + ("" if left else " Из чата выйти не удалось — привязка снята.")
            + "\n\nДобавь бота в новый чат с включёнными темами — он подключится сам."
        )
        await callback.answer("🔗 Перепривязка выполнена", show_alert=True)
    else:
        result = (
            "ℹ️ Бот и так не был привязан ни к одному чату.\n\n"
            "Добавь его в чат с включёнными темами — он подключится сам."
        )
        await callback.answer()

    bot_info = get_bot_by_id(user_id, bot_id) or bot_info
    running = child_manager.is_running(bot_id)
    text = _single_bot_text(bot_info, running)
    await render_callback(callback, f"{result}\n\n{text}",
                          single_bot_kb(bot_id, running, bool(bot_info.get("anonymous_mode", 0))))