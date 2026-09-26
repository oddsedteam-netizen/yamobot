import logging
import time

from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from handlers._common import (render_callback, cb_uid, msg_uid,
                              msg_username, msg_firstname, try_edit,
                              try_edit_answer)
from services.child_manager import ChildManager
from services.config import is_super_admin
from services.constants import BOT_VERSION
from services.storage import (
    get_admin_invite,
    consume_admin_invite,
    add_admin,
    get_admin_by_user_id,
    register_user,
    is_registry_user_banned,
    get_user_bots,
    get_all_topics_for_bot,
    bot_display_name,
    get_owner_by_admin_chat,
    delete_topic_record,
)

router = Router()

_logger = logging.getLogger(__name__)

# Анти-спам /start: повторные /start одного пользователя в течение интервала
# (секунды) игнорируются, чтобы наплыв команд не ронял бота.
MAIN_START_MIN_INTERVAL = 3.0
_LAST_START: dict[int, float] = {}


class StartFSM(StatesGroup):
    waiting_admin_tag = State()


class StatsFSM(StatesGroup):
    # Ожидание номера топика при удалении из списка ПЗ.
    waiting_pz_delete = State()


WELCOME_TEXT = (
    "👋 <b>Добро пожаловать в YamoBot!</b>\n\n"
    "Это бот-менеджер. Через него ты сможешь подключать "
    "и управлять другими Telegram-ботами.\n\n"
    "Выбери действие кнопками ниже:"
)


def _welcome_text_with_version() -> str:
    return f"{WELCOME_TEXT}\n\n⚙️ Версия бота: <b>{BOT_VERSION}</b>"


