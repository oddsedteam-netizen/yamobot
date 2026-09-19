"""Раздел «📢 Мой ТГК» — синяя reply-кнопка главного меню.

Весь функционал бывших «Эксп функций» переехал сюда: отдельного раздела
«Эксп функции» больше нет.

Что умеет:
  • если ТГК ещё не привязан — бот прямо пишет об этом и просит привязать канал;
  • привязка канала: пользователь добавляет YamoBot в свой канал админом
    (особенно с правом «Публикация сообщений»), бот видит это сам и присылает
    подтверждение, а кнопка «✅ Я привязал» проверяет привязку вручную;
  • «📢 Мой ТГК» — статистика канала: подписчики, посты бота и отложенные посты;
  • «📝 Выложить пост» — текст (можно с фото, разметкой и премиум-эмодзи),
    при желании инлайн-кнопки-ссылки с выбором цвета, проверка поста в личке
    (если Telegram вырезал премиум-эмодзи — честно предупреждаем) и кнопки
    «✅ Опубликовать» / «🕒 Опубликовать позже» (день и время по МСК);
  • «🕒 Отложенные посты» — просмотр, редактирование и отмена;
  • «🔴 Отвязать ТГК» — с подтверждением: бот выходит из канала.

Просмотры постов Bot API не отдаёт, поэтому в статистике показываем то, что
бот знает сам (подписчики и его посты), и честно об этом пишем.
"""

import logging
import re
from datetime import date, datetime
from html import escape as _esc

from aiogram import Router, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import (cb_data, cb_uid, edit_or_answer, event_bot,
                              msg_uid, normalize_link, render_callback)
from services.channel_service import (
    BUTTON_STYLE_LABELS,
    PREMIUM_LOST_HINT,
    build_buttons,
    dump_buttons,
    msk_moment,
    parse_buttons,
    parse_day,
    parse_time,
    post_link,
    premium_emoji_lost,
    publish_channel_post,
    safe_button_style,
    to_msk_str,
    to_utc_str,
)
from services.constants import BTN_DANGER, BTN_PRIMARY, BTN_SUCCESS
from services.storage import (
    add_channel_post,
    bind_channel,
    clear_channel_bind_request,
    count_channel_posts,
    delete_channel_post,
    get_bound_channel,
    get_channel_bind_request,
    get_channel_post,
    get_channel_posts,
    set_channel_bind_request,
    unbind_channel,
    update_channel_info,
    update_channel_post,
)

logger = logging.getLogger(__name__)

router = Router()

# username YamoBot нужен в инструкции по привязке — берём один раз и кэшируем.
_BOT_USERNAME_CACHE = ""

# username канала в Telegram: 5–32 символа, начинается с буквы.
_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")

# Максимум кнопок-ссылок у поста (ограничение Telegram — 100, нам хватит 10).
MAX_POST_BUTTONS = 10


class ChannelFSM(StatesGroup):
    """Шаги создания поста: текст → кнопки → день → время."""

    waiting_text = State()
    waiting_link_url = State()
    waiting_link_name = State()
    # Цвет кнопки поста: спрашиваем после названия (primary/success/danger).
    waiting_link_style = State()
    waiting_day = State()
    waiting_time = State()
    editing_post = State()
    # Ждём от пользователя пересланный пост из канала или @username канала —
    # нужно, если бот не поймал добавление себя в канал (например, событие
    # пришло до запуска бота).
    waiting_channel_ref = State()


# ── Вспомогательные функции ───────────────────────────────────────────


async def _bot_username(bot) -> str:
    """username основного бота (для инструкции «добавь @бот»)."""
    global _BOT_USERNAME_CACHE
    if not _BOT_USERNAME_CACHE:
        try:
            me = await bot.get_me()
            _BOT_USERNAME_CACHE = me.username or ""
        except Exception as e:
            logger.warning("Не удалось получить username бота: %s", e)
    return _BOT_USERNAME_CACHE


def _back_main_kb() -> InlineKeyboardMarkup:
    """«Назад» из раздела ТГК: ТГК — отдельная кнопка главного меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main",
                              style=BTN_PRIMARY)],
    ])


def _no_channel_kb() -> InlineKeyboardMarkup:
    """Клавиатура, когда канал не привязан: предлагаем привязать."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Привязать ТГК", callback_data="ch_tgk",
                              style=BTN_SUCCESS)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main",
                              style=BTN_PRIMARY)],
    ])


def _cancel_kb(text: str = "❌ Отмена") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data="ch_cancel",
                              style=BTN_DANGER)],
    ])


