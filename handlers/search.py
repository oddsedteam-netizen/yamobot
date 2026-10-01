"""Раздел «🔎 Поиск»: поиск ботов и админов.

Смысл раздела
-------------
Владелец бота ищет админов, админ ищет бот. С обеих сторон люди описывают
себя анкетой, а потом смотрят чужие анкеты и отправляют предложения.

Как это выглядит у пользователя
-------------------------------
::

    🔎 Поиск
    📬 Откликов: 2   👁 Просмотрели: 14
    ✉️ Разослал сегодня: 3/50   ❌ Отказали: 1
    📨 Приглашений в твои боты: 1

    [📝 Анкета бота]                    ← своей строкой, зелёная
    [🪪 Анкета админа]                  ← под ней, зелёная
    [🤖 Просмотр ботов]   [📨 Приглашения]   ← синий ряд
    [👥 Просмотр профилей] [📬 Отклики]      ← синий ряд
    [⬅️ YID]

Почему карточка такая
---------------------
Человеку важно видеть, что его анкету кто-то смотрит и что предложения
работают. Поэтому счётчики — на первом экране, а не спрятаны глубоко.

Анкетницы в разделе не участвуют
-------------------------------
Бот типа «Анкетница» не держит ПЗ и не ищет админов, поэтому его нет ни в
«Просмотре ботов», ни в рейтинге. Проверка — в ``services.db.marketplace``.
"""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import (
    cb_data,
    cb_uid,
    html_escape,
    msg_firstname,
    msg_uid,
    msg_username,
    render_callback,
)
from services.constants import BOT_TYPE_ANKETA
from services.db.marketplace import DASH
from services.storage import (
    DAILY_OFFER_LIMIT,
    REOFFER_COOLDOWN_HOURS,
    OFFER_ADMIN_TO_BOT,
    OFFER_BOT_TO_ADMIN,
    STATUS_ACCEPTED,
    STATUS_DECLINED,
    STATUS_PENDING,
    add_offer,
    bot_display_name,
    admin_profiles_feed,
    bump_daily_sent,
    bot_profiles_feed,
    count_offers,
    count_offers_received,
    count_profile_views,
    get_admin_profile,
    get_bot_profile,
    get_daily_sent,
    get_offer,
    get_user_banned_bots,
    get_user_bots,
    get_user_registry,
    mark_profile_viewed,
    offer_send_block,
    offers_inbox,
    set_admin_profile,
    set_bot_profile,
    set_offer_status,
    unban_user_everywhere,
)

logger = logging.getLogger(__name__)

router = Router()

# Категории анкеты. Совпадают с категориями ПЗ в ботах, поэтому берём
# те же слова — админ узнаёт свою категорию из привычных слов.
CATEGORIES = ("общение", "поддержка", "универсал", "флирт")
GENDERS = ("мужской", "женский", "неважно")


class SearchFSM(StatesGroup):
    """Пошаговое заполнение анкет.

    Отдельные состояния на каждый шаг не заводим: шаг хранится в данных
    состояния (``search_step``), иначе пришлось бы писать шесть одинаковых
    обработчиков. Пропуск нажатием «⏭» переводит на следующий вопрос, а не
    прерывает заполнение.
    """

    bot_pick = State()        # выбор бота для анкеты
    bot_age = State()         # возраст админа
    bot_category = State()    # категория
    bot_gender = State()      # пол
    bot_text = State()        # текст владельца
    admin_age = State()       # сколько лет
    admin_category = State()
    admin_pz = State()        # сколько ПЗ комфортно вести
    admin_tz = State()        # часовой пояс
    admin_hours = State()     # сколько времени уделять
    admin_text = State()


# ═══════════════ Карточка поиска ═══════════════

def _dash(value) -> str:
    """Значение вопроса или прочерк, если вопрос пропущен."""
    text = str(value or "").strip()
    return html_escape(text) if text else DASH


