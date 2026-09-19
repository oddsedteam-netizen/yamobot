"""Раздел «✨ Прочее»: о проекте, поддержка и обучение.

Содержит:
• reply-кнопку «✨ Прочее» главного меню;
• инлайн-разделы: поддержать проект (донат), проект YamoChan, тестовый бот
  и обучение;
• пошаговое обучение (/tutor) — проходит прямо в этом боте, кнопкой
  «📚 Обучение» или командой /tutor.

Функции для каналов (бывшие «🧩 Эксп функции») переехали в отдельную синюю
reply-кнопку «📢 Мой ТГК» главного меню — см. handlers/channels.py.

Ссылки на YamoChan, донат и тестового бота задаёт супер-админ в
«👤 Профиль → 🛡 Админ-панель → 🔗 Настройки ссылок».
"""

from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import render_callback, cb_data, normalize_link
from services.constants import (
    SETTING_DONATE_URL,
    SETTING_TEST_BOT_URL,
    SETTING_YAMOCHAN_URL,
)
from services.storage import get_app_setting

router = Router()


# ═══════════════ Работа со ссылками из админ-панели ═══════════════
# Само приведение ссылок живёт в handlers/_common.py (normalize_link) —
# одна реализация и для редактора кнопок, и для настроек админ-панели.



def donate_link() -> str:
    """Ссылка на донат (задаётся в админ-панели)."""
    return normalize_link(get_app_setting(SETTING_DONATE_URL))


def yamochan_link() -> str:
    """Ссылка проекта YamoChan (задаётся в админ-панели)."""
    return normalize_link(get_app_setting(SETTING_YAMOCHAN_URL))


def test_bot_link() -> str:
    """Ссылка на тестового бота (задаётся в админ-панели)."""
    return normalize_link(get_app_setting(SETTING_TEST_BOT_URL))


# ═══════════════ Меню «Прочее» ═══════════════

OTHER_TEXT = (
    "🟢 <b>Прочее</b>\n\n"
    "Здесь всё, что не про управление ботами: наш проект, поддержка команды "
    "и обучение.\n\n"
    "Выбери раздел кнопками ниже 👇"
)


def other_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Мой ТГК", callback_data="ch_tgk_root",
                              style="primary")],
        [
            InlineKeyboardButton(text="💚 Поддержать проект", callback_data="other_donate",
                                 style="success"),
            InlineKeyboardButton(text="🤖 Проект YamoChan", callback_data="other_yamochan",
                                 style="primary"),
        ],
        [
            InlineKeyboardButton(text="🧪 Тестовый бот", callback_data="other_testbot",
                                 style="primary"),
            InlineKeyboardButton(text="📚 Обучение", callback_data="other_tutorial",
                                 style="success"),
        ],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")],
    ])


@router.message(F.text == "✨ Прочее", F.chat.type == ChatType.PRIVATE)
async def on_other_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(OTHER_TEXT, reply_markup=other_menu_kb())


@router.callback_query(F.data == "other")
async def cb_other(callback: CallbackQuery) -> None:
    await render_callback(callback, OTHER_TEXT, other_menu_kb())


# ═══════════════ Проект YamoChan ═══════════════

YAMOCHAN_TEXT = (
    "🤖 <b>Проект YamoChan</b>\n\n"
    "YamoChan — проект, который сейчас находится в процессе создания.\n\n"
    "Это <b>бот-модератор</b>: он следит за порядком в чате и настраивается "
    "под тебя — под твои правила, темы и стиль общения. Мы делаем его так, "
    "чтобы даже большой и шумный чат оставался под контролем.\n\n"
    "📅 Примерный релиз планируется на <b>25 сентября</b>.\n\n"
    "Как только проект будет готов, здесь появится кнопка со ссылкой — "
    "а пока заглядывай сюда за новостями."
)