def _style_pick_kb() -> InlineKeyboardMarkup:
    """Выбор цвета кнопки поста (как у линков в редакторе приветствия)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔵 Синий", callback_data="ch_btnstyle_primary",
                                 style=BTN_PRIMARY),
            InlineKeyboardButton(text="🟢 Зелёный", callback_data="ch_btnstyle_success",
                                 style=BTN_SUCCESS),
            InlineKeyboardButton(text="🔴 Красный", callback_data="ch_btnstyle_danger",
                                 style=BTN_DANGER),
        ],
        [InlineKeyboardButton(text="⬜ Без цвета", callback_data="ch_btnstyle_none")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ch_cancel",
                              style=BTN_DANGER)],
    ])


async def _channel_admin_state(bot, channel_id: int) -> tuple[bool, bool]:
    """Проверяет права бота в канале: (админ, может публиковать посты)."""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=bot.id)
    except Exception as e:
        logger.warning("Не удалось проверить права бота в канале %s: %s",
                       channel_id, e)
        return False, False
    if getattr(member, "status", "") != ChatMemberStatus.ADMINISTRATOR:
        return False, False
    return True, bool(getattr(member, "can_post_messages", False))


async def _resolve_channel_ref(bot, message: Message) -> tuple[int, str, str] | None:
    """Определяет канал по пересланному посту или по @username/ссылке.

    Возвращает (channel_id, title, username) или None, если канал определить
    не удалось. Нужно для ручной привязки: Telegram не всегда сообщает боту,
    что его добавили в канал (например, бот был добавлен до запуска).
    """
    origin = getattr(message, "forward_from_chat", None)
    if origin is not None and getattr(origin, "type", "") == ChatType.CHANNEL:
        return int(origin.id), origin.title or "", origin.username or ""

    raw = (message.text or "").strip()
    if not raw:
        return None

    target = raw
    if target.startswith("@"):
        target = target[1:]
    else:
        link = normalize_link(raw)
        if link.startswith("https://t.me/"):
            target = link[len("https://t.me/"):]
        elif _USERNAME_RE.fullmatch(raw):
            # Пользователь мог написать просто username канала — без @.
            target = raw
        else:
            return None
    target = target.split("/")[0].split("?")[0].strip()
    # Приватные приглашения (t.me/+hash) по ссылке не разобрать.
    if not target or target.startswith("+"):
        return None

    try:
        chat = await bot.get_chat(f"@{target}")
    except Exception as e:
        logger.warning("Не удалось получить канал @%s: %s", target, e)
        return None
    if getattr(chat, "type", "") != ChatType.CHANNEL:
        return None
    return int(chat.id), getattr(chat, "title", "") or "", getattr(chat, "username", "") or ""


def _bind_instruction_text(bot_username: str) -> str:
    bot_line = f"<b>@{bot_username}</b>" if bot_username else "<b>этого бота</b>"
    return (
        "🔗 <b>Привязка ТГК</b>\n\n"
        "Чтобы бот публиковал посты в твоём канале:\n"
        "1. Открой свой канал → «Управление» → «Администраторы».\n"
        f"2. Добавь {bot_line} и выдай права администратора — особенно "
        "<b>«Публикация сообщений»</b> (без него посты не выйдут).\n"
        "3. Вернись сюда: я пришлю подтверждение сам, как только увижу привязку.\n\n"
        "Если подтверждение не пришло (например, бот уже был в канале) — нажми "
        "<b>«✅ Я привязал»</b> и просто <b>перешли мне любой пост из канала</b> "
        "или пришли <b>@username</b> канала: я найду его и привяжу.\n\n"
        "⚠️ К боту можно привязать только один канал."
    )


def _bind_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я привязал", callback_data="ch_check",
                              style=BTN_SUCCESS)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main",
                              style=BTN_PRIMARY)],
    ])


def _channel_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Выложить пост", callback_data="ch_post_new",
                              style=BTN_SUCCESS)],
        [InlineKeyboardButton(text="🕒 Отложенные посты", callback_data="ch_sched_list",
                              style=BTN_PRIMARY)],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="ch_menu",
                              style=BTN_PRIMARY)],
        [InlineKeyboardButton(text="🔴 Отвязать ТГК", callback_data="ch_unbind",
                              style=BTN_DANGER)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main",
                              style=BTN_PRIMARY)],
    ])


def _channel_title(channel: dict) -> str:
    """Название канала для интерфейса (без учёта разметки)."""
    name = str(channel.get("title") or "")
    if not name:
        name = f"канал {channel.get('channel_id')}"
    return _esc(name)


async def _channel_stats_text(bot, owner_id: int, channel: dict) -> str:
    """Текст статистики канала для раздела «📢 Мой ТГК»."""
    channel_id = int(channel.get("channel_id") or 0)
    username = str(channel.get("username") or "").strip().lstrip("@")
    username_line = f" (@{_esc(username)})" if username else ""

    subscribers = 0
    try:
        subscribers = int(await bot.get_chat_member_count(channel_id))
    except Exception as e:
        logger.warning("Не удалось получить число подписчиков канала %s: %s",
                       channel_id, e)

    published = count_channel_posts(owner_id, "published")
    scheduled = count_channel_posts(owner_id, "scheduled")

    text = (
        "📢 <b>Мой ТГК</b>\n\n"
        f"📎 Канал: <b>{_channel_title(channel)}</b>{username_line}\n"
        f"🆔 <code>{channel_id}</code>\n\n"
        f"👥 Подписчиков: <b>{subscribers}</b>\n"
        f"📝 Постов от бота: <b>{published}</b>\n"
        f"🕒 Отложенных постов: <b>{scheduled}</b>\n"
    )

    text += (
        "\n👀 Просмотры постов Bot API не отдаёт — их видно в самом канале.\n\n"
        "Выбери действие:"
    )
    return text


async def _render(callback: CallbackQuery, text: str,
                  reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Правит сообщение колбэка (или шлёт новое) — спиннер гасит вызывающий.

    Отдельный хелпер нужен там, где ответ колбэка уже дан (например, показали
    всплывающее «✅ Привязка подтверждена») — повторный ``callback.answer()``
    Telegram не примет.
    """
    await edit_or_answer(callback.message, text, reply_markup)


async def _show_channel_menu(target, owner_id: int, channel: dict) -> None:
    """Показывает «📢 Мой ТГК» (для колбэка или сообщения)."""
    text = await _channel_stats_text(event_bot(target), owner_id, channel)
    kb = _channel_menu_kb()
    if isinstance(target, CallbackQuery):
        await _render(target, text, kb)
    else:
        await target.answer(text, reply_markup=kb)


def _owner_of(target) -> int:
    """Владелец апдейта: колбэк или сообщение."""
    return cb_uid(target) if isinstance(target, CallbackQuery) else msg_uid(target)


def _unbound_text(bot_username: str) -> str:
    """Текст «ТГК не привязан» + инструкция по привязке."""
    return (
        "⚠️ <b>ТГК не привязан</b>\n\n"
        "Чтобы выкладывать посты от лица канала, сначала привяжи свой канал.\n\n"
        + _bind_instruction_text(bot_username)
    )


