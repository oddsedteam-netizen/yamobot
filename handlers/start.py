import logging

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

from handlers._common import (render_callback, cb_data, cb_uid, msg_uid,
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
    """Главное меню — reply-клавиатура."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🤖 Боты")],
            [KeyboardButton(text="📋 ПЗ"), KeyboardButton(text="👥 Админы")],
            [KeyboardButton(text="⚠️ Жалоба"), KeyboardButton(text="❓ FAQ")],
            [KeyboardButton(text="👤 Профиль")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


async def _show_main(message: Message) -> None:
    await message.answer(_welcome_text_with_version(), reply_markup=main_menu_kb())


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = msg_uid(message)
    register_user(user_id, msg_username(message) or "", msg_firstname(message) or "")
    if is_registry_user_banned(user_id) and not is_super_admin(user_id):
        await message.answer("🚫 Вы заблокированы администрацией.")
        return
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
    await _show_main(message)


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


@router.message(F.text == "⚠️ Жалоба")
async def on_complaint_button(message: Message, state: FSMContext) -> None:
    from handlers.complaints import start_complaint
    await start_complaint(message, state)


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
        chat_info = "есть" if fchat else "нет"
        lines.append(
            f"🤖 @{b.get('username') or b['id']} — {status}\n"
            f"   Тип: {b.get('bot_type') or 'standard'} | /connect: {chat_info} | "
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
    "👤 <b>Профиль</b> — привязка бота к «чату админов» и «чату работы», передача прав.\n"
    "⌨️ <b>Команды</b> — все команды для дочерних ботов и «чата админов»."
)


def faq_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Админы", callback_data="faq_admins")],
        [InlineKeyboardButton(text="🤖 Боты", callback_data="faq_bots")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="faq_profile")],
        [InlineKeyboardButton(text="⌨️ Команды", callback_data="faq_commands")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ])


def _faq_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К разделам", callback_data="faq")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
    ])


@router.callback_query(F.data == "faq")
async def cb_faq(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_TEXT, faq_menu_kb())

FAQ_ADMINS = (
    "👥 <b>Админы</b>\n\n"
    "Админ — это человек, который отвечает на «обращения» (ПЗ) твоих ботов. "
    "Админы привязаны ко всем твоим ботам сразу, поэтому одним набором они "
    "видят ПЗ из всех ботов.\n\n"
    "—— <b>Как добавить админа вручную</b> ——\n"
    "1. Открой меню <b>«👥 Админы»</b>.\n"
    "2. Нажми <b>«➕ Добавить админа»</b>.\n"
    "3. Отправь <b>ID</b> или <b>@username</b> пользователя Telegram.\n"
    "4. Дальше бот попросит ввести <b>тег</b> — короткое слово (без решётки), "
    "по которому тебя видно в списках, например <code>продажи</code>.\n"
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
    "—— <b>Уведомление, когда админ отказывается от ПЗ</b> ——\n"
    "Если админ командой <code>/otkaz</code> отказывается от обращения, "
    "пользователю приходит уведомление с кнопкой <b>«🔍 Найти админа»</b> — "
    "он сам подберёт себе нового админа.\n\n"
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
    "• <b>🗂 Стандарт</b> — обычный бот-консультант. Под приветствием появляется "
    "нижняя панель с кнопкой «сменить админа», чтобы пользователь мог поменять "
    "своего админа в один клик. Подходит для живой переписки и поддержки.\n"
    "• <b>📝 Анкетница</b> — бот-анкета. У него нет панели с кнопкой "
    "«сменить админа» — только приветствие и кнопки-ссылки. Подходит для сбора "
    "заявок, когда обращение просто передаётся админу.\n\n"
    "—— <b>Как отредактировать приветствие и добавить кнопки</b> ——\n"
    "1. Открой бота в списке и нажми <b>«✏️ Редактор»</b>.\n"
    "2. <b>«💬 Изменить приветствие»</b> — отправь новый текст (поддерживаются "
    "HTML и премиум-эмодзи).\n"
    "3. <b>«🔗 Линки»</b> — добавь кнопки-ссылки к приветствию: название кнопки "
    "и её ссылку (http/https/tg://). Кнопки появятся прямо под приветствием, "
    "их можно удалять по одной.\n"
    "4. Для всех ботов сразу есть отдельный редактор в «📌 Выбрать все».\n\n"
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
    "—— <b>Как выбрать бота</b> ——\n"
    "Нажми на бота в меню «Боты» — откроется карточка со статусом (🟢 работает / "
    "🔴 остановлен), приветствием, анонимным режимом и действиями: рассылка, "
    "антиспам, статистика, редактор, ПЗ, запуск/остановка и удаление. Если ботов "
    "несколько — выбирай по категориям или используй «📌 Выбрать все».\n\n"
    "—— <b>Как привязать дочерний бот к рабочему чату — /connect</b> ——\n"
    "Чтобы ПЗ дочернего бота открывались у админов в тематических топиках, "
    "бот привязывают к групповому чату:\n"
    "1. Добавь дочернего бота в группу и выдай ему права администратора.\n"
    "2. В этой группе отправь команду <code>/connect</code>.\n"
    "3. Бот создаст топики-чаты под каждое ПЗ. Админы отвечают на обращения "
    "прямо из топиков.\n"
    "4. Статус привязки виден в карточке бота (поле «/connect»).\n\n"
    "Продолжение — команды и привязка чатов в разделах «Профиль» и «Команды»."
)


@router.callback_query(F.data == "faq_admins")
async def cb_faq_admins(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_ADMINS, _faq_back_kb())


@router.callback_query(F.data == "faq_bots")
async def cb_faq_bots(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_BOTS, _faq_back_kb())


FAQ_PROFILE = (
    "👤 <b>Профиль</b>\n\n"
    "Профиль — это твоя карточка: имя, ID, количество ботов, админов и обращений. "
    "Отсюда же настраиваются привязанные чаты и передача прав.\n\n"
    "—— <b>Как привязать основной бот к «чату админов»</b> ——\n"
    "«Чат админов» — это групповой чат, куда приходят уведомления о новых ПЗ "
    "и где работают команды <code>/стата</code>, <code>/perezap</code> и другие.\n"
    "1. В профиле нажми <b>«🛡 Чат админов»</b>.\n"
    "2. Добавь основного бота (YamoBot) в групповой чат, который хочешь "
    "использовать как «чат админов», и дождись подтверждения привязки.\n"
    "3. Выдай боту <b>права администратора</b> в этом чате — иначе уведомления "
    "могут не доходить.\n"
    "4. Готово: бот останется в чате, и сюда будут приходить уведомления о новых ПЗ.\n\n"
    "—— <b>Как привязать «чат работы»</b> ——\n"
    "«Чат работы» — рабочая площадка для ведения дел. Привязка похожая:\n"
    "1. В профиле нажми <b>«💼 Чат работы»</b>.\n"
    "2. Добавь основного бота в групповой чат и дождись подтверждения.\n"
    "3. В отличие от «чата админов», бот <b>запомнит ID и покинет чат</b> — "
    "он не будет там находиться.\n\n"
    "—— <b>Как работает передача прав</b> ——\n"
    "Кнопка <b>«👑 Передать права»</b> в профиле. Можно передать:\n"
    "• <b>👑 Все права</b> — полностью передать боты, админов, совладельцев, "
    "привязанные чаты и настройки предупреждений другому владельцу.\n"
    "• <b>🤖 Только одного бота</b> — передать лишь выбранного бота.\n"
    "1. Нажми «Передать права», выбери вариант, укажи ID получателя.\n"
    "2. Бот отправит получателю ссылку-приглашение; тот нажмёт её — права перейдут.\n"
    "3. После передачи нового владельца автоматически добавляют админом "
    "с его тегом, чтобы ПЗ корректно привязывались.\n"
    "Внимание: при полной передаче <b>все</b> данные (боты, чаты, админы) "
    "переходят новому владельцу."
)


@router.callback_query(F.data == "faq_profile")
async def cb_faq_profile(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_PROFILE, _faq_back_kb())


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
    "или «завис». Привязки не меняет.\n\n"
    "Команды работают и в другом виде: <code>.стата</code>, <code>/stata</code>."
)


@router.callback_query(F.data == "faq_commands")
async def cb_faq_commands(callback: CallbackQuery) -> None:
    await render_callback(callback, FAQ_COMMANDS, _faq_back_kb())

# ═══════════════ .стата — сводка в чате админов ═══════════════

def _stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ ПЗ без админов", callback_data="gstat_noadmin")]
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


@router.callback_query(F.data == "gstat_noadmin")
async def cb_gstat_noadmin(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    chat = callback.message.chat if callback.message else None
    owner_id = _resolve_stats_owner(chat, cb_uid(callback))
    bots = get_user_bots(owner_id)

    # Нумерованный список ПЗ без админа. Сохраняем маппинг «номер → топик»,
    # чтобы кнопка «удалить из списка» могла найти нужную запись.
    entries: list[dict] = []
    for b in bots:
        for t in get_all_topics_for_bot(b["id"]):
            if not t.get("admin_user_id"):
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

    await state.update_data(stats_no_admin=entries)
    text, kb = _noadmin_payload(entries)
    await render_callback(callback, text, kb)


def _noadmin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить из списка", callback_data="gstat_del")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="gstat_noadmin")],
        [InlineKeyboardButton(text="⬅️ К сводке", callback_data="gstat_back")],
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
    await state.update_data(stats_no_admin=entries)

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

    # Перерисовываем обновлённый список.
    await state.update_data(stats_no_admin=entries)
    text, kb = _noadmin_payload(entries)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "gstat_back")
async def cb_gstat_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    chat = callback.message.chat if callback.message else None
    owner_id = _resolve_stats_owner(chat, cb_uid(callback))
    text, kb = _stats_payload(owner_id)
    await render_callback(callback, text, kb)