def main_menu_kb() -> ReplyKeyboardMarkup:
    """Главное меню — reply-клавиатура.

    Кнопки покрашены (Bot API: danger/primary/success):
    «Боты» — зелёная (success)
    «ПЗ» и «Админы» — синие (primary)
    «Жалоба» и «FAQ» — красные (danger)
    «Профиль» и прочее — без цвета (style=None)
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🤖 Боты", style="success")],
            [
                KeyboardButton(text="📋 ПЗ", style="primary"),
                KeyboardButton(text="👥 Админы", style="primary"),
            ],
            [
                KeyboardButton(text="🎫 Тикеты", style="danger"),
                KeyboardButton(text="❓ FAQ", style="danger"),
            ],
            [KeyboardButton(text="👤 Профиль")],
            # «Мой ТГК» живёт в «Прочее» — в главном меню лишняя кнопка
            [KeyboardButton(text="✨ Прочее")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


async def _show_main(message: Message) -> None:
    await message.answer(_welcome_text_with_version(), reply_markup=main_menu_kb())
    # Инлайн-кнопки под приветствием при каждом новом запуске (/start, /menu):
    # FAQ и обучение (обучение проходит внутри этого же бота).
    await message.answer(
        "❓ <b>Есть вопросы?</b> Загляни в FAQ — там ответы на частые вопросы.\n\n"
        "📚 А ещё можно пройти <b>обучение</b> — простым языком про весь путь "
        "настройки бота.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❓ FAQ", callback_data="faq", style="success")],
            [InlineKeyboardButton(text="📚 Обучение", callback_data="other_tutorial",
                                  style="primary")],
        ]),
    )


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, state: FSMContext,
                    child_manager: ChildManager) -> None:
    await state.clear()
    user_id = msg_uid(message)
    register_user(user_id, msg_username(message) or "", msg_firstname(message) or "")
    if is_registry_user_banned(user_id) and not is_super_admin(user_id):
        await message.answer("🚫 Вы заблокированы администрацией.")
        return

    # Анти-спам /start: повторные команды одного пользователя в течение интервала
    # игнорируются, чтобы бот не падал от наплыва /start (например, при долгом
    # нажатии на кнопку Start или автокликерах).
    _uid = msg_uid(message)
    _now = time.monotonic()
    if _now - _LAST_START.get(_uid, 0.0) < MAIN_START_MIN_INTERVAL:
        return
    _LAST_START[_uid] = _now

    if message.text and "addadmin_" in message.text:
        token = message.text.split("addadmin_", 1)[1].strip().split()[0]
        invite = get_admin_invite(token)
        if invite is None:
            await message.answer("❌ Ссылка-приглашение недействительна.")
            return
        owner_id = invite["owner_id"]
        remaining = invite["max_uses"] - invite["used"]

        # Админ уже зарегистрирован в этом боте — повторная регистрация не нужна.
        existing = get_admin_by_user_id(owner_id, user_id)
        if existing:
            await message.answer(
                "⚠️ <b>Ты уже зарегистрирован в этом боте.</b>\n\n"
                f"🏷 Твой тег: <b>#{existing['tag']}</b>\n"
                "Повторно регистрироваться не нужно."
            )
            return

        await state.set_state(StartFSM.waiting_admin_tag)
        await state.update_data(admin_owner_id=owner_id, admin_token=token)
        if remaining > 1:
            await message.answer(
                "🎉 <b>Вас пригласили стать админом!</b>\n\n"
                f"Осталось мест по этой ссылке: <b>{remaining}</b>\n\n"
                "Отправь свой <b>тег</b>."
            )
        else:
            await message.answer(
                "🎉 <b>Вас пригласили стать админом!</b>\n\n"
                "Отправь свой <b>тег</b>."
            )
        return
    if message.text and "transfer_" in message.text:
        from handlers.profile import handle_transfer_link
        token = message.text.split("transfer_", 1)[1].strip().split()[0]
        await handle_transfer_link(message, token)
        return
    if message.text and "config_" in message.text:
        token = message.text.split("config_", 1)[1].strip().split()[0]
        await _apply_config_link(message, token, child_manager)
        return
    await _show_main(message)


async def _apply_config_link(message: Message, token: str,
                             child_manager: ChildManager) -> None:
    """Применяет конфиг по ссылке вида ``?start=config_<код>``."""
    from handlers.configs import apply_config_by_code
    from services.storage import get_bot_config

    config = get_bot_config(token)
    if not config:
        await message.answer(
            "❌ <b>Конфиг не найден.</b>\n\n"
            "Проверь ссылку или код конфига — возможно, он был очищен."
        )
        return

    user_id = msg_uid(message)
    await message.answer(
        "⚙️ <b>Применяю данные конфига…</b>\n\n"
        f"🔑 Код: <code>{config['code']}</code>"
    )
    ok, text = await apply_config_by_code(
        user_id, int(config.get("bot_id") or 0), str(config["code"]), child_manager
    )
    await message.answer(text)


@router.message(StartFSM.waiting_admin_tag, F.chat.type == ChatType.PRIVATE)
async def fsm_waiting_admin_tag(message: Message, state: FSMContext) -> None:
    tag = (message.text or "").strip().lstrip("#")
    if not tag:
        await message.answer("❌ Тег не может быть пустым.")
        return
    data = await state.get_data()
    owner_id = int(data.get("admin_owner_id") or 0)
    token = data.get("admin_token") or ""
    user_id = msg_uid(message)
    username = msg_username(message) or ""
    await state.clear()

    already = get_admin_by_user_id(owner_id, user_id)
    if already:
        await message.answer(f"⚠️ Ты уже админ с тегом #{already['tag']}.")
        return

    ok = add_admin(owner_id, user_id, username, tag)
    if not ok:
        await message.answer("⚠️ Не удалось добавить тебя как админа.")
        return

    # «Слот» приглашения считается использованным только при успешном вступлении.
    remaining = consume_admin_invite(token)
    if remaining is None:
        await message.answer(f"✅ <b>Ты стал админом!</b>\n\n🏷 Тег: <b>#{tag}</b>")
    elif remaining == 0:
        await message.answer(
            f"✅ <b>Ты стал админом!</b>\n\n🏷 Тег: <b>#{tag}</b>\n\n"
            "Это был последний слот — ссылка-приглашение больше не действует."
        )
    else:
        await message.answer(
            f"✅ <b>Ты стал админом!</b>\n\n🏷 Тег: <b>#{tag}</b>\n\n"
            f"👥 Осталось мест по ссылке: <b>{remaining}</b>"
        )


# ═══════════════ Reply-кнопки главного меню ═══════════════

@router.message(F.text == "🤖 Боты")
async def on_bots_button(message: Message, state: FSMContext,
                         child_manager: ChildManager) -> None:
    await state.clear()
    from handlers.my_bots import show_my_bots
    await show_my_bots(message, child_manager)


@router.message(F.text == "👤 Профиль")
async def on_profile_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    from handlers.profile import show_profile
    await show_profile(message)


@router.message(F.text == "📢 Мой ТГК", F.chat.type == ChatType.PRIVATE)
async def on_tgk_button(message: Message, state: FSMContext) -> None:
    """Reply-кнопка «📢 Мой ТГК»: привязка канала и посты от лица канала.

    Всё, что раньше лежало в «🧩 Эксп функции», теперь здесь. Если ТГК ещё не
    привязан — бот честно пишет об этом и просит привязать канал.
    """
    await state.clear()
    from handlers.channels import show_tgk_entry
    await show_tgk_entry(message)


@router.message(F.text == "👥 Админы")
async def on_admins_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    from handlers.admins import show_admins
    await show_admins(message)


@router.message(F.text == "📋 ПЗ")
async def on_pz_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    from handlers.overview import show_global_pz
    await show_global_pz(message)


@router.message(F.text == "🎫 Тикеты")
async def on_tickets_button(message: Message, state: FSMContext) -> None:
    """Кнопка «🎫 Тикеты» в главном меню."""
    from handlers.tickets import tickets_menu_kb

    await state.clear()
    from services.storage import get_user_tickets

    user_id = message.from_user.id if message.from_user else 0
    await message.answer(
        "🎫 <b>Тикеты</b>\n\n"
        "Обращения в поддержку: можно создать тикет и посмотреть свои "
        "открытые и закрытые обращения.\n\n"
        f"🟢 Открытых: <b>{len(get_user_tickets(user_id, 'open'))}</b>\n"
        f"⚪ Закрытых: <b>{len(get_user_tickets(user_id, 'closed'))}</b>",
        reply_markup=tickets_menu_kb(),
    )


@router.message(F.text == "❓ FAQ")
async def on_faq_button(message: Message) -> None:
    await message.answer(FAQ_TEXT, reply_markup=faq_menu_kb())


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if callback.message is None:
        return

    # Возвращаемся в главное меню: убираем старый inline-экран и выводим
    # меню ОДНИМ сообщением (без дублирующего текста «Главное меню»).
    delete = getattr(callback.message, "delete", None)
    if delete is not None:
        try:
            await delete()
        except Exception:
            try:
                await try_edit(callback.message, "🏠 <b>Главное меню</b>", reply_markup=None)
            except Exception:
                pass

    try:
        await callback.message.answer(
            "🏠 <b>Главное меню</b>\n\nВыбери действие кнопками ниже 👇",
            reply_markup=main_menu_kb(),
        )
    except Exception:
        pass


@router.message(Command("menu"), F.chat.type == ChatType.PRIVATE)
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_main(message)


@router.message(Command("adm"), F.chat.type == ChatType.PRIVATE)
async def cmd_adm(message: Message) -> None:
    """Открывает админ-панель только для супер-админа (переменная ADMIN)."""
    user_id = msg_uid(message)
    if not is_super_admin(user_id):
        await message.answer("⛔ Доступ запрещён.")
        return
    from handlers.profile import admin_kb
    await message.answer("🛡 <b>Админ-панель</b>\n\nВыбери раздел:", reply_markup=admin_kb())


@router.message(Command("status"), F.chat.type == ChatType.PRIVATE)
async def cmd_status(message: Message, child_manager: ChildManager) -> None:
    """Диагностика: состояние всех дочерних ботов (только для супер-админа)."""
    user_id = msg_uid(message)
    if not is_super_admin(user_id):
        await message.answer("⛔ Доступ запрещён.")
        return

    from services.storage import (
        get_all_bots_flat,
        get_feedback_chat,
        get_admins_all,
        get_child_users,
    )

    all_bots = get_all_bots_flat()
    if not all_bots:
        await message.answer("📭 Боты не подключены.")
        return

    lines = ["📊 <b>Состояние ботов</b>\n"]
    for b in all_bots:
        bot_id = b["id"]
        running = child_manager.is_running(bot_id)
        status = "🟢 работает" if running else "🔴 не работает"
        fchat = get_feedback_chat(bot_id)
        admins = len(get_admins_all(b["owner_id"]))
        users = len(get_child_users(bot_id, only_active=False))
        chat_info = "✅ подключён" if fchat else "❌ не подключён"
        lines.append(
            f"🤖 @{b.get('username') or b['id']} — {status}\n"
            f"   Тип: {b.get('bot_type') or 'standard'} | чат: {chat_info} | "
            f"админов: {admins} | юзеров: {users}"
        )

    await message.answer("\n".join(lines))


FAQ_TEXT = (
    "❓ <b>Разделы справки</b>\n\n"
    "Выбери интересующий раздел — бот пришлёт подробную инструкцию:\n\n"
    "👥 <b>Админы</b> — как добавлять админов (вручную и по ссылке), "
    "менять тег, удалять, а также про уведомления и карточки админа.\n"
    "🤖 <b>Боты</b> — как подключать ботов, чем стандарт отличается от "
    "анкетницы, как настраивать приветствие, антиспам и связывать бота с чатом.\n"
    "👤 <b>Профиль</b> — привязка бота к «чату админов» и «чату работы», "
    "передача прав, антинакрутка ПЗ и раздел <b>«📊 Норма»</b> (недельная "
    "норма админов и уведомления о тех, кто её не набрал).\n"
    "⌨️ <b>Команды</b> — все команды для дочерних ботов и «чата админов».\n"
    "✨ <b>Прочее</b> — проект YamoChan, поддержка команды, тестовый бот, "
    "логи и обучение (кнопка «✨ Прочее» в главном меню).\n"
    "📢 <b>Мой ТГК</b> — привязка своего канала и публикация постов от его "
    "лица (кнопка «📢 Мой ТГК» в разделе «Прочее»).\n"
    "🎫 <b>Тикеты</b> — обращения в поддержку: можно создать тикет и следить "
    "за его статусом (кнопка «🎫 Тикеты» в главном меню)."
)


def faq_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Админы", callback_data="faq_admins", style="primary"),
            InlineKeyboardButton(text="🤖 Боты", callback_data="faq_bots", style="primary"),
        ],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="faq_profile", style="primary"),
            InlineKeyboardButton(text="⌨️ Команды", callback_data="faq_commands", style="primary"),
        ],
        [InlineKeyboardButton(text="⚙️ Основные настройки бота", callback_data="faq_settings", style="primary")],
        [InlineKeyboardButton(text="📊 Норма админов", callback_data="faq_norm", style="primary")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ])


def _faq_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К разделам", callback_data="faq", style="success")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
        [InlineKeyboardButton(text="📚 Обучение", callback_data="other_tutorial", style="primary")],
    ])


@router.callback_query(F.data == "faq")
async def cb_faq(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_TEXT, faq_menu_kb(), force_answer=True)

FAQ_ADMINS = (
    "👥 <b>Админы</b>\n\n"
    "Админ — это человек, который отвечает на «обращения» (ПЗ) твоих ботов. "
    "Админы привязаны ко всем твоим ботам сразу, поэтому одним набором они "
    "видят ПЗ из всех ботов.\n\n"
    "—— <b>Как добавить админа вручную</b> ——\n"
    "1. Открой меню <b>«👥 Админы»</b>.\n"
    "2. Нажми <b>«➕ Добавить админа»</b>.\n"
    "3. Отправь <b>ID</b> или <b>@username</b> пользователя Telegram.\n"
    "4. Дальше бот попросит ввести <b>тег</b> — короткое слово, которое нужно "
    "писать с решёткой: <code>#тег</code>. По нему тебя видно в списках.\n"
    "5. Готово — админ добавлен и сразу доступен для взятия ПЗ.\n\n"
    "—— <b>Как добавить админа по ссылке</b> ——\n"
    "Удобно, когда админов много и ты хочешь, чтобы они сами зарегистрировались:\n"
    "1. В меню админов нажми <b>«🔗 Добавить админа ссылкой»</b>.\n"
    "2. Укажи, на сколько человек рассчитана ссылка (от 1 до 10).\n"
    "3. Бот пришлёт ссылку вида <code>t.me/...?start=addadmin_...</code>.\n"
    "4. Передай её человеку. Он откроет YamoBot, введёт свой тег — и станет админом.\n"
    "5. Каждый вступивший вычитает один «слот». Когда слоты закончатся, "
    "ссылка перестанет работать.\n\n"
    "—— <b>Как поменять тег или удалить админа</b> ——\n"
    "• Открой <b>«Админы» → «📋 Список админов»</b>, нажми на админа — "
    "откроется его карточка.\n"
    "• <b>«✏️ Изменить тег»</b> — задай новый тег, история старых тегов сохранится "
    "в карточке.\n"
    "• <b>«🗑 Удалить админа»</b> — подтверди удаление. Админ потеряет доступ, "
    "но его взятые ПЗ останутся.\n\n"
    "—— <b>Уведомление при удалении админа</b> ——\n"
    "Когда ты удаляешь админа из бота, YamoBot может сам оповестить всех, "
    "у кого он был админом: бот спросит, разослать ли по его ПЗ сообщение "
    "об уходе. Если подтвердишь, пользователям придёт уведомление "
    "«Ваш администратор покинул бота» с кнопкой <b>«🔍 Найти админа»</b> — "
    "они сами подберут нового админа.\n\n"
    "—— <b>Карточка админа</b> ——\n"
    "Это подробная информация об админе:\n"
    "• 📛 username и 🆔 ID;\n"
    "• 🏷 текущий тег и 📅 дата добавления;\n"
    "• 📊 статистика сообщений (за день / неделю / месяц / всего);\n"
    "• 📋 сколько ПЗ сейчас закреплено за админом;\n"
    "• 🏷 история смены тегов.\n"
    "Из карточки можно сразу редактировать тег или удалить админа."
)


FAQ_BOTS = (
    "🤖 <b>Боты</b>\n\n"
    "Через меню <b>«🤖 Боты»</b> ты управляешь всеми подключёнными ботами: "
    "добавляешь, настраиваешь и запускаешь.\n\n"
    "—— <b>Как добавить бота</b> ——\n"
    "1. В меню «Боты» нажми <b>«➕ Добавить бота»</b>.\n"
    "2. Создай бота у <b>@BotFather</b> и отправь сюда его <b>токен</b> "
    "(набор букв и цифр с двоеточием).\n"
    "3. Выбери <b>тип бота</b> (см. ниже).\n"
    "4. Бот подключится и появится в списке.\n\n"
    "—— <b>Стандарт или анкетница — в чём разница</b> ——\n"
    "Тип выбирается при добавлении:\n"
    "• <b>🗂 Стандарт</b> — обычный бот для ПЗ. Под приветствием появляется "
    "нижняя панель с кнопкой «сменить админа», чтобы пользователь мог поменять "
    "своего админа в один клик. Через этого бота нужно общаться с ПЗ.\n"
    "• <b>📝 Анкетница</b> — бот-анкета. Отличие в том, что у него нет панели "
    "с кнопкой «сменить админа» — только приветствие и кнопки-ссылки. "
    "В кнопки-ссылки можно вставить ссылки на статью (например, на "
    "Telegra.ph) и всё красиво оформить.\n\n"
    "—— <b>Как отредактировать приветствие и добавить кнопки</b> ——\n"
    "1. Открой бота в списке и нажми <b>«✏️ Редактор»</b>.\n"
    "2. <b>«💬 Изменить приветствие»</b> — отправь новый текст (поддерживаются "
    "HTML и премиум-эмодзи).\n"
    "Можно отправить и <b>фото</b> с подписью, и готовую <b>статью</b> "
    "(пост с форматированием) — приветствие сохранит её оформление.\n"
    "3. <b>«🔗 Линки»</b> — добавь кнопки-ссылки к приветствию: название кнопки "
    "и её ссылку. Ссылку можно отправлять в любом виде — <code>@username</code>, "
    "<code>t.me/канал</code> или полную <code>https://…</code>: бот сам приведёт "
    "её к нужному формату. Кнопки появятся прямо под приветствием, "
    "их можно удалять по одной. При добавлении линка бот спросит, "
    "в какой цвет покрасить кнопку: 🔵 синий, 🟢 зелёный или 🔴 красный.\n"
    "4. Для всех ботов сразу есть отдельный редактор в «📌 Выбрать все».\n\n"
    "—— <b>Смена админа</b> ——\n"
    "Пользователь может написать боту «сменить админа» — и ему покажут, сколько "
    "смен у него осталось. В боте есть кнопка <b>«🔄 Смена админа»</b>: "
    "она объясняет ограничение и позволяет его включить/выключить и задать "
    "число смен в сутки (по умолчанию <b>3</b>). Когда лимит исчерпан, ПЗ "
    "получит отказ, а счётчик обновится через сутки.\n\n"
    "—— <b>Свои категории</b> ——\n"
    "В <b>«🏷 Уточнение категории»</b> можно добавить до <b>3 своих "
    "категорий</b> к стандартным, выключать ненужные и удалять. Кнопки у ПЗ "
    "расставляются компактно по 2–3 в ряд, так что сообщение не растягивается.\n\n"
    "—— <b>Правка и удаление сообщений</b> ——\n"
    "И ПЗ, и админ могут править и удалять сообщения: в топике админ пишет "
    "<code>/ред новый текст</code> или <code>/уд</code> (если ответил на "
    "сообщение — изменится именно оно), а ПЗ делает то же самое в личке бота.\n\n"
    "—— <b>Как работает антиспам</b> ——\n"
    "Открой бота и нажми <b>«🛡 Антиспам»</b>. Три режима:\n"
    "• <b>Авто</b> — за 5 стикеров подряд бот выдаёт предупреждение, за следующие "
    "5 — банит пользователя и закрывает его топик с пометкой «🚫 бан спам».\n"
    "• <b>Ручной</b> — не больше 1 сообщения в минуту в личках.\n"
    "• <b>Выключен</b> — без ограничений.\n"
    "Режим применяется сразу, бот перезапускается автоматически.\n\n"
    "—— <b>Анонимный режим</b> ——\n"
    "Кнопка <b>«🕶 Аноним»</b> в карточке бота. Когда включён:\n"
    "• пользователи скрыты из списков и поиска ПЗ;\n"
    "• вместо имени юзера в топиках пишется «Новое сообщение 🕶»;\n"
    "• списки и пагинация ПЗ бота скрыты.\n\n"
    "—— <b>🕐 Время работы</b> ——\n"
    "Кнопка в профиле. Если ПЗ напишет боту <b>вне рабочего времени</b>, "
    "бот сам ответит ему, что сейчас не работает, и предупредит, что "
    "свободный админ обязательно напишет позже (обращение при этом всё равно "
    "уходит в топик).\n"
    "По умолчанию — с <b>09:00</b> до <b>21:00</b>. Можно выключить, изменить "
    "время (в том числе ночную смену через полночь) и переписать текст ответа — "
    "вместе с фото и премиум-эмодзи.\n\n"
    "—— <b>Как выбрать бота</b> ——\n"
    "Нажми на бота в меню «Боты» — откроется карточка со статусом (🟢 работает / "
    "🔴 остановлен), приветствием, анонимным режимом и действиями: рассылка, "
    "антиспам, статистика, редактор, ПЗ, запуск/остановка и удаление. Если ботов "
    "несколько — выбирай по категориям или используй «📌 Выбрать все».\n\n"
    "—— <b>Как привязать созданного бота к чату</b> ——\n"
    "Бот работает исключительно в чатах с темами (топиками). Добавь бота "
    "в рабочий чат с включёнными темами и выдай ему права администратора — "
    "бот <b>подключится сам</b> и начнёт создавать топики с ПЗ. Если по каким-то "
    "причинам он не подключился, напиши <code>/connect</code> в теме "
    "<b>General</b>.\n\n"
    "⚠️ <b>Один бот — один чат.</b> Если бота добавят в другой чат, он "
    "сообщит, что уже привязан, выйдет из нового чата, а тебе придёт "
    "уведомление «вашего бота пытались добавить в чужой чат».\n\n"
    "—— <b>⚙️ Конфиг бота</b> ——\n"
    "Кнопка <b>«⚙️ Конфиг»</b> в карточке бота:\n"
    "• <b>«💾 Сохранить конфиг»</b> — бот запомнит текущие настройки "
    "(приветствие, кнопки-ссылки, антиспам, анонимность, уточнение "
    "категории, смену админа, время работы и норму админов) и выдаст код "
    "и ссылку для переноса;\n"
    "• <b>«📥 Внести конфиг»</b> — бот спросит, какие настройки перенести, "
    "и применит только выбранные;\n"
    "• категория бота конфигом <b>не переносится</b> — её выбирают при "
    "добавлении бота;\n"
    "Все свои коды и ссылки собраны в <b>«👤 Профиль» → "
    "«📂 Мои ссылки и конфиги»</b>.\n\n"
    "Продолжение — команды и привязка чатов в разделах «Профиль» и «Команды»."
)


@router.callback_query(F.data == "faq_admins")
async def cb_faq_admins(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_ADMINS, _faq_back_kb(), force_answer=True)


@router.callback_query(F.data == "faq_bots")
async def cb_faq_bots(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_BOTS, _faq_back_kb(), force_answer=True)


FAQ_SETTINGS = (
    "⚙️ <b>Основные настройки бота</b>\n\n"
    "Здесь собрано главное: как редактировать бота и как подключить его "
    "к рабочему чату.\n\n"
    "—— <b>Как отредактировать бота</b> ——\n"
    "1. В меню <b>«🤖 Боты»</b> нажми на нужного бота — откроется его карточка "
    "со статусом, приветствием и списком действий.\n"
    "2. Нажми <b>«✏️ Редактор»</b>.\n"
    "3. <b>«💬 Изменить приветствие»</b> — отправь новый текст приветствия. "
    "Поддерживаются HTML-разметка и премиум-эмодзи.\n"
    "4. <b>«🔗 Линки»</b> — добавь кнопки-ссылки к приветствию: укажи название "
    "кнопки и её ссылку (можно коротко — <code>@username</code> или "
    "<code>t.me/канал</code>, бот сам приведёт её к нужному виду). Кнопки "
    "появятся прямо под приветствием у всех новых пользователей, их можно "
    "удалять по одной.\n"
    "5. Изменения применяются сразу — бот автоматически перезапускается "
    "и подхватывает новое приветствие и кнопки.\n"
    "6. В карточке бота также доступны: <b>«🛡 Антиспам»</b>, "
    "<b>«🕶 Аноним»</b>, <b>«📊 Статистика»</b>, <b>«📨 Рассылка»</b>, "
    "<b>«📋 ПЗ»</b>, <b>«⚙️ Конфиг»</b> и <b>«🔗 Перепривязка»</b> "
    "(последняя отвязывает бота от старого рабочего чата, если чат сменился).\n\n"
    "—— <b>Как сделать рассылку</b> ——\n"
    "В карточке бота нажми <b>«📨 Рассылка»</b>:\n"
    "1. Пришли сообщение — текст, фото, видео, GIF, документ или стикер "
    "(поддерживаются премиум-эмодзи);\n"
    "2. Если нужны кнопки со ссылками под сообщением — скажи «да»: "
    "бот спросит название, ссылку и цвет для каждой (до 3 штук);\n"
    "3. Проверь превью и нажми <b>«✅ Отправить»</b>.\n"
    "После отправки бот покажет, сколько сообщений доставлено, сколько нет, "
    "за сколько времени прошла рассылка и по какой причине не дошло.\n\n"
    "Чтобы отредактировать несколько ботов сразу, открой <b>«📌 Выбрать все»</b>: "
    "там общий редактор, общая рассылка и статистика по всем ботам.\n\n"
    "—— <b>Как подключить бота к рабочему чату</b> ——\n"
    "Дочерний бот работает только в чатах с темами (топиками). Чтобы он начал "
    "принимать обращения:\n"
    "1. Создай или выбери групповой чат и включи в нём темы.\n"
    "2. Добавь дочернего бота в этот чат и выдай ему <b>права администратора</b> "
    "— без этого он не сможет создавать и переименовывать топики.\n"
    "3. Всё — бот <b>подключится сам</b>! (Если не подключился, напиши "
    "<code>/connect</code> в теме <b>General</b>.)\n"
    "4. Готово: теперь все, кто напишет боту в личку, будут создавать топики "
    "в этом чате, а админы смогут отвечать в них.\n\n"
    "Если бот завис или перестал отвечать — открой <b>«👤 Профиль»</b> "
    "и нажми <b>«🔄 Полный перезапуск»</b>: это перезапустит всех твоих "
    "дочерних ботов без удаления и перепривязки."
)


@router.callback_query(F.data == "faq_settings")
async def cb_faq_settings(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_SETTINGS, _faq_back_kb(), force_answer=True)


FAQ_NORM = (
    "📊 <b>Норма админов</b>\n\n"
    "Раздел <b>«📊 Норма»</b> в профиле показывает, кто из админов сколько "
    "наработал за период, и помогает не потерять тех, кто отстаёт.\n\n"
    "—— <b>Что настраивается</b> ——\n"
    "1. <b>«✏️ Норма в неделю»</b> — сколько сообщений админ должен набрать "
    "за период (например, <code>500</code>). Ноль выключает норму.\n"
    "2. <b>«📅 Первый и последний день подсчёта»</b> — с какого по какой день "
    "недели считается норма. По умолчанию с понедельника по пятницу. Можно "
    "написать коротко: <code>пн-пт</code>, <code>с понедельника по пятницу</code> "
    "или <code>1-5</code>.\n"
    "3. <b>«🔔 Уведомления»</b> — включает и выключает оповещение о тех, "
    "кто не набрал норму.\n\n"
    "—— <b>Что показывает экран</b> ——\n"
    "• 📊 саму норму и период подсчёта;\n"
    "• ✅ сколько админов норму набрали и ❌ сколько не набрали;\n"
    "• 🏆 рейтинг всех админов: место, тег, сколько сообщений за период "
    "и общая статистика (день / неделя / месяц / всего).\n\n"
    "—— <b>Уведомление в «чат админов»</b> ——\n"
    "Когда период заканчивается (в последний день вечером), бот присылает "
    "в «чат админов» список админов, которые не набрали норму, и кнопку "
    "<b>«📋 ПЗ без админа»</b> — по ней открывается список обращений, "
    "которые ещё никто не взял.\n"
    "Уведомление приходит один раз за период — спамить не будет. "
    "Выключить его можно кнопкой <b>«🔔 Уведомления»</b> в разделе «📊 Норма»."
)


@router.callback_query(F.data == "faq_norm")
async def cb_faq_norm(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_NORM, _faq_back_kb(), force_answer=True)


FAQ_PROFILE = (
    "👤 <b>Профиль</b>\n\n"
    "Профиль — это твоя карточка: имя, ID, количество ботов, админов и обращений. "
    "Отсюда же настраиваются привязанные чаты и передача прав.\n\n"
    "—— <b>Как привязать основной бот к «чату админов»</b> ——\n"
    "«Чат админов» — это групповой чат, куда приходят уведомления о новых ПЗ "
    "и где работают команды <code>/стата</code>, <code>/perezap</code> и другие.\n"
    "1. В профиле нажми <b>«🔗 Привязать чаты»</b>.\n"
    "2. Нажми <b>«🛡 Привязать чат админов»</b>.\n"
    "3. Добавь основного бота (YamoBot) в групповой чат, который хочешь "
    "использовать как «чат админов», и дождись подтверждения привязки.\n"
    "4. Выдай боту <b>права администратора</b> в этом чате — иначе уведомления "
    "могут не доходить.\n"
    "5. Готово: бот останется в чате, и сюда будут приходить уведомления о новых ПЗ.\n\n"
    "—— <b>Как привязать «чат работы»</b> ——\n"
    "«Чат работы» — рабочая площадка для ведения дел. Привязка похожая:\n"
    "1. В профиле нажми <b>«🔗 Привязать чаты»</b>, затем <b>«💼 Привязать чат работы»</b>.\n"
    "2. Добавь основного бота в групповой чат и дождись подтверждения.\n"
    "3. В отличие от «чата админов», бот <b>запомнит ID и покинет чат</b> — "
    "он не будет там находиться.\n"
    "После привязки кнопка в профиле меняется на <b>«📎 Чаты»</b>: там можно "
    "посмотреть привязанные чаты, отвязать их или перезапустить привязку.\n\n"
    "—— <b>Как работает передача прав</b> ——\n"
    "Кнопка <b>«👑 Передать права»</b> в профиле. Можно передать:\n"
    "• <b>👑 Все права</b> — полностью передать боты, админов, совладельцев, "
    "привязанные чаты и настройки предупреждений другому владельцу.\n"
    "• <b>🤖 Только одного бота</b> — передать лишь выбранного бота.\n"
    "1. Нажми «👑 Передать права» и выбери вариант — ID получателя\n"
    "вводить не нужно: бот сразу выдаст <b>ссылку-приглашение</b>.\n"
    "2. Отправь ссылку тому, кому передаёшь права. Получатель переходит по\n"
    "ней и нажимает <b>«✅ Принять»</b> — только после этого права переходят.\n"
    "3. После передачи нового владельца автоматически добавляют админом "
    "с его тегом, чтобы ПЗ корректно привязывались.\n"
    "Внимание: при полной передаче <b>все</b> данные (боты, чаты, админы) "
    "переходят новому владельцу.\n\n"
    "—— <b>🚨 Антинакрутка</b> ——\n"
    "Кнопка <b>«🚨 Антинакрутка»</b> в профиле — защита статистики и чата от "
    "наплыва фейковых ПЗ. Сверху есть переключатель "
    "<b>«🟢 Включить» / «🔴 Выключить»</b>: когда защита выключена, бот просто "
    "не следит за наплывом и никаких уведомлений не присылает.\n"
    "1. Задай, сколько ПЗ за сколько минут считать подозрительным наплывом.\n"
    "2. Когда порог превышен, бот сохраняет статистику и спрашивает: "
    "<b>засчитывать ли наплыв</b> («✅ Сохранить» — это реальные обращения, "
    "«🚫 Это накрутка» — статистика откатится к моменту срабатывания).\n"
    "3. Сразу после ответа бот спрашивает: <b>«Снимаю защиту?»</b>\n"
    "   • <b>Да</b> — топики ПЗ и уведомления снова работают;\n"
    "   • <b>Нет</b> — защита остаётся.\n"
    "4. Пока защита активна, бот <b>не создаёт новые ПЗ</b> и <b>не присылает "
    "уведомления</b> в «чат админов», а пользователям отвечает, что бот "
    "находится в режиме защиты от спама и сообщения временно не доходят.\n"
    "5. Снять защиту в любой момент: <b>Профиль → 🛡 Защита → "
    "🚨 Антинакрутка → 🔄 Сбросить защиту</b> — уведомления и создание "
    "топиков возобновятся.\n\n"
    "—— <b>⏰ Напоминалка: настройка времени</b> ——\n"
    "В <b>«⏰ Напоминалка» → «🕒 Настройка времени»</b> укажи, с какого по "
    "какое время напоминания <b>не приходят</b> в «чат админов» — чтобы не "
    "спамить, когда админы спят. Время указывается <b>по МСК</b>.\n"
    "Пример формата: <code>21:00-09:00</code> (по умолчанию с 21:00 до 09:00).\n\n"
    "—— <b>📂 Мои ссылки и конфиги</b> ——\n"
    "Кнопка в профиле — всё под рукой:\n"
    "• список сохранённых <b>конфигов</b> с кодами и названиями ботов, "
    "к которым они привязаны (можно открыть, применить и <b>🗑 очистить</b>);\n"
    "• <b>ссылки для приглашения админов</b>: видно, сколько мест осталось. "
    "«🔄 Пересоздать» — новая ссылка с тем же лимитом (старая перестаёт "
    "работать), «❌ Аннулировать» — ссылка отключается."
)


@router.callback_query(F.data == "faq_profile")
async def cb_faq_profile(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_PROFILE, _faq_back_kb(), force_answer=True)


FAQ_COMMANDS = (
    "⌨️ <b>Команды</b>\n\n"
    "—— В дочерних ботах (в топиках-диалогах с ПЗ) ——\n"
    "Эти команды админ пишет прямо в топике обращения:\n"
    "• <code>/ban</code> — забанить пользователя. Топик закроется, а пользователь "
    "больше не сможет писать боту.\n"
    "• <code>/unban</code> — разбанить пользователя и вернуть ему доступ.\n"
    "• <code>/otkaz</code> — отказаться от данного обращения. Админ сбрасывается, "
    "а пользователю приходит уведомление с кнопкой подбора нового админа.\n"
    "• <code>/smena</code> — принудительно сменить админа у этого обращения. "
    "Полезно, если кнопка «сменить админа» не работает или нужна быстрая смена "
    "без подтверждения.\n\n"
    "—— В «чате админов» (в групповом чате YamoBot) ——\n"
    "• <code>/стата</code> — показывает, сколько ПЗ без админа, и краткую сводку "
    "по всем ботам.\n"
    "• <code>/стата неделя</code> — активность админов за неделю: сколько "
    "сообщений и ПЗ у каждого.\n"
    "• <code>/perezap</code> — перезапуск бота с перепривязкой чата. Рекомендуется "
    "после передачи прав, чтобы все чаты перешли новому владельцу.\n"
    "• <code>/perestart</code> — простой перезапуск, если бот перестал отвечать "
    "или «завис». Привязки не меняет.\n"
    "• <b>🛡 Антирейд</b> — включается и выключается кнопками "
    "«🟢 Включить» / «🔴 Выключить» в <b>«👤 Профиль → 🛡 Защита → "
    "🛡 Антирейд»</b> "
    "(писать команды в чате не нужно). Когда антирейд включён, бот следит "
    "за заходами в чат и за спамом, а при подозрении на рейд блокирует чат "
    "и зовёт владельца.\n"
    "• Команды <code>/вкланти</code>, <code>/вклчат</code> и "
    "<code>/выкланти</code> тоже работают — как запасной вариант из чата "
    "(<code>/вклчат</code> возвращает чату права после срабатывания, "
    "защита остаётся включённой).\n\n"
    "Команды работают и в другом виде: <code>.стата</code>, <code>/stata</code>."
)


@router.callback_query(F.data == "faq_commands")
async def cb_faq_commands(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_COMMANDS, _faq_back_kb(), force_answer=True)

# ═══════════════ .стата — сводка в чате админов ═══════════════

def _stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ ПЗ без админов", callback_data="gstat_noadmin", style="primary")]
    ])


def _stats_payload(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Сводка по ПЗ всех ботов владельца (общая и по каждому боту)."""
    bots = get_user_bots(owner_id)
    if not bots:
        return "📭 Нет подключённых ботов.", _stats_kb()

    total = 0
    noadmin = 0
    lines = []
    for b in bots:
        ts = get_all_topics_for_bot(b["id"])
        a = sum(1 for t in ts if t.get("admin_user_id"))
        noadmin += len(ts) - a
        total += len(ts)
        lines.append(f"  • {bot_display_name(b)} — 📋 {len(ts)} (🟢 {a} / ⏳ {len(ts) - a})")

    text = (
        f"📊 <b>Сводка по ПЗ</b>\n\n"
        f"Всего ПЗ: <b>{total}</b>\n"
        f"🟢 С админом: <b>{total - noadmin}</b>\n"
        f"⏳ Без админа: <b>{noadmin}</b>\n\n"
        f"<b>По ботам:</b>\n" + "\n".join(lines)
    )
    return text, _stats_kb()


