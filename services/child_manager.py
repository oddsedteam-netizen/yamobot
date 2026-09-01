import asyncio
import json
import logging
import time
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType, ChatType
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    CallbackQuery,
    MessageEntity,
    ReplyKeyboardMarkup,
)

from services.storage import (
    add_child_user,
    add_stat,
    add_admin_message,
    ban_user,
    unban_user,
    is_user_banned,
    get_all_bots_flat,
    get_bot_by_id_any_owner,
    get_child_users,
    get_admin_by_user_id,
    get_admin_message_stats,
    get_admin_active_topics,
    mark_user_blocked,
    save_mailing,
    get_antispam_mode,
    set_feedback_chat,
    get_feedback_chat,
    get_topic_by_user,
    get_topic_by_topic_id,
    create_topic_record,
    assign_admin_to_topic,
    reset_topic_admin,
    save_feedback_message,
    get_feedback_msg_by_group_msg,
    get_feedback_msg_by_user_msg,
    get_bot_owner,
    get_bot_keyboard_by_bot,
    get_bot_type,
)

logger = logging.getLogger(__name__)


def _build_welcome_kb(bot_data: dict) -> InlineKeyboardMarkup | None:
    try:
        links = json.loads(bot_data.get("links", "[]"))
    except (json.JSONDecodeError, TypeError):
        links = []
    if not links:
        return None
    rows = []
    for link in links:
        rows.append([InlineKeyboardButton(text=link["text"], url=link["url"])])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_reply_kb(bot_data: dict) -> ReplyKeyboardMarkup | None:
    """Строит reply-клавиатуру дочернего бота из его настроек.

    Для анкетницы (anketa) reply-кнопки не используются — возвращает None.
    Для обычного бота используются сохранённые кнопки (или дефолт «сменить админа»).
    """
    if bot_data.get("bot_type") == "anketa":
        return None

    buttons = get_bot_keyboard_by_bot(bot_data["id"])
    rows: list[list[KeyboardButton]] = []
    for item in buttons:
        if not isinstance(item, dict):
            continue
        text = item.get("text", "").strip()
        if not text:
            continue
        rows.append([KeyboardButton(text=text)])

    if not rows:
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="сменить админа")]], resize_keyboard=True)

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def _send_to_topic(source_msg: Message, bot: Bot,
                         group_chat_id: int, topic_id: int,
                         reply_to: int | None = None) -> Message | None:
    kwargs = {"chat_id": group_chat_id, "message_thread_id": topic_id}
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to

    try:
        if source_msg.photo:
            return await bot.send_photo(**kwargs, photo=source_msg.photo[-1].file_id,
                                         caption=source_msg.html_text or source_msg.caption or "")
        elif source_msg.video:
            return await bot.send_video(**kwargs, video=source_msg.video.file_id,
                                         caption=source_msg.html_text or source_msg.caption or "")
        elif source_msg.animation:
            return await bot.send_animation(**kwargs, animation=source_msg.animation.file_id,
                                             caption=source_msg.html_text or source_msg.caption or "")
        elif source_msg.document:
            return await bot.send_document(**kwargs, document=source_msg.document.file_id,
                                            caption=source_msg.html_text or source_msg.caption or "")
        elif source_msg.sticker:
            return await bot.send_sticker(**kwargs, sticker=source_msg.sticker.file_id)
        elif source_msg.voice:
            return await bot.send_voice(**kwargs, voice=source_msg.voice.file_id,
                                         caption=source_msg.caption or "")
        elif source_msg.video_note:
            return await bot.send_video_note(**kwargs, video_note=source_msg.video_note.file_id)
        elif source_msg.audio:
            return await bot.send_audio(**kwargs, audio=source_msg.audio.file_id,
                                         caption=source_msg.html_text or source_msg.caption or "")
        else:
            text = source_msg.html_text or source_msg.text or ""
            if text:
                return await bot.send_message(**kwargs, text=text)
    except Exception as e:
        logger.error("Ошибка отправки в топик: %s", e)
    return None


