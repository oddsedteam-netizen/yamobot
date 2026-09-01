from aiogram import Bot, Router, F
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from handlers._common import render_callback
from services.child_manager import ChildManager
from services.storage import (
    add_user_bot, bot_display_name, set_bot_type, set_bot_keyboard,
)
router = Router()


class AddBotFSM(StatesGroup):
    waiting_for_token = State()


REPLY_PRESETS = {
    'smena': {'label': 'Сменить админа',
              'buttons': [{'kind': 'admin', 'text': 'сменить админа'}]},
    'none': {'label': 'Без reply-клавиатуры', 'buttons': []},
}

def _back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Назад', callback_data='back_main')],
    ])


def _type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Стандарт', callback_data='bot_type_standard')],
        [InlineKeyboardButton(text='Анкетница', callback_data='bot_type_anketa')],
        [InlineKeyboardButton(text='Отмена', callback_data='back_main')],
    ])


def _reply_kb():
    rows = []
    for k in REPLY_PRESETS:
        rows.append([InlineKeyboardButton(text=REPLY_PRESETS[k]['label'],
                                         callback_data='reply_set_' + k)])
    rows.append([InlineKeyboardButton(text='Назад', callback_data='bot_type_back')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def verify_token(token):
    try:
        tmp = Bot(token=token)
        me = await tmp.get_me()
        info = {'token': token, 'id': me.id,
                'username': me.username or '', 'first_name': me.first_name or '',
                'welcome_text': '', 'stopped': False, 'links': []}
        await tmp.session.close()
        return info
    except (TelegramUnauthorizedError, Exception):
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
        'Отлично! Теперь выбери тип бота:', reply_markup=_type_kb())


@router.callback_query(F.data.startswith('bot_type_'))
async def cb_choose_bot_type(callback, state, child_manager):
    bot_type = callback.data.split('_')[-1]
    if bot_type == 'back':
        await state.set_state(AddBotFSM.waiting_for_token)
        await render_callback(callback, 'Отправь токен ещё раз.', _back_kb())
        return
    await state.update_data(bot_type=bot_type)
    if bot_type == 'anketa':
        await state.update_data(reply_preset='none')
        await _finish_add(callback.message, state, child_manager)
        return
    text = 'Добавить reply-клавиатуру дочернему боту? Выбери набор кнопок:'
    await render_callback(callback, text, _reply_kb())


@router.callback_query(F.data.startswith('reply_set_'))
async def cb_choose_reply(callback, state, child_manager):
    preset_key = callback.data.split('_')[-1]
    if preset_key not in REPLY_PRESETS:
        await callback.answer('Не найдено')
        return
    await state.update_data(reply_preset=preset_key)
    await _finish_add(callback.message, state, child_manager)


async def _finish_add(message, state, child_manager):
    from handlers.start import main_menu_kb
    data = await state.get_data()
    info = data.get('bot_info')
    bot_type = data.get('bot_type', 'standard')
    preset_key = data.get('reply_preset', 'none')
    user_id = message.from_user.id
    if not info:
        await state.clear()
        return
    add_user_bot(user_id, info)
    set_bot_type(user_id, info['id'], bot_type)
    set_bot_keyboard(user_id, info['id'], REPLY_PRESETS[preset_key]['buttons'])
    await state.clear()
    ok = await child_manager.start_child(info)
    status = 'Бот запущен' if ok else 'Бот сохранён, но не удалось запустить'
    text = ('Бот подключён!\n' + bot_display_name(info) + '\n' + status)
    await message.answer(text, reply_markup=main_menu_kb())