async def _tgk_payload(bot, owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Экран раздела ТГК: статистика канала или просьба привязать канал."""
    channel = get_bound_channel(owner_id)
    if not channel:
        # Канала нет — честно говорим об этом и просим привязать.
        return _unbound_text(await _bot_username(bot)), _no_channel_kb()

    adm, _can_post = await _channel_admin_state(bot, int(channel["channel_id"]))
    if not adm:
        # Канал привязан, но прав у бота нет — просим выдать права.
        return _no_rights_text(), _no_rights_kb()

    return await _channel_stats_text(bot, owner_id, channel), _channel_menu_kb()


def _no_rights_text() -> str:
    return (
        "⚠️ <b>Канал привязан, но у бота нет прав администратора.</b>\n\n"
        "Зайди: «Управление каналом» → «Администраторы» → бот → "
        "выдай права, особенно <b>«Публикация сообщений»</b>.\n\n"
        "Потом нажми «✅ Проверить права»."
    )


def _no_rights_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Проверить права", callback_data="ch_check",
                              style=BTN_SUCCESS)],
        [InlineKeyboardButton(text="🔴 Отвязать ТГК", callback_data="ch_unbind",
                              style=BTN_DANGER)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main",
                              style=BTN_PRIMARY)],
    ])


async def show_tgk_entry(target) -> None:
    """Точка входа в раздел «📢 Мой ТГК» (reply-кнопка главного меню).

    Работает и для сообщения (reply-кнопка), и для колбэка (кнопка «📢 Мой ТГК»
    в разделе «Прочее»). Если ТГК не привязан — пишем об этом и просим привязать.
    """
    owner_id = _owner_of(target)
    text, kb = await _tgk_payload(event_bot(target), owner_id)
    if isinstance(target, CallbackQuery):
        await _render(target, text, kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "ch_tgk_root")
async def cb_tgk_root(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «📢 Мой ТГК» в разделе «✨ Прочее»."""
    await state.clear()
    await callback.answer()
    await show_tgk_entry(callback)


# ═══════════════ Привязка канала ═══════════════


@router.callback_query(F.data == "ch_tgk")
async def cb_channel_bind(callback: CallbackQuery, state: FSMContext) -> None:
    """«🔗 Привязать ТГК»: инструкция или сразу «📢 Мой ТГК», если уже привязан."""
    await state.clear()
    owner_id = cb_uid(callback)
    channel = get_bound_channel(owner_id)

    if channel:
        adm, _can_post = await _channel_admin_state(event_bot(callback), int(channel["channel_id"]))
        if adm:
            await callback.answer()
            await _show_channel_menu(callback, owner_id, channel)
            return
        # Канал привязан, но прав у бота нет — просим выдать права.
        await render_callback(callback, _no_rights_text(), _no_rights_kb(),
                              force_answer=True)
        return

    bot_username = await _bot_username(event_bot(callback))
    # Заявка нужна, если Telegram не сообщит, кто добавил бота в канал
    # (анонимный админ) — тогда привяжем канал именно этому владельцу.
    set_channel_bind_request(owner_id)
    await render_callback(callback, _bind_instruction_text(bot_username), _bind_kb(),
                          force_answer=True)


@router.callback_query(F.data == "ch_check")
async def cb_channel_check(callback: CallbackQuery, state: FSMContext) -> None:
    """Ручная проверка привязки канала («✅ Я привязал» / «✅ Проверить права»)."""
    owner_id = cb_uid(callback)
    channel = get_bound_channel(owner_id)

    if not channel:
        # Бот мог не поймать добавление в канал (например, его добавили до
        # запуска) — просим переслать пост или прислать @username канала.
        await state.set_state(ChannelFSM.waiting_channel_ref)
        await render_callback(
            callback,
            "⏳ <b>Пока не вижу привязки.</b>\n\n"
            "Проверь, что бот добавлен в канал и у него есть права "
            "администратора (обязательно <b>«Публикация сообщений»</b>).\n\n"
            "Затем <b>перешли мне любой пост из канала</b> или пришли "
            "<b>@username</b> канала — я найду его и привяжу.",
            _cancel_kb(),
            force_answer=True,
        )
        return

    channel_id = int(channel["channel_id"])
    adm, can_post = await _channel_admin_state(event_bot(callback), channel_id)
    if not adm:
        await callback.answer(
            "⚠️ Бот ещё не администратор канала. Выдай права и нажми ещё раз.",
            show_alert=True,
        )
        return
    if not can_post:
        await callback.answer(
            "⚠️ У бота нет права «Публикация сообщений» — без него посты не выйдут.",
            show_alert=True,
        )
        return

    await callback.answer("✅ Привязка подтверждена")
    await state.clear()
    clear_channel_bind_request(owner_id)
    # Заодно освежаем название/username канала (мог переименоваться).
    try:
        chat = await event_bot(callback).get_chat(channel_id)
        update_channel_info(channel_id, chat.title or "", chat.username or "")
        channel = get_bound_channel(owner_id) or channel
    except Exception as e:
        logger.warning("Не удалось обновить данные канала %s: %s", channel_id, e)
    await _show_channel_menu(callback, owner_id, channel)


@router.message(ChannelFSM.waiting_channel_ref, F.chat.type == ChatType.PRIVATE)
async def fsm_channel_ref(message: Message, state: FSMContext) -> None:
    """Ручная привязка: пересланный пост из канала или @username канала."""
    owner_id = msg_uid(message)
    bot = event_bot(message)

    ref = await _resolve_channel_ref(bot, message)
    if ref is None:
        await message.answer(
            "❌ Не смог определить канал.\n\n"
            "• <b>перешли любой пост из своего канала</b> (именно пересланный, "
            "не скопированный текст);\n"
            "• или пришли <b>@username</b> канала (например "
            "<code>@my_channel</code>).\n\n"
            "Если канал приватный (по ссылке-приглашению) — перешли из него пост.",
            reply_markup=_cancel_kb(),
        )
        return

    channel_id, title, username = ref
    adm, can_post = await _channel_admin_state(bot, channel_id)
    if not adm or not can_post:
        await message.answer(
            f"⚠️ <b>Канал найден, но прав не хватает.</b>\n\n"
            f"📎 Канал: <b>{_esc(title or str(channel_id))}</b>\n"
            f"🆔 <code>{channel_id}</code>\n\n"
            "Добавь бота в этот канал и выдай права администратора — обязательно "
            "<b>«Публикация сообщений»</b>. Потом перешли пост или пришли "
            "@username канала ещё раз.",
            reply_markup=_cancel_kb(),
        )
        return

    bind_channel(owner_id, channel_id, title, username)
    clear_channel_bind_request(owner_id)
    await state.clear()
    logger.info("Пользователь %s привязал канал %s (%s) вручную",
                owner_id, channel_id, title)

    await message.answer(
        "🎉 <b>ТГК привязан!</b>\n\n"
        f"📎 Канал: <b>{_esc(title or str(channel_id))}</b>\n"
        f"🆔 <code>{channel_id}</code>\n\n"
        "Теперь можешь публиковать посты: нажми «📢 Мой ТГК» в главном меню → "
        "«📝 Выложить пост».",
        reply_markup=_channel_menu_kb(),
    )


@router.callback_query(F.data == "ch_menu")
async def cb_channel_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """«📢 Мой ТГК» — статистика канала."""
    await state.clear()
    owner_id = cb_uid(callback)
    channel = get_bound_channel(owner_id)
    if not channel:
        # Канала нет — показываем экран «ТГК не привязан» с привязкой.
        await callback.answer("⚠️ ТГК не привязан", show_alert=True)
        await show_tgk_entry(callback)
        return
    await callback.answer()
    await _show_channel_menu(callback, owner_id, channel)
# ── Бота добавили в канал / убрали из канала ──


@router.my_chat_member(F.chat.type == ChatType.CHANNEL)
async def on_channel_member_update(event: ChatMemberUpdated) -> None:
    """Бот добавлен в канал админом — привязываем канал и подтверждаем.

    Если бота убрали из канала — снимаем привязку и сообщаем владельцу.
    Чужой канал «увести» привязку не может: пока привязан другой канал,
    новую привязку не подтверждаем.

    Если Telegram не сообщил, кто добавил бота (анонимный админ канала),
    привязываем канал владельцу со свежей заявкой «привязать ТГК».
    """
    chat = event.chat
    bot = event_bot(event)

    adder = event.from_user
    adder_id = adder.id if adder is not None and not adder.is_bot else 0
    if not adder_id:
        # Инициатор неизвестен — смотрим, кто просил привязать ТГК.
        adder_id = get_channel_bind_request() or 0
    if not adder_id:
        logger.info("Канал %s: не понял, кто добавил бота — ничего не меняю", chat.id)
        return

    new_status = getattr(event.new_chat_member, "status", "")
    is_member_now = new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER)

    async def _dm(text: str) -> None:
        try:
            await bot.send_message(chat_id=adder_id, text=text)
        except Exception as e:
            logger.warning("Не удалось написать владельцу %s: %s", adder_id, e)

    if not is_member_now:
        # Бота удалили из канала: если он был привязан — снимаем привязку.
        bound = get_bound_channel(adder_id)
        if bound and int(bound["channel_id"]) == int(chat.id):
            unbind_channel(adder_id)
            logger.info("Канал %s (%s) отвязан: бота убрали из канала",
                        chat.id, chat.title)
            await _dm(
                "⚠️ <b>Бот удалён из твоего канала.</b>\n\n"
                f"📎 Канал: <b>{_esc(chat.title or str(chat.id))}</b>\n"
                "Привязка снята. Чтобы снова публиковать посты — добавь бота "
                "админом и открой «📢 Мой ТГК» в главном меню."
            )
        return

    bound = get_bound_channel(adder_id)
    if bound and int(bound["channel_id"]) != int(chat.id):
        # Уже привязан другой канал — не даём молча перепривязаться.
        old_title = _esc(bound.get("title") or str(bound.get("channel_id")))
        await _dm(
            "⚠️ <b>Тебя добавили с ботом в другой канал.</b>\n\n"
            f"📎 Текущий ТГК: <b>{old_title}</b>\n"
            f"📎 Новый канал: <b>{_esc(chat.title or str(chat.id))}</b>\n\n"
            "Сначала отвяжи текущий ТГК: «📢 Мой ТГК» в главном меню → "
            "«🔴 Отвязать ТГК», затем добавь бота в нужный канал."
        )
        return

    bind_channel(adder_id, chat.id, chat.title or "", chat.username or "")
    clear_channel_bind_request(adder_id)
    logger.info("Пользователь %s привязал канал %s (%s)", adder_id, chat.id, chat.title)

    if new_status != ChatMemberStatus.ADMINISTRATOR:
        await _dm(
            "⚠️ <b>Бот добавлен в канал, но без прав администратора.</b>\n\n"
            "Выдай боту права (обязательно <b>«Публикация сообщений»</b>), иначе "
            "он не сможет публиковать посты. Потом нажми «✅ Проверить права» "
            "в разделе «📢 Мой ТГК»."
        )
        return

    await _dm(
        "🎉 <b>ТГК привязан!</b>\n\n"
        f"📎 Канал: <b>{_esc(chat.title or str(chat.id))}</b>\n"
        f"🆔 <code>{chat.id}</code>\n\n"
        "Теперь можешь публиковать посты: нажми «📢 Мой ТГК» в главном меню → "
        "«📝 Выложить пост»."
    )