def _resolve_stats_owner(chat, fallback_user_id: int) -> int:
    """Владелец: в ЛС — сам юзер, в группе — владелец привязанного «чата админов»."""
    if chat and chat.type != ChatType.PRIVATE:
        own = get_owner_by_admin_chat(chat.id)
        if own:
            return own
    return fallback_user_id


def collect_noadmin_entries(owner_id: int) -> list[dict]:
    """Список ПЗ без админа по всем ботам владельца (нумеруется в рендере).

    Каждая запись: bot_id, user_chat_id и label (имя бота + ссылка на топик).
    Используется и в сводке «⏳ ПЗ без админов», и в уведомлении раздела
    «📊 Норма» — чтобы формат списка был одинаковым.
    """
    entries: list[dict] = []
    for b in get_user_bots(owner_id):
        for t in get_all_topics_for_bot(b["id"]):
            if t.get("admin_user_id"):
                continue
            cid, tid = t["group_chat_id"], t["topic_id"]
            if cid < 0 and str(cid).startswith("-100"):
                chat_part = int(str(cid)[4:])
            else:
                chat_part = int(cid)
            link = f"https://t.me/c/{chat_part}/{tid}"
            entries.append({
                "bot_id": b["id"],
                "user_chat_id": t["user_chat_id"],
                "label": f"{bot_display_name(b)}: {link}",
            })
    return entries


