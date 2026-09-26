"""Конфиги ботов и «Мои ссылки и конфиги».

Что здесь:
  • «⚙️ Конфиг» в карточке бота — сохранить слепок настроек бота под кодом
    или применить чужой/сохранённый конфиг на этого бота;
  • «📂 Мои ссылки и конфиги» в профиле — список сохранённых конфигов
    (с названиями ботов) и ссылок-приглашений админов с возможностью
    аннулировать/пересоздать их, а также очистить конфиг.

При применении конфига меняются только настройки (приветствие, инлайны,
тип бота, антиспам, анонимность, reply-кнопки). Статистика, имя бота, токен,
привязанные чаты, ПЗ и админы остаются как были.
"""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import cb_data, cb_uid, event_bot, msg_uid, render_callback
from services.bot_config import (
    CONFIG_SECTIONS,
    apply_bot_config,
    config_preview,
    snapshot_bot,
)
from services.child_manager import ChildManager
from services.storage import (
    bot_display_name,
    create_admin_invite,
    create_bot_config,
    delete_admin_invite,
    delete_bot_config,
    get_bot_by_id,
    get_bot_config,
    get_owner_admin_invites,
    get_user_bot_configs,
    get_user_bots,
    normalize_config_code,
    update_bot_config,
)

logger = logging.getLogger(__name__)

router = Router()


class ConfigFSM(StatesGroup):
    waiting_code = State()       # ждём код конфига
    waiting_sections = State()  # выбираем разделы для переноса


# ── Выбор разделов при переносе конфига ────────────────────────────────
# Хранится в состоянии FSM: код конфига, бот-получатель и набор разделов.

def _sections_kb(code: str, bot_id: int, chosen: set[str]) -> InlineKeyboardMarkup:
    """Кнопки-вкл/выкл по разделам конфига + подтверждение переноса."""
    rows: list[list[InlineKeyboardButton]] = []
    for key, title in CONFIG_SECTIONS:
        mark = "✅" if key in chosen else "⚪"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {title}",
            callback_data=f"cfg_sec_{code}_{bot_id}_{key}",
            style="success" if key in chosen else None,
        )])
    rows.append([
        InlineKeyboardButton(text="✅ Всё", callback_data=f"cfg_all_{code}_{bot_id}",
                              style="primary"),
        InlineKeyboardButton(text="⬜ Ничего", callback_data=f"cfg_none_{code}_{bot_id}",
                              style="primary"),
    ])
    rows.append([InlineKeyboardButton(text="📥 Перенести выбранное",
                                      callback_data=f"cfg_do_{code}_{bot_id}", style="success")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к конфигу",
                                      callback_data=f"cfg_view_{code}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _sections_text(code: str, bot_id: int, chosen: set[str], bot_name: str) -> str:
    """Экран выбора: что именно переносить из конфига в бота."""
    return (
        "📥 <b>Что перенести в бота?</b>\n\n"
        f"🤖 Бот: <b>{bot_name}</b>\n"
        f"🔑 Конфиг: <code>{code}</code>\n\n"
        "Нажми на раздел, чтобы включить или выключить его. "
        "✅ — настройки перенесутся, ⚪ — останутся как есть.\n\n"
        "🕐 <b>Время работы</b> и 📊 <b>Норма админов</b> — общие настройки "
        "для всех твоих ботов: они тоже переносятся, если включены.\n"
        "⌨️ Reply-кнопки переносятся вместе с типом бота — отдельно выбирать "
        "их не нужно.\n\n"
        f"Выбрано разделов: <b>{len(chosen)}</b> из {len(CONFIG_SECTIONS)}"
    )


def _config_state(data: dict) -> tuple[str, int, set[str]]:
    """Достаёт из FSM код, бот-получателя и выбранные разделы."""
    code = str(data.get("cfg_code") or "")
    bot_id = int(data.get("cfg_target_bot") or 0)
    chosen = {s for s in (data.get("cfg_sections") or []) if s}
    return code, bot_id, chosen


# ═══════════════ Инфо о конфиге ═══════════════

def _config_text(config: dict) -> str:
    """Подробности конфига (превью настроек)."""
    lines = config_preview(config.get("data"))
    return (
        f"⚙️ <b>Конфиг {config['code']}</b>\n\n"
        f"🤖 Бот: <b>{config.get('bot_name') or '—'}</b>\n"
        f"🆔 <code>{config.get('bot_id')}</code>\n"
        f"📅 Создан: <code>{str(config.get('created_at') or '')[:19]}</code>\n\n"
        "<b>Что внутри:</b>\n" + "\n".join(lines)
    )


# ═══════════════ Меню «⚙️ Конфиг» в карточке бота ═══════════════

def cfg_menu_kb(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Сохранить конфиг", callback_data=f"cfg_save_{bot_id}",
                              style="success")],
        [InlineKeyboardButton(text="📥 Внести конфиг", callback_data=f"cfg_apply_{bot_id}",
                              style="primary")],
        [InlineKeyboardButton(text="📂 Мои ссылки и конфиги", callback_data="my_links",
                              style="primary")],
        [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}")],
    ])