def _yamochan_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    link = yamochan_link()
    if link:
        rows.append([InlineKeyboardButton(text="🔗 Перейти к YamoChan", url=link,
                                          style="primary")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="other",
                                      style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "other_yamochan")
async def cb_other_yamochan(callback: CallbackQuery) -> None:
    await render_callback(callback, YAMOCHAN_TEXT, _yamochan_kb())


# ═══════════════ Поддержать проект (донат) ═══════════════

DONATE_TEXT = (
    "💚 <b>Поддержать проект</b>\n\n"
    "Спасибо, что решил поддержать нашу команду! 🙌\n\n"
    "Мы — команда, которая делает конструктор <b>YamoBot</b> и проект "
    "<b>YamoChan</b>. Любая поддержка помогает развивать конструктор: "
    "добавлять новые функции, ускорять работу и делать настройку бота "
    "ещё проще и понятнее.\n\n"
    "Если хочешь помочь — нажми кнопку ниже 👇"
)


def _donate_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    link = donate_link()
    if link:
        rows.append([InlineKeyboardButton(text="💚 Задонатить", url=link,
                                          style="success")])
    else:
        rows.append([InlineKeyboardButton(text="💚 Задонатить", callback_data="other_donate_soon",
                                          style="success")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="other",
                                      style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "other_donate")
async def cb_other_donate(callback: CallbackQuery) -> None:
    await render_callback(callback, DONATE_TEXT, _donate_kb())


@router.callback_query(F.data == "other_donate_soon")
async def cb_other_donate_soon(callback: CallbackQuery) -> None:
    await callback.answer("💚 Ссылка на донат пока не задана — скоро появится.",
                          show_alert=True)


# ═══════════════ Тестовый бот ═══════════════

TESTBOT_TEXT = (
    "🧪 <b>Тестовый бот</b>\n\n"
    "Этот бот настроен нашей командой. В нём использованы все фишки "
    "<b>YamoBot</b> — в основном оформление: приветствие, кнопки и режимы.\n\n"
    "Посмотри, как это выглядит вживую, — так проще понять, что можно "
    "сделать со своим ботом.\n\n"
    "Хочешь перейти? Нажми кнопку ниже 👇"
)


def _testbot_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    link = test_bot_link()
    if link:
        rows.append([InlineKeyboardButton(text="➡️ Перейти", url=link, style="success")])
    else:
        rows.append([InlineKeyboardButton(text="➡️ Перейти", callback_data="other_testbot_soon",
                                          style="success")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="other",
                                      style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "other_testbot")
async def cb_other_testbot(callback: CallbackQuery) -> None:
    await render_callback(callback, TESTBOT_TEXT, _testbot_kb())


@router.callback_query(F.data == "other_testbot_soon")
async def cb_other_testbot_soon(callback: CallbackQuery) -> None:
    await callback.answer("🧪 Ссылка на тестового бота пока не задана — скоро появится.",
                          show_alert=True)


# ═══════════════ Обучение: раздел «Прочее» ═══════════════

TUTORIAL_TEXT = (
    "📚 <b>Обучение</b>\n\n"
    "В обучении мы подробно рассказываем про весь этап создания и подключения "
    "бота: от первого токена до привязанных чатов и включённых режимов.\n\n"
    "Можно просто прочитать, а можно следовать инструкции и параллельно "
    "настраивать своего бота — так получится запустить его прямо во время "
    "обучения.\n\n"
    "Обучение проходит <b>здесь же, в YamoBot</b> — никуда переходить не нужно. "
    "Нажми «📚 Обучение», и начнём 👇"
)


def tutorial_kb() -> InlineKeyboardMarkup:
    """Кнопки экрана обучения.

    Зелёная кнопка запускает урок прямо в этом чате — обучение полностью
    проходит внутри основного бота, никаких переходов в другие боты.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Обучение", callback_data="tutor_start",
                              style="success")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="other",
                              style="primary")],
    ])


@router.callback_query(F.data == "other_tutorial")
async def cb_other_tutorial(callback: CallbackQuery) -> None:
    await render_callback(callback, TUTORIAL_TEXT, tutorial_kb())


@router.callback_query(F.data == "tutor_start")
async def cb_tutor_start(callback: CallbackQuery) -> None:
    """Запуск обучения прямо в текущем чате (кнопка «📚 Обучение»)."""
    message = callback.message
    if message is not None:
        try:
            await message.answer(TUTOR_INTRO)
        except Exception:
            pass
    text, kb = _tutor_page(0)
    await render_callback(callback, text, kb, force_answer=True)


# ═══════════════ Обучение: пошаговый урок (/tutor) ═══════════════

TUTOR_INTRO = (
    "👋 <b>Привет!</b>\n\n"
    "Это <b>YamoBot</b> — обучение пройдёт прямо здесь, никуда переходить "
    "не нужно.\n\n"
    "Можно просто читать шаги, а можно сразу настраивать своего бота по "
    "инструкции — так ты запустишь его уже во время обучения."
)

TUTOR_STEP_0 = (
    "🎓 <b>Добро пожаловать на обучение!</b>\n\n"
    "Мы пройдём весь путь настройки бота простым языком и по шагам:\n"
    "1. подключение бота к платформе;\n"
    "2. выбор категории;\n"
    "3. настройка приветствия и инлайн-кнопок;\n"
    "4. включение и настройка режимов;\n"
    "5. подключение YamoBot к чатам работы и админов.\n\n"
    "Можно просто прочитать, а можно идти по шагам и сразу настраивать "
    "своего бота — тогда получится запустить его прямо во время обучения.\n\n"
    "Готов? Нажми «Продолжить» 👇"
)

TUTOR_STEP_1 = (
    "1️⃣ <b>Подключение бота к платформе</b>\n\n"
    "Сначала создаём самого бота в Telegram, а потом отдаём его нам "
    "в управление.\n\n"
    "1. Открой чат с <b>@BotFather</b> — это официальный бот Telegram "
    "для создания ботов.\n"
    "2. Отправь ему команду <code>/newbot</code>.\n"
    "3. BotFather попросит придумать <b>имя</b> бота (его увидят люди) "
    "и <b>адрес</b> — короткое имя на латинице, которое заканчивается "
    "на слово «bot». Название придумай своё, любое удобное.\n"
    "4. В ответ BotFather пришлёт <b>токен</b> — длинный набор букв и цифр. "
    "Это «ключ» от твоего бота: никому его не показывай и не пересылай.\n"
    "5. Вернись сюда, открой <b>«🤖 Боты» → «➕ Добавить бота»</b> "
    "и отправь этот токен.\n"
    "6. Бот подключится и появится в списке — с этого момента им можно "
    "управлять прямо отсюда."
)


TUTOR_STEP_2 = (
    "2️⃣ <b>Выбор категории бота</b>\n\n"
    "При добавлении бот спросит категорию. От неё зависит, как бот "
    "выглядит и что умеет.\n\n"
    "🗂 <b>Стандарт</b> — обычный бот для обращений. Под приветствием у него "
    "есть панель с кнопкой смены админа: человек может в один клик "
    "поменять своего администратора. Выбирай его, если людям нужно "
    "переписываться с админами.\n\n"
    "📝 <b>Анкетница</b> — бот-визитка без панели админов. В ней можно "
    "оставить кнопки-ссылки со статьёй в Телеграме (например, ссылку на "
    "Telegra.ph) и красиво оформить свою анкетницу: описание, правила, "
    "прайс, контакты. Её же удобно использовать как бота категории "
    "<b>Стандарт</b>, если кнопка смены админа не нужна — просто чтобы "
    "всё было красиво оформлено.\n\n"
    "Коротко: нужен диалог с админами — «Стандарт», нужно только показать "
    "информацию — «Анкетница». Потом всегда можно добавить второго бота "
    "другой категории."
)

TUTOR_STEP_3 = (
    "3️⃣ <b>Приветствие и инлайн-кнопки</b>\n\n"
    "Приветствие — это то, что человек видит первым, когда запускает бота. "
    "Кнопки под ним помогают сразу перейти куда нужно.\n\n"
    "1. Открой <b>«🤖 Боты»</b>, выбери своего бота и нажми "
    "<b>«✏️ Редактор»</b>.\n"
    "2. <b>«💬 Изменить приветствие»</b> — отправь текст приветствия. "
    "Можно оформить его: сделать жирным, курсивом, добавить премиум-эмодзи. "
    "Можно отправить <b>фото с подписью</b> — тогда приветствие будет "
    "с картинкой.\n"
    "3. <b>«🔗 Линки»</b> — кнопки-ссылки под приветствием. Сначала "
    "напиши название кнопки, потом вставь ссылку (она начинается "
    "с http:// или https://). Бот спросит цвет: синий, зелёный или "
    "красный. Ненужные кнопки можно удалять по одной.\n"
    "4. Если ботов много, есть общий редактор: <b>«📌 Выбрать все»</b> — "
    "он меняет приветствие и кнопки сразу у всех ботов.\n\n"
    "Совет: пиши приветствие так, будто объясняешь человеку, куда он попал "
    "и что делать дальше."
)


TUTOR_STEP_4 = (
    "4️⃣ <b>Включение и настройка режимов</b>\n\n"
    "Режимы — это защита и помощники бота. Включаются они в карточке бота, "
    "а настраиваются парой чисел или времени.\n\n"
    "🛡 <b>Антиспам</b> — следит за спамом в личке бота (например, "
    "за стикерами и флудом) и временно ограничивает нарушителя.\n\n"
    "🚨 <b>Антинакрутка</b> — защита от накрутки обращений: если за короткое "
    "время приходит слишком много сообщений, бот временно перестаёт "
    "создавать новые и предупреждает владельца. Снимается вручную.\n\n"
    "🛡 <b>Антирейд</b> — для группового чата: следит за массовыми заходами "
    "и при подозрении на рейд закрывает чат и зовёт владельца.\n\n"
    "⏰ <b>Напоминалка</b> — напоминает админам об обращениях, которые долго "
    "висят без ответа. В <b>«🕒 Настройка времени»</b> указываешь часы "
    "(по МСК), когда напоминания не приходят — чтобы не будить ночью.\n\n"
    "Включи сначала один режим, посмотри, как удобно, и добавь остальные."
)

TUTOR_STEP_5 = (
    "5️⃣ <b>Чаты работы и админов</b>\n\n"
    "Чтобы приходили уведомления и можно было работать командой, привяжи "
    "к боту два групповых чата.\n\n"
    "💼 <b>Чат работы</b> — обычный рабочий чат команды. Бот запомнит его "
    "и выйдет из него.\n\n"
    "🛡 <b>Чат админов</b> — сюда приходят уведомления о новых обращениях "
    "и статистика. Бот остаётся в этом чате, и ему нужно выдать права "
    "администратора, иначе часть уведомлений может не дойти.\n\n"
    "Как привязать:\n"
    "1. Открой <b>«👤 Профиль» → «🔗 Привязать чаты»</b>.\n"
    "2. Выбери, какой чат привязываешь — работы или админов.\n"
    "3. Добавь бота в нужную группу.\n"
    "4. Вернись в бота и нажми <b>«✅ Я добавил бота»</b>.\n\n"
    "После привязки чата админов бот пришлёт в него приветствие со списком "
    "команд — по нему удобно ориентироваться."
)

TUTOR_STEP_6 = (
    "✅ <b>Обучение пройдено!</b>\n\n"
    "Коротко, что ты уже умеешь:\n"
    "1. подключать бота к платформе;\n"
    "2. выбирать категорию;\n"
    "3. оформлять приветствие и кнопки;\n"
    "4. включать и настраивать режимы;\n"
    "5. привязывать чаты работы и админов.\n\n"
    "Что дальше:\n"
    "• добавь админов и передай им теги — <b>«👥 Админы»</b>;\n"
    "• следи за обращениями — <b>«📋 ПЗ»</b>;\n"
    "• настрой напоминалку, чтобы ничего не терялось.\n\n"
    "Если что-то забылось — загляни в <b>«❓ FAQ»</b> или запусти обучение "
    "заново командой <code>/tutor</code>.\n\n"
    "Приятной работы! 🤖"
)

TUTOR_STEPS: list[str] = [
    TUTOR_STEP_0,
    TUTOR_STEP_1,
    TUTOR_STEP_2,
    TUTOR_STEP_3,
    TUTOR_STEP_4,
    TUTOR_STEP_5,
    TUTOR_STEP_6,
]



def _tutor_page(index: int) -> tuple[str, InlineKeyboardMarkup]:
    """Возвращает текст и клавиатуру шага обучения по его номеру."""
    index = max(0, min(index, len(TUTOR_STEPS) - 1))
    last = index == len(TUTOR_STEPS) - 1

    nav: list[InlineKeyboardButton] = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад",
                                        callback_data=f"tutor_step_{index - 1}",
                                        style="primary"))
    if not last:
        nav.append(InlineKeyboardButton(text="▶️ Продолжить",
                                        callback_data=f"tutor_step_{index + 1}",
                                        style="success"))

    rows: list[list[InlineKeyboardButton]] = []
    if nav:
        rows.append(nav)
    if last:
        rows.append([InlineKeyboardButton(text="🔄 Пройти заново",
                                          callback_data="tutor_step_0",
                                          style="primary")])
        rows.append([InlineKeyboardButton(text="🏠 Главное меню",
                                          callback_data="back_main")])

    return TUTOR_STEPS[index], InlineKeyboardMarkup(inline_keyboard=rows)


async def start_tutorial(message: Message) -> None:
    """Запускает обучение: короткое приветствие + первый шаг."""
    await message.answer(TUTOR_INTRO)
    text, kb = _tutor_page(0)
    await message.answer(text, reply_markup=kb)


@router.message(Command("tutor"), F.chat.type == ChatType.PRIVATE)
async def cmd_tutor(message: Message, state: FSMContext) -> None:
    await state.clear()
    await start_tutorial(message)


@router.callback_query(F.data.startswith("tutor_step_"))
async def cb_tutor_step(callback: CallbackQuery) -> None:
    raw = cb_data(callback).rsplit("_", 1)[-1]
    try:
        index = int(raw)
    except ValueError:
        await callback.answer("⚠️ Шаг не найден", show_alert=True)
        return
    text, kb = _tutor_page(index)
    await render_callback(callback, text, kb, force_answer=True)