async def _send_to_user(source_msg: Message, bot: Bot,
                        chat_id: int, reply_to: int | None = None,
                        bot_id: int | None = None) -> Message | None:
    kwargs = {"chat_id": chat_id}
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to

    try:
        if source_msg.photo:
            return await bot.send_photo(**kwargs, photo=source_msg.photo[-1].file_id,
                                         caption=source_msg.html_text or source_msg.caption or "")
        elif source_msg.video:
            return await bot.send_video(**kwargs, video=source_msg.video.file_id,
                                         caption=source_msg.html_text or source_msg.caption or "")
        elif source_msg.animation:
            return await bot.send_animation(**kwargs, animation=source_msg.animation.file_id,
                                             caption=source_msg.html_text or source_msg.caption or "")
        elif source_msg.document:
            return await bot.send_document(**kwargs, document=source_msg.document.file_id,
                                            caption=source_msg.html_text or source_msg.caption or "")
        elif source_msg.sticker:
            return await bot.send_sticker(**kwargs, sticker=source_msg.sticker.file_id)
        elif source_msg.voice:
            return await bot.send_voice(**kwargs, voice=source_msg.voice.file_id,
                                         caption=source_msg.caption or "")
        elif source_msg.video_note:
            return await bot.send_video_note(**kwargs, video_note=source_msg.video_note.file_id)
        elif source_msg.audio:
            return await bot.send_audio(**kwargs, audio=source_msg.audio.file_id,
                                         caption=source_msg.html_text or source_msg.caption or "")
        else:
            text = source_msg.html_text or source_msg.text or ""
            if text:
                return await bot.send_message(**kwargs, text=text)
    except TelegramForbiddenError:
        # Юзер заблокировал/забанил бота — обрабатываем топик
        if bot_id:
            await _handle_user_blocked(bot, bot_id, chat_id)
        return None
    except Exception as e:
        logger.error("Ошибка отправки юзеру: %s", e)
    return None


async def _handle_user_blocked(bot: Bot, bot_id: int, user_chat_id: int) -> None:
    """Юзер забанил бота: помечаем заблокированным, переименовываем и закрываем топик,
    уведомляем админа в этом же топике."""
    try:
        mark_user_blocked(bot_id, user_chat_id)
    except Exception:
        pass

    topic = get_topic_by_user(bot_id, user_chat_id)
    if not topic:
        return

    g_id = topic["group_chat_id"]
    t_id = topic["topic_id"]
    reset_topic_admin(bot_id, t_id, g_id)

    try:
        await bot.edit_forum_topic(chat_id=g_id, message_thread_id=t_id, name="🚫 забанил бота")
    except Exception:
        pass

    try:
        await bot.send_message(
            chat_id=g_id, message_thread_id=t_id,
            text=f"🚫 Пользователь <code>{user_chat_id}</code> забанил бота.\nТопик закрыт."
        )
    except Exception:
        pass

    try:
        await bot.close_forum_topic(chat_id=g_id, message_thread_id=t_id)
    except Exception:
        pass


