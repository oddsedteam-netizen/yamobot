from aiogram import Bot, Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
)
import asyncio

from handlers._common import render_callback, cb_data, cb_uid
from services.child_manager import ChildManager
from services.storage import (
    add_user_bot, bot_display_name, set_bot_type, set_bot_keyboard,
    get_feedback_chat,
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
    bot_type = cb_data(callback).split('_')[-1]
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
    data = await state.get_data()
    info = data.get('bot_info')
    bot_type = data.get('bot_type', 'standard')
    preset_key = bot_type if bot_type in REPLY_PRESETS else 'standard'
    user_id = cb_uid(callback)
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
    status = '🟢 Бот запущен' if ok else '⚠️ Бот сохранён, но не удалось запустить'
    name = bot_display_name(info)
    username = info.get('username', '')
    bot_link = f"https://t.me/{username}" if username else f"бот (ID <code>{info['id']}</code>)"

    text = (
        '✅ <b>Бот подключён!</b>\n\n'
        f'🤖 {name}\n'
        f'Тип: <b>{REPLY_PRESETS[preset_key]["label"]}</b>\n'
        f'Статус: {status}\n\n'
        f'Теперь добавь бота {bot_link} в рабочий чат с темами — '
        'бот <b>подключится сам</b>, как только окажется в чате с '
        'включёнными темами. Никаких команд вводить не нужно.'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='▶️ Заработал', callback_data=f'addbot_done_{info["id"]}'),
            InlineKeyboardButton(text='⏭ Пропустить', callback_data='addbot_skip'),
        ],
    ])
    if callback.message:
        await callback.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == 'addbot_skip')
async def cb_addbot_skip(callback: CallbackQuery) -> None:
    from handlers.start import main_menu_kb
    if callback.message:
        await callback.message.answer(
            '👌 Ок. Когда захочешь подключить бота к рабочему чату — '
            'просто добавь его туда (с включёнными темами): бот подключится сам. '
            'Если вдруг не подключится — напиши <code>/connect</code> в тему '
            '<b>General</b>.',
            reply_markup=main_menu_kb(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith('addbot_done_'))
async def cb_addbot_done(callback: CallbackQuery) -> None:
    from handlers.start import main_menu_kb
    bot_id = int(cb_data(callback).split('_')[-1])
    connected = get_feedback_chat(bot_id) is not None
    if not connected:
        await callback.answer(
            '⚠️ Бот ещё не подключился к рабочему чату. Добавь его в чат '
            'с включёнными темами — он подключится сам. Если не подключился '
            'сам, напиши /connect в теме General, затем нажми снова.',
            show_alert=True,
        )
        return
    if callback.message:
        await callback.message.answer(
            '✅ <b>Бот работает!</b>\n\nЧат подключён — новые обращения будут '
            'создавать топики в рабочем чате.',
            reply_markup=main_menu_kb(),
        )
    await callback.answer()