@router.callback_query(F.data.regexp(r"^cfg_menu_\d+$"))
async def cb_cfg_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    bot_info = get_bot_by_id(cb_uid(callback), bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    text = (
        "⚙️ <b>Конфиг бота</b>\n\n"
        f"🤖 {bot_display_name(bot_info)}\n\n"
        "<b>💾 Сохранить конфиг</b> — бот сохранит текущие настройки "
        "(приветствие, кнопки, тип, антиспам, анонимность, уточнение категории, "
        "смену админа, а также время работы и норму админов) и выдаст код, "
        "по которому их можно перенести на другого бота.\n\n"
        "<b>📥 Внести конфиг</b> — бот спросит, какие именно настройки "
        "перенести, и применит только выбранные.\n\n"
        "⚠️ Токен, имя бота, статистика, ПЗ и админы не переносятся никогда."
    )
    await render_callback(callback, text, cfg_menu_kb(bot_id))


# ═══════════════ Сохранить конфиг ═══════════════

async def _show_config_saved(callback: CallbackQuery, bot_id: int, code: str,
                             existed: bool) -> None:
    """Показывает код конфига и ссылку для его применения."""
    username = ""
    try:
        me = await event_bot(callback).get_me()
        username = me.username or ""
    except Exception:
        username = ""

    link_line = ""
    if username:
        link_line = (
            "\n🔗 <b>Ссылка для переноса:</b>\n"
            f"<code>https://t.me/{username}?start=config_{code}</code>\n"
            "Перешедший по ней применит эти настройки к боту, с которого "
            "сохранён конфиг.\n"
        )

    action = "обновлён" if existed else "сохранён"
    text = (
        f"💾 <b>Конфиг {action}!</b>\n\n"
        f"🔑 Код конфига: <code>{code}</code>\n"
        f"{link_line}\n"
        "Открой <b>«📂 Мои ссылки и конфиги»</b> и нажми <b>📥</b> на нужном боте — "
        "бот спросит, какие настройки перенести, и перенесёт только выбранные.\n\n"
        "⚠️ Переносятся только настройки: статистика, имя, токен, ПЗ и админы "
        "остаются прежними."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Внести на этого бота", callback_data=f"cfg_apply_{bot_id}",
                              style="primary")],
        [InlineKeyboardButton(text="📂 Мои ссылки и конфиги", callback_data="my_links",
                              style="primary")],
        [InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}")],
    ])
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^cfg_save_\d+$"))
async def cb_cfg_save(callback: CallbackQuery) -> None:
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    user_id = cb_uid(callback)
    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    data = snapshot_bot(bot_info, user_id)
    name = bot_display_name(bot_info)

    # Если у бота уже есть конфиг — обновляем его, коды не плодим.
    existing = next(
        (cfg for cfg in get_user_bot_configs(user_id)
         if int(cfg.get("bot_id") or 0) == bot_id),
        None,
    )
    if existing:
        update_bot_config(existing["code"], user_id, bot_id, name, data)
        code = existing["code"]
    else:
        code = create_bot_config(user_id, bot_id, name, data)

    logger.info("Владелец %s сохранил конфиг %s для бота %s", user_id, code, bot_id)
    await _show_config_saved(callback, bot_id, code, existed=bool(existing))