# ═══════════════ Создание поста ═══════════════


def _preview_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="ch_publish",
                              style=BTN_SUCCESS)],
        [InlineKeyboardButton(text="🕒 Опубликовать позже", callback_data="ch_publish_later",
                              style=BTN_PRIMARY)],
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="ch_edit_draft",
                              style=BTN_PRIMARY)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ch_cancel",
                              style=BTN_DANGER)],
    ])


def _strip_html(text: str) -> str:
    """Убирает теги разметки (для превью в списках и фолбэков)."""
    return re.sub(r"<[^>]+>", "", text or "")


def _plain_preview(text: str, limit: int = 120) -> str:
    """Короткое «плоское» превью текста поста для списка."""
    plain = _strip_html(text).replace("\n", " ").strip()
    if not plain:
        return "«без текста»"
    if len(plain) > limit:
        plain = plain[:limit] + "…"
    return _esc(plain)


async def _send_preview(bot, chat_id: int, data: dict) -> None:
    """Присылает пост в личку на проверку — так, как он выйдет в канале."""
    text = data.get("post_text") or ""
    photo = str(data.get("post_photo") or "").strip()
    markup = build_buttons(data.get("buttons") or [])

    await bot.send_message(chat_id=chat_id, text="👀 <b>Проверь пост перед публикацией:</b>")
    premium_lost = False
    try:
        if photo:
            sent = await bot.send_photo(chat_id=chat_id, photo=photo,
                                        caption=text or None, reply_markup=markup)
        else:
            sent = await bot.send_message(chat_id=chat_id, text=text,
                                          reply_markup=markup)
        # В личке премиум-эмодзи могут не пройти так же, как в канале — предупредим.
        premium_lost = premium_emoji_lost(sent, text, getattr(bot, "id", None))
    except Exception as e:
        # Разметку могла не принять — повторяем без разметки, но пост не теряем.
        logger.warning("Не удалось показать превью поста (%s) — показываю без разметки", e)
        plain = _strip_html(text)
        if photo:
            await bot.send_photo(chat_id=chat_id, photo=photo,
                                 caption=plain or None, reply_markup=markup)
        else:
            await bot.send_message(chat_id=chat_id, text=plain, parse_mode=None,
                                   reply_markup=markup)

    hint = ("Что делаем с постом? Можно опубликовать сейчас или отложить "
            "на нужный день.")
    if premium_lost:
        hint += "\n\n" + PREMIUM_LOST_HINT
    await bot.send_message(chat_id=chat_id, text=hint, reply_markup=_preview_kb())


async def _require_channel(callback: CallbackQuery) -> dict | None:
    """Проверяет, что у пользователя привязан канал (иначе подсказывает)."""
    channel = get_bound_channel(cb_uid(callback))
    if not channel:
        await callback.answer("⚠️ Сначала привяжи ТГК", show_alert=True)
        return None
    return channel


@router.callback_query(F.data == "ch_post_new")
async def cb_post_new(callback: CallbackQuery, state: FSMContext) -> None:
    """«📝 Выложить пост» — шаг 1: текст поста."""
    await state.clear()
    channel = await _require_channel(callback)
    if not channel:
        return

    await state.set_state(ChannelFSM.waiting_text)
    await state.update_data(stage="new", post_text="", post_photo="", buttons=[])

    await render_callback(
        callback,
        "📝 <b>Новый пост</b>\n\n"
        "Пришли текст поста одним сообщением.\n"
        "• можно с фото — тогда текст отправь подписью;\n"
        "• форматирование сохраняется: жирный, курсив, подчёркнутый, спойлер, "
        "моноширинный и премиум-эмодзи.\n\n"
        "Отмена — кнопкой ниже.",
        _cancel_kb(),
        force_answer=True,
    )


