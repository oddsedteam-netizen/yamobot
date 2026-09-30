import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import User
from dotenv import load_dotenv

from handlers import register_all_handlers
from services.channel_service import ChannelService
from services.child_manager import ChildManager
from services.config import BOT_TOKEN, OWNER_ID, proxy_hint, proxy_settings
from services.constants import BOT_VERSION
from services.delivery import get_outbox
from services.logging_setup import log_startup_banner, setup_logging
from services.polling import ResilientDispatcher
from services.reminder_service import ReminderService
from services.storage import (
    ensure_db,
    reset_all_antiraid_triggered,
)

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Сколько раз пробуем достучаться до Telegram при старте и с какими паузами.
# Раньше стоял один-единственный bot.get_me(): без интернета процесс просто
# падал с длинным трейсбеком, хотя через минуту сеть могла подняться.
STARTUP_ATTEMPTS = 6
STARTUP_DELAY = 5.0

# Настройка логов до импорта сервисов: они уже могут писать в журнал при
# импорте, и без настроенного хендлера эти строки потерялись бы.
setup_logging()
log_startup_banner()
logger = logging.getLogger("app")



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


async def wait_for_telegram(bot: Bot) -> User:
    """Дожидается связи с Telegram и возвращает ``User`` основного бота.

    Отдельные сообщения для «нет сети» и «мёртвый токен» — по трейсбеку
    aiohttp разница не видна. Пробуем несколько раз с растущей паузой: связь
    часто появляется через несколько секунд (VPN, сеть «моргает»).
    """
    last_error: Exception | None = None

    for attempt in range(1, STARTUP_ATTEMPTS + 1):
        try:
            return await bot.get_me()
        except TelegramUnauthorizedError:
            raise RuntimeError(
                "⛔ Токен бота недействителен или отозван.\n"
                "Проверь BOT_TOKEN в .env или возьми новый токен у @BotFather."
            ) from None
        except TelegramNetworkError as e:
            last_error = e
            logger.warning(
                "Нет связи с Telegram (попытка %d/%d): %s",
                attempt, STARTUP_ATTEMPTS, getattr(e, "message", e),
            )
            if attempt < STARTUP_ATTEMPTS:
                await asyncio.sleep(min(STARTUP_DELAY * attempt, 30.0))

    logger.error("Telegram недоступен: %s\n%s", last_error, proxy_hint())
    raise RuntimeError(f"Telegram недоступен: {last_error}")


async def main() -> None:
    ensure_db()
    # После рестарта бота сбрасываем флаг сработавшего антирейда: если чат
    # остался заблокированным, владелец сам выключит защиту через /выкланти.
    reset_all_antiraid_triggered()
    # Защиту от накрутки (антинакрутку) НЕ сбрасываем: если она сработала,
    # то остаётся включённой до решения владельца — иначе после рестарта
    # спам-наплыв снова открыл бы шлюз. Снять защиту владелец может кнопкой
    # «🚨 Антинакрутка» → «🔄 Сбросить защиту» в профиле.
    token = load_token()

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        # Прокси из .env (PROXY_URL) — нужен там, где api.telegram.org
        # напрямую недоступен. Если не задан, работаем без него.
        **proxy_settings(),
    )

    # Устойчивый диспетчер: конфликт вебхука (409) и мёртвый токен больше не
    # крутятся в логе бесконечно — см. services/polling.py.
    dp = ResilientDispatcher()
    dp["owner_id"] = OWNER_ID
    child_manager = ChildManager()
    dp["child_manager"] = child_manager
    reminder_service = ReminderService()
    dp["reminder_service"] = reminder_service
    channel_service = ChannelService()
    dp["channel_service"] = channel_service

    # Даём менеджеру ссылку на основной бот — для уведомлений в «чат админов».
    from services.child_manager import set_main_bot
    set_main_bot(bot)

    # Очередь недоставленных сообщений. Основной бот пишет в неё от своего
    # имени (bot_id = 0), дочерние регистрируются по мере старта.
    outbox = get_outbox()
    outbox.register_bot(0, bot)

    register_all_handlers(dp)

    try:
        me = await wait_for_telegram(bot)
        logger.info("YamoBot запущен: @%s (%s) | версия %s", me.username, me.id,
                    BOT_VERSION)
        # Уборка старых записей очереди — раз в сутки делает воркер, но и на
        # старте полезно, чтобы таблица не росла после долгого простоя.
        removed = outbox.maintenance()
        if removed:
            logger.info("Очередь доставки: убрано старых записей — %d", removed)
        await bot.delete_webhook(drop_pending_updates=True)
        await child_manager.start_all_children()
        # Фоновый воркер очереди: доставляет то, что не ушло с первого раза.
        await outbox.start()
        # Фоновый сканер напоминалок (авточек ответа админа / напоминание про ПЗ).
        reminder_service.start()
        # Фоновый публикатор отложенных постов в ТГК (раздел «Мой ТГК»).
        channel_service.start()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except asyncio.CancelledError:
        logger.info("Получена команда остановки — корректно выключаемся.")
        raise
    finally:
        # Останавливаем всё в обратном порядке: сначала потоки, которые шлют
        # сообщения, потом дочерние боты, потом сессия основного.
        await channel_service.stop()
        await reminder_service.stop()
        await outbox.stop()
        await child_manager.stop_all_children()
        await bot.session.close()
        logger.info("YamoBot остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("YamoBot остановлен.")
    except RuntimeError as e:
        # Частые стартовые ситуации (нет сети, мёртвый токен, плохой BOT_TOKEN)
        # — показываем короткое понятное сообщение вместо трейсбека.
        logger.error("Запуск не удался: %s", e)
        raise SystemExit(1) from None
    except Exception:
        logger.exception("Критическая ошибка.")
        raise