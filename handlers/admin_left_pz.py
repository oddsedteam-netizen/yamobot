"""Рассылка ПЗ об уходе админа.

Зачем этот модуль
-----------------
Раньше предложение «разослать ПЗ с оповещением об уходе» жило только в одном
из трёх мест удаления админа — в карточке админа. Владелец, который удалял
админа обычным способом (меню админов → «Удалить админа» → ввод тега, либо
«админ вышел из чата админов»), этого предложения не видел вообще — то есть
функция «работала» только на одном из трёх путей.

Здесь она собрана в одном месте и используется отовсюду: ``admins.py``
(карточка и ввод тега) и ``admin_chat_watch.py`` (админ вышел из чата).

Порядок действий важен
----------------------
1. ``release_admin_from_topics`` — СНАЧАЛА снимаем админа с его ПЗ (и заодно
   запоминаем список, потому что после сброса его уже не выбрать). Иначе ПЗ
   навсегда остались бы «за» несуществующим админом.
2. Только потом предлагаем рассылку по запомненному списку.
"""

import logging

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from handlers._common import cb_data, cb_uid, render_callback
from services.child_manager import ChildManager
from services.storage import (
    delete_admin_left_pz,
    get_admin_left_pz,
    release_admin_from_topics,
)

logger = logging.getLogger(__name__)

router = Router()

# Текст, который уходит ПЗ. Кнопка «Подобрать нового» — это callback
# ``find_admin_<topic>_<group>``, который обрабатывает дочерний бот: по нему
# топик освобождается и в него возвращаются кнопки «Я беру» / «Отказ».
LEFT_PZ_TEXT = (
    "⚠️ <b>Ваш администратор покинул бота.</b>\n\n"
    "Мы подберём вам нового. Нажмите кнопку ниже, чтобы ускорить процесс."
)


def _nav_rows() -> list[list[InlineKeyboardButton]]:
    """Навигация под любым экраном этого модуля."""
    return [[InlineKeyboardButton(text="📋 Список", callback_data="gadmins_list",
                                  style="primary")],
            [InlineKeyboardButton(text="⬅️ Меню админов", callback_data="gadmins",
                                  style="primary")]]


async def _show(target, text: str, kb: InlineKeyboardMarkup) -> None:
    """Показать экран: ``CallbackQuery`` правим, ``Message`` — отвечаем.

    Тип проверяем именно по ``CallbackQuery``, а не через ``isinstance`` по
    ``Message``: во втором случае подменённый в тестах объект «уехал бы» в
    ветку правки колбэка и упал бы на отсутствии ``.message``.
    """
    if isinstance(target, CallbackQuery):
        await render_callback(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


# ═══════════════ Предложение рассылки ═══════════════

async def offer_left_pz_mailing(target, owner_id: int, admin_user_id: int,
                                uname: str, tag: str) -> int:
    """Предлагает рассылку по ПЗ ушедшего админа. Возвращает число ПЗ.

    Вызывается из любого места, где админ только что удалён. Если ПЗ за ним
    не было — сразу показывает финальный экран, иначе — вопрос «разослать?».

    ``target`` — ``CallbackQuery`` (рисуем через ``render_callback``) или
    ``Message`` (рисуем ответом).
    """
    released = release_admin_from_topics(owner_id, admin_user_id)

    if released == 0:
        await _show(target, f"✅ <b>Админ удалён!</b>\n\n👤 {uname}\n🏷 #{tag}",
                    InlineKeyboardMarkup(inline_keyboard=_nav_rows()))
        return 0

    text = (
        "🗑 <b>Админ удалён</b>\n\n"
        f"👤 {uname}\n🏷 #{tag}\n\n"
        f"📋 Его ПЗ освобождено и ждёт нового админа: <b>{released}</b>\n\n"
        "Хотите сделать рассылку по ПЗ админа с оповещением об уходе?"
    )
    await _show(target, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, разослать",
                              callback_data=f"gadmins_delmail_yes_{admin_user_id}",
                              style="success")],
        [InlineKeyboardButton(text="❌ Нет",
                              callback_data=f"gadmins_delmail_no_{admin_user_id}")],
        *_nav_rows(),
    ]))
    return released


async def finish_left_pz(target, text: str) -> None:
    """Финальный экран без вопроса (админ удалён, ПЗ не было или отказ)."""
    await _show(target, text, InlineKeyboardMarkup(inline_keyboard=_nav_rows()))


# ═══════════════ Ответы владельца ═══════════════

@router.callback_query(F.data.regexp(r"^gadmins_delmail_no_\d+$"))
async def cb_delmail_no(callback: CallbackQuery) -> None:
    """Владелец отказался от рассылки — чистим запомненный список.

    Чистить обязательно: иначе при повторном удалении того же админа
    показался бы СТАРЫЙ список ПЗ, а таблица росла бы бесконечно.
    """
    admin_user_id = int(cb_data(callback).rsplit("_", 1)[-1])
    delete_admin_left_pz(cb_uid(callback), admin_user_id)
    await callback.answer("👌 Хорошо")
    await finish_left_pz(
        callback,
        "👌 <b>Без рассылки.</b>\n\n"
        "ПЗ остались в топиках без админа — их можно забрать кнопкой «✋ Я беру».",
    )
@router.callback_query(F.data.regexp(r"^gadmins_delmail_yes_\d+$"))
async def cb_delmail_yes(callback: CallbackQuery,
                         child_manager: ChildManager) -> None:
    """Рассылает ПЗ ушедшего админа сообщение «ваш админ покинул бота».

    Список берём из ``admin_left_pz``, а не из топиков: к этому моменту админ с
    них уже снят. Список сохранился и пережил бы перезапуск бота.
    """
    admin_user_id = int(cb_data(callback).rsplit("_", 1)[-1])
    owner_id = cb_uid(callback)
    topics = get_admin_left_pz(owner_id, admin_user_id)

    sent = failed = 0
    for t in topics:
        bot = child_manager.get_bot(int(t["bot_id"]))
        if bot is None:
            failed += 1
            continue
        try:
            await bot.send_message(
                chat_id=int(t["user_chat_id"]),
                text=LEFT_PZ_TEXT,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="👀 Подобрать нового",
                        # Именно find_admin_: такой callback_data обрабатывает
                        # хендлер «Найти админа» в дочернем боте. Раньше здесь
                        # был picknew_, под который обработчика не существовало —
                        # кнопка просто не работала.
                        callback_data=f"find_admin_{t['topic_id']}_{t['group_chat_id']}",
                    )
                ]]),
            )
            sent += 1
        except Exception as e:
            failed += 1
            logger.info("Рассылка об уходе админа не дошла до ПЗ %s: %s",
                        t["user_chat_id"], e)

    # Список отработал — убираем, чтобы повторно не предлагать то же самое.
    delete_admin_left_pz(owner_id, admin_user_id)

    if not topics:
        await finish_left_pz(callback, "ℹ️ <b>Рассылать было нечего.</b>\n\n"
                                     "За этим админом не осталось ПЗ.")
        await callback.answer()
        return

    await render_callback(
        callback,
        "📨 <b>Рассылка завершена</b>\n\n"
        f"✅ Отправлено ПЗ: <b>{sent}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>",
        InlineKeyboardMarkup(inline_keyboard=_nav_rows()),
    )
    await callback.answer()