@router.callback_query(F.data == "ch_edit_draft")
async def cb_edit_draft(callback: CallbackQuery, state: FSMContext) -> None:
    """Просьба переписать текст поста (черновик уже собран)."""
    data = await state.get_data()
    if not data.get("post_text") and not data.get("post_photo"):
        await callback.answer("⚠️ Черновик потерян — начни заново", show_alert=True)
        return
    await state.set_state(ChannelFSM.waiting_text)
    await state.update_data(stage="edit_text")
    await render_callback(
        callback,
        "✏️ <b>Изменить текст</b>\n\nПришли новый текст поста (можно с фото).",
        _cancel_kb(),
        force_answer=True,
    )


@router.callback_query(F.data == "ch_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена любого шага создания/правки поста."""
    await state.clear()
    owner_id = cb_uid(callback)
    clear_channel_bind_request(owner_id)
    channel = get_bound_channel(owner_id)
    await callback.answer()
    if channel:
        await _show_channel_menu(callback, owner_id, channel)
    else:
        await _render(callback, "❌ Отменено.", _no_channel_kb())


@router.message(ChannelFSM.waiting_text, F.chat.type == ChatType.PRIVATE)
async def fsm_post_text(message: Message, state: FSMContext) -> None:
    """Шаг 1: принял текст (и/или фото) поста."""
    data = await state.get_data()
    stage = data.get("stage") or "new"

    if message.photo:
        post_text = message.html_text or message.caption or ""
        post_photo = message.photo[-1].file_id
    elif message.text:
        post_text = message.html_text or message.text
        post_photo = ""
    else:
        await message.answer(
            "⚠️ Нужен текст или фото. Пришли пост ещё раз.",
            reply_markup=_cancel_kb(),
        )
        return

    if not post_text and not post_photo:
        await message.answer("⚠️ Пустой пост. Пришли текст или фото.",
                             reply_markup=_cancel_kb())
        return

    update: dict = {"post_text": post_text}
    # При правке текста фото сохраняем, если новое сообщение без фото.
    if post_photo or stage == "new":
        update["post_photo"] = post_photo
    await state.update_data(**update)
    data = await state.get_data()

    if stage == "edit_text":
        # Кнопки уже собраны — сразу показываем обновлённый пост.
        await _send_preview(event_bot(message), msg_uid(message), data)
        return

    await message.answer(
        "🔗 <b>Вставлять инлайн-кнопки к посту?</b>\n\n"
        "Кнопки — это ссылки под постом (например «Наш чат», «Подписаться»).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data="ch_btn_yes",
                                  style=BTN_SUCCESS)],
            [InlineKeyboardButton(text="❌ Нет", callback_data="ch_btn_no",
                                  style=BTN_PRIMARY)],
        ]),
    )
# ── Шаг 2: инлайн-кнопки поста ──


@router.callback_query(F.data == "ch_btn_no")
async def cb_post_no_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    """Пост без кнопок — сразу на проверку."""
    data = await state.get_data()
    if not data.get("post_text") and not data.get("post_photo"):
        await callback.answer("⚠️ Черновик потерян — начни заново", show_alert=True)
        return
    await callback.answer()
    await _send_preview(event_bot(callback), cb_uid(callback), data)


@router.callback_query(F.data == "ch_btn_yes")
async def cb_post_yes_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    """Пост с кнопками — спрашиваем ссылку первой кнопки."""
    data = await state.get_data()
    buttons = data.get("buttons") or []
    if len(buttons) >= MAX_POST_BUTTONS:
        await callback.answer("⚠️ Достаточно кнопок", show_alert=True)
        return
    await state.set_state(ChannelFSM.waiting_link_url)
    await render_callback(
        callback,
        "🔗 <b>Ссылка для кнопки</b>\n\n"
        "Пришли ссылку: <code>@username</code>, <code>t.me/…</code> "
        "или полную <code>https://…</code> — я приведу её к нужному виду.",
        _cancel_kb(),
        force_answer=True,
    )


@router.message(ChannelFSM.waiting_link_url, F.chat.type == ChatType.PRIVATE)
async def fsm_link_url(message: Message, state: FSMContext) -> None:
    """Принял ссылку кнопки — спрашиваем её название."""
    url = normalize_link(message.text or "")
    if not url:
        await message.answer(
            "❌ Не похоже на ссылку.\n\n"
            "Отправь её в любом виде: <code>@username</code>, "
            "<code>t.me/канал</code> или <code>https://…</code>.",
            reply_markup=_cancel_kb(),
        )
        return
    await state.update_data(link_url=url)
    await state.set_state(ChannelFSM.waiting_link_name)
    await message.answer(
        "✍️ <b>Название кнопки</b>\n\nПришли текст, который будет на кнопке "
        "(например «Наш чат»).",
        reply_markup=_cancel_kb(),
    )


@router.message(ChannelFSM.waiting_link_name, F.chat.type == ChatType.PRIVATE)
async def fsm_link_name(message: Message, state: FSMContext) -> None:
    """Принял название кнопки — спрашиваем её цвет в канале."""
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название пустое. Пришли текст кнопки.",
                             reply_markup=_cancel_kb())
        return

    data = await state.get_data()
    if not data.get("link_url"):
        await state.set_state(None)
        await message.answer("⚠️ Ссылка потерялась — добавь кнопку заново.",
                             reply_markup=_cancel_kb())
        return

    # Кнопку запишем только после выбора цвета — чтобы он не потерялся.
    await state.update_data(link_name=name[:64])
    await state.set_state(ChannelFSM.waiting_link_style)
    await message.answer(
        f"✅ Название: <b>{_esc(name[:64])}</b>\n"
        f"🔗 Ссылка: {_esc(str(data.get('link_url') or ''))}\n\n"
        "🎨 Какого цвета будет кнопка в канале?",
        reply_markup=_style_pick_kb(),
    )


@router.message(ChannelFSM.waiting_link_style, F.chat.type == ChatType.PRIVATE)
async def fsm_link_style_prompt(message: Message) -> None:
    """Цвет выбирают кнопкой — на текст мягко напоминаем про кнопки."""
    await message.answer(
        "🎨 Цвет кнопки выбирается кнопкой ниже.\n\n"
        "Ссылку и название заново присылать не нужно — просто нажми нужный цвет "
        "или «Отмена».",
        reply_markup=_style_pick_kb(),
    )


