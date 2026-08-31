from aiogram import Dispatcher

from handlers.start import router as start_router
from handlers.my_bots import router as my_bots_router
from handlers.add_bot import router as add_bot_router
from handlers.bot_actions import router as bot_actions_router
from handlers.select_all import router as select_all_router
from handlers.editor import router as editor_router
from handlers.mailing import router as mailing_router
from handlers.admins import router as admins_router
from handlers.coowners import router as coowners_router
from handlers.keyboard import router as keyboard_router
from handlers.pz import router as pz_router
from handlers.overview import router as overview_router


def register_all_handlers(dp: Dispatcher) -> None:
    dp.include_router(start_router)
    dp.include_router(my_bots_router)
    dp.include_router(add_bot_router)
    dp.include_router(editor_router)
    dp.include_router(mailing_router)
    dp.include_router(admins_router)
    dp.include_router(coowners_router)
    dp.include_router(keyboard_router)
    dp.include_router(pz_router)
    dp.include_router(bot_actions_router)
    dp.include_router(select_all_router)
    dp.include_router(overview_router)