def _make_child_dp(bot_data: dict, bot_obj: Bot) -> Dispatcher:
    child_dp = Dispatcher()
    bot_id = bot_data["id"]

    sticker_counts: dict[int, list[float]] = defaultdict(list)
    sticker_warnings: dict[int, bool] = defaultdict(bool)
    last_message_time: dict[int, float] = {}

    # ═══════════════ /start в ЛС ═══════════════

    @child_dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
    async def child_start(message: Message) -> None:
        if is_user_banned(bot_id, message.from_user.id):
            return

        add_child_user(
            bot_id, message.from_user.id,
            message.from_user.username or "",
            message.from_user.first_name or ""
        )
        add_stat(bot_id, "message_in")

        fresh = get_bot_by_id_any_owner(bot_id)
        if not fresh:
            return

        welcome = fresh.get("welcome_text", "") or f"👋 Привет! Я {fresh.get('first_name', 'бот')}."

        # Инлайн-кнопки (ссылки) прикрепляем прямо к приветствию.
        welcome_kb = _build_welcome_kb(fresh)
        try:
            if welcome_kb:
                await message.answer(welcome, reply_markup=welcome_kb)
            else:
                await message.answer(welcome)
        except Exception:
            try:
                await message.answer(welcome)
            except Exception:
                pass

        # Reply-клавиатуру дочернего бота активируем отдельным сообщением,
        # чтобы её можно было показывать вместе с инлайн-ссылками.
        reply_kb = _build_reply_kb(fresh)
        if reply_kb:
            try:
                await message.answer("Выберите действие:", reply_markup=reply_kb)
            except Exception:
                pass
        add_stat(bot_id, "message_out")

    # ═══════════════ /connect в группе ═══════════════

    @child_dp.message(Command("connect"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
    async def cmd_connect_group(message: Message) -> None:
        chat = message.chat
        set_feedback_chat(bot_id, chat.id)
        await message.answer(
            f"✅ Чат <b>{chat.title}</b> подключён.\n"
            f"Сообщения от пользователей будут создавать топики здесь."
        )

    # ═══════════════ /ban в топике ═══════════════

    @child_dp.message(
        Command("ban"),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id")
    )
    async def cmd_ban(message: Message, thread_id: int) -> None:
        group_chat_id = message.chat.id
        topic = get_topic_by_topic_id(bot_id, group_chat_id, thread_id)
        if not topic:
            return

        user_chat_id = topic["user_chat_id"]
        ban_user(bot_id, user_chat_id)

        try:
            await bot_obj.send_message(
                chat_id=user_chat_id,
                text="🚫 Вас заблокировали в данном боте навсегда. Всего доброго."
            )
        except Exception as e:
            logger.warning("Не удалось отправить бан-уведомление: %s", e)

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=thread_id, name="🚫 забанен"
            )
        except Exception:
            pass

        try:
            await bot_obj.close_forum_topic(
                chat_id=group_chat_id, message_thread_id=thread_id
            )
        except Exception:
            pass

        reset_topic_admin(bot_id, thread_id, group_chat_id)
        await message.answer(f"✅ Пользователь <code>{user_chat_id}</code> заблокирован.")

    # ═══════════════ /unban в топике ═══════════════

    @child_dp.message(
        Command("unban"),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id")
    )
    async def cmd_unban(message: Message, thread_id: int) -> None:
        group_chat_id = message.chat.id
        topic = get_topic_by_topic_id(bot_id, group_chat_id, thread_id)
        if not topic:
            return

        user_chat_id = topic["user_chat_id"]

        # Снимаем бан
        success = unban_user(bot_id, user_chat_id)
        if not success:
            await message.answer("⚠️ Пользователь не найден в базе.")
            return

        # Отправляем юзеру сообщение
        try:
            await bot_obj.send_message(
                chat_id=user_chat_id,
                text="✅ Вас разбанили в боте, можете снова писать."
            )
        except Exception as e:
            logger.warning("Не удалось отправить разбан-уведомление: %s", e)

        # Сбрасываем админа
        reset_topic_admin(bot_id, thread_id, group_chat_id)

        # Переименовываем топик
        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id,
                message_thread_id=thread_id,
                name="⏳ без админа"
            )
        except Exception:
            pass

        # Открываем топик если был закрыт
        try:
            await bot_obj.reopen_forum_topic(
                chat_id=group_chat_id,
                message_thread_id=thread_id
            )
        except Exception:
            pass

        # Отправляем кнопку "Я беру"
        await bot_obj.send_message(
            chat_id=group_chat_id,
            message_thread_id=thread_id,
            text=f"🔓 Пользователь <code>{user_chat_id}</code> разбанен.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✋ Я беру",
                    callback_data=f"take_user_{thread_id}_{group_chat_id}"
                )]
            ])
        )

        await message.answer(f"✅ Пользователь <code>{user_chat_id}</code> разбанен.")

    # ═══════════════ /otkaz в топике ═══════════════

    @child_dp.message(
        Command("otkaz"),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id")
    )
    async def cmd_otkaz(message: Message, thread_id: int) -> None:
        group_chat_id = message.chat.id
        topic = get_topic_by_topic_id(bot_id, group_chat_id, thread_id)
        if not topic:
            return

        user_chat_id = topic["user_chat_id"]
        reset_topic_admin(bot_id, thread_id, group_chat_id)

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=thread_id, name="🔄 смена админа"
            )
        except Exception:
            pass

        await bot_obj.send_message(
            chat_id=user_chat_id,
            text="⚠️ Ваш администратор отказался от вас.\nПодобрать нового?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Найти админа",
                                       callback_data=f"find_admin_{thread_id}_{group_chat_id}")]
            ])
        )
        await message.answer("✅ Пользователю отправлено уведомление.")

    # ═══════════════ /smena — смена админа без подтверждения (для админа) ═══════════════

    @child_dp.message(
        Command("smena"),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id")
    )
    async def cmd_smena(message: Message, thread_id: int) -> None:
        group_chat_id = message.chat.id
        topic = get_topic_by_topic_id(bot_id, group_chat_id, thread_id)
        if not topic:
            return

        user_chat_id = topic["user_chat_id"]
        reset_topic_admin(bot_id, thread_id, group_chat_id)

        # Уведомляем самого юзера, что его админа меняют
        try:
            await bot_obj.send_message(
                chat_id=user_chat_id,
                text="🔄 Вашего администратора меняют. С вами скоро свяжется новый админ."
            )
        except Exception:
            pass

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=thread_id, name="🔄 смена админа"
            )
        except Exception:
            pass

        await bot_obj.send_message(
            chat_id=group_chat_id, message_thread_id=thread_id,
            text="🔔 Пользователь запросил смену админа!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✋ Я беру",
                                       callback_data=f"take_user_{thread_id}_{group_chat_id}")]
            ])
        )
        await message.answer("✅ Запрос на смену админа отправлен.")


    # ═══════════════ Callback: "я беру" ═══════════════

    @child_dp.callback_query(F.data.startswith("take_user_"))
    async def cb_take_user(callback: CallbackQuery) -> None:
        parts = callback.data.split("_")
        topic_id = int(parts[2])
        group_chat_id = int(parts[3])

        admin = get_admin_by_user_id(get_bot_owner(bot_id) or 0, callback.from_user.id)
        if admin:
            tag = admin["tag"]
        else:
            tag = callback.from_user.first_name or str(callback.from_user.id)

        # Назначаем и получаем инфу
        result = assign_admin_to_topic(bot_id, topic_id, group_chat_id, callback.from_user.id, tag)

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=topic_id, name=f"#{tag}"
            )
        except Exception as e:
            logger.warning("Не удалось переименовать топик: %s", e)

        if admin:
            add_admin_message(bot_id, callback.from_user.id, "action")

        # Только редактируем текст — НЕ удаляем инфу о юзере
        try:
            # Если это было сообщение с кнопкой "Я беру" — просто убираем кнопку
            if callback.message and callback.message.reply_markup:
                # Оставляем оригинальный текст, но добавляем строку
                original_text = callback.message.html_text or callback.message.text or ""
                new_text = f"{original_text}\n\n✅ Взял: <b>#{tag}</b>"
                await callback.message.edit_text(new_text, reply_markup=None)
        except Exception:
            pass

        # Если это была смена админа — уведомляем
        if result["is_change"]:
            try:
                await bot_obj.send_message(
                    chat_id=group_chat_id,
                    message_thread_id=topic_id,
                    text=f"🔄 Админ сменился на <b>#{tag}</b>"
                )
            except Exception:
                pass

        await callback.answer(f"Ты взял пользователя. Тег: #{tag}")

    # ═══════════════ Callback: "найти админа" ═══════════════

    @child_dp.callback_query(F.data.startswith("find_admin_"))
    async def cb_find_admin(callback: CallbackQuery) -> None:
        parts = callback.data.split("_")
        topic_id = int(parts[2])
        group_chat_id = int(parts[3])

        reset_topic_admin(bot_id, topic_id, group_chat_id)

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id, message_thread_id=topic_id, name="⏳ без админа"
            )
        except Exception:
            pass

        await bot_obj.send_message(
            chat_id=group_chat_id, message_thread_id=topic_id,
            text="🔔 Пользователь запросил нового админа!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✋ Я беру",
                                       callback_data=f"take_user_{topic_id}_{group_chat_id}")]
            ])
        )

        try:
            await callback.message.edit_text("✅ Запрос отправлен.")
        except Exception:
            pass
        await callback.answer()

    # ═══════════════ Callback: подтверждение смены ═══════════════

    @child_dp.callback_query(F.data.startswith("confirm_change_"))
    async def cb_confirm_change(callback: CallbackQuery) -> None:
        parts = callback.data.split("_")
        answer = parts[2]
        topic_id = int(parts[3])
        group_chat_id = int(parts[4])

        if answer == "yes":
            reset_topic_admin(bot_id, topic_id, group_chat_id)

            try:
                await bot_obj.edit_forum_topic(
                    chat_id=group_chat_id, message_thread_id=topic_id, name="🔄 смена админа"
                )
            except Exception:
                pass

            await bot_obj.send_message(
                chat_id=group_chat_id, message_thread_id=topic_id,
                text="🔔 Пользователь запросил смену админа!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✋ Я беру",
                                           callback_data=f"take_user_{topic_id}_{group_chat_id}")]
                ])
            )

            try:
                await callback.message.edit_text("✅ Запрос на смену админа отправлен.")
            except Exception:
                pass
        else:
            try:
                await callback.message.edit_text("👌 Оставляем текущего админа.")
            except Exception:
                pass

        await callback.answer()

    # ═══════════════ Сообщения из ЛС → топик ═══════════════

    @child_dp.message(F.chat.type == ChatType.PRIVATE)
    async def private_message(message: Message) -> None:
        if is_user_banned(bot_id, message.from_user.id):
            return

        add_stat(bot_id, "message_in")
        add_child_user(
            bot_id, message.from_user.id,
            message.from_user.username or "",
            message.from_user.first_name or ""
        )

        user_chat_id = message.from_user.id

        # Обработка "сменить админа"
        if message.text and message.text.strip().lower() == "сменить админа":
            topic = get_topic_by_user(bot_id, user_chat_id)
            if topic and topic["admin_user_id"]:
                await message.answer(
                    "❓ Вы уверены, что хотите сменить админа?",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(text="✅ Да",
                                                  callback_data=f"confirm_change_yes_{topic['topic_id']}_{topic['group_chat_id']}"),
                            InlineKeyboardButton(text="❌ Нет",
                                                  callback_data=f"confirm_change_no_{topic['topic_id']}_{topic['group_chat_id']}"),
                        ]
                    ])
                )
                return
            else:
                await message.answer("У вас сейчас нет назначенного админа.")
                return

        # Настройки антиспама
        now = time.time()
        antispam = get_antispam_mode(bot_id)

        if antispam == "manual":
            last = last_message_time.get(user_chat_id, 0)
            if now - last < 60:
                await message.answer("⏳ Подождите минуту.")
                return
            last_message_time[user_chat_id] = now

        # Авто-антиспам (с предупреждением и блокировкой)
        if antispam == "auto":
            if message.content_type == ContentType.STICKER:
                sticker_counts[user_chat_id].append(now)
                sticker_counts[user_chat_id] = [t for t in sticker_counts[user_chat_id] if now - t < 30]

                has_warning = sticker_warnings[user_chat_id]

                if len(sticker_counts[user_chat_id]) >= 5:
                    if not has_warning:
                        # Этап 1: Предупреждение
                        sticker_warnings[user_chat_id] = True
                        sticker_counts[user_chat_id] = []  # Очищаем счётчик для отслеживания следующих 5 стикеров
                        await message.answer(
                            "⚠️ <b>Предупреждение!</b>\n\n"
                            "Пожалуйста, прекратите спам стикерами. "
                            "Если вы отправите ещё 5 стикеров подряд, вы будете заблокированы навсегда!"
                        )
                        return
                    else:
                        # Этап 2: Бан навсегда
                        ban_user(bot_id, user_chat_id)
                        sticker_counts[user_chat_id] = []
                        sticker_warnings[user_chat_id] = False

                        try:
                            await message.answer("🚫 Вас заблокировали в данном боте навсегда. Всего доброго.")
                        except Exception:
                            pass

                        # Находим топик и закрываем его с плашкой "бан спам"
                        topic = get_topic_by_user(bot_id, user_chat_id)
                        if topic:
                            g_id = topic["group_chat_id"]
                            t_id = topic["topic_id"]
                            reset_topic_admin(bot_id, t_id, g_id)
                            try:
                                await bot_obj.edit_forum_topic(
                                    chat_id=g_id, message_thread_id=t_id, name="🚫 бан спам"
                                )
                                await bot_obj.close_forum_topic(
                                    chat_id=g_id, message_thread_id=t_id
                                )
                            except Exception:
                                pass
                        return
            else:
                # Если отправлен текст — сбрасываем контигуальный счетчик стикеров
                sticker_counts[user_chat_id] = []

        group_chat_id = get_feedback_chat(bot_id)
        if not group_chat_id:
            return

        topic = get_topic_by_user(bot_id, user_chat_id)

        if not topic:
            user_name = message.from_user.first_name or message.from_user.username or str(user_chat_id)
            try:
                forum_topic = await bot_obj.create_forum_topic(
                    chat_id=group_chat_id, name="⏳ без админа"
                )
                topic_id = forum_topic.message_thread_id
            except Exception as e:
                logger.error("Не удалось создать топик: %s", e)
                return

            create_topic_record(bot_id, user_chat_id, group_chat_id, topic_id)

            await bot_obj.send_message(
                chat_id=group_chat_id, message_thread_id=topic_id,
                text=f"👤 Новый пользователь: <b>{user_name}</b>\n🆔 <code>{user_chat_id}</code>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✋ Я беру",
                                           callback_data=f"take_user_{topic_id}_{group_chat_id}")]
                ])
            )

            sent = await _send_to_topic(message, bot_obj, group_chat_id, topic_id)
            if sent:
                save_feedback_message(bot_id, topic_id, group_chat_id, user_chat_id,
                                       "in", sent.message_id, message.message_id)

            add_stat(bot_id, "message_out")
            return

        topic_id = topic["topic_id"]

        reply_to_group = None
        if message.reply_to_message:
            orig = get_feedback_msg_by_user_msg(bot_id, user_chat_id, message.reply_to_message.message_id)
            if orig:
                reply_to_group = orig["group_msg_id"]

        sent = await _send_to_topic(message, bot_obj, group_chat_id, topic_id, reply_to_group)

        if sent:
            save_feedback_message(bot_id, topic_id, group_chat_id, user_chat_id,
                                   "in", sent.message_id, message.message_id)

    # ═══════════════ Сообщения из топика → юзеру ═══════════════

    @child_dp.message(
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.message_thread_id.as_("thread_id")
    )
    async def group_topic_message(message: Message, thread_id: int) -> None:
        if message.from_user and message.from_user.is_bot:
            return

        group_chat_id = message.chat.id
        topic = get_topic_by_topic_id(bot_id, group_chat_id, thread_id)
        if not topic:
            return

        user_chat_id = topic["user_chat_id"]

        if message.text and message.text.startswith("/"):
            return

        add_stat(bot_id, "message_out")

        admin = get_admin_by_user_id(get_bot_owner(bot_id) or 0, message.from_user.id)
        if admin:
            add_admin_message(bot_id, message.from_user.id, "out")

        reply_to_user = None
        if message.reply_to_message:
            orig = get_feedback_msg_by_group_msg(bot_id, group_chat_id, message.reply_to_message.message_id)
            if orig:
                reply_to_user = orig["user_msg_id"]

        sent = await _send_to_user(message, bot_obj, user_chat_id, reply_to_user, bot_id=bot_id)

        if sent:
            save_feedback_message(bot_id, thread_id, group_chat_id, user_chat_id,
                                   "out", message.message_id, sent.message_id)

    # ═══════════════ Игнорируем general ═══════════════

    @child_dp.message(
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        ~F.message_thread_id
    )
    async def ignore_general(message: Message) -> None:
        pass

    return child_dp


