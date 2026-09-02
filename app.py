import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

from handlers import register_all_handlers
from services.child_manager import ChildManager
from services.config import BOT_TOKEN, OWNER_ID
from services.storage import ensure_db

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


def _clean_token(raw: str) -> str:
    """Убирает кавычки и пробелы вокруг токена."""
    return raw.strip().strip('"').strip("'")


def load_token() -> str:
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH, override=True, encoding="utf-8-sig")

    token = _clean_token(os.getenv("BOT_TOKEN", "") or BOT_TOKEN)

    if not token:
        raise RuntimeError(
            "Не найден BOT_TOKEN.\n"
            "Для BotHost: добавь переменную окружения BOT_TOKEN в панели.\n"
            "Для локального запуска: используй файл .env"
        )
    if ":" not in token:
        raise RuntimeError("BOT_TOKEN некорректен")
    return token


async def main() -> None:
    ensure_db()
    token = load_token()

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp["owner_id"] = OWNER_ID
    child_manager = ChildManager()
    dp["child_manager"] = child_manager

    # Даём менеджеру ссылку на основной бот — для уведомлений в «чат админов».
    from services.child_manager import set_main_bot
    set_main_bot(bot)

    register_all_handlers(dp)

    try:
        me = await bot.get_me()
        logging.info("YamoBot запущен: @%s (%s)", me.username, me.id)
        await bot.delete_webhook(drop_pending_updates=True)
        await child_manager.start_all_children()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await child_manager.stop_all_children()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("YamoBot остановлен.")
    except Exception:
        logging.exception("Критическая ошибка.")
        raise