@router.callback_query(F.data == "gstat_noadmin")
async def cb_gstat_noadmin(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    chat = callback.message.chat if callback.message else None
    owner_id = _resolve_stats_owner(chat, cb_uid(callback))

    # Нумерованный список ПЗ без админа. Сохраняем маппинг «номер → топик»,
    # чтобы кнопка «удалить из списка» могла найти нужную запись.
    entries = collect_noadmin_entries(owner_id)

    await state.update_data(stats_no_admin=entries)
    text, kb = _noadmin_payload(entries)
    await render_callback(callback, text, kb)


def _noadmin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить из списка", callback_data="gstat_del", style="danger")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="gstat_noadmin", style="primary")],
        [InlineKeyboardButton(text="⬅️ К сводке", callback_data="gstat_back", style="primary")],
    ])


def _noadmin_payload(entries: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    if not entries:
        text = "🎉 Все ПЗ закрыты админами или удалены из списка. Топиков без админа нет."
    else:
        lines = [f"{i}. {e['label']}" for i, e in enumerate(entries, start=1)]
        text = (
            f"⏳ <b>ПЗ без админа ({len(entries)})</b>\n\n"
            "Если топик удалил вручную, но он остался в списке — "
            "нажми «🗑 Удалить из списка» и напиши его номер.\n\n"
            + "\n".join(lines)
        )
    return text, _noadmin_kb()


@router.callback_query(F.data == "gstat_del")
async def cb_gstat_del(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    entries = data.get("stats_no_admin") or []
    if not entries:
        await callback.answer("Сначала открой список ПЗ без админов.", show_alert=True)
        return

    await state.set_state(StatsFSM.waiting_pz_delete)
    # Запоминаем сообщение-подсказку: после удаления мы отредактируем ИМЕННО
    # его в обновлённый список, а не отправим новое сообщение.
    await state.update_data(
        stats_no_admin=entries,
        stats_prompt_chat_id=callback.message.chat.id if callback.message else None,
        stats_prompt_message_id=callback.message.message_id if callback.message else None,
    )

    text = (
        "🗑 <b>Удаление из списка ПЗ</b>\n\n"
        f"В списке <b>{len(entries)}</b> позиций.\n"
        "Напиши <b>номер</b> топика из списка, который хочешь удалить "
        "(например, если удалил его вручную)."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="gstat_cancel_del")]
    ])
    if callback.message:
        await try_edit_answer(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "gstat_cancel_del")