class ChildManager:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        self._bots: dict[int, Bot] = {}
        self._dispatchers: dict[int, Dispatcher] = {}

    async def start_child(self, bot_data: dict) -> bool:
        bot_id = bot_data["id"]
        token = bot_data.get("token", "")

        # Если задача уже жива — ничего не делаем.
        existing = self._tasks.get(bot_id)
        if existing and not existing.done():
            return True
        # Убираем зависшие записи от ранее упавшего процесса.
        if existing:
            self._tasks.pop(bot_id, None)
            self._bots.pop(bot_id, None)
            self._dispatchers.pop(bot_id, None)

        try:
            child_bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            me = await child_bot.get_me()
            logger.info("Подключаю бот: @%s (%s)", me.username, me.id)

            child_dp = _make_child_dp(bot_data, child_bot)
            await child_bot.delete_webhook(drop_pending_updates=True)

            task = asyncio.create_task(child_dp.start_polling(child_bot), name=f"child_{bot_id}")
            task.add_done_callback(self._make_task_done_callback(bot_id))

            self._tasks[bot_id] = task
            self._bots[bot_id] = child_bot
            self._dispatchers[bot_id] = child_dp

            logger.info("Бот @%s запущен", me.username)
            return True
        except Exception as e:
            logger.error("Не удалось запустить бот %s: %s", bot_id, e)
            return False

    def _make_task_done_callback(self, bot_id: int):
        """Возвращает колбэк, который чистит состояние при завершении задачи бота."""
        def _on_done(task: asyncio.Task) -> None:
            self._tasks.pop(bot_id, None)
            self._bots.pop(bot_id, None)
            self._dispatchers.pop(bot_id, None)
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error("Дочерний бот %s аварийно завершился: %s", bot_id, exc)
        return _on_done

    async def stop_child(self, bot_id: int) -> bool:
        if bot_id not in self._tasks:
            return False

        task = self._tasks.pop(bot_id)
        dp = self._dispatchers.pop(bot_id, None)
        bot = self._bots.pop(bot_id, None)

        if dp:
            await dp.stop_polling()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if bot:
            await bot.session.close()

        return True

    async def restart_child(self, bot_data: dict) -> bool:
        await self.stop_child(bot_data["id"])
        return await self.start_child(bot_data)

    async def start_all_children(self) -> None:
        all_bots = get_all_bots_flat()
        for bot_data in all_bots:
            if bot_data.get("stopped"):
                continue
            await self.start_child(bot_data)

    async def stop_all_children(self) -> None:
        bot_ids = list(self._tasks.keys())
        for bot_id in bot_ids:
            await self.stop_child(bot_id)

    def is_running(self, bot_id: int) -> bool:
        return bot_id in self._tasks and not self._tasks[bot_id].done()

    def get_bot(self, bot_id: int) -> Bot | None:
        return self._bots.get(bot_id)

    async def send_mailing(self, bot_id: int, text: str,
                           media_type: str = "", media_id: str = "",
                           entities=None, progress_callback=None) -> dict:
        bot = self._bots.get(bot_id)
        if not bot:
            return {"sent": 0, "failed": 0, "total": 0}

        msg_entities = None
        if entities:
            msg_entities = [MessageEntity(**e) for e in entities]

        users = get_child_users(bot_id, only_active=True)
        total = len(users)
        sent = 0
        failed = 0

        for i, user in enumerate(users):
            chat_id = user["chat_id"]
            try:
                if media_type == "photo" and media_id:
                    await bot.send_photo(chat_id=chat_id, photo=media_id,
                                         caption=text, caption_entities=msg_entities,
                                         parse_mode=None)
                elif media_type == "video" and media_id:
                    await bot.send_video(chat_id=chat_id, video=media_id,
                                         caption=text, caption_entities=msg_entities,
                                         parse_mode=None)
                elif media_type == "document" and media_id:
                    await bot.send_document(chat_id=chat_id, document=media_id,
                                            caption=text, caption_entities=msg_entities,
                                            parse_mode=None)
                elif media_type == "animation" and media_id:
                    await bot.send_animation(chat_id=chat_id, animation=media_id,
                                             caption=text, caption_entities=msg_entities,
                                             parse_mode=None)
                elif media_type == "sticker" and media_id:
                    await bot.send_sticker(chat_id=chat_id, sticker=media_id)
                else:
                    await bot.send_message(chat_id=chat_id, text=text,
                                           entities=msg_entities, parse_mode=None)
                sent += 1
                add_stat(bot_id, "message_out")
            except TelegramForbiddenError:
                await _handle_user_blocked(bot, bot_id, chat_id)
                failed += 1
            except Exception as e:
                logger.warning("Ошибка отправки %s -> %s: %s", bot_id, chat_id, e)
                failed += 1

            if progress_callback and ((i + 1) % 5 == 0 or (i + 1) == total):
                await progress_callback(sent, failed, total, i + 1)

            await asyncio.sleep(0.05)

        save_mailing(bot_id, text, media_type, media_id, sent, failed)
        add_stat(bot_id, "mailing_done")

        return {"sent": sent, "failed": failed, "total": total}
