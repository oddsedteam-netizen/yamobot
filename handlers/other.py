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
«👤 Профиль → 🔗 Настройки ссылок».
Раздел доступен только владельцу платформы.
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


# ═══════════════ Работа со ссылками из «Прочее» ═══════════════
# Само приведение ссылок живёт в handlers/_common.py (normalize_link) —
# одна реализация и для редактора кнопок, и для настроек админ-панели.



def donate_link() -> str:
    """Ссылка на донат (задаётся владельцем платформы)."""
    return normalize_link(get_app_setting(SETTING_DONATE_URL))


def yamochan_link() -> str:
    """Ссылка проекта YamoChan (задаётся владельцем платформы)."""
    return normalize_link(get_app_setting(SETTING_YAMOCHAN_URL))


def test_bot_link() -> str:
    """Ссылка на тестового бота (задаётся владельцем платформы)."""
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
        [InlineKeyboardButton(text="📋 Логи", callback_data="logs_send", style="primary")],
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
    "Короткий путь от пустого чата до работающего бота: подключение, "
    "оформление, режимы и чаты.\n\n"
    "🎓 <b>8 шагов</b>, каждый — на одном экране. Можно просто читать, а можно "
    "сразу настраивать своего бота и запустить его прямо во время обучения.\n\n"
    "Всё происходит здесь же, в YamoBot — переходить никуда не нужно.\n\n"
    "Нажми «🎓 Начать обучение» 👇"
)