async def cb_gstat_cancel_del(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    data = await state.get_data()
    entries = data.get("stats_no_admin") or []
    text, kb = _noadmin_payload(entries)
    await render_callback(callback, text, kb)


@router.message(StatsFSM.waiting_pz_delete)
async def fsm_pz_delete(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    entries = data.get("stats_no_admin") or []

    raw = (message.text or "").strip()
    try:
        n = int(raw)
    except ValueError:
        await message.answer("❌ Номер должен быть числом. Напиши номер из списка.")
        return

    if n < 1 or n > len(entries):
        await message.answer(f"❌ Номер вне диапазона (1–{len(entries)}). Попробуй ещё раз.")
        return

    target = entries.pop(n - 1)
    delete_topic_record(target["bot_id"], target["user_chat_id"])

    # Перерисовываем обновлённый список в ТОМ ЖЕ сообщении, где была подсказка
    # об удалении: раньше бот присылал новое сообщение, а старое «удаление»
    # оставалось висеть. Новое сообщение отправляем только если отредактировать
    # не удалось (например, сообщение уже удалили).
    await state.update_data(stats_no_admin=entries)
    await state.set_state(None)
    text, kb = _noadmin_payload(entries)

    edited = False
    chat_id = data.get("stats_prompt_chat_id")
    message_id = data.get("stats_prompt_message_id")
    if chat_id and message_id and message.bot is not None:
        try:
            await message.bot.edit_message_text(
                chat_id=int(chat_id), message_id=int(message_id),
                text=text, reply_markup=kb,
            )
            edited = True
        except Exception:
            edited = False

    # Сообщение с номером тоже убираем, чтобы не засорять чат.
    try:
        await message.delete()
    except Exception:
        pass

    if not edited:
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "gstat_back")
async def cb_gstat_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    chat = callback.message.chat if callback.message else None
    owner_id = _resolve_stats_owner(chat, cb_uid(callback))
    text, kb = _stats_payload(owner_id)
    await render_callback(callback, text, kb)