def _more_buttons_kb() -> InlineKeyboardMarkup:
    """Клавиатура после добавления кнопки: ещё одна или готово."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="ch_more_yes",
                              style=BTN_PRIMARY)],
        [InlineKeyboardButton(text="✅ Готово", callback_data="ch_more_no",
                              style=BTN_SUCCESS)],
    ])


def _button_added_text(name: str, style: str, count: int) -> str:
    """Текст подтверждения после добавления кнопки (с её цветом)."""
    label = BUTTON_STYLE_LABELS.get(style or "", style or "")
    return (
        f"✅ Кнопка <b>{_esc(name)}</b> добавлена — цвет: {label}.\n"
        f"Всего кнопок: {count}.\n\n"
        "Вставляем ещё одну ссылку?"
    )


@router.callback_query(F.data.startswith("ch_btnstyle_"))
async def cb_button_style(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбран цвет кнопки — записываем кнопку в черновик поста."""
    # Цвет необязателен: «Без цвета» → safe_button_style отдаст пустую строку.
    style = safe_button_style(cb_data(callback).removeprefix("ch_btnstyle_"))

    data = await state.get_data()
    name = str(data.get("link_name") or "").strip()
    url = str(data.get("link_url") or "").strip()
    buttons = list(data.get("buttons") or [])
    # Состояние снимаем, но черновик поста (текст/фото/кнопки) не трогаем.
    await state.set_state(None)
    await state.update_data(link_name="", link_url="")

    if not name or not url:
        await callback.answer("⚠️ Кнопка потерялась — добавь её заново.",
                              show_alert=True)
        return
    if len(buttons) >= MAX_POST_BUTTONS:
        await callback.answer(f"⚠️ Максимум {MAX_POST_BUTTONS} кнопок",
                              show_alert=True)
        return

    button: dict = {"text": name[:64], "url": url}
    if style:
        button["style"] = style
    buttons.append(button)
    await state.update_data(buttons=buttons)

    await render_callback(
        callback, _button_added_text(name[:64], style, len(buttons)),
        _more_buttons_kb(), force_answer=True,
    )


@router.callback_query(F.data == "ch_more_yes")
async def cb_more_links(callback: CallbackQuery, state: FSMContext) -> None:
    """Добавляем ещё одну ссылку."""
    data = await state.get_data()
    if len(data.get("buttons") or []) >= MAX_POST_BUTTONS:
        await callback.answer(f"⚠️ Максимум {MAX_POST_BUTTONS} кнопок", show_alert=True)
        return
    await state.set_state(ChannelFSM.waiting_link_url)
    await render_callback(
        callback,
        "🔗 <b>Ещё одна ссылка</b>\n\nПришли ссылку для следующей кнопки.",
        _cancel_kb(),
        force_answer=True,
    )


