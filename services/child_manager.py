import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message

from services.storage import get_all_bots_flat, get_bot_by_id

logger = logging.getLogger(__name__)


def _make_child_dp(bot_data: dict) -> Dispatcher:
    """Создаёт Dispatcher для дочернего бота."""
    child_dp = Dispatcher()

    @child_dp.message(CommandStart())
    async def child_start(message: Message) -> None:
        welcome = bot_data.get("welcome_text", "")
        if not welcome:
            welcome = f"👋 Привет! Я {bot_data.get('first_name', 'бот')}."
        await message.answer(welcome)

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

            child_dp = _make_child_dp(bot_data)

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

    async def send_message(self, bot_id: int, chat_id: int, text: str) -> bool:
        bot = self._bots.get(bot_id)
        if not bot:
            return False
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return True
        except Exception as e:
            logger.error("Ошибка отправки от бота %s: %s", bot_id, e)
            return False