# ═══════════════ Внести конфиг ═══════════════

@router.callback_query(F.data.regexp(r"^cfg_apply_\d+$"))
async def cb_cfg_apply(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    if not get_bot_by_id(cb_uid(callback), bot_id):
        await callback.answer("⚠️ Бот не найден", show_alert=True)
        return

    await state.set_state(ConfigFSM.waiting_code)
    await state.update_data(cfg_bot_id=bot_id)

    text = (
        "📥 <b>Внести конфиг</b>\n\n"
        "Отправь <b>код конфига</b> или ссылку с ним — бот спросит, какие "
        "настройки перенести в этого бота, и применит только выбранные.\n\n"
        "⚠️ Статистика, имя, токен, ПЗ и админы останутся прежними — "
        "переносятся только настройки."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cfg_menu_{bot_id}",
                              style="primary")]
    ])
    await render_callback(callback, text, kb)


def _extract_code(raw: str) -> str:
    """Достаёт код конфига из текста сообщения (код или ссылка)."""
    text = (raw or "").strip()
    marker = "config_"
    if marker in text:
        tail = text.split(marker, 1)[1]
        text = tail.split()[0] if tail.split() else tail
        text = text.strip("/ ")
    return normalize_config_code(text)


async def apply_config_by_code(user_id: int, target_bot_id: int, raw_code: str,
                               child_manager: ChildManager,
                               sections: set[str] | None = None) -> tuple[bool, str]:
    """Применяет конфиг к боту. Возвращает (успех, текст-ответ).

    ``sections`` — какие разделы переносить (None = все разделы).
    """
    code = _extract_code(raw_code)
    config = get_bot_config(code)
    if not config:
        return False, (
            "❌ <b>Конфиг не найден.</b>\n\n"
            f"Проверь код: <code>{code or '—'}</code>\n"
            "Свои коды смотри в <b>Профиль → 📂 Мои ссылки и конфиги</b>."
        )
    if int(config.get("owner_id") or 0) != int(user_id):
        return False, "⛔ Этот конфиг принадлежит другому владельцу — применить его нельзя."

    bot_info = get_bot_by_id(user_id, target_bot_id)
    if not bot_info:
        return False, "⚠️ Бот не найден."

    result = apply_bot_config(user_id, target_bot_id, config.get("data"), sections)

    # Перезапускаем бота, чтобы он подхватил новое приветствие и кнопки.
    restarted = False
    fresh = get_bot_by_id(user_id, target_bot_id) or bot_info
    if child_manager.is_running(target_bot_id):
        restarted = await child_manager.restart_child(fresh)

    status = ("🟢 Бот перезапущен — изменения уже действуют." if restarted
              else "💾 Сохранено (бот не запущен — применится при запуске).")

    chosen_titles = ", ".join(
        title for key, title in CONFIG_SECTIONS if sections is None or key in sections
    )
    parts = [
        "✅ <b>Настройки перенесены!</b>\n\n",
        f"🔑 Конфиг: <code>{config['code']}</code>\n",
        f"🤖 Бот: <b>{bot_display_name(fresh)}</b>\n",
        f"📂 Разделы: <i>{chosen_titles or '—'}</i>\n",
    ]
    if result.get("links"):
        parts.append(f"🔗 Кнопок-ссылок: <b>{result['links']}</b>\n")
    if result.get("keyboard"):
        parts.append(f"⌨️ Reply-кнопок: <b>{result['keyboard']}</b>\n")
    if result.get("owner"):
        parts.append("\n🌐 <b>Общие настройки (для всех ботов):</b>\n"
                     + "\n".join(f"  • {item}" for item in result["owner"]) + "\n")
    parts.append("\n📊 Статистика, имя и токен бота не тронуты.\n")
    parts.append(status)

    logger.info("Владелец %s применил конфиг %s к боту %s (разделы: %s)",
                user_id, config["code"], target_bot_id,
                sorted(sections) if sections else "все")
    return True, "".join(parts)