@router.callback_query(F.data == "ch_more_no")
async def cb_more_links_done(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопки собраны — показываем пост на проверку."""
    data = await state.get_data()
    await callback.answer()
    await _send_preview(event_bot(callback), cb_uid(callback), data)
# ── Шаг 3: публикация сразу или по расписанию ──


@router.callback_query(F.data == "ch_publish")
async def cb_publish_now(callback: CallbackQuery, state: FSMContext) -> None:
    """Публикуем пост сразу от лица канала."""
    data = await state.get_data()
    if not data.get("post_text") and not data.get("post_photo"):
        await callback.answer("⚠️ Черновик потерян — начни заново", show_alert=True)
        return

    channel = get_bound_channel(cb_uid(callback))
    if not channel:
        await callback.answer("⚠️ ТГК не привязан — привяжи его кнопкой «📢 Мой ТГК»",
                              show_alert=True)
        return

    channel_id = int(channel["channel_id"])
    adm, can_post = await _channel_admin_state(event_bot(callback), channel_id)
    if not adm or not can_post:
        await callback.answer(
            "⚠️ Боту нужны права администратора с «Публикацией сообщений»",
            show_alert=True,
        )
        return

    buttons_json = dump_buttons(data.get("buttons") or [])
    post = {
        "channel_id": channel_id,
        "text": data.get("post_text") or "",
        "photo": data.get("post_photo") or "",
        "buttons": buttons_json,
    }
    ok, message_id, error, premium_lost = await publish_channel_post(
        event_bot(callback), post
    )
    if not ok:
        await callback.answer(f"❌ Не получилось: {error[:100]}", show_alert=True)
        return

    post_id = add_channel_post(cb_uid(callback), channel_id, post["text"], post["photo"],
                               buttons_json, "published", "")
    update_channel_post(post_id, message_id=message_id)
    await state.clear()

    link = post_link(channel, message_id)
    text = "✅ <b>Пост опубликован!</b>\n"
    if link:
        text += f"🔗 {link}\n"
    if premium_lost:
        text += "\n" + PREMIUM_LOST_HINT + "\n"
    text += "\nЧто дальше?"
    await render_callback(callback, text, _channel_menu_kb(), force_answer=True)


@router.callback_query(F.data == "ch_publish_later")
async def cb_publish_later(callback: CallbackQuery, state: FSMContext) -> None:
    """Отложенная публикация — шаг 1: день."""
    data = await state.get_data()
    if not data.get("post_text") and not data.get("post_photo"):
        await callback.answer("⚠️ Черновик потерян — начни заново", show_alert=True)
        return
    if not get_bound_channel(cb_uid(callback)):
        await callback.answer("⚠️ ТГК не привязан — привяжи его кнопкой «📢 Мой ТГК»",
                              show_alert=True)
        return

    await state.set_state(ChannelFSM.waiting_day)
    await render_callback(
        callback,
        "📅 <b>Когда опубликовать?</b>\n\n"
        "Нажми «Сегодня» / «Завтра» или пришли дату, например <code>17.08</code> "
        "или <code>17.08.2026</code>.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Сегодня", callback_data="ch_day_today",
                                  style=BTN_SUCCESS)],
            [InlineKeyboardButton(text="Завтра", callback_data="ch_day_tomorrow",
                                  style=BTN_PRIMARY)],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ch_cancel",
                                  style=BTN_DANGER)],
        ]),
        force_answer=True,
    )


async def _ask_time(state: FSMContext, day: date) -> str:
    """Сохраняет день и возвращает текст запроса времени."""
    await state.update_data(day=day.isoformat())
    await state.set_state(ChannelFSM.waiting_time)
    return (
        f"🕐 <b>Во сколько выложить {day.strftime('%d.%m.%Y')}?</b>\n\n"
        "Пришли время по МСК, например <code>15:00</code>."
    )


async def _handle_day_choice(callback: CallbackQuery, state: FSMContext, raw: str) -> None:
    """Общий обработчик выбора дня (кнопки «Сегодня»/«Завтра»)."""
    day = parse_day(raw)
    if day is None:
        await callback.answer("⚠️ Не понял день", show_alert=True)
        return
    text = await _ask_time(state, day)
    await render_callback(callback, text, _cancel_kb(), force_answer=True)


@router.callback_query(F.data == "ch_day_today")
async def cb_day_today(callback: CallbackQuery, state: FSMContext) -> None:
    await _handle_day_choice(callback, state, "сегодня")


@router.callback_query(F.data == "ch_day_tomorrow")
async def cb_day_tomorrow(callback: CallbackQuery, state: FSMContext) -> None:
    await _handle_day_choice(callback, state, "завтра")


@router.message(ChannelFSM.waiting_day, F.chat.type == ChatType.PRIVATE)
async def fsm_post_day(message: Message, state: FSMContext) -> None:
    """Принял день публикации текстом (например «17.08»)."""
    day = parse_day(message.text or "")
    if day is None:
        await message.answer(
            "❌ Не понял дату.\n\n"
            "Пришли её как <code>17.08</code> или <code>17.08.2026</code> "
            "(дата не должна быть в прошлом).",
            reply_markup=_cancel_kb(),
        )
        return
    text = await _ask_time(state, day)
    await message.answer(text, reply_markup=_cancel_kb())


@router.message(ChannelFSM.waiting_time, F.chat.type == ChatType.PRIVATE)
async def fsm_post_time(message: Message, state: FSMContext) -> None:
    """Принял время публикации — ставим пост в очередь."""
    time_str = parse_time(message.text or "")
    if not time_str:
        await message.answer(
            "❌ Не понял время.\n\nПришли его как <code>15:00</code> (по МСК).",
            reply_markup=_cancel_kb(),
        )
        return

    data = await state.get_data()
    try:
        day = date.fromisoformat(data.get("day") or "")
    except ValueError:
        await message.answer("⚠️ День потерялся — начни заново.", reply_markup=_cancel_kb())
        return

    moment = msk_moment(day, time_str)
    if moment <= datetime.now(moment.tzinfo):
        await message.answer(
            "❌ Это время уже прошло. Пришли более позднее время (по МСК).",
            reply_markup=_cancel_kb(),
        )
        return

    owner_id = msg_uid(message)
    channel = get_bound_channel(owner_id)
    if not channel:
        await state.clear()
        await message.answer(
            "⚠️ ТГК не привязан — привяжи его кнопкой «📢 Мой ТГК» в главном меню.",
            reply_markup=_no_channel_kb(),
        )
        return

    publish_at = to_utc_str(moment)
    post_id = add_channel_post(
        owner_id, int(channel["channel_id"]),
        data.get("post_text") or "", data.get("post_photo") or "",
        dump_buttons(data.get("buttons") or []), "scheduled", publish_at,
    )
    await state.clear()

    await message.answer(
        "✅ <b>Пост запланирован!</b>\n\n"
        f"📅 {to_msk_str(publish_at)} (МСК)\n"
        f"🆔 Пост #{post_id}\n\n"
        "Посмотреть, отредактировать или отменить его можно в "
        "«🕒 Отложенные посты».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🕒 Отложенные посты", callback_data="ch_sched_list",
                                  style=BTN_PRIMARY)],
            [InlineKeyboardButton(text="⬅️ В меню ТГК", callback_data="ch_menu",
                                  style=BTN_PRIMARY)],
        ]),
    )
# ═══════════════ Отложенные посты ═══════════════


def _buttons_style_line(buttons: list[dict]) -> str:
    """Короткая строка про цвета кнопок поста (для карточки отложенного поста)."""
    if not buttons:
        return ""
    colors = ", ".join(
        BUTTON_STYLE_LABELS.get(safe_button_style(b.get("style")), "⬜ без цвета")
        for b in buttons
    )
    return f"\n🎨 Цвета кнопок: {colors}"


def _scheduled_post_text(post: dict) -> str:
    buttons = parse_buttons(post.get("buttons"))
    return (
        f"🕒 <b>Отложенный пост #{post['id']}</b>\n"
        f"📅 {to_msk_str(post.get('publish_at'))} (МСК)\n"
        f"🔗 Кнопок: <b>{len(buttons)}</b>{_buttons_style_line(buttons)}\n\n"
        f"{_plain_preview(post.get('text') or '', 400)}"
    )


def _scheduled_post_kb(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"ch_edit_{post_id}",
                              style=BTN_PRIMARY)],
        [InlineKeyboardButton(text="🔴 Отменить пост", callback_data=f"ch_del_{post_id}",
                              style=BTN_DANGER)],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="ch_sched_list",
                              style=BTN_PRIMARY)],
    ])


@router.callback_query(F.data == "ch_sched_list")
async def cb_sched_list(callback: CallbackQuery, state: FSMContext) -> None:
    """«🕒 Отложенные посты» — список с переходом к каждому посту."""
    await state.clear()
    await callback.answer()
    await _render_sched_list(callback)


async def _render_sched_list(callback: CallbackQuery) -> None:
    """Рисует список отложенных постов (используется и после отмены поста)."""
    owner_id = cb_uid(callback)
    posts = get_channel_posts(owner_id, "scheduled")

    if not posts:
        await _render(
            callback,
            "🕒 <b>Отложенные посты</b>\n\nПока ничего не запланировано.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Выложить пост", callback_data="ch_post_new",
                                      style=BTN_SUCCESS)],
                [InlineKeyboardButton(text="⬅️ В меню ТГК", callback_data="ch_menu",
                                      style=BTN_PRIMARY)],
            ]),
        )
        return

    lines = [f"🕒 <b>Отложенные посты</b> — {len(posts)}\n"]
    rows: list[list[InlineKeyboardButton]] = []
    for p in posts[:20]:
        when = to_msk_str(p.get("publish_at"))
        lines.append(
            f"• <b>#{p['id']}</b> — {when} (МСК)\n  {_plain_preview(p.get('text') or '', 60)}"
        )
        rows.append([InlineKeyboardButton(text=f"#{p['id']} — {when}",
                                          callback_data=f"ch_post_{p['id']}",
                                          style=BTN_PRIMARY)])
    if len(posts) > 20:
        lines.append(f"\n…и ещё <b>{len(posts) - 20}</b> (показаны 20 последних)")

    lines.append("\nВыбери пост, чтобы посмотреть, изменить или отменить:")
    rows.append([InlineKeyboardButton(text="⬅️ В меню ТГК", callback_data="ch_menu",
                                      style=BTN_PRIMARY)])
    await _render(callback, "\n".join(lines),
                  InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^ch_post_\d+$"))
async def cb_sched_post(callback: CallbackQuery, state: FSMContext) -> None:
    """Просмотр конкретного отложенного поста."""
    await state.clear()
    post_id = int((callback.data or "").rsplit("_", 1)[-1])
    post = get_channel_post(post_id)
    if not post or int(post.get("owner_id") or 0) != cb_uid(callback):
        await callback.answer("⚠️ Пост не найден", show_alert=True)
        return
    await render_callback(callback, _scheduled_post_text(post), _scheduled_post_kb(post_id),
                          force_answer=True)


@router.callback_query(F.data.regexp(r"^ch_edit_\d+$"))
async def cb_edit_scheduled(callback: CallbackQuery, state: FSMContext) -> None:
    """Правка текста отложенного поста."""
    post_id = int((callback.data or "").rsplit("_", 1)[-1])
    post = get_channel_post(post_id)
    if not post or int(post.get("owner_id") or 0) != cb_uid(callback):
        await callback.answer("⚠️ Пост не найден", show_alert=True)
        return
    await state.set_state(ChannelFSM.editing_post)
    await state.update_data(post_id=post_id)
    await render_callback(
        callback,
        f"✏️ <b>Правка поста #{post_id}</b>\n\n"
        "Пришли новый текст (можно с фото). Текущее время публикации "
        f"не изменится: {to_msk_str(post.get('publish_at'))} (МСК).",
        _cancel_kb(),
        force_answer=True,
    )


@router.message(ChannelFSM.editing_post, F.chat.type == ChatType.PRIVATE)
async def fsm_edit_scheduled(message: Message, state: FSMContext) -> None:
    """Принял новый текст отложенного поста."""
    data = await state.get_data()
    post_id = int(data.get("post_id") or 0)
    post = get_channel_post(post_id)
    if not post or int(post.get("owner_id") or 0) != msg_uid(message):
        await state.clear()
        await message.answer("⚠️ Пост не найден или уже опубликован.")
        return

    if message.photo:
        update_channel_post(post_id, text=message.html_text or message.caption or "",
                            photo=message.photo[-1].file_id)
    elif message.text:
        update_channel_post(post_id, text=message.html_text or message.text)
    else:
        await message.answer("⚠️ Нужен текст или фото.", reply_markup=_cancel_kb())
        return

    await state.clear()
    updated = get_channel_post(post_id) or post
    await message.answer("✅ Пост обновлён.")
    await message.answer(_scheduled_post_text(updated),
                         reply_markup=_scheduled_post_kb(post_id))
# ── Отмена отложенного поста ──


@router.callback_query(F.data.regexp(r"^ch_del_\d+$"))
async def cb_delete_scheduled(callback: CallbackQuery) -> None:
    """Спрашивает подтверждение отмены отложенного поста."""
    post_id = int((callback.data or "").rsplit("_", 1)[-1])
    post = get_channel_post(post_id)
    if not post or int(post.get("owner_id") or 0) != cb_uid(callback):
        await callback.answer("⚠️ Пост не найден", show_alert=True)
        return
    await render_callback(
        callback,
        f"🔴 <b>Отменить пост #{post_id}?</b>\n\n"
        f"📅 Он был запланирован на {to_msk_str(post.get('publish_at'))} (МСК).\n\n"
        "Это действие необратимо.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔴 Да, отменить", callback_data=f"ch_del_yes_{post_id}",
                                  style=BTN_DANGER)],
            [InlineKeyboardButton(text="⬅️ Нет, назад", callback_data=f"ch_post_{post_id}",
                                  style=BTN_PRIMARY)],
        ]),
        force_answer=True,
    )


@router.callback_query(F.data.regexp(r"^ch_del_yes_\d+$"))
async def cb_delete_scheduled_yes(callback: CallbackQuery) -> None:
    """Отменяет (удаляет) отложенный пост."""
    post_id = int((callback.data or "").rsplit("_", 1)[-1])
    post = get_channel_post(post_id)
    if not post or int(post.get("owner_id") or 0) != cb_uid(callback):
        await callback.answer("⚠️ Пост не найден", show_alert=True)
        return
    delete_channel_post(post_id)
    await callback.answer("🔴 Пост отменён")
    await _render_sched_list(callback)


# ═══════════════ Отвязка канала ═══════════════


@router.callback_query(F.data == "ch_unbind")
async def cb_unbind(callback: CallbackQuery, state: FSMContext) -> None:
    """«🔴 Отвязать ТГК» — сначала подтверждение."""
    await state.clear()
    owner_id = cb_uid(callback)
    channel = get_bound_channel(owner_id)
    if not channel:
        await callback.answer("ℹ️ ТГК уже не привязан", show_alert=False)
        await render_callback(callback, "🔗 ТГК не привязан.", _no_channel_kb(),
                              force_answer=True)
        return

    await render_callback(
        callback,
        "🔴 <b>Отвязать ТГК?</b>\n\n"
        f"📎 Канал: <b>{_channel_title(channel)}</b>\n"
        f"🆔 <code>{channel.get('channel_id')}</code>\n\n"
        "Бот выйдет из канала, отложенные посты будут удалены. "
        "Чтобы вернуть публикации — снова добавь бота админом.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔴 Да, отвязать", callback_data="ch_unbind_yes",
                                  style=BTN_DANGER)],
            [InlineKeyboardButton(text="⬅️ Нет, назад", callback_data="ch_menu",
                                  style=BTN_PRIMARY)],
        ]),
        force_answer=True,
    )


@router.callback_query(F.data == "ch_unbind_yes")
async def cb_unbind_yes(callback: CallbackQuery, state: FSMContext) -> None:
    """Отвязывает канал: бот выходит из канала (если может)."""
    await state.clear()
    owner_id = cb_uid(callback)
    channel = get_bound_channel(owner_id)
    channel_id = int(channel["channel_id"]) if channel else 0

    if channel_id:
        try:
            await event_bot(callback).leave_chat(channel_id)
        except Exception as e:
            logger.warning("Не удалось выйти из канала %s: %s", channel_id, e)

    unbind_channel(owner_id)
    await callback.answer("🔴 ТГК отвязан")

    await _render(
        callback,
        "🔴 <b>ТГК отвязан.</b>\n\n"
        f"📎 Канал: <code>{channel_id or '—'}</code>\n"
        "Отложенные посты удалены.\n\n"
        "Чтобы привязать снова — добавь бота админом в канал и нажми "
        "«🔗 Привязать ТГК».",
        _no_channel_kb(),
    )