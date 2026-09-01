from aiogram import Bot, Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
import asyncio

from handlers._common import render_callback
from services.child_manager import ChildManager
from services.storage import (
    add_user_bot, bot_display_name, set_bot_type, set_bot_keyboard,
)
router = Router()


class AddBotFSM(StatesGroup):
    waiting_for_token = State()


REPLY_PRESETS = {
    'standard': {'label': 'Стандарт',
                 'buttons': [{'kind': 'admin', 'text': 'сменить админа'}]},
    'anketa': {'label': 'Анкетница', 'buttons': []},
}

def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Назад', callback_data='back_main')],
    ])


def _type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🗂 Стандарт', callback_data='bot_type_standard')],
        [InlineKeyboardButton(text='📝 Анкетница', callback_data='bot_type_anketa')],
        [InlineKeyboardButton(text='Отмена', callback_data='back_main')],
    ])


async def verify_token(token: str) -> dict | None:
    try:
        tmp = Bot(token=token)
        me = await tmp.get_me()
        info = {'token': token, 'id': me.id,
                'username': me.username or '', 'first_name': me.first_name or '',
                'welcome_text': '', 'stopped': False, 'links': []}
        await tmp.session.close()
        return info
    except Exception:
        return None


@router.callback_query(F.data == 'add_bot')
async def cb_add_bot(callback, state):
    await state.set_state(AddBotFSM.waiting_for_token)
    text = ('Добавить бота\n\n'
            'Отправь токен бота от @BotFather.\n\n')
    await render_callback(callback, text, _back_kb())


@router.message(AddBotFSM.waiting_for_token)
async def fsm_receive_token(message, state):
    token = message.text.strip() if message.text else ''
    if ':' not in token or len(token) < 30:
        await message.answer('Неверный формат токена.', reply_markup=_back_kb())
        return
    wait_msg = await message.answer('Проверяю токен...')
    info = await verify_token(token)
    if info is None:
        await wait_msg.edit_text('Токен недействителен.', reply_markup=_back_kb())
        return
    await state.update_data(bot_info=info)
    await wait_msg.edit_text(
        'Отлично! Теперь выбери тип бота:\n\n'
        '🗂 <b>Стандарт</b> — обычный бот, к нему привяжется '
        'reply-клавиатура «сменить админа».\n'
        '📝 <b>Анкетница</b> — бот-анкета без reply-кнопок, '
        'только инлайн-кнопки.',
        reply_markup=_type_kb())


@router.callback_query(F.data.startswith('bot_type_'))
async def cb_choose_bot_type(callback: CallbackQuery, state: FSMContext,
                             child_manager: ChildManager) -> None:
    bot_type = callback.data.split('_')[-1]
    if bot_type == 'back':
        await state.set_state(AddBotFSM.waiting_for_token)
        await render_callback(callback, 'Отправь токен ещё раз.', _back_kb())
        return
    if bot_type not in REPLY_PRESETS:
        await callback.answer('Не найдено')
        return
    await state.update_data(bot_type=bot_type)
    await _finish_add(callback, state, child_manager)


async def _finish_add(callback: CallbackQuery, state: FSMContext,
                      child_manager: ChildManager) -> None:
    from handlers.start import main_menu_kb
    data = await state.get_data()
    info = data.get('bot_info')
    bot_type = data.get('bot_type', 'standard')
    preset_key = bot_type if bot_type in REPLY_PRESETS else 'standard'
    user_id = callback.from_user.id
    if not info:
        await state.clear()
        return
    add_user_bot(user_id, info)
    set_bot_type(user_id, info['id'], bot_type)
    set_bot_keyboard(user_id, info['id'], REPLY_PRESETS[preset_key]['buttons'])
    await state.clear()
    ok = await child_manager.start_child(info)
    if ok:
        # Даём таску первую секунду — если бот упал сразу, считаем неудачным запуском.
        await asyncio.sleep(1.0)
        if not child_manager.is_running(info['id']):
            ok = False
    status = 'Бот запущен' if ok else 'Бот сохранён, но не удалось запустить'
    text = ('✅ <b>Бот подключён!</b>\n\n'
            f'🤖 {bot_display_name(info)}\n'
            f'Тип: <b>{REPLY_PRESETS[preset_key]["label"]}</b>\n'
            f'Статус: {status}')
    await callback.message.answer(text, reply_markup=main_menu_kb())