async def _open_sections(callback: CallbackQuery, state: FSMContext,
                         code: str, bot_id: int, chosen: set[str] | None = None) -> None:
    """Открывает экран выбора разделов конфига для переноса в бота."""
    config = get_bot_config(code)
    bot_info = get_bot_by_id(cb_uid(callback), bot_id)
    if not config or not bot_info:
        await callback.answer("⚠️ Конфиг или бот не найден", show_alert=True)
        return

    if chosen is None:
        # По умолчанию берём всё, кроме общих настроек владельца: их лучше
        # переносить осознанно.
        chosen = {key for key, _ in CONFIG_SECTIONS if key not in ("work", "norm")}

    await state.set_state(ConfigFSM.waiting_sections)
    await state.update_data(cfg_code=code, cfg_target_bot=bot_id, cfg_sections=list(chosen))
    await render_callback(
        callback,
        _sections_text(code, bot_id, chosen, bot_display_name(bot_info)),
        _sections_kb(code, bot_id, chosen),
    )


async def _open_sections_message(message: Message, state: FSMContext,
                                 code: str, bot_id: int, user_id: int) -> None:
    """Тот же экран выбора разделов, но после отправки кода в личку."""
    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await message.answer("⚠️ Бот не найден")
        return

    chosen = {key for key, _ in CONFIG_SECTIONS if key not in ("work", "norm")}
    await state.update_data(cfg_code=code, cfg_target_bot=bot_id, cfg_sections=list(chosen))
    await message.answer(
        _sections_text(code, bot_id, chosen, bot_display_name(bot_info)),
        reply_markup=_sections_kb(code, bot_id, chosen),
    )