def search_payload(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопки карточки «🔎 Поиск»."""
    invites = count_offers(OFFER_BOT_TO_ADMIN, user_id, STATUS_PENDING)
    invites_total = count_offers(OFFER_BOT_TO_ADMIN, user_id)
    # Приглашения, которые прислали ЭТОМУ человеку (владельцы позвали его к себе).
    # Это обратная сторона «Откликов»: там админ сам просит в бот, здесь его зовут.
    invites_recv = count_offers_received(OFFER_BOT_TO_ADMIN, user_id, STATUS_PENDING)
    apps = count_offers_received(OFFER_ADMIN_TO_BOT, user_id, STATUS_PENDING)
    declined = count_offers(OFFER_BOT_TO_ADMIN, user_id, STATUS_DECLINED)
    sent_today = get_daily_sent(user_id)

    # Просмотры считаются по карточкам БОТОВ: анкет несколько, а смотреть их
    # могут все подряд. Берём максимум, а не сумму — иначе четыре просмотра
    # одного бота выглядели бы как четыре разных человека.
    own_bots = get_user_bots(user_id)
    views = 0
    for b in own_bots:
        views = max(views, count_profile_views("bot", int(b["id"])))

    lines = [
        "🔎 <b>Поиск</b>\n",
        f"📬 Откликов на твои анкеты: <b>{invites}</b>",
        f"  <i>всего предложений: {invites_total}</i>",
        f"👁 Карточки посмотрели: <b>{views}</b>",
        f"✉️ Разослал сегодня: <b>{sent_today}/{DAILY_OFFER_LIMIT}</b>",
        "   <i>обновляется раз в сутки</i>",
        f"❌ Отказали: <b>{declined}</b>",
        f"📩 Заявок на вступление: <b>{apps}</b>",
        f"📨 Приглашений в твои боты: <b>{invites_recv}</b>",
    ]

    # Раскладка кнопок — по ТЗ владельца:
    #   • обе анкеты («Анкета бота», «Анкета админа») — широкие и зелёные,
    #     каждая на своей строке: это то, ради чего человек чаще всего сюда
    #     приходит, и «Анкета админа» уехала из ряда в ряд под анкетой бота;
    #   • «Просмотр ботов» и «Приглашения» — в один ряд, синие: это просмотр
    #     чужого и разбор своего;
    #   • «Просмотр профилей» и «Отклики» — в ряд, синие.
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Анкета бота",
                              callback_data="search_bot_profile",
                              style="success")],
        [InlineKeyboardButton(text="🪪 Анкета админа",
                              callback_data="search_admin_profile",
                              style="success")],
        [InlineKeyboardButton(text="🤖 Просмотр ботов",
                              callback_data="search_bots",
                              style="primary"),
         InlineKeyboardButton(text=f"📨 Приглашения ({invites_recv})",
                              callback_data="search_invites",
                              style="primary")],
        [InlineKeyboardButton(text="👥 Просмотр профилей",
                              callback_data="search_profiles",
                              style="primary"),
         InlineKeyboardButton(text="📬 Отклики",
                              callback_data="search_inbox",
                              style="primary")],
        [InlineKeyboardButton(text="⬅️ YID", callback_data="yid_card",
                              style="primary")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data == "search_open")
async def cb_search_open(callback: CallbackQuery, state: FSMContext) -> None:
    """Открывает карточку «🔎 Поиск»."""
    await state.clear()
    text, kb = search_payload(cb_uid(callback))
    await render_callback(callback, text, kb)
# ═══════════════ Анкета бота ═══════════════

def _fit(text: str, limit: int = 40) -> str:
    """Обрезает подпись кнопки: Telegram режет текст длиннее 64 символов."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _standard_bots(user_id: int) -> list[dict]:
    """Боты пользователя, к которым можно привязать анкету.

    Только «Стандарт»: у анкетницы нет ПЗ и админов, искать их некого.
    """
    return [b for b in get_user_bots(user_id)
            if str(b.get("bot_type") or "standard") != BOT_TYPE_ANKETA]


def _skip_kb() -> InlineKeyboardMarkup:
    """Кнопки вопроса: пропустить и выйти.

    «⏭ Пропустить» НЕ прерывает анкету — ставит прочерк и идёт к следующему
    вопросу. Кнопка «выйти» в других разделах означает отмену, и здесь это
    путало бы: человек жал бы «выйти», а накопленные ответы пропадали бы.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить",
                              callback_data="search_step_skip", style="primary")],
        [InlineKeyboardButton(text="❌ Выйти", callback_data="search_open",
                              style="primary")],
    ])


def _choice_kb(prefix: str, options: tuple[str, ...]) -> InlineKeyboardMarkup:
    """Кнопки выбора из списка (категория, пол)."""
    rows = [[InlineKeyboardButton(text=name, callback_data=f"{prefix}_{i}",
                                  style="primary")]
            for i, name in enumerate(options)]
    rows.append([InlineKeyboardButton(text="⏭ Пропустить",
                                      callback_data="search_step_skip",
                                      style="primary")])
    rows.append([InlineKeyboardButton(text="❌ Выйти",
                                      callback_data="search_open",
                                      style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "search_bot_profile")
async def cb_bot_profile_start(callback: CallbackQuery,
                               state: FSMContext) -> None:
    """Начало анкеты бота: выбираем, к какому боту её прикрепить."""
    user_id = cb_uid(callback)
    bots = _standard_bots(user_id)

    if not bots:
        await render_callback(
            callback,
            "📝 <b>Анкета бота</b>\n\n"
            "Прикрепить анкету не к чему: у тебя нет ботов типа «Стандарт».\n\n"
            "Анкетницы в поиске не участвуют — им не нужны админы.",
            search_payload(user_id)[1],
        )
        return

    if len(bots) == 1:
        await state.set_state(SearchFSM.bot_age)
        await state.update_data(search_bot_id=int(bots[0]["id"]))
        await render_callback(
            callback,
            "📝 <b>Анкета бота</b>\n\n"
            "👤 <b>Возраст админа?</b>\n\n"
            "Можно указать диапазон — например: <code>12-15</code>.\n\n"
            "Напиши возраст или диапазон:",
            _skip_kb(),
        )
        return

    await state.set_state(SearchFSM.bot_pick)
    rows = [
        [InlineKeyboardButton(text=_fit(bot_display_name(b)),
                              callback_data=f"search_botpick_{b['id']}",
                              style="primary")]
        for b in bots
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Поиск",
                                      callback_data="search_open",
                                      style="primary")])
    await render_callback(
        callback,
        "📝 <b>Анкета бота</b>\n\nК какому боту прикрепить анкету?",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.regexp(r"^search_botpick_\d+$"))
async def cb_bot_profile_pick(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбрали бота — задаём первый вопрос."""
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    await state.set_state(SearchFSM.bot_age)
    await state.update_data(search_bot_id=bot_id)
    await render_callback(
        callback,
        "📝 <b>Анкета бота</b>\n\n"
        "👤 <b>Возраст админа?</b>\n\n"
        "Можно указать диапазон — например: <code>12-15</code>.\n\n"
        "Напиши возраст или диапазон:",
        _skip_kb(),
    )


@router.message(SearchFSM.bot_age)
async def fsm_bot_age(message: Message, state: FSMContext) -> None:
    """Возраст админа (можно диапазоном)."""
    value = (message.text or "").strip()
    if not value:
        await message.answer("❌ Пустое значение. Напиши возраст или диапазон.")
        return
    await state.update_data(search_age_range=value[:60])
    await state.set_state(SearchFSM.bot_category)
    await message.answer(
        "📝 <b>Анкета бота</b>\n\n"
        "💬 <b>Какая категория?</b>\n\n"
        "Выбери, кого ты ждёшь в этом боте:",
        reply_markup=_choice_kb("search_cat", CATEGORIES),
    )


@router.callback_query(F.data.regexp(r"^search_cat_[0-3]$"))
async def cb_bot_category(callback: CallbackQuery, state: FSMContext) -> None:
    """Категория бота."""
    index = int(cb_data(callback).rsplit("_", 1)[-1])
    await state.update_data(search_category=CATEGORIES[index])
    await state.set_state(SearchFSM.bot_gender)
    await render_callback(
        callback,
        "📝 <b>Анкета бота</b>\n\n"
        "🚻 <b>Пол админа?</b>\n",
        _choice_kb("search_gender", GENDERS),
    )


@router.callback_query(F.data.regexp(r"^search_gender_[0-2]$"))
async def cb_bot_gender(callback: CallbackQuery, state: FSMContext) -> None:
    """Пол админа."""
    index = int(cb_data(callback).rsplit("_", 1)[-1])
    await state.update_data(search_gender=GENDERS[index])
    await state.set_state(SearchFSM.bot_text)
    await render_callback(
        callback,
        "📝 <b>Анкета бота</b>\n\n"
        "✍️ <b>Ваш текст</b>\n\n"
        "Этот текст увидят админы, когда ты пригласишь их в бо�� — напиши "
        "тут преимущества своего бота или что угодно ещё.\n\n"
        "Можно пропустить:",
        _skip_kb(),
    )


async def _save_bot_profile(message: Message, state: FSMContext) -> None:
    """Сохраняет анкету бота из накопленных ответов."""
    data = await state.get_data()
    bot_id = int(data.get("search_bot_id") or 0)
    if not bot_id:
        await state.clear()
        return
    set_bot_profile(
        bot_id,
        msg_uid(message),
        age_range=str(data.get("search_age_range") or ""),
        category=str(data.get("search_category") or ""),
        gender=str(data.get("search_gender") or ""),
        text=str(data.get("search_text") or ""),
    )
    await state.clear()
    text, kb = search_payload(msg_uid(message))
    await message.answer(
        "✅ <b>Анкета бота сохранена!</b>\n\n"
        "Теперь её видно в разделе «🤖 Просмотр ботов», и админы смогут "
        "подать на неё заявку.\n\n" + text,
        reply_markup=kb,
    )


@router.message(SearchFSM.bot_text)
async def fsm_bot_text(message: Message, state: FSMContext) -> None:
    """Текст владельца к анкете."""
    await state.update_data(search_text=(message.text or "").strip())
    await _save_bot_profile(message, state)


# ═══════════════ «⏭ Пропустить» — один хендлер на обе анкеты ═══════════════
#
# Почему один
# -----------
# Кнопка «⏭ Пропустить» одна и та же в обеих анкетах — значит, и callback_data
# один: ``search_step_skip``. Раньше на него подвисало ДВА обработчика (по
# одному на анкету), и в aiogram срабатывает первый подошедший. Так вот
# второй обработчик был недостижим НИКОГДА, и при пропуске в анкете админа
# срабатывал обработчик анкеты бота: он не узнавал состояние, попадал в
# «Сейчас нечего пропускать» и выбрасывал человека из анкеты — заполнить её
# кнопкой было нельзя.
#
# Поэтому шаги описаны таблицей, а состояние само выбирает нужную строку.
#
# Что значит «пропустить»
# ----------------------
# Пропускается РОВНО ОДИН вопрос: в его поле записывается пустое значение
# (на карточке это прочерк «—»), и анкета идёт к следующему вопросу. Пропуск
# всех сразу означал бы потерять анкету целиком.
#
# ``(текст вопроса, следующее состояние, поле ответа, клавиатура)``
_SKIP_STEPS: dict = {
    # ── Анкета бота ──
    SearchFSM.bot_age: (
        "📝 <b>Анкета бота</b>\n\n💬 <b>Какая категория?</b>\n\n"
        "Выбери, кого ты ждёшь в этом боте:",
        SearchFSM.bot_category, "search_age_range",
        _choice_kb("search_cat", CATEGORIES),
    ),
    SearchFSM.bot_category: (
        "📝 <b>Анкета бота</b>\n\n🚻 <b>Пол админа?</b>\n",
        SearchFSM.bot_gender, "search_category",
        _choice_kb("search_gender", GENDERS),
    ),
    SearchFSM.bot_gender: (
        "📝 <b>Анкета бота</b>\n\n✍️ <b>Ваш текст</b>\n\n"
        "Этот текст увидят админы, когда ты их пригласишь. Можно пропустить:",
        SearchFSM.bot_text, "search_gender",
        _skip_kb(),
    ),
    # ── Анкета админа ──
    SearchFSM.admin_age: (
        "🪪 <b>Анкета админа</b>\n\n💬 <b>Какая у вас категория?</b>\n",
        SearchFSM.admin_category, "sa_age",
        _choice_kb("search_acat", CATEGORIES),
    ),
    SearchFSM.admin_category: (
        "🪪 <b>Анкета админа</b>\n\n📋 <b>Сколько ПЗ вам комфортно вести?</b>\n\n"
        "Можно написать «до 20» или «20-30». Можно пропустить:",
        SearchFSM.admin_pz, "sa_category",
        _skip_kb(),
    ),
    SearchFSM.admin_pz: (
        "🪪 <b>Анкета админа</b>\n\n🕐 <b>Ваш часовой пояс</b>\n\n"
        "Например: <code>МСК+3</code>, <code>UTC+5</code>. Можно пропустить:",
        SearchFSM.admin_tz, "sa_pz",
        _skip_kb(),
    ),
    SearchFSM.admin_tz: (
        "🪪 <b>Анкета админа</b>\n\n⏳ <b>Сколько времени готов уделять боту?</b>\n\n"
        "Например: <code>2 часа в день</code>. Можно пропустить:",
        SearchFSM.admin_hours, "sa_tz",
        _skip_kb(),
    ),
    SearchFSM.admin_hours: (
        "🪪 <b>Анкета админа</b>\n\n✍️ <b>Ваш текст</b>\n\n"
        "Этот текст увидит владелец бота, когда пригласит тебя. Можно пропустить:",
        SearchFSM.admin_text, "sa_hours",
        _skip_kb(),
    ),
}


@router.callback_query(F.data == "search_step_skip")
async def cb_step_skip(callback: CallbackQuery, state: FSMContext) -> None:
    """«⏭ Пропустить» — пропускает текущий вопрос и идёт к следующему.

    Обслуживает обе анкеты: текущее состояние само выбирает нужный шаг из
    ``_SKIP_STEPS``, поэтому отдельных обработчиков не нужно (и не должно
    быть — на один ``callback_data`` подходит только первый).
    """
    current = await state.get_state()

    step = _SKIP_STEPS.get(current)
    if step is not None:
        text, next_state, field, kb = step
        # Пустое значение = прочерк: вопрос пропущен, но не «потерян».
        await state.update_data(**{field: ""})
        await state.set_state(next_state)
        await render_callback(callback, text, kb)
        return

    # Последний вопрос: здесь пропуск означает «сохранить как есть».
    if current == SearchFSM.bot_text:
        await state.update_data(search_text="")
        await _save_bot_profile_by_callback(callback, state)
        return
    if current == SearchFSM.admin_text:
        await state.update_data(sa_text="")
        await _save_admin_profile_by_callback(callback, state)
        return

    user_id = cb_uid(callback)
    text, kb = search_payload(user_id)
    await render_callback(
        callback, "ℹ️ <b>Сейчас нечего пропускать.</b>\n\n" + text, kb,
    )


async def _save_bot_profile_by_callback(callback: CallbackQuery,
                                        state: FSMContext) -> None:
    """То же сохранение, что и по сообщению, но с экрана колбэка."""
    data = await state.get_data()
    bot_id = int(data.get("search_bot_id") or 0)
    user_id = cb_uid(callback)
    if not bot_id:
        await state.clear()
        return
    set_bot_profile(
        bot_id, user_id,
        age_range=str(data.get("search_age_range") or ""),
        category=str(data.get("search_category") or ""),
        gender=str(data.get("search_gender") or ""),
        text=str(data.get("search_text") or ""),
    )
    await state.clear()
    text, kb = search_payload(user_id)
    await render_callback(
        callback, "✅ <b>Анкета бота сохранена!</b>\n\n" + text, kb,
    )


# ═══════════════ Анкета админа ═══════════════

@router.callback_query(F.data == "search_admin_profile")
async def cb_admin_profile_start(callback: CallbackQuery,
                                 state: FSMContext) -> None:
    """Начало анкеты админа: первый вопрос — сколько лет."""
    await state.set_state(SearchFSM.admin_age)
    await render_callback(
        callback,
        "🪪 <b>Анкета админа</b>\n\n"
        "🎂 <b>Сколько вам лет?</b>\n\nНапиши число:",
        _skip_kb(),
    )


@router.message(SearchFSM.admin_age)
async def fsm_admin_age(message: Message, state: FSMContext) -> None:
    """Возраст админа."""
    value = (message.text or "").strip()
    if not value:
        await message.answer("❌ Напиши число — сколько вам лет.")
        return
    await state.update_data(sa_age=value[:40])
    await state.set_state(SearchFSM.admin_category)
    await message.answer(
        "🪪 <b>Анкета админа</b>\n\n💬 <b>Какая у вас категория?</b>\n",
        reply_markup=_choice_kb("search_acat", CATEGORIES),
    )


@router.callback_query(F.data.regexp(r"^search_acat_[0-3]$"))
async def cb_admin_category(callback: CallbackQuery, state: FSMContext) -> None:
    """Категория админа."""
    index = int(cb_data(callback).rsplit("_", 1)[-1])
    await state.update_data(sa_category=CATEGORIES[index])
    await state.set_state(SearchFSM.admin_pz)
    await render_callback(
        callback,
        "🪪 <b>Анкета админа</b>\n\n"
        "📋 <b>Сколько ПЗ вам комфортно вести?</b>\n\n"
        "Можно написать «до 20» или «20-30».\n\nНапиши:",
        _skip_kb(),
    )


@router.message(SearchFSM.admin_pz)
async def fsm_admin_pz(message: Message, state: FSMContext) -> None:
    """Сколько обращений админ готов вести."""
    value = (message.text or "").strip()
    if not value:
        await message.answer("❌ Напиши, сколько ПЗ тебе комфортно.")
        return
    await state.update_data(sa_pz=value[:60])
    await state.set_state(SearchFSM.admin_tz)
    await message.answer(
        "🪪 <b>Анкета админа</b>\n\n"
        "🕐 <b>Ваш часовой пояс</b>\n\n"
        "Например: <code>МСК+3</code>, <code>UTC+5</code>.\n\nНапиши:",
        reply_markup=_skip_kb(),
    )


@router.message(SearchFSM.admin_tz)
async def fsm_admin_tz(message: Message, state: FSMContext) -> None:
    """Часовой пояс админа."""
    value = (message.text or "").strip()
    if not value:
        await message.answer("❌ Напиши часовой пояс.")
        return
    await state.update_data(sa_tz=value[:60])
    await state.set_state(SearchFSM.admin_hours)
    await message.answer(
        "🪪 <b>Анкета админа</b>\n\n"
        "⏳ <b>Сколько времени готов уделять боту?</b>\n\n"
        "Например: <code>2 часа в день</code>, <code>5ч в неделю</code>.\n\n"
        "Напиши:",
        reply_markup=_skip_kb(),
    )


@router.message(SearchFSM.admin_hours)
async def fsm_admin_hours(message: Message, state: FSMContext) -> None:
    """Сколько времени админ готов уделять."""
    value = (message.text or "").strip()
    if not value:
        await message.answer("❌ Напиши, сколько времени готов уделять.")
        return
    await state.update_data(sa_hours=value[:60])
    await state.set_state(SearchFSM.admin_text)
    await message.answer(
        "🪪 <b>Анкета админа</b>\n\n"
        "✍️ <b>Ваш текст</b>\n\n"
        "Этот текст увидит владелец бота, когда пригласит тебя. Можно "
        "пропустить.",
        reply_markup=_skip_kb(),
    )


async def _save_admin_profile(message: Message, state: FSMContext) -> None:
    """Сохраняет анкету админа из накопленных ответов."""
    data = await state.get_data()
    user_id = msg_uid(message)
    set_admin_profile(
        user_id,
        username=msg_username(message) or "",
        first_name=msg_firstname(message) or "",
        age=str(data.get("sa_age") or ""),
        category=str(data.get("sa_category") or ""),
        pz_limit=str(data.get("sa_pz") or ""),
        timezone=str(data.get("sa_tz") or ""),
        hours=str(data.get("sa_hours") or ""),
        text=str(data.get("sa_text") or ""),
    )
    await state.clear()
    text, kb = search_payload(user_id)
    await message.answer(
        "✅ <b>Анкета админа сохранена!</b>\n\n"
        "Теперь тебя видно в разделе «👥 Просмотр профилей», и владельцы "
        "ботов смогут пригласить тебя.\n\n" + text,
        reply_markup=kb,
    )


@router.message(SearchFSM.admin_text)
async def fsm_admin_text(message: Message, state: FSMContext) -> None:
    """Текст админа."""
    await state.update_data(sa_text=(message.text or "").strip())
    await _save_admin_profile(message, state)


async def _save_admin_profile_by_callback(callback: CallbackQuery,
                                          state: FSMContext) -> None:
    """Сохраняет анкету админа с экрана колбэка (после «⏭ Пропустить»).

    Отдельная функция, а не переиспользование ``_save_admin_profile``: там
    нужен ``Message`` (имя, username, ``answer``), а здесь — ``CallbackQuery``.
    Раньше сохранение было написано прямо в теле обработчика, но без
    ``state.clear()`` и без отрисовки — анкета сохранялась, а человек
    оставался на том же экране с кнопкой, которая уже ничего не делала.
    """
    data = await state.get_data()
    user_id = cb_uid(callback)
    registry = get_user_registry(user_id) or {}
    set_admin_profile(
        user_id,
        username=str(registry.get("username") or ""),
        first_name=str(registry.get("first_name") or ""),
        age=str(data.get("sa_age") or ""),
        category=str(data.get("sa_category") or ""),
        pz_limit=str(data.get("sa_pz") or ""),
        timezone=str(data.get("sa_tz") or ""),
        hours=str(data.get("sa_hours") or ""),
        text=str(data.get("sa_text") or ""),
    )
    await state.clear()
    text, kb = search_payload(user_id)
    await render_callback(
        callback,
        "✅ <b>Анкета админа сохранена!</b>\n\n"
        "Теперь тебя видно в разделе «👥 Просмотр профилей», и владельцы "
        "ботов смогут пригласить тебя.\n\n" + text,
        kb,
    )


# ═══════════════ Ленты и листание ═══════════════

# Сколько карточек на страницу. Много не показываем: у Telegram есть лимит
# на размер клавиатуры, и на длинных списках экран переставал открываться.
FEED_PAGE = 5


def _page_rows(prefix: str, page: int, total: int) -> list[list[InlineKeyboardButton]]:
    """Строка листания «◀️ · N/M · ▶️» (пустая, если страница одна)."""
    if total <= 1:
        return []
    prev_page = page - 1 if page > 1 else total
    next_page = page + 1 if page < total else 1
    return [[
        InlineKeyboardButton(text="◀️", callback_data=f"{prefix}_{prev_page}",
                             style="primary"),
        InlineKeyboardButton(text=f"📄 {page}/{total}",
                             callback_data=f"{prefix}_{page}", style="primary"),
        InlineKeyboardButton(text="▶️", callback_data=f"{prefix}_{next_page}",
                             style="primary"),
    ]]


def _page_of(data: str, prefix: str) -> int:
    """Номер страницы из callback_data вида ``<prefix>_<N>``."""
    tail = data.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() and int(tail) > 0 else 1


# ── Анкеты админов ─────────────────────────────────────────────────────────

def _admin_card(row: dict, page: int) -> str:
    """Карточка анкеты админа для ленты."""
    who = f"@{html_escape(row['username'])}" if row.get("username") else (
        html_escape(row.get("first_name") or "") or "Админ"
    )
    parts = [
        f"\n👤 <b>{who}</b>",
        f"🎂 возраст: {_dash(row.get('age'))}",
        f"💬 категория: {_dash(row.get('category'))}",
        f"📋 ПЗ: {_dash(row.get('pz_limit'))}",
        f"🕐 пояс: {_dash(row.get('timezone'))}",
        f"⏳ время: {_dash(row.get('hours'))}",
    ]
    text = row.get("text")
    if str(text or "").strip():
        parts.append(f"\n✍️ {html_escape(str(text)[:500])}")
    return "\n".join(parts)


def _profiles_payload(user_id: int, page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    """Лента анкет админов («👥 Просмотр профилей»)."""
    rows = admin_profiles_feed(exclude_user_id=user_id, limit=FEED_PAGE * 3,
                               offset=(page - 1) * FEED_PAGE)
    window = rows[:FEED_PAGE]
    if not window:
        return (
            "👥 <b>Анкеты админов</b>\n\n"
            "Пока никто не заполнил анкету админа.\n\n"
            "Заполни свою: «🪪 Анкета админа» в разделе «🔎 Поиск».",
            search_payload(user_id)[1],
        )

    total = max(1, (len(rows) + FEED_PAGE - 1) // FEED_PAGE)
    text = [f"👥 <b>Анкеты админов</b> (стр. {page}/{total})\n"]
    for row in window:
        text.append(_admin_card(row, page))
        # Считаем просмотр: по нему владелец видит, что его анкету кто-то
        # смотрит. Повторный просмотр того же человека не увеличивает счётчик.
        mark_profile_viewed(user_id, "admin", int(row["user_id"]))

    kb_rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=f"✉️ Пригласить {_fit(row.get('username') or row.get('first_name') or 'админа', 24)}",
            callback_data=f"search_invite_{row['user_id']}", style="success")]
        for row in window
    ]
    # «🚫 Разбанить» показываем только тем, кого действительно где-то банят:
    # у человека бан хранится по ботам, и в ленте не видно, в каком именно.
    # Поэтому кнопка снимает бан во всех ботах сразу (см. unban_user_everywhere).
    for row in window:
        admin_id = int(row["user_id"])
        if get_user_banned_bots(admin_id):
            kb_rows.append([InlineKeyboardButton(
                text=f"🚫 Разбанить {_fit(row.get('username') or 'админа', 24)}",
                callback_data=f"search_unban_{admin_id}", style="danger")])
    kb_rows.extend(_page_rows("search_profiles_p", page, total))
    kb_rows.append([InlineKeyboardButton(text="⬅️ Поиск",
                                         callback_data="search_open",
                                         style="primary")])
    return "\n".join(text), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data.regexp(r"^search_unban_(\d+)$"))