def tutorial_kb() -> InlineKeyboardMarkup:
    """Кнопки экрана обучения.

    Зелёная кнопка запускает урок прямо в этом чате — обучение полностью
    проходит внутри основного бота, никаких переходов в другие боты.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎓 Начать обучение", callback_data="tutor_start",
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
    "👋 <b>Поехали!</b>\n\n"
    "Обучение идёт прямо здесь, в YamoBot. Читай шаги и сразу настраивай "
    "своего бота — так он будет готов уже к концу обучения 🤖"
)

TUTOR_STEP_0 = (
    "🎓 <b>Что мы настроим</b>\n\n"
    "🤖 <b>Подключение</b> — создадим бота и отдадим его платформе.\n"
    "🗂 <b>Категория</b> — выберем, как бот будет работать с людьми.\n"
    "✏️ <b>Оформление</b> — приветствие и кнопки-ссылки.\n"
    "🛡 <b>Режимы</b> — защита, напоминания, время работы, нормы.\n"
    "💼 <b>Чаты</b> — привяжем чат работы и чат админов.\n\n"
    "На каждом шаге есть кнопка «▶️ Продолжить», а в конце — короткая шпаргалка.\n\n"
    "Готов? Нажимай 👇"
)

TUTOR_STEP_1 = (
    "1️⃣ <b>Подключение бота</b>\n\n"
    "🤖 Своего бота создаёт сам Telegram, а платформа просто управляет им.\n\n"
    "🔹 Открой чат с <b>@BotFather</b> — официальный бот для создания ботов.\n"
    "🔹 Отправь <code>/newbot</code>.\n"
    "🔹 BotFather попросит <b>имя</b> — его увидят люди, и <b>адрес</b> — "
    "короткое имя на латинице, обязательно с окончанием «bot». Придумай своё.\n"
    "🔹 Он пришлёт <b>токен</b> — длинный набор символов. Это ключ от бота: "
    "никому его не показывай и не пересылай.\n"
    "🔹 Вернись сюда: <b>«🤖 Боты» → «➕ Добавить бота»</b> и отправь токен.\n"
    "🔹 Бот появится в списке — им уже можно управлять.\n\n"
    "⚠️ Токен хранится только у нас. Если он попадёт к постороннему, бота можно "
    "перехватить — токен придётся перевыпустить."
)

TUTOR_STEP_2 = (
    "2️⃣ <b>Категория бота</b>\n\n"
    "От категории зависит, как бот работает с людьми в личке.\n\n"
    "💬 <b>Стандарт</b> — бот для общения с ПЗ. В панели бота есть кнопка "
    "<b>сменить админа</b>: человек может попросить другого администратора, "
    "если его админ не отвечает.\n\n"
    "📋 <b>Анкетница</b> — бот для приёма анкет. Кнопки смены админа у него нет: "
    "люди просто отправляют анкету, и она уходит админам.\n\n"
    "🤖 Ботов можно добавить сколько угодно: один — для общения, другой — "
    "для анкет. Категория выбирается один раз при добавлении бота."
)

TUTOR_STEP_3 = (
    "3️⃣ <b>Приветствие и кнопки</b>\n\n"
    "Приветствие человек видит первым, когда нажимает «Запустить». Кнопки под "
    "ним ведут дальше.\n\n"
    "✏️ Открой <b>«🤖 Боты»</b> → выбери бота → <b>«✏️ Редактор»</b>.\n"
    "💬 <b>«Изменить приветствие»</b> — пришли текст. Поддерживаются разметка, "
    "премиум-эмодзи, фото с подписью и готовые посты: оформление сохранится.\n"
    "🔗 <b>«Линки»</b> — кнопки-ссылки под приветствием: сначала название, "
    "потом сама ссылка. Цвет выберет бот: 🔵 синий, 🟢 зелёный, 🔴 красный или "
    "без цвета. Ненужную кнопку можно убрать.\n"
    "📌 Ботов много — есть общий редактор <b>«Выбрать все»</b>: он меняет "
    "приветствие и кнопки сразу у всех.\n\n"
    "💡 Хорошее приветствие сразу объясняет человеку, куда он попал и что делать "
    "дальше."
)

TUTOR_STEP_4 = (
    "4️⃣ <b>Режимы и защита</b>\n\n"
    "Всё это включается в карточке бота или в профиле и настраивается парой "
    "чисел.\n\n"
    "🛡 <b>Антиспам</b> — следит за личкой бота. <b>Авто</b> предупредит за "
    "спам стикерами и забанит, <b>Ручной</b> ограничит одно сообщение в минуту, "
    "<b>Выключен</b> снимет ограничения.\n\n"
    "🚨 <b>Антинакрутка</b> — если за короткое время приходит слишком много "
    "обращений, бот перестаёт создавать новые топики и зовёт тебя. Снимается "
    "вручную.\n\n"
    "🛡 <b>Антирейд</b> — для группового чата: при подозрении на рейд закрывает "
    "чат, удаляет зашедших и ссылки, зовёт владельца.\n\n"
    "⏰ <b>Напоминалка</b> — напоминает админам об обращениях без ответа. Есть "
    "авточек ответа админа, своя частота и «🕒 Настройка времени», чтобы не будить "
    "ночью.\n\n"
    "🕐 <b>Время работы</b> — в профиле. Если человек напишет боту не в рабочие "
    "часы, бот сам ответит, что сейчас отдыхает, а свободный админ напишет "
    "позже. Текст ответа редактируется, можно приложить фото и премиум-эмодзи.\n\n"
    "📊 <b>Норма админов</b> — сколько обращений в неделю считается нормой для "
    "админа.\n\n"
    "Включай по одному режиму и проверяй: так проще понять, что мешает."
)

TUTOR_STEP_5 = (
    "5️⃣ <b>Чаты</b>\n\n"
    "🛡 <b>Чат админов</b> — это общалка админов. Туда YamoBot присылает "
    "уведомления о новых ПЗ и статистику по тем, кто ждёт админа. Добавлять "
    "туда нужно <b>основного бота (YamoBot)</b>: он остаётся в чате, и ему "
    "нужны права администратора.\n\n"
    "💼 <b>Чат работы</b> — рабочая общалка, куда добавляются <b>дочерние "
    "боты</b>. Каждый дочерний бот работает в одном чате с темами: получив "
    "сообщение от человека, он создаёт отдельный топик и зовёт туда админов. "
    "Этот чат тоже привязывается к основному боту — YamoBot запомнит его, "
    "чтобы перезапускать привязку.\n\n"
    "🔹 Открой <b>«👤 Профиль» → «🔗 Привязать чаты»</b> и выбери нужный чат.\n"
    "🔹 Добавь туда <b>основного бота (YamoBot)</b> и дождись подтверждения — "
    "для каждого чата это делается отдельно.\n"
    "🔹 Рабочий чат: добавь в него <b>дочерних ботов</b> и выдай им права "
    "администратора — подключатся они сами.\n"
    "🔹 Если дочерний бот почему-то не подключился, открой тему <b>General</b> "
    "и напиши <code>/connect</code> — это запасной способ подключения.\n"
    "🔹 Бот привязался не к тому чату? В карточке бота есть "
    "<b>«🔗 Перепривязка»</b> — выручает, когда привязка сбилась.\n\n"
    "⚠️ Один дочерний бот — один рабочий чат. Если добавить его в другую "
    "группу, бот сам выйдет из неё и напишет тебе об этом."
)

TUTOR_STEP_6 = (
    "6️⃣ <b>Работа с обращениями</b>\n\n"
    "📋 <b>«📋 ПЗ»</b> в карточке бота — список всех, кто писал. Там же поиск по "
    "ID пользователя.\n"
    "✋ <b>«✋ Я беру»</b> в топике — админ берёт обращение себе, топик "
    "переименовывается на его тег.\n"
    "🔄 <b>«Сменить админа»</b> — если ответа нет, пользователь может попросить "
    "другого. Число смен в сутки ограничивается в <b>«🔄 Смена админа»</b>.\n"
    "✏️ Если ПЗ ошибся, он может дописать админу в топик сам: сообщения не "
    "редактируются и не удаляются, вся переписка остаётся в топике.\n"
    "🏷 <b>«🏷 Уточнение категории»</b> — бот спросит у пользователя, о чём "
    "обращение, и передаст это админам. К стандартным категориям можно добавить "
    "до трёх своих, выключать и удалять их.\n"
    "📊 <b>«📊 Статистика»</b> — обращения, ответы, активность и сломанные "
    "токены.\n"
    "📨 <b>«📨 Рассылка»</b> — сообщение всем, кто писал боту. Перед отправкой "
    "показывается превью и количество получателей, а в конце — отчёт, сколько "
    "доставлено.\n"
    "👥 <b>«👥 Админы»</b> — добавь команду, раздай теги, смотри статистику. "
    "Новичка, который зашёл в чат админов, бот сам спросит, добавлять ли его."
)

TUTOR_STEP_7 = (
    "✅ <b>Готово!</b>\n\n"
    "Короткая шпаргалка, чтобы ничего не искать:\n\n"
    "🤖 <b>Боты</b> — добавить, остановить, настроить, удалить, перепривязать.\n"
    "📋 <b>ПЗ</b> — обращения, поиск, ПЗ без админов.\n"
    "👥 <b>Админы</b> — команда, теги, статистика.\n"
    "👤 <b>Профиль</b> — привязка чатов, время работы, напоминалка, защита.\n"
    "📢 <b>Мой ТГК</b> — посты и отложенные публикации.\n"
    "✨ <b>Прочее</b> — обучение, FAQ, поддержка проекта.\n\n"
    "Что-то забыл — открой <b>«❓ FAQ»</b> или запусти обучение заново командой "
    "<code>/tutor</code>.\n\n"
    "Удачной работы! 🤍"
)

TUTOR_STEPS: list[str] = [
    TUTOR_STEP_0,
    TUTOR_STEP_1,
    TUTOR_STEP_2,
    TUTOR_STEP_3,
    TUTOR_STEP_4,
    TUTOR_STEP_5,
    TUTOR_STEP_6,
    TUTOR_STEP_7,
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

