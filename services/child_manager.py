import asyncio
import json
import logging
import time
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.storage import (
    add_child_user,
    add_stat,
    get_all_bots_flat,
    get_bot_by_id_any_owner,
    get_child_users,
    mark_user_blocked,
    save_mailing,
    get_antispam_mode,
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
        rows.append([
            InlineKeyboardButton(text=link["text"], url=link["url"])
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _make_child_dp(bot_data: dict, bot_obj: Bot) -> Dispatcher:
    child_dp = Dispatcher()
    bot_id = bot_data["id"]

    # Антиспам: трекеры
    sticker_counts: dict[int, list[float]] = defaultdict(list)
    last_message_time: dict[int, float] = {}

    @child_dp.message(CommandStart())
    async def child_start(message: Message) -> None:
        # Сохраняем пользователя
        add_child_user(
            bot_id,
            message.from_user.id,
            message.from_user.username or "",
            message.from_user.first_name or ""
        )
        add_stat(bot_id, "message_in")

        # Получаем свежие данные
        fresh = get_bot_by_id_any_owner(bot_id)
        if not fresh:
            return

        welcome = fresh.get("welcome_text", "") or f"👋 Привет! Я {fresh.get('first_name', 'бот')}."
        kb = _build_welcome_kb(fresh)

        await message.answer(welcome, reply_markup=kb)
        add_stat(bot_id, "message_out")

    @child_dp.message()
    async def child_any_message(message: Message) -> None:
        add_stat(bot_id, "message_in")

        # Сохраняем юзера
        add_child_user(
            bot_id,
            message.from_user.id,
            message.from_user.username or "",
            message.from_user.first_name or ""
        )

        user_id = message.from_user.id
        now = time.time()

        antispam = get_antispam_mode(bot_id)

        if antispam == "off":
            return

        # ── Режим "manual" — ограничение 1 сообщение в минуту для всех ──
        if antispam == "manual":
            last = last_message_time.get(user_id, 0)
            if now - last < 60:
                try:
                    await message.delete()
                except Exception:
                    pass
                return
            last_message_time[user_id] = now
            return

        # ── Режим "auto" — бан за 5 стикеров подряд ──
        if antispam == "auto":
            if message.content_type == ContentType.STICKER:
                sticker_counts[user_id].append(now)
                # Оставляем только за последние 30 сек
                sticker_counts[user_id] = [
                    t for t in sticker_counts[user_id] if now - t < 30
                ]

                if len(sticker_counts[user_id]) >= 5:
                    try:
                        await message.chat.ban(user_id)
                        await message.answer(
                            f"🛡 Пользователь {message.from_user.first_name} "
                            f"заблокирован за спам стикерами."
                        )
                        add_stat(bot_id, "antispam_ban")
                    except Exception as e:
                        logger.warning("Не удалось забанить %s: %s", user_id, e)
                    sticker_counts[user_id] = []
            else:
                # Не стикер — сбрасываем счётчик
                sticker_counts[user_id] = []

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
        """Рассылка по всем активным пользователям бота."""
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