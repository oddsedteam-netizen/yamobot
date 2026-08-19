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
    Message,
    CallbackQuery,
)

from services.storage import (
    add_child_user,
    add_stat,
    add_admin_message,
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


async def _send_to_topic(source_msg: Message, bot: Bot,
                         group_chat_id: int, topic_id: int,
                         reply_to: int | None = None) -> Message | None:
    kwargs = {
        "chat_id": group_chat_id,
        "message_thread_id": topic_id,
    }
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to

    try:
        if source_msg.photo:
            return await bot.send_photo(
                **kwargs, photo=source_msg.photo[-1].file_id,
                caption=source_msg.html_text or source_msg.caption or "",
            )
        elif source_msg.video:
            return await bot.send_video(
                **kwargs, video=source_msg.video.file_id,
                caption=source_msg.html_text or source_msg.caption or "",
            )
        elif source_msg.animation:
            return await bot.send_animation(
                **kwargs, animation=source_msg.animation.file_id,
                caption=source_msg.html_text or source_msg.caption or "",
            )
        elif source_msg.document:
            return await bot.send_document(
                **kwargs, document=source_msg.document.file_id,
                caption=source_msg.html_text or source_msg.caption or "",
            )
        elif source_msg.sticker:
            return await bot.send_sticker(**kwargs, sticker=source_msg.sticker.file_id)
        elif source_msg.voice:
            return await bot.send_voice(
                **kwargs, voice=source_msg.voice.file_id,
                caption=source_msg.caption or "",
            )
        elif source_msg.video_note:
            return await bot.send_video_note(**kwargs, video_note=source_msg.video_note.file_id)
        elif source_msg.audio:
            return await bot.send_audio(
                **kwargs, audio=source_msg.audio.file_id,
                caption=source_msg.html_text or source_msg.caption or "",
            )
        else:
            text = source_msg.html_text or source_msg.text or ""
            if text:
                return await bot.send_message(**kwargs, text=text)
    except Exception as e:
        logger.error("Ошибка отправки в топик: %s", e)
    return None


async def _send_to_user(source_msg: Message, bot: Bot,
                        chat_id: int, reply_to: int | None = None) -> Message | None:
    kwargs = {"chat_id": chat_id}
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to

    try:
        if source_msg.photo:
            return await bot.send_photo(
                **kwargs, photo=source_msg.photo[-1].file_id,
                caption=source_msg.html_text or source_msg.caption or "",
            )
        elif source_msg.video:
            return await bot.send_video(
                **kwargs, video=source_msg.video.file_id,
                caption=source_msg.html_text or source_msg.caption or "",
            )
        elif source_msg.animation:
            return await bot.send_animation(
                **kwargs, animation=source_msg.animation.file_id,
                caption=source_msg.html_text or source_msg.caption or "",
            )
        elif source_msg.document:
            return await bot.send_document(
                **kwargs, document=source_msg.document.file_id,
                caption=source_msg.html_text or source_msg.caption or "",
            )
        elif source_msg.sticker:
            return await bot.send_sticker(**kwargs, sticker=source_msg.sticker.file_id)
        elif source_msg.voice:
            return await bot.send_voice(
                **kwargs, voice=source_msg.voice.file_id,
                caption=source_msg.caption or "",
            )
        elif source_msg.video_note:
            return await bot.send_video_note(**kwargs, video_note=source_msg.video_note.file_id)
        elif source_msg.audio:
            return await bot.send_audio(
                **kwargs, audio=source_msg.audio.file_id,
                caption=source_msg.html_text or source_msg.caption or "",
            )
        else:
            text = source_msg.html_text or source_msg.text or ""
            if text:
                return await bot.send_message(**kwargs, text=text)
    except Exception as e:
        logger.error("Ошибка отправки юзеру: %s", e)
    return None


def _make_child_dp(bot_data: dict, bot_obj: Bot) -> Dispatcher:
    child_dp = Dispatcher()
    bot_id = bot_data["id"]

    sticker_counts: dict[int, list[float]] = defaultdict(list)
    last_message_time: dict[int, float] = {}

    # ═══════════════ /start в ЛС ═══════════════

    @child_dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
    async def child_start(message: Message) -> None:
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
        kb = _build_welcome_kb(fresh)
        await message.answer(welcome, reply_markup=kb)
        add_stat(bot_id, "message_out")

    # ═══════════════ /connect в группе ═══════════════

    @child_dp.message(Command("connect"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
    async def cmd_connect_group(message: Message) -> None:
        chat = message.chat
        set_feedback_chat(bot_id, chat.id)
        await message.answer(
            f"✅ Чат <b>{chat.title}</b> подключён как чат обратной связи.\n"
            f"Сообщения от пользователей будут создавать топики здесь."
        )

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
                chat_id=group_chat_id,
                message_thread_id=thread_id,
                name="🔄 смена админа"
            )
        except Exception as e:
            logger.warning("Не удалось переименовать топик: %s", e)

        await bot_obj.send_message(
            chat_id=user_chat_id,
            text="⚠️ Ваш администратор отказался от вас.\nПодобрать нового?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔍 Найти админа",
                    callback_data=f"find_admin_{thread_id}_{group_chat_id}"
                )]
            ])
        )
        await message.answer("✅ Пользователю отправлено уведомление.")

    # ═══════════════ "кто я" в general ═══════════════

    @child_dp.message(
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.text.lower() == "кто я"
    )
    async def cmd_who_am_i(message: Message) -> None:
        # Только если НЕ в топике (general)
        if message.message_thread_id:
            return

        admin = get_admin_by_user_id(message.from_user.id)
        if not admin:
            await message.reply("❌ Ты не зарегистрирован как админ.")
            return

        stats = get_admin_message_stats(admin["user_id"])
        topics_count = get_admin_active_topics(admin["user_id"])

        text = (
            f"👤 <b>Твой профиль</b>\n\n"
            f"🏷 Тег: <b>#{admin['tag']}</b>\n"
            f"📛 Username: @{admin['username']}\n\n"
            f"📊 <b>Сообщения:</b>\n"
            f"  📅 День: <b>{stats['day']}</b>\n"
            f"  📅 Неделя: <b>{stats['week']}</b>\n"
            f"  📅 Месяц: <b>{stats['month']}</b>\n"
            f"  📊 Всего: <b>{stats['total']}</b>\n\n"
            f"👥 ПЗ за тобой: <b>{topics_count}</b>"
        )
        await message.reply(text)

    # ═══════════════ Callback: "я беру" ═══════════════

    @child_dp.callback_query(F.data.startswith("take_user_"))
    async def cb_take_user(callback: CallbackQuery) -> None:
        parts = callback.data.split("_")
        topic_id = int(parts[2])
        group_chat_id = int(parts[3])

        admin = get_admin_by_user_id(callback.from_user.id)

        if admin:
            tag = admin["tag"]
        else:
            tag = callback.from_user.first_name or str(callback.from_user.id)

        assign_admin_to_topic(bot_id, topic_id, group_chat_id, callback.from_user.id, tag)

        try:
            await bot_obj.edit_forum_topic(
                chat_id=group_chat_id,
                message_thread_id=topic_id,
                name=f"#{tag}"
            )
        except Exception as e:
            logger.warning("Не удалось переименовать топик: %s", e)

        if admin:
            add_admin_message(bot_id, callback.from_user.id, "action")

        try:
            await callback.message.edit_text(f"✅ Админ <b>#{tag}</b> взял пользователя.")
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
                chat_id=group_chat_id,
                message_thread_id=topic_id,
                name="⏳ без админа"
            )
        except Exception:
            pass

        await bot_obj.send_message(
            chat_id=group_chat_id,
            message_thread_id=topic_id,
            text="🔔 Пользователь запросил нового админа!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✋ Я беру",
                    callback_data=f"take_user_{topic_id}_{group_chat_id}"
                )]
            ])
        )

        try:
            await callback.message.edit_text("✅ Запрос отправлен. Ожидайте нового админа.")
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
                    chat_id=group_chat_id,
                    message_thread_id=topic_id,
                    name="🔄 смена админа"
                )
            except Exception:
                pass

            await bot_obj.send_message(
                chat_id=group_chat_id,
                message_thread_id=topic_id,
                text="🔔 Пользователь запросил смену админа!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="✋ Я беру",
                        callback_data=f"take_user_{topic_id}_{group_chat_id}"
                    )]
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
        add_stat(bot_id, "message_in")
        add_child_user(
            bot_id, message.from_user.id,
            message.from_user.username or "",
            message.from_user.first_name or ""
        )

        user_chat_id = message.from_user.id

        # "сменить админа"
        if message.text and message.text.strip().lower() == "сменить админа":
            topic = get_topic_by_user(bot_id, user_chat_id)
            if topic and topic["admin_user_id"]:
                await message.answer(
                    "❓ Вы уверены, что хотите сменить админа?",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✅ Да",
                                callback_data=f"confirm_change_yes_{topic['topic_id']}_{topic['group_chat_id']}"
                            ),
                            InlineKeyboardButton(
                                text="❌ Нет",
                                callback_data=f"confirm_change_no_{topic['topic_id']}_{topic['group_chat_id']}"
                            ),
                        ]
                    ])
                )
                return
            else:
                await message.answer("У вас сейчас нет назначенного админа.")
                return

        # Антиспам
        now = time.time()
        antispam = get_antispam_mode(bot_id)

        if antispam == "manual":
            last = last_message_time.get(user_chat_id, 0)
            if now - last < 60:
                await message.answer("⏳ Подождите минуту перед следующим сообщением.")
                return
            last_message_time[user_chat_id] = now

        if antispam == "auto" and message.content_type == ContentType.STICKER:
            sticker_counts[user_chat_id].append(now)
            sticker_counts[user_chat_id] = [t for t in sticker_counts[user_chat_id] if now - t < 30]
            if len(sticker_counts[user_chat_id]) >= 5:
                await message.answer("🛡 Слишком много стикеров.")
                sticker_counts[user_chat_id] = []
                return

        # Чат обратной связи
        group_chat_id = get_feedback_chat(bot_id)
        if not group_chat_id:
            return

        topic = get_topic_by_user(bot_id, user_chat_id)

        if not topic:
            # Создаём топик
            user_name = message.from_user.first_name or message.from_user.username or str(user_chat_id)
            try:
                forum_topic = await bot_obj.create_forum_topic(
                    chat_id=group_chat_id,
                    name="⏳ без админа"
                )
                topic_id = forum_topic.message_thread_id
            except Exception as e:
                logger.error("Не удалось создать топик: %s", e)
                return

            create_topic_record(bot_id, user_chat_id, group_chat_id, topic_id)

            # Инфо о юзере + кнопка "Я беру"
            await bot_obj.send_message(
                chat_id=group_chat_id,
                message_thread_id=topic_id,
                text=f"👤 Новый пользователь: <b>{user_name}</b>\n🆔 <code>{user_chat_id}</code>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="✋ Я беру",
                        callback_data=f"take_user_{topic_id}_{group_chat_id}"
                    )]
                ])
            )

            # Первое сообщение в топик
            sent = await _send_to_topic(message, bot_obj, group_chat_id, topic_id)
            if sent:
                save_feedback_message(
                    bot_id, topic_id, group_chat_id, user_chat_id,
                    "in", sent.message_id, message.message_id
                )

            add_stat(bot_id, "message_out")
            return

        # Существующий топик
        topic_id = topic["topic_id"]

        reply_to_group = None
        if message.reply_to_message:
            orig = get_feedback_msg_by_user_msg(bot_id, user_chat_id, message.reply_to_message.message_id)
            if orig:
                reply_to_group = orig["group_msg_id"]

        sent = await _send_to_topic(message, bot_obj, group_chat_id, topic_id, reply_to_group)

        if sent:
            save_feedback_message(
                bot_id, topic_id, group_chat_id, user_chat_id,
                "in", sent.message_id, message.message_id
            )

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

        admin = get_admin_by_user_id(message.from_user.id)
        if admin:
            add_admin_message(bot_id, message.from_user.id, "out")

        reply_to_user = None
        if message.reply_to_message:
            orig = get_feedback_msg_by_group_msg(bot_id, group_chat_id, message.reply_to_message.message_id)
            if orig:
                reply_to_user = orig["user_msg_id"]

        sent = await _send_to_user(message, bot_obj, user_chat_id, reply_to_user)

        if sent:
            save_feedback_message(
                bot_id, thread_id, group_chat_id, user_chat_id,
                "out", message.message_id, sent.message_id
            )

    # ═══════════════ Игнорируем general ═══════════════

    @child_dp.message(
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        ~F.message_thread_id
    )
    async def ignore_general(message: Message) -> None:
        # Сообщения в general без thread_id — игнорируем
        # кроме команд и "кто я" которые уже обработаны выше
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

        if bot_id in self._tasks and not self._tasks[bot_id].done():
            logger.info("Дочерний бот %s уже запущен", bot_id)
            return True

        try:
            child_bot = Bot(
                token=token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            me = await child_bot.get_me()
            logger.info("Подключаю дочерний бот: @%s (%s)", me.username, me.id)

            child_dp = _make_child_dp(bot_data, child_bot)
            await child_bot.delete_webhook(drop_pending_updates=True)

            task = asyncio.create_task(
                child_dp.start_polling(child_bot),
                name=f"child_{bot_id}"
            )

            self._tasks[bot_id] = task
            self._bots[bot_id] = child_bot
            self._dispatchers[bot_id] = child_dp

            logger.info("Дочерний бот @%s запущен", me.username)
            return True
        except Exception as e:
            logger.error("Не удалось запустить бот %s: %s", bot_id, e)
            return False

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

        logger.info("Дочерний бот %s остановлен", bot_id)
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
                           progress_callback=None) -> dict:
        bot = self._bots.get(bot_id)
        if not bot:
            return {"sent": 0, "failed": 0, "total": 0}

        users = get_child_users(bot_id, only_active=True)
        total = len(users)
        sent = 0
        failed = 0

        for i, user in enumerate(users):
            chat_id = user["chat_id"]
            try:
                if media_type == "photo" and media_id:
                    await bot.send_photo(chat_id=chat_id, photo=media_id, caption=text)
                elif media_type == "video" and media_id:
                    await bot.send_video(chat_id=chat_id, video=media_id, caption=text)
                elif media_type == "document" and media_id:
                    await bot.send_document(chat_id=chat_id, document=media_id, caption=text)
                elif media_type == "animation" and media_id:
                    await bot.send_animation(chat_id=chat_id, animation=media_id, caption=text)
                elif media_type == "sticker" and media_id:
                    await bot.send_sticker(chat_id=chat_id, sticker=media_id)
                else:
                    await bot.send_message(chat_id=chat_id, text=text)
                sent += 1
                add_stat(bot_id, "message_out")
            except TelegramForbiddenError:
                mark_user_blocked(bot_id, chat_id)
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