async def cb_search_unban(callback: CallbackQuery) -> None:
    """«🚫 Разбанить» в карточке ПЗ/админа из «Просмотра профилей».

    Бан может стоять в любом из ботов человека, а в ленте не указано, в
    каком, — поэтому снимаем его во всех ботах разом и честно перечисляем,
    где бан был. Так кнопка не оставит человека «наполовину разбаненным».
    """
    admin_id = int(cb_data(callback).rsplit("_", 1)[-1])
    bots = unban_user_everywhere(admin_id)
    if not bots:
        await callback.answer("ℹ️ Активных банов не найдено", show_alert=True)
    else:
        # Показываем id ботов: подставить username здесь нечем — в списке
        # только идентификаторы, а поднимать каждый бот ради подписи дорого.
        where = ", ".join(f"#{b}" for b in bots)
        await callback.answer(f"✅ Разбанен в ботах: {where}", show_alert=True)

    page = _page_of("search_profiles_p1", "search_profiles_p")
    text, kb = _profiles_payload(cb_uid(callback), page)
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^search_profiles(_p_\d+)?$"))
async def cb_search_profiles(callback: CallbackQuery, state: FSMContext) -> None:
    """Лента анкет админов с листанием."""
    await state.clear()
    page = _page_of(cb_data(callback), "search_profiles_p")
    text, kb = _profiles_payload(cb_uid(callback), page)
    await render_callback(callback, text, kb)