@router.callback_query(F.data.regexp(r"^cfg_sec_[A-Z0-9-]+_\d+_[a-z]+$"))
async def cb_cfg_section_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Вкл/выкл отдельный раздел конфига."""
    data = cb_data(callback)
    parts = data.split("_")
    code, bot_id, section = parts[2], int(parts[3]), parts[4]

    fs = await state.get_data()
    chosen = {s for s in (fs.get("cfg_sections") or []) if s}
    if section in chosen:
        chosen.discard(section)
    else:
        chosen.add(section)

    await state.update_data(cfg_sections=list(chosen))
    await render_callback(
        callback,
        _sections_text(code, bot_id, chosen, "бот"),
        _sections_kb(code, bot_id, chosen),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^cfg_(all|none)_[A-Z0-9-]+_\d+$"))
async def cb_cfg_sections_bulk(callback: CallbackQuery, state: FSMContext) -> None:
    """«✅ Всё» / «⬜ Ничего» — разом включает или выключает все разделы."""
    data = cb_data(callback)
    parts = data.split("_")
    mode, code, bot_id = parts[1], parts[2], int(parts[3])

    chosen = ({key for key, _ in CONFIG_SECTIONS} if mode == "all" else set())
    await state.update_data(cfg_sections=list(chosen))
    await render_callback(
        callback,
        _sections_text(code, bot_id, chosen, "бот"),
        _sections_kb(code, bot_id, chosen),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^cfg_do_[A-Z0-9-]+_\d+$"))
async def cb_cfg_do(callback: CallbackQuery, state: FSMContext,
                    child_manager: ChildManager) -> None:
    """Переносит выбранные разделы конфига в бота."""
    data = cb_data(callback)
    parts = data.split("_")
    code, bot_id = parts[2], int(parts[3])

    fs = await state.get_data()
    chosen = {s for s in (fs.get("cfg_sections") or []) if s}
    if not chosen:
        await callback.answer("⚠️ Выбери хотя бы один раздел", show_alert=True)
        return

    await state.clear()
    await callback.answer("⏳ Переношу…")
    ok, text = await apply_config_by_code(cb_uid(callback), bot_id, code,
                                          child_manager, chosen)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Конфиг бота", callback_data=f"cfg_menu_{bot_id}",
                              style="primary")],
        [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
    ])
    await render_callback(callback, text, kb if ok else None)


@router.callback_query(F.data.regexp(r"^cfg_load_[A-Z0-9-]+_\d+$"))
async def cb_cfg_load(callback: CallbackQuery, state: FSMContext) -> None:
    """«📥 Внести конфиг в бота» прямо из списка конфигов."""
    data = cb_data(callback)
    parts = data.split("_")
    code, bot_id = parts[2], int(parts[3])
    await _open_sections(callback, state, code, bot_id)


@router.message(ConfigFSM.waiting_code)
async def fsm_cfg_apply(message: Message, state: FSMContext,
                        child_manager: ChildManager) -> None:
    data = await state.get_data()
    bot_id = int(data.get("cfg_bot_id") or 0)
    user_id = msg_uid(message)
    code = _extract_code(message.text or "")
    await state.clear()

    # Сначала спросим, что именно переносить — так владелец не затрёт лишнего.
    if get_bot_config(code) and int((get_bot_config(code) or {}).get("owner_id") or 0) == user_id:
        await state.set_state(ConfigFSM.waiting_sections)
        await state.update_data(cfg_code=code, cfg_target_bot=bot_id)
        await _open_sections_message(message, state, code, bot_id, user_id)
        return

    await message.answer(
        f"⚙️ <b>Применяю данные конфига…</b>\n\n🔑 Код: <code>{code or '—'}</code>"
    )
    ok, text = await apply_config_by_code(user_id, bot_id, message.text or "",
                                          child_manager)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Конфиг", callback_data=f"cfg_menu_{bot_id}",
                              style="primary")],
        [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
    ])
    await message.answer(text, reply_markup=kb if ok else None)


# ═══════════════ Мои ссылки и конфиги ═══════════════

def _invite_link(username: str, token: str) -> str:
    """Ссылка-приглашение админа (или пустая строка, если нет username)."""
    if not username:
        return ""
    return f"https://t.me/{username}?start=addadmin_{token}"


async def _my_links_payload(callback: CallbackQuery) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура экрана «Мои ссылки и конфиги»."""
    user_id = cb_uid(callback)
    configs = get_user_bot_configs(user_id)
    invites = get_owner_admin_invites(user_id)

    try:
        me = await event_bot(callback).get_me()
        username = me.username or ""
    except Exception:
        username = ""

    lines: list[str] = ["📂 Мои ссылки и конфиги\n"]

    # ── Конфиги ──
    lines.append("⚙️ <b>Конфиги ботов:</b>")
    if not configs:
        lines.append("  — пока нет сохранённых конфигов —")
    else:
        for i, cfg in enumerate(configs, 1):
            name = cfg.get("bot_name") or f"bot_{cfg.get('bot_id')}"
            lines.append(f"  {i}. {name} — <code>{cfg['code']}</code>")

    # ── Ссылки-приглашения админов ──
    lines.append("\n🔗 <b>Ссылки для приглашения админов:</b>")
    if not invites:
        lines.append("  — активных ссылок нет —")
    else:
        for i, inv in enumerate(invites, 1):
            left = max(0, int(inv["max_uses"]) - int(inv["used"]))
            link = _invite_link(username, inv["token"])
            lines.append(f"  {i}. Осталось мест: <b>{left}</b> из {inv['max_uses']}")
            if link:
                lines.append(f"     <code>{link}</code>")

    lines.append(
        "\n💡 Ссылку можно скопировать и отправить админу — он введёт свой тег "
        "и получит доступ.\n«🔄 Пересоздать» — новая ссылка с тем же лимитом, "
        "старая перестаёт работать.\n«❌ Аннулировать» — ссылка отключается."
    )
    text = "\n".join(lines)

    rows: list[list[InlineKeyboardButton]] = []
    bots = get_user_bots(user_id)
    for cfg in configs:
        name = (cfg.get("bot_name") or "бот")[:18]
        rows.append([
            InlineKeyboardButton(text=f"⚙️ {name} · {cfg['code']}",
                                 callback_data=f"cfg_view_{cfg['code']}", style="primary"),
            InlineKeyboardButton(text="🗑 Очистить",
                                 callback_data=f"cfg_clear_{cfg['code']}", style="danger"),
        ])
        # Отдельная строка «внести в бота»: конфиг можно загрузить не только
        # в исходного бота, но и в любой другой свой.
        target_buttons = [
            InlineKeyboardButton(
                text=f"📥 {bot_display_name(b)[:14]}" if i == 0 else bot_display_name(b)[:14],
                callback_data=f"cfg_load_{cfg['code']}_{b['id']}",
                style="success",
            )
            for i, b in enumerate(bots)
        ]
        if target_buttons:
            rows.append(target_buttons)
    for inv in invites:
        rows.append([
            InlineKeyboardButton(text="🔄 Пересоздать",
                                 callback_data=f"inv_recreate_{inv['token']}", style="primary"),
            InlineKeyboardButton(text="❌ Аннулировать",
                                 callback_data=f"inv_revoke_{inv['token']}", style="danger"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Новая ссылка для админов",
                                      callback_data="gadmins_addlink", style="success")])
    rows.append([InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "my_links")
async def cb_my_links(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _my_links_payload(callback)
    await render_callback(callback, text, kb, force_answer=True)


# ═══════════════ Конфиг: просмотр и очистка ═══════════════

@router.callback_query(F.data.startswith("cfg_view_"))
async def cb_cfg_view(callback: CallbackQuery) -> None:
    code = cb_data(callback)[len("cfg_view_"):]
    config = get_bot_config(code)
    if not config or int(config.get("owner_id") or 0) != cb_uid(callback):
        await callback.answer("⚠️ Конфиг не найден", show_alert=True)
        return

    bot_id = int(config.get("bot_id") or 0)
    rows = []
    if get_bot_by_id(cb_uid(callback), bot_id):
        rows.append([InlineKeyboardButton(text="📥 Применить к этому боту",
                                          callback_data=f"cfg_apply_{bot_id}",
                                          style="primary")])
    rows.append([InlineKeyboardButton(text="🗑 Очистить конфиг",
                                      callback_data=f"cfg_clear_{config['code']}",
                                      style="danger")])
    rows.append([InlineKeyboardButton(text="⬅️ Мои ссылки и конфиги",
                                      callback_data="my_links", style="primary")])
    await render_callback(callback, _config_text(config),
                          InlineKeyboardMarkup(inline_keyboard=rows), force_answer=True)


@router.callback_query(F.data.startswith("cfg_clear_"))
async def cb_cfg_clear(callback: CallbackQuery) -> None:
    code = normalize_config_code(cb_data(callback)[len("cfg_clear_"):])
    removed = delete_bot_config(code, cb_uid(callback))
    await callback.answer("🗑 Конфиг очищен" if removed else "⚠️ Конфиг не найден")
    text, kb = await _my_links_payload(callback)
    await render_callback(callback, text, kb, force_answer=True)


# ══════════ Приглашения админов: аннулировать / пересоздать ══════════

@router.callback_query(F.data.startswith("inv_revoke_"))
async def cb_inv_revoke(callback: CallbackQuery) -> None:
    token = cb_data(callback)[len("inv_revoke_"):]
    removed = delete_admin_invite(token)
    await callback.answer("❌ Ссылка аннулирована" if removed else "⚠️ Ссылка не найдена")
    text, kb = await _my_links_payload(callback)
    await render_callback(callback, text, kb, force_answer=True)


@router.callback_query(F.data.startswith("inv_recreate_"))
async def cb_inv_recreate(callback: CallbackQuery) -> None:
    token = cb_data(callback)[len("inv_recreate_"):]
    owner_id = cb_uid(callback)

    invite = next((i for i in get_owner_admin_invites(owner_id)
                   if i["token"] == token), None)
    if not invite:
        await callback.answer("⚠️ Ссылка не найдена", show_alert=True)
        return

    max_uses = max(1, int(invite.get("max_uses") or 1))
    delete_admin_invite(token)
    create_admin_invite(owner_id, max_uses)
    await callback.answer(f"🔄 Ссылка пересоздана (мест: {max_uses})")
    text, kb = await _my_links_payload(callback)
    await render_callback(callback, text, kb, force_answer=True)