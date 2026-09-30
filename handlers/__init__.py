import logging

from aiogram import Dispatcher
from aiogram.types import ErrorEvent

from services.premium_emoji import PremiumEmojiLearningMiddleware

logger = logging.getLogger(__name__)


async def on_error(event: ErrorEvent) -> bool:
    """Ловим ошибки обработчиков: логируем с контекстом и гасим их.

    Без этого любая мелкая ошибка (например, битый callback_data от старой
    кнопки) поднималась стектрейсом в лог и выглядела как падение бота, хотя
    процесс продолжал работать. Здесь мы пишем понятную строку и возвращаем
    True — событие считается обработанным.
    """
    update = event.update
    update_type = update.event_type if update else "?"
    callback = getattr(event, "callback_query", None)
    message = getattr(event, "message", None)
    data = getattr(callback, "data", None) if callback else None
    user_id = None
    source = callback or message
    if source is not None and getattr(source, "from_user", None) is not None:
        user_id = source.from_user.id

    logger.error(
        "Ошибка в обработчике (%s), юзер=%s, callback=%r: %s",
        update_type, user_id, data, event.exception,
        exc_info=event.exception,
    )
    return True


def register_all_handlers(dp: Dispatcher) -> None:
    # Импортируем роутеры ВНУТРИ функции: так `import handlers.<любой модуль>` и
    # `import services.child_manager` безопасны в любом порядке. Раньше импорт
    # на уровне модуля создавал цикл
    # handlers/__init__ → handlers.start → services.child_manager → handlers._common
    # и падал с ImportError, если первым импортировали services.child_manager.
    from handlers.start import router as start_router
    from handlers.my_bots import router as my_bots_router
    from handlers.add_bot import router as add_bot_router
    from handlers.bot_actions import router as bot_actions_router
    from handlers.select_all import router as select_all_router
    from handlers.editor import router as editor_router
    from handlers.mailing import router as mailing_router
    from handlers.admins import router as admins_router
    from handlers.coowners import router as coowners_router
    from handlers.pz import router as pz_router
    from handlers.overview import router as overview_router
    from handlers.profile import router as profile_router
    from handlers.complaints import router as complaints_router
    from handlers.admin_moderation import router as admin_moderation_router
    from handlers.restart import router as restart_router
    from handlers.antiraid import router as antiraid_router
    from handlers.antinakrutka import router as antinakrutka_router
    from handlers.configs import router as configs_router
    from handlers.reminders import router as reminders_router
    # Заглушки/сброс отсчёта напоминалки. ВАЖНО: подключаем ДО antiraid —
    # у антирейда есть обработчик с широким фильтром («любое сообщение в
    # группе»), и номер, написанный владельцем в «чате админов», съедался бы
    # им раньше, чем дойдёт до этого роутера. Ровно поэтому ввод номера
    # работает в handlers/start.py: он подключён самым первым.
    from handlers.reminder_mutes import router as reminder_mutes_router
    # Раздел YID (внутренний номер и личная статистика админа).
    from handlers.yid import router as yid_router
    # Диагностика: проверка ботов и привязок с подсказками «что делать».
    from handlers.diagnostics import router as diagnostics_router
    from handlers.other import router as other_router
    from handlers.norms import router as norms_router
    from handlers.admin_chat_watch import router as admin_chat_watch_router
    from handlers.hours import router as hours_router
    from handlers.logs import router as logs_router
    from handlers.tickets import router as tickets_router
    from handlers.channels import router as channels_router
    # Учим словарь премиум-эмодзи по ВСЕМ сообщениям (до фильтров): Telegram сам
    # присылает custom_emoji-сущности, а по ним бот потом возвращает премиум в
    # приветствиях, где эмодзи потерял «премиум» (см. services/premium_emoji.py).
    dp.message.outer_middleware(PremiumEmojiLearningMiddleware())

    # Ловим ошибки обработчиков: логируем с контекстом и гасим, чтобы одна
    # неудачная кнопка не выглядела как падение бота.
    dp.errors.register(on_error)

    dp.include_router(start_router)
    dp.include_router(my_bots_router)
    dp.include_router(add_bot_router)
    dp.include_router(editor_router)
    dp.include_router(mailing_router)
    dp.include_router(admins_router)
    # Заходы/выходы в чат админов.
    dp.include_router(admin_chat_watch_router)
    dp.include_router(coowners_router)
    dp.include_router(pz_router)
    dp.include_router(bot_actions_router)
    dp.include_router(select_all_router)
    dp.include_router(overview_router)
    dp.include_router(profile_router)
    dp.include_router(complaints_router)
    dp.include_router(admin_moderation_router)
    # restart ДО antiraid: у антирейда есть широкий фильтр сообщений группы
    # (мониторинг спама), и он не должен перехватывать /perezap и /perestart.
    dp.include_router(restart_router)
    # reminder_mutes ТОЖЕ до antiraid: его ввод номера обращения — обычное
    # сообщение в группе, и без этого порядка антирейд перехватил бы его
    # раньше (широкий фильтр + «обработка останавливается на первом
    # подошедшем обработчике»). Порядок критичен, не переставлять.
    dp.include_router(reminder_mutes_router)
    dp.include_router(antiraid_router)
    dp.include_router(antinakrutka_router)
    dp.include_router(configs_router)
    # Напоминалка: список режимов, тихие часы, диагностика. Кнопки (callback)
    # не конфликтуют по порядку, но держим рядом с reminder_mutes для читаемости.
    dp.include_router(reminders_router)
    # YID держим после напоминалки: его кнопки живут в профиле, и порядок
    # не влияет на фильтры — регистрируем рядом с профилем для читаемости.
    dp.include_router(yid_router)
    # Диагностика идёт следом: кнопка живёт в профиле рядом с YID.
    dp.include_router(diagnostics_router)
    # Нормы админов: кнопка «📊 Норма» в профиле + уведомления о недоборе.
    dp.include_router(norms_router)
    # Время работы: кнопка «🕐 Время работы» в профиле.
    dp.include_router(hours_router)
    # Логи пользователя и логи ботов для владельца платформы.
    dp.include_router(logs_router)
    # Тикеты поддержки (бывшие жалобы): меню пользователя + админка.
    dp.include_router(tickets_router)
    dp.include_router(other_router)
    # ТГК (кнопка «📢 Мой ТГК»): привязка канала, посты и отложенная публикация.
    dp.include_router(channels_router)