# ── Карточки ботов ────────────────────────────────────────────────────────

def _bot_card(row: dict) -> str:
    """Карточка бота: анкета, если есть, иначе юзер + статистика."""
    username = f"@{html_escape(row['username'])}"
    parts = [f"\n🤖 <b>{username}</b>"]
    has_profile = bool(str(row.get("age_range") or "").strip()
                       or str(row.get("category") or "").strip()
                       or str(row.get("gender") or "").strip()
                       or str(row.get("text") or "").strip())
    if has_profile:
        parts.append(f"👤 возраст: {_dash(row.get('age_range'))}")
        parts.append(f"💬 категория: {_dash(row.get('category'))}")
        parts.append(f"🚻 пол: {_dash(row.get('gender'))}")
        text = row.get("text")
        if str(text or "").strip():
            parts.append(f"\n✍️ {html_escape(str(text)[:500])}")
    else:
        # Анкеты нет — показываем то, что есть, и говорим об этом прямо:
        # иначе человек решит, что бот «пустой» и его никто не зовёт.
        parts.append(f"📋 ПЗ: <b>{int(row.get('pz_count') or 0)}</b>")
        parts.append(f"💬 ответов админов: <b>{int(row.get('replies') or 0)}</b>")
        parts.append("\n<i>Анкета не заполнена</i>")
    return "\n".join(parts)


def _bots_payload(user_id: int, page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    """Лента ботов («🤖 Просмотр ботов»)."""
    rows = bot_profiles_feed(exclude_owner_id=user_id, limit=FEED_PAGE * 3,
                             offset=(page - 1) * FEED_PAGE)
    window = rows[:FEED_PAGE]
    if not window:
        return (
            "🤖 <b>Боты</b>\n\n"
            "Пока в поиске нет ни одного бота.",
            search_payload(user_id)[1],
        )

    total = max(1, (len(rows) + FEED_PAGE - 1) // FEED_PAGE)
    text = [f"🤖 <b>Боты</b> (стр. {page}/{total})\n"]
    for row in window:
        text.append(_bot_card(row))
        # Просмотр карточки бота засчитываем владельцу этого бота.
        mark_profile_viewed(user_id, "bot", int(row["bot_id"]))

    kb_rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=f"📨 Подать заявку @{html_escape(row['username'])[:20]}",
            callback_data=f"search_apply_{row['bot_id']}", style="success")]
        for row in window
    ]
    kb_rows.extend(_page_rows("search_bots_p", page, total))
    kb_rows.append([InlineKeyboardButton(text="⬅️ Поиск",
                                         callback_data="search_open",
                                         style="primary")])
    return "\n".join(text), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data.regexp(r"^search_bots(_p_\d+)?$"))
async def cb_search_bots(callback: CallbackQuery, state: FSMContext) -> None:
    """Лента ботов с листанием."""
    await state.clear()
    page = _page_of(cb_data(callback), "search_bots_p")
    text, kb = _bots_payload(cb_uid(callback), page)
    await render_callback(callback, text, kb)


# ── Отклики на анкеты ботов ───────────────────────────────────────────────

def _inbox_payload(user_id: int, page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    """Отклики: заявки админов на анкеты этого владельца."""
    rows = offers_inbox(OFFER_ADMIN_TO_BOT, user_id)
    window = rows[:FEED_PAGE]
    if not window:
        return (
            "📬 <b>Отклики</b>\n\n"
            "Пока никто не откликнулся на твои анкеты.\n\n"
            "Админ видит ботов в разделе «🤖 Просмотр ботов» и может подать "
            "заявку кнопкой «📨 Подать заявку».",
            search_payload(user_id)[1],
        )

    total = max(1, (len(rows) + FEED_PAGE - 1) // FEED_PAGE)
    text = [f"📬 <b>Отклики на твои анкеты</b> (стр. {page}/{total})\n"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    for row in window:
        who = f"@{html_escape(row.get('username') or '')}" if row.get("username") \
            else html_escape(row.get("first_name") or "") or "Админ"
        bot_name = row.get("bot_username") or row.get("bot_first_name") or "бот"
        text.append(
            f"\n👤 <b>{who}</b> → 🤖 <b>{html_escape(str(bot_name))}</b>\n"
            f"   🎂 {_dash(row.get('age'))} · 💬 {_dash(row.get('category'))}"
            f" · 📋 {_dash(row.get('pz_limit'))}\n"
            f"   🕐 {_dash(row.get('timezone'))} · ⏳ {_dash(row.get('hours'))}"
        )
        note = row.get("text")
        if str(note or "").strip():
            text.append(f"\n   ✍️ {html_escape(str(note)[:300])}")
        kb_rows.append([
            InlineKeyboardButton(text="✅ Одобрить",
                                 callback_data=f"search_accept_{row['id']}",
                                 style="success"),
            InlineKeyboardButton(text="❌ Отклонить",
                                 callback_data=f"search_decline_{row['id']}",
                                 style="danger"),
        ])
        # Отдельная кнопка «Открыть анкету» с решением внутри — чтобы не искать
        # заявку в тексте топика.
        kb_rows.append([
            InlineKeyboardButton(
                text=f"📄 Анкета {who}",
                callback_data=f"search_show_admin_{int(row['sender_id'])}_{int(row['id'])}",
                style="primary"),
        ])

    kb_rows.extend(_page_rows("search_inbox_p", page, total))
    kb_rows.append([InlineKeyboardButton(text="⬅️ Поиск",
                                         callback_data="search_open",
                                         style="primary")])
    return "\n".join(text), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data.regexp(r"^search_inbox(_p_\d+)?$"))
async def cb_search_inbox(callback: CallbackQuery, state: FSMContext) -> None:
    """Отклики с кнопками одобрения и отказа."""
    await state.clear()
    page = _page_of(cb_data(callback), "search_inbox_p")
    text, kb = _inbox_payload(cb_uid(callback), page)
    await render_callback(callback, text, kb)


# ═══════════════ Приглашения (владелец → админ) ═══════════════
#
# Обратная сторона «Откликов»: там админ присылает заявку владельцу, здесь
# владелец приглашает админа. Раздел нужен по двум причинам:
#
# 1. Приглашение лежит в личке, и когда оно пришло минуту назад, человек
#    мог его уже не помнить. Список показывает все неотвеченные.
# 2. Решение по приглашению — ответ на вопрос «согласиться или отказаться».
#    Раньше в приглашении не было ни одной такой кнопки (см. ``_invite_kb``),
#    поэтому админ физически не мог ответить, и приглашения висели вечно.


def _invites_payload(user_id: int, page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    """Неотвеченные приглашения, отправленные этому админу владельцами."""
    rows = offers_inbox(OFFER_BOT_TO_ADMIN, user_id)
    window = rows[:FEED_PAGE]
    if not window:
        return (
            "📨 <b>Приглашения</b>\n\n"
            "Тебя пока никто не приглашал в свои боты.\n\n"
            "Как только владелец пришлёт приглашение, оно появится здесь — "
            "с кнопками «✅ Принять» и «❌ Отклонить».",
            search_payload(user_id)[1],
        )

    total = max(1, (len(rows) + FEED_PAGE - 1) // FEED_PAGE)
    text = [f"📨 <b>Приглашения в твои боты</b> (стр. {page}/{total})\n"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    for row in window:
        bot_name = (row.get("bot_username") or row.get("bot_first_name")
                    or f"бот {row.get('bot_id')}")
        who = row.get("owner_username") or ""
        text.append(
            f"\n🤖 <b>@{html_escape(str(bot_name))}</b>"
            + (f"\n   👤 от @{html_escape(str(who))}" if who else "")
        )
        note = row.get("text")
        if str(note or "").strip():
            text.append(f"\n   ✍️ {html_escape(str(note)[:200])}")
        offer_id = int(row["id"])
        kb_rows.append([
            InlineKeyboardButton(text="✅ Принять",
                                 callback_data=f"search_accept_{offer_id}",
                                 style="success"),
            InlineKeyboardButton(text="❌ Отклонить",
                                 callback_data=f"search_decline_{offer_id}",
                                 style="danger"),
        ])
        kb_rows.append([
            InlineKeyboardButton(
                text=f"📄 Анкета @{html_escape(str(bot_name))}",
                callback_data=f"search_show_bot_{int(row['bot_id'])}",
                style="primary"),
        ])

    kb_rows.extend(_page_rows("search_invites_p", page, total))
    kb_rows.append([InlineKeyboardButton(text="⬅️ Поиск",
                                         callback_data="search_open",
                                         style="primary")])
    return "\n".join(text), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data.regexp(r"^search_invites(_p_\d+)?$"))
async def cb_search_invites(callback: CallbackQuery, state: FSMContext) -> None:
    """«📨 Приглашения» — все приглашения, ждущие ответа."""
    await state.clear()
    page = _page_of(cb_data(callback), "search_invites_p")
    text, kb = _invites_payload(cb_uid(callback), page)
    await render_callback(callback, text, kb)
# ═══════════════ Действия с анкетами ═══════════════

def _bot_profile_text(profile: dict | None, bot_name: str) -> str:
    """Текст анкеты бота для личного сообщения."""
    if not profile:
        return (f"🤖 <b>{html_escape(bot_name)}</b>\n\n"
                "Анкета бота пока не заполнена.")
    lines = [f"🤖 <b>{html_escape(bot_name)}</b>\n"]
    lines.append(f"👤 возраст: {_dash(profile.get('age_range'))}")
    lines.append(f"💬 категория: {_dash(profile.get('category'))}")
    lines.append(f"🚻 пол: {_dash(profile.get('gender'))}")
    note = profile.get("text")
    if str(note or "").strip():
        lines.append(f"\n✍️ {html_escape(str(note)[:800])}")
    return "\n".join(lines)


def _admin_profile_text(profile: dict | None, who: str) -> str:
    """Текст анкеты админа для личного сообщения."""
    if not profile:
        return f"🪪 <b>{html_escape(who)}</b>\n\nАнкета пока не заполнена."
    lines = [f"🪪 <b>{html_escape(who)}</b>\n"]
    lines.append(f"🎂 возраст: {_dash(profile.get('age'))}")
    lines.append(f"💬 категория: {_dash(profile.get('category'))}")
    lines.append(f"📋 сколько ПЗ: {_dash(profile.get('pz_limit'))}")
    lines.append(f"🕐 пояс: {_dash(profile.get('timezone'))}")
    lines.append(f"⏳ время: {_dash(profile.get('hours'))}")
    note = profile.get("text")
    if str(note or "").strip():
        lines.append(f"\n✍️ {html_escape(str(note)[:800])}")
    return "\n".join(lines)


async def _send_private(bot, user_id: int, text: str,
                        kb: InlineKeyboardMarkup | None) -> bool:
    """Шлёт в личку и возвращает, дошло ли.

    Человек мог не начать диалог с ботом — тогда Telegram отвечает
    «bot can't initiate conversation». Молчать об этом нельзя: отправитель
    должен знать, что приглашение не дошло.
    """
    try:
        await bot.send_message(user_id, text, reply_markup=kb)
        return True
    except Exception as e:
        logger.info("Не удалось отправить сообщение в личку %s: %s", user_id, e)
        return False


def _invite_kb(bot_id: int, offer_id: int) -> InlineKeyboardMarkup:
    """Кнопки в личном приглашении: принять, отклонить, посмотреть анкету.

    Раньше здесь была только кнопка «Открыть анкету» — и это был баг: получив
    приглашение, админ физически не мог на него ответить. Нечем было ни согласиться,
    ни отказаться: приглашение висело, а «Отклики» показывают только заявки
    САМИХ админов, а не приглашения владельцев. Решение можно было принять
    только перепиской в личке, если владелец туда писал.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if offer_id:
        rows.append([
            InlineKeyboardButton(text="✅ Принять",
                                 callback_data=f"search_accept_{offer_id}",
                                 style="success"),
            InlineKeyboardButton(text="❌ Отклонить",
                                 callback_data=f"search_decline_{offer_id}",
                                 style="danger"),
        ])
    rows.append([InlineKeyboardButton(text="📄 Открыть анкету",
                                      callback_data=f"search_show_bot_{bot_id}",
                                      style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)



@router.callback_query(F.data.regexp(r"^search_invite_\d+$"))
async def cb_search_invite(callback: CallbackQuery) -> None:
    """«✉️ Пригласить» — пишем админу в личку с анкетой бота."""
    admin_id = int(cb_data(callback).rsplit("_", 1)[-1])
    user_id = cb_uid(callback)
    bots = _standard_bots(user_id)
    if not bots:
        await callback.answer("Нет ботов для приглашения", show_alert=True)
        return

    # К какому боту приглашаем: берём первый «Стандарт». Отдельного выбора
    # здесь нет намеренно — приглашение отправляется одним касанием, а
    # владелец уточнит бот в переписке.
    bot_row = bots[0]
    bot_id = int(bot_row["id"])
    bot_name = bot_display_name(bot_row)
    profile = get_bot_profile(bot_id)

    sent_today = get_daily_sent(user_id)
    if sent_today >= DAILY_OFFER_LIMIT:
        await callback.answer(
            f"⏳ Сегодня уже отправлено {sent_today} приглашений.\n"
            f"Лимит — {DAILY_OFFER_LIMIT} в сутки, обновится завтра.",
            show_alert=True,
        )
        return

    # Повторные приглашения ограничены: отправлено / принято / недавно отказано.
    blocked = offer_send_block(OFFER_BOT_TO_ADMIN, user_id, admin_id, bot_id)
    if blocked:
        await callback.answer(blocked, show_alert=True)
        return

    offer_id = add_offer(OFFER_BOT_TO_ADMIN, user_id, admin_id, bot_id)
    ok = await _send_private(
        callback.bot, admin_id,
        "📨 <b>Тебя приглашают в бота</b>\n\n" + _bot_profile_text(
            profile, bot_name),
        _invite_kb(bot_id, offer_id),
    )
    if ok:
        bump_daily_sent(user_id)
        await callback.answer("✉️ Приглашение отправлено")
    else:
        await callback.answer(
            "❌ Не удалось написать: админ не начал диалог с ботом",
            show_alert=True,
        )
    page = _page_of("search_profiles_p1", "search_profiles_p")
    text, kb = _profiles_payload(user_id, page)
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^search_apply_\d+$"))
async def cb_search_apply(callback: CallbackQuery) -> None:
    """«📨 Подать заявку» — владельцу бота уходит анкета админа."""
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    user_id = cb_uid(callback)

    row = next((r for r in bot_profiles_feed(exclude_owner_id=user_id)
                if int(r["bot_id"]) == bot_id), None)
    if row is None:
        await callback.answer("Бот не найден", show_alert=True)
        return
    owner_id = int(row["owner_id"])
    if owner_id == user_id:
        await callback.answer("Это твой бот", show_alert=True)
        return

    sent_today = get_daily_sent(user_id)
    if sent_today >= DAILY_OFFER_LIMIT:
        await callback.answer(
            f"⏳ Сегодня уже отправлено {sent_today} приглашений.\n"
            f"Лимит — {DAILY_OFFER_LIMIT} в сутки, обновится завтра.",
            show_alert=True,
        )
        return

    # Повторные заявки ограничены: отправил / приняли / недавно отказали.
    blocked = offer_send_block(OFFER_ADMIN_TO_BOT, user_id, owner_id, bot_id)
    if blocked:
        await callback.answer(blocked, show_alert=True)
        return

    profile = get_admin_profile(user_id)
    who = (profile or {}).get("username") or "Админ"
    offer_id = add_offer(OFFER_ADMIN_TO_BOT, user_id, owner_id, bot_id)

    # Кнопки решения шлём СРАЗУ в уведомлении: владельцу не нужно искать
    # заявку в «Откликах», он решает прямо здесь.
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять",
                              callback_data=f"search_accept_{offer_id}",
                              style="success"),
         InlineKeyboardButton(text="❌ Отклонить",
                              callback_data=f"search_decline_{offer_id}",
                              style="danger")],
        [InlineKeyboardButton(text="📄 Открыть анкету",
                              callback_data=f"search_show_admin_{user_id}_{offer_id}",
                              style="primary")],
    ])
    ok = await _send_private(
        callback.bot, owner_id,
        f"📩 <b>Новая заявка на вступление</b>\n\n"
        f"🤖 Бот: <b>@{html_escape(str(row['username']))}</b>\n\n"
        + _admin_profile_text(profile, str(who)),
        kb,
    )
    if ok:
        bump_daily_sent(user_id)
        await callback.answer("📨 Заявка отправлена владельцу")
    else:
        await callback.answer(
            "❌ Не удалось написать владельцу — он не начал диалог с ботом",
            show_alert=True,
        )
    text, kb = _bots_payload(user_id, 1)
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^search_show_bot_\d+$"))
async def cb_show_bot_profile(callback: CallbackQuery) -> None:
    """«📄 Открыть анкету» — показывает анкету бота из личного сообщения."""
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    row = next((r for r in bot_profiles_feed(exclude_owner_id=0)
                if int(r["bot_id"]) == bot_id), None)
    bot_name = str(row["username"]) if row else f"бот {bot_id}"
    profile = get_bot_profile(bot_id)

    await render_callback(
        callback, _bot_profile_text(profile, bot_name),
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Поиск", callback_data="search_open",
                                 style="primary"),
        ]]),
    )


@router.callback_query(F.data.regexp(r"^search_show_admin_(\d+)(?:_(\d+))?$"))
async def cb_show_admin_profile(callback: CallbackQuery) -> None:
    """«📄 Открыть анкету» — карточка админа сразу с решением.

    Раньше карточка была только для просмотра: чтобы решить, владельцу надо
    было вернуться в «Отклики» и найти заявку там. Теперь, если нажатие
    пришло из заявки (``search_show_admin_<id>_<offer_id>``), сразу показываем
    «✅ Принять» / «❌ Отклонить».
    """
    parts = cb_data(callback).split("_")
    admin_id = int(parts[3])
    offer_id = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    profile = get_admin_profile(admin_id)
    who = str((profile or {}).get("username")
              or (profile or {}).get("first_name") or "Админ")

    rows: list[list[InlineKeyboardButton]] = []
    if offer_id:
        rows.append([
            InlineKeyboardButton(text="✅ Принять",
                                 callback_data=f"search_accept_{offer_id}",
                                 style="success"),
            InlineKeyboardButton(text="❌ Отклонить",
                                 callback_data=f"search_decline_{offer_id}",
                                 style="danger"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Поиск", callback_data="search_open",
                                      style="primary")])

    await render_callback(
        callback, _admin_profile_text(profile, who),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.regexp(r"^search_(accept|decline)_\d+$"))
async def cb_search_decide(callback: CallbackQuery) -> None:
    """«✅ Принять» / «❌ Отклонить» — и на заявке, и на приглашении.

    Обе кнопки ведут сюда, потому что решение принимает всегда ПОЛУЧАТЕЛЬ
    предложения, а он один и тот же человек в обоих случаях. Различаются
    они только тем, КОМУ уходит уведомление и каким оно должно быть:

    * заявка (админ → владелец): владельцу уходит **контакт админа**, чтобы
      он знал, кого позвать в админы;
    * приглашение (владелец → админ): владельцу уходит **юзернейм и ID
      принявшего** — иначе он не знает, кто согласился, и приглашение
      пропадало бы у него в личке без следа.
    """
    parts = cb_data(callback).split("_")
    action = parts[1]
    offer_id = int(parts[2])
    user_id = cb_uid(callback)

    # Кнопка может висеть в личке у получателя — право проверяем по таблице,
    # а не по нажавшему.
    offer = get_offer(offer_id)
    if not offer or int(offer["recipient_id"]) != user_id:
        await callback.answer("❌ Это не твоё приглашение", show_alert=True)
        return
    # Уже решённое повторно не переигрываем: иначе старое уведомление «приняли»
    # могло бы прийти дважды, а дважды и отказаться — нечестно с отправителем.
    if str(offer.get("status") or "") != STATUS_PENDING:
        await callback.answer("ℹ️ На это приглашение уже ответили",
                              show_alert=True)
        return

    accepted = action == "accept"
    is_invite = str(offer.get("kind") or "") == OFFER_BOT_TO_ADMIN
    set_offer_status(offer_id,
                     STATUS_ACCEPTED if accepted else STATUS_DECLINED)

    sender_id = int(offer["sender_id"])
    if accepted:
        registry = get_user_registry(user_id) or {}
        username = str(registry.get("username") or "")
        first_name = str(registry.get("first_name") or "")

        if is_invite:
            # Владелец звал админа — сообщаем ему, КТО согласился.
            who = f"@{html_escape(username)}" if username else (
                html_escape(first_name) or f"ID:{user_id}"
            )
            contact_line = (
                f"👤 <b>Кто принял:</b> {who}\n"
                f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"
                + (f"Написать ему: <code>https://t.me/{html_escape(username)}</code>"
                   if username else
                   "Юзернейма у него нет — найдите его в чате админов по ID.")
            )
            await _send_private(
                callback.bot, sender_id,
                "✅ <b>Приглашение принято!</b>\n\n"
                "В твой бот согласился вступить админ. Он пока не стал "
                "админом — добавь его в админы бота и напиши лично:\n\n"
                + contact_line,
                None,
            )
        else:
            # Владелец одобрил заявку — сообщаем ему контакт админа.
            profile = get_admin_profile(sender_id) or {}
            admin_username = str(profile.get("username") or "")
            contact_line = (
                f"👤 <b>Юзернейм:</b> @{html_escape(admin_username)}\n"
                f"🆔 <b>ID:</b> <code>{sender_id}</code>\n\n"
                f"Напишите ему: <code>https://t.me/{html_escape(admin_username)}</code>"
                if admin_username else
                f"🆔 <b>ID:</b> <code>{sender_id}</code>\n\n"
                f"Юзернейма у него нет — найдите его в чате админов по ID."
            )
            await _send_private(
                callback.bot, user_id,
                "✅ <b>Заявка принята!</b>\n\n"
                "Админ получит уведомление. Он не станет админом автоматически — "
                "добавьте его в админы бота и напишите ему лично:\n\n"
                + contact_line,
                None,
            )

    # ── Кому что уходит ────────────────────────────────────────────────
    #
    # Здесь легко перепутать адресата, и это уже случилось: текст «Вы
    # приняли/отклонили приглашение» написан от лица НАЖАВШЕГО, то есть его
    # получает user_id. Раньше оно уходило отправителю (sender_id), и при
    # отказе ВЛАДЕЛЕЦ получал «Вы отклонили приглашение» — обращение к нему
    # же, хотя отказал админ, а самому админу не приходило ничего.
    #
    # Итого по приглашению (владелец звал → решает админ):
    #   * принятие — владельцу уходит контакт согласившегося (выше),
    #     админу — подтверждение его собственного действия;
    #   * отказ   — владельцу уходит «приглашение отклонено» (иначе его
    #     приглашение просто исчезнет без следа), админу — подтверждение.
    if is_invite:
        if not accepted:
            await _send_private(
                callback.bot, sender_id,
                "❌ <b>Приглашение отклонено</b>\n\n"
                "Админ, которому ты предлагал вступить в бота, отказался. "
                "Он не стал админом. Передумает — пригласи его снова.",
                None,
            )
        await _send_private(
            callback.bot, user_id,
            ("✅ <b>Вы приняли приглашение!</b>\n\nВладелец бота узнал об этом "
             "и скоро добавит вас в админы — следите за сообщениями от бота."
             if accepted else
             "❌ <b>Вы отклонили приглашение</b>\n\nВладелец бота узнал об этом. "
             "Если передумаете — он может позвать вас снова."),
            None,
        )
    else:
        # Заявка (админ просил → решает владелец). Ждёт ответа автор заявки,
        # то есть sender_id; контакт админа при принятии ушёл выше.
        await _send_private(
            callback.bot, sender_id,
            ("✅ <b>Тебя приняли!</b>\n\nВладелец бота одобрил твою заявку. "
             "Скоро он добавит тебя в админы — следи за сообщениями от бота."
             if accepted else
             f"❌ <b>Заявка отклонена</b>\n\nВладелец бота не принял твою заявку. "
             f"Повторно подать её можно будет через "
             f"{REOFFER_COOLDOWN_HOURS} ч."),
            None,
        )

    if is_invite:
        await callback.answer(
            "✅ Принято — владельцу ушёл контакт" if accepted
            else "❌ Отклонено — владелец узнает об этом")
        # Возвращаем в «Приглашения»: решение принято, предложение ушло из
        # списка, и показывать снова «Отклики» было бы не туда.
        text, kb = _invites_payload(user_id, 1)
    else:
        await callback.answer("✅ Принято — контакт отправлен" if accepted
                              else "❌ Отклонено")
        text, kb = _inbox_payload(user_id, 1)
    await render_callback(callback, text, kb)
