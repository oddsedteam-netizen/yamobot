from aiogram import Bot, Router, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import (render_callback, ADMIN_CHAT_WELCOME, cb_data,
                              cb_uid, cb_username, cb_firstname, msg_uid,
                              msg_username, msg_firstname, try_edit_answer)
from services.child_manager import ChildManager
from services.config import is_super_admin, proxy_settings
from services.constants import (
    BOT_VERSION,
    SETTING_DONATE_URL,
    SETTING_TEST_BOT_URL,
    SETTING_YAMOCHAN_URL,
)
from services.storage import (
    get_all_users_registry,
    get_user_bots,
    get_admins_all,
    ticket_counts,
    get_all_bots_flat,
    get_all_topics_for_bot,
    get_all_stats,
    get_stats,
    bot_display_name,
    utc_to_msk,
    is_registry_user_banned,
    set_registry_user_blocked,
    remove_user_bot,
    remove_admin,
    get_bound_chat,
    set_bound_chat,
    get_pending_bind,
    set_pending_bind,
    get_user_registry,
    get_bot_by_id_any_owner,
    create_transfer,
    get_transfer,
    delete_transfer,
    transfer_all_rights,
    transfer_bot,
    get_app_setting,
    set_app_setting,
    get_antiraid_settings,
    get_antinakrutka_settings,
    get_dead_bots,
    mark_bot_dead,
    clear_bot_dead,
    remove_dead_bots,
)

router = Router()

# Ожидание привязки чатов: user_id -> kind ("work"|"admin").
_PENDING_BINDS: dict[int, str] = {}
# Последний добавленный чат для юзера: user_id -> chat_id (для кнопки «я добавил бота»).
_LAST_ADDED: dict[int, int] = {}


class BroadcastFSM(StatesGroup):
    waiting_text = State()


class LinksFSM(StatesGroup):
    # Ожидание новой ссылки для раздела «🟢 Прочее» (админ-панель).
    waiting_link = State()


class ProfileFSM(StatesGroup):
    """Админ-панель: поиск бота и удаление одного бота."""
    waiting_find_bot = State()
    waiting_del_bot = State()



def _user_line(u: dict) -> str:
    name = u.get("username") or u.get("first_name") or str(u["user_id"])
    status = "🚫" if u.get("blocked") else "🟢"
    return f"{status} {name}"


# ── Постраничный вывод больших списков ────────────────────────────────
# Большие списки (ВЛД, боты, админы) раньше выводились целиком: сообщение
# упиралось в лимиты Telegram (4096 символов и размер клавиатуры), и вкладка
# просто не открывалась. Теперь такие списки показываются страницами.
LIST_PAGE_SIZE = 10
# Списки админов показываем короче: у каждого админа три кнопки действий.
ADMINS_PAGE_SIZE = 8


def _parse_page(data: str, prefix: str) -> int:
    """Номер страницы из callback_data вида ``<prefix><N>`` (по умолчанию 1)."""
    tail = data[len(prefix):]
    return int(tail) if tail.isdigit() and int(tail) > 0 else 1


def _page_slice(items: list, page: int,
                size: int = LIST_PAGE_SIZE) -> tuple[list, int, int]:
    """Окно списка для страницы: ``(элементы, номер страницы, всего страниц)``."""
    total_pages = max(1, (len(items) + size - 1) // size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * size
    return items[start:start + size], page, total_pages


def _pager_rows(prefix: str, page: int,
                total_pages: int) -> list[list[InlineKeyboardButton]]:
    """Строка перелистывания «◀️ · N/M · ▶️» (пусто, если страница одна)."""
    if total_pages <= 1:
        return []
    prev_page = page - 1 if page > 1 else total_pages
    next_page = page + 1 if page < total_pages else 1
    return [[
        InlineKeyboardButton(text="◀️", callback_data=f"{prefix}{prev_page}", style="primary"),
        InlineKeyboardButton(text=f"📄 {page}/{total_pages}",
                             callback_data=f"{prefix}{page}", style="primary"),
        InlineKeyboardButton(text="▶️", callback_data=f"{prefix}{next_page}", style="primary"),
    ]]


def _page_note(page: int, total_pages: int) -> str:
    """Приписка «Страница N из M» к тексту (пусто, если страница одна)."""
    if total_pages <= 1:
        return ""
    return f"\n\n📄 Страница <b>{page}</b> из <b>{total_pages}</b>"


def _short(text: str, limit: int = 60) -> str:
    """Обрезает подпись кнопки: Telegram режет текст длиннее 64 символов."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎫 Тикеты", callback_data="tickets_admin", style="primary"),
            InlineKeyboardButton(text="👤 Профиль ВЛД", callback_data="adm_owner_profile",
                                 style="primary"),
        ],
        [
            InlineKeyboardButton(text="🗂 Логи", callback_data="botlogs", style="primary"),
            InlineKeyboardButton(text="🔎 Найти бота", callback_data="adm_find_bot", style="primary"),
        ],
        [
            InlineKeyboardButton(text="📊 Сводка", callback_data="adm_overview", style="primary"),
            InlineKeyboardButton(text="👥 Профили", callback_data="profiles_list", style="primary"),
        ],
        [
            # Удаление ОДНОГО бота — с подтверждением (не «удалить всё сразу»).
            InlineKeyboardButton(text="🗑 Удалить бота", callback_data="adm_del_bot", style="danger"),
        ],
        # Мёртвые боты (авто-детект) — красная: там удаление и «пачка».
        [InlineKeyboardButton(text="🧟 Мёртвые боты", callback_data="dead_bots", style="danger")],
        [InlineKeyboardButton(text="📨 Рассылка всем", callback_data="broadcast", style="primary")],
        [InlineKeyboardButton(text="🔗 Настройки ссылок", callback_data="links_settings", style="primary")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")],
    ])


# ═══════════════ Рассылка всем пользователям (только супер-админ) ═══════════════

@router.callback_query(F.data == "broadcast")
async def cb_broadcast(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    all_users = [u for u in get_all_users_registry() if not u.get("blocked")]
    vld = [u for u in all_users if get_user_bots(u["user_id"])]
    text = (
        "📨 <b>Рассылка пользователям</b>\n\n"
        f"👥 Всего пользователей: <b>{len(all_users)}</b>\n"
        f"🤖 Только ВЛД (владельцев): <b>{len(vld)}</b>\n\n"
        "Кому выслать?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Всем", callback_data="broadcast_all", style="primary")],
        [InlineKeyboardButton(text="🤖 Только ВЛД", callback_data="broadcast_vld", style="primary")],
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="profile_admin", style="primary")],
    ])
    await render_callback(callback, text, kb)


async def _ask_broadcast_message(callback: CallbackQuery, state: FSMContext, mode: str) -> None:
    await state.set_state(BroadcastFSM.waiting_text)
    await state.update_data(broadcast_mode=mode)
    label = "Всем пользователям" if mode == "all" else "Только ВЛД (владельцам)"
    if callback.message:
        await try_edit_answer(
            callback.message,
            f"📨 <b>Рассылка — {label}</b>\n\n"
            "Отправь <b>сообщение</b>, которое нужно разослать (текст с HTML "
            "или премиум-эмодзи).",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="profile_admin", style="primary")]
            ]),
        )
    await callback.answer()


@router.callback_query(F.data == "broadcast_all")
async def cb_broadcast_all(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await _ask_broadcast_message(callback, state, "all")


@router.callback_query(F.data == "broadcast_vld")
async def cb_broadcast_vld(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await _ask_broadcast_message(callback, state, "vld")


@router.message(BroadcastFSM.waiting_text)
async def fsm_broadcast(message: Message, state: FSMContext) -> None:
    user_id = msg_uid(message)
    if not is_super_admin(user_id):
        await state.clear()
        return
    text = message.html_text or message.text or ""
    if not text.strip():
        await message.answer("❌ Сообщение не должно быть пустым.")
        return
    data = await state.get_data()
    mode = data.get("broadcast_mode", "all")
    await state.clear()

    recipients = [u for u in get_all_users_registry() if not u.get("blocked")]
    if mode == "vld":
        recipients = [u for u in recipients if get_user_bots(u["user_id"])]

    await message.answer(f"📨 Рассылка запущена... 👥 {len(recipients)}")
    ok, fail = 0, 0
    for u in recipients:
        if u["user_id"] == user_id:
            continue
        try:
            bot = message.bot
            if bot is None:
                continue
            await bot.send_message(u["user_id"], text)
            ok += 1
        except Exception:
            fail += 1

    await message.answer(
        f"📨 <b>Рассылка завершена</b>\n\n"
        f"👥 Получателей: <b>{len(recipients)}</b>\n"
        f"✅ Доставлено: <b>{ok}</b>\n"
        f"❌ Ошибок: <b>{fail}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛡 Админ-панель", callback_data="profile_admin", style="primary")]
        ]),
    )
# ═══════════════ Мёртвые боты: авто-детект и удаление (только супер-админ) ═══════════════

def _dead_row_display(row: dict) -> dict:
    """Приводит запись «мёртвого» бота к виду, который понимает bot_display_name()."""
    return {
        "id": row.get("bot_id"),
        "username": row.get("username") or "",
        "first_name": row.get("first_name") or "",
    }


def _dead_reason_text(reason: str) -> str:
    """Человекочитаемая причина, по которой бот признан мёртвым."""
    reason = (reason or "").strip()
    if reason == "unauthorized":
        return "токен отозван или недействителен"
    if reason == "нет токена":
        return "нет токена"
    return reason or "не отвечает"


def _dead_bots_payload(status: str = "", page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «🧟 Мёртвые боты»: список авто-детекта + действия (постранично)."""
    dead = get_dead_bots()
    total_bots = len(get_all_bots_flat())
    window, page, total_pages = _page_slice(dead, page, LIST_PAGE_SIZE)

    lines: list[str] = []
    start = (page - 1) * LIST_PAGE_SIZE
    for offset, row in enumerate(window):
        i = start + offset + 1
        name = bot_display_name(_dead_row_display(row))
        lines.append(
            f"{i}. {name} ⸱ 🆔 <code>{row['bot_id']}</code>\n"
            f"    👤 <code>{row['owner_id']}</code> ⸱ "
            f"⚠️ {_dead_reason_text(str(row.get('reason') or ''))}"
        )
    dead_text = "\n".join(lines) if lines else "  — мёртвых ботов нет 🎉 —"

    text = (
        "🧟 <b>Мёртвые боты</b>\n\n"
        f"🤖 Всего ботов в панели: <b>{total_bots}</b>\n"
        f"💀 Помечено мёртвыми: <b>{len(dead)}</b>\n\n"
        f"{dead_text}\n\n"
        "Бот помечается мёртвым автоматически, когда Telegram отвечает "
        "«Unauthorized» (токен отозван или бот удалён в @BotFather). "
        "Кнопка «🔍 Проверить ботов» опрашивает всех ботов панели сразу.\n\n"
        "Что делаем?"
    )
    text += _page_note(page, total_pages)
    if status:
        text = f"{status}\n\n{text}"

    rows: list[list[InlineKeyboardButton]] = []
    for row in window:
        bot_id = int(row["bot_id"])
        rows.append([
            InlineKeyboardButton(
                text=_short(f"🗑 {bot_display_name(_dead_row_display(row))}"),
                callback_data=(f"dead_del_{bot_id}"
                               + (f"_p{page}" if total_pages > 1 else "")),
                style="danger",
            )
        ])
    if total_pages > 1:
        rows.append([
            InlineKeyboardButton(text="◀️",
                                 callback_data=f"dead_bots_p{page - 1 if page > 1 else total_pages}",
                                 style="primary"),
            InlineKeyboardButton(text=f"📄 {page}/{total_pages}",
                                 callback_data=f"dead_bots_p{page}", style="primary"),
            InlineKeyboardButton(text="▶️",
                                 callback_data=f"dead_bots_p{page + 1 if page < total_pages else 1}",
                                 style="primary"),
        ])
    rows.append([
        InlineKeyboardButton(text="🔍 Проверить ботов", callback_data="dead_check", style="primary")
    ])
    if dead:
        rows.append([
            InlineKeyboardButton(text=f"🗑 Удалить всех ({len(dead)})",
                                 callback_data="dead_del_all", style="danger")
        ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="profile_admin", style="primary")
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _check_bots_alive() -> tuple[int, int, int]:
    """Опрашивает всех ботов панели через ``get_me()``.

    Возвращает ``(проверено, нерабочих, непроверенных)``:

    * Telegram ответил «Unauthorized» (токен отозван/бот удалён) — это и есть
      «мёртвый» бот, помечаем его;
    * связи с Telegram нет вовсе (VPN отвалился, нет интернета) — бот ни при
      чём, **не трогаем** пометки, иначе проверка пометила бы весь список
      мёртвым из-за собственной сети админа;
    * прочие ошибки (например, бот заблокирован) — помечаем с причиной.
    """
    checked = 0
    dead = 0
    skipped = 0
    for b in get_all_bots_flat():
        bot_id = int(b["id"])
        token = str(b.get("token") or "")
        if not token:
            mark_bot_dead(bot_id, "нет токена")
            dead += 1
            checked += 1
            continue

        probe = Bot(token=token, **proxy_settings())
        try:
            await probe.get_me()
        except TelegramUnauthorizedError:
            mark_bot_dead(bot_id, "unauthorized")
            dead += 1
        except TelegramNetworkError:
            # Нет связи с Telegram — это не «мёртвый» бот.
            skipped += 1
        except Exception as e:
            mark_bot_dead(bot_id, f"ошибка проверки: {type(e).__name__}")
            dead += 1
        else:
            clear_bot_dead(bot_id)
        finally:
            checked += 1
            try:
                await probe.session.close()
            except Exception:
                pass
    return checked, dead, skipped


async def _delete_bot_forever(bot_id: int, child_manager: ChildManager) -> str:
    """Останавливает и полностью удаляет бота вместе с его данными."""
    bot = get_bot_by_id_any_owner(bot_id)
    owner_id = int(bot.get("owner_id") or 0) if bot else 0
    if child_manager.is_running(bot_id):
        try:
            await child_manager.stop_child(bot_id)
        except Exception:
            pass
    name = bot_display_name(bot) if bot else f"бот {bot_id}"
    remove_user_bot(owner_id, bot_id)
    clear_bot_dead(bot_id)
    return name


# ═══════════════ Профиль пользователя (админ-панель) ═══════════════

def _owner_profile_text(owner_id: int) -> str:
    """Полный профиль пользователя: чаты, боты, админы, тикеты."""
    user = next(
        (u for u in get_all_users_registry() if int(u.get("user_id") or 0) == owner_id),
        {},
    )
    bots = get_user_bots(owner_id)
    admins = get_admins_all(owner_id)
    tickets = ticket_counts()

    work_chat = get_bound_chat(owner_id, "work")
    admin_chat = get_bound_chat(owner_id, "admin")

    def _chat(value: int | None) -> str:
        return f"<code>{value}</code>" if value else "не привязан"

    bot_lines = "\n".join(
        f"  • {bot_display_name(b)} <code>{b['id']}</code>"
        + ("" if b.get("stopped") else " — 🟢 работает")
        for b in bots
    ) or "  — нет —"

    return (
        f"👤 <b>Профиль {owner_id}</b>\n\n"
        f"🆔 ID: <code>{owner_id}</code>\n"
        f"👤 Имя: {user.get('first_name') or '—'}"
        f"{('@' + user['username']) if user.get('username') else ''}\n"
        f"📅 В базе с: {str(user.get('created_at') or '—')[:19]}\n"
        f"🚫 Забанен: {'да' if user.get('blocked') else 'нет'}\n\n"
        f"💼 Чат работы: {_chat(work_chat)}\n"
        f"🛡 Чат админов: {_chat(admin_chat)}\n\n"
        f"🤖 <b>Ботов: {len(bots)}</b>\n{bot_lines}\n\n"
        f"🛡 Админов: <b>{len(admins)}</b>\n"
        f"🎫 Тикетов: 🟢 <b>{tickets.get('open', 0)}</b> / "
        f"⚪ <b>{tickets.get('closed', 0)}</b>"
    )


@router.callback_query(F.data == "adm_owner_profile")
async def cb_owner_profile(callback: CallbackQuery) -> None:
    """Полный профиль владельца бота: чаты, боты, админы, тикеты."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await render_callback(
        callback,
        _owner_profile_text(cb_uid(callback)),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗂 Логи ботов", callback_data="botlogs",
                                  style="primary")],
            [InlineKeyboardButton(text="🎫 Тикеты", callback_data="tickets_admin",
                                  style="primary")],
            [InlineKeyboardButton(text="📨 Рассылка", callback_data="broadcast",
                                  style="primary")],
            [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="profile_admin",
                                  style="primary")],
        ]),
    )


# ═══════════════ Сводка / поиск / удаление одного бота ═══════════════

def _overview_text() -> str:
    """Сводка по платформе: сколько людей, ботов, ПЗ и мёртвых токенов."""
    bots = get_all_bots_flat()
    users = get_all_users_registry()
    dead = get_dead_bots()
    bot_ids = [int(b["id"]) for b in bots]
    stats = get_all_stats(bot_ids) if bot_ids else {}
    running = sum(1 for b in bots if not b.get("stopped"))

    return (
        "📊 <b>Сводка по платформе</b>\n\n"
        f"👤 Пользователей платформы: <b>{len(users)}</b>\n"
        f"🤖 Ботов: <b>{len(bots)}</b> (из них работает <b>{running}</b>)\n"
        f"🧟 Мёртвых токенов: <b>{len(dead)}</b>\n\n"
        f"👥 ПЗ всего: <b>{stats.get('users_total', 0)}</b> "
        f"(🚫 заблокировано <b>{stats.get('users_blocked', 0)}</b>)\n"
        f"💬 Сообщений от ПЗ: <b>{stats.get('messages_in', 0)}</b>\n"
        f"📨 Ответов админов: <b>{stats.get('messages_out', 0)}</b>\n"
        f"📮 Рассылок отправлено: <b>{stats.get('mailings_sent', 0)}</b>"
    )


def _bot_card_text(bot: dict) -> str:
    name = bot_display_name(bot)
    owner_id = int(bot.get("owner_id") or 0)
    owner = next(
        (u.get("username") or u.get("first_name") for u in get_all_users_registry()
         if int(u.get("user_id") or 0) == owner_id),
        None,
    ) or f"user {owner_id}"
    state = "⏸ остановлен" if bot.get("stopped") else "🟢 работает"
    return (
        f"🤖 <b>{name}</b>\n\n"
        f"🆔 ID: <code>{bot.get('id')}</code>\n"
        f"🔗 Username: <code>{bot.get('username') or '—'}</code>\n"
        f"👤 Владелец: <b>{owner}</b> (<code>{owner_id}</code>)\n"
        f"📊 Состояние: {state}"
    )


def _find_bot_any(raw: str) -> dict | None:
    """Ищет бота по ID или @username среди всех ботов платформы."""
    raw = (raw or "").strip().lstrip("@")
    if raw.isdigit():
        bot = get_bot_by_id_any_owner(int(raw))
        if bot:
            return bot
    return next(
        (b for b in get_all_bots_flat()
         if str(b.get("username") or "").lower().lstrip("@") == raw.lower()),
        None,
    )


@router.callback_query(F.data == "adm_overview")
async def cb_adm_overview(callback: CallbackQuery) -> None:
    """Сводка по платформе."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await render_callback(
        callback,
        _overview_text(),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧟 Мёртвые боты", callback_data="dead_bots", style="danger")],
            [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="profile_show")],
        ]),
    )


@router.callback_query(F.data == "adm_find_bot")
async def cb_adm_find_bot(callback: CallbackQuery, state: FSMContext) -> None:
    """Поиск бота по ID или @username."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(ProfileFSM.waiting_find_bot)
    await render_callback(
        callback,
        "🔎 <b>Найти бота</b>\n\n"
        "Пришли <b>ID</b> бота (число) или его <b>@username</b> — покажу, "
        "кому он принадлежит и работает ли он.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="profile_show", style="primary")]
        ]),
    )
    await callback.answer()


@router.message(ProfileFSM.waiting_find_bot)
async def fsm_find_bot(message: Message, state: FSMContext) -> None:
    """Показываем карточку найденного бота."""
    if not is_super_admin(msg_uid(message)):
        await state.clear()
        await message.answer("⛔ Доступ запрещён")
        return

    await state.clear()
    bot = _find_bot_any(message.text or "")
    if bot is None:
        await message.answer(
            "🤷 Бот не найден. Проверь ID или @username — и попробуй ещё раз.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔎 Искать снова", callback_data="adm_find_bot",
                                      style="primary")]
            ]),
        )
        return

    bot_id = int(bot["id"])
    await message.answer(
        _bot_card_text(bot),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить этого бота",
                                  callback_data=f"adm_del_ask_{bot_id}", style="danger")],
            [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="profile_show",
                                  style="primary")],
        ]),
    )


@router.callback_query(F.data == "adm_del_bot")
async def cb_adm_del_bot(callback: CallbackQuery, state: FSMContext) -> None:
    """Удаление ОДНОГО бота: сначала просим ID или username."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(ProfileFSM.waiting_del_bot)
    await render_callback(
        callback,
        "🗑 <b>Удаление одного бота</b>\n\n"
        "Удаляется <b>только выбранный бот</b> — остальные останутся работать.\n\n"
        "Пришли <b>ID</b> или <b>@username</b> бота, которого нужно удалить.\n\n"
        "⚠️ Вместе с ботом удалятся его топики, админы и статистика. Пользователи "
        "и ПЗ платформы не пострадают.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="profile_show", style="primary")]
        ]),
    )
    await callback.answer()


@router.message(ProfileFSM.waiting_del_bot)
async def fsm_del_bot(message: Message, state: FSMContext) -> None:
    """Нашли бота по ID/username — просим подтверждение удаления."""
    if not is_super_admin(msg_uid(message)):
        await state.clear()
        await message.answer("⛔ Доступ запрещён")
        return

    await state.clear()
    bot = _find_bot_any(message.text or "")
    if bot is None:
        await message.answer("🤷 Бот не найден. Проверь ID или @username.")
        return

    bot_id = int(bot["id"])
    await message.answer(
        f"{_bot_card_text(bot)}\n\n"
        "⚠️ <b>Удалить этого бота?</b> Отменить будет нельзя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Да, удалить",
                                      callback_data=f"adm_del_yes_{bot_id}", style="danger"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="profile_show",
                                      style="primary"),
            ]
        ]),
    )


@router.callback_query(F.data.regexp(r"^adm_del_ask_\d+$"))
async def cb_adm_del_ask(callback: CallbackQuery) -> None:
    """Подтверждение удаления из карточки найденного бота."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    bot = get_bot_by_id_any_owner(bot_id)
    if not bot:
        await callback.answer("⚠️ Бот уже удалён", show_alert=True)
        return

    await render_callback(
        callback,
        f"{_bot_card_text(bot)}\n\n"
        "⚠️ <b>Удалить этого бота?</b> Остальные боты не пострадают.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"adm_del_yes_{bot_id}",
                                  style="danger")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="profile_show", style="primary")],
        ]),
    )


@router.callback_query(F.data.regexp(r"^adm_del_yes_\d+$"))
async def cb_adm_del_yes(callback: CallbackQuery, child_manager: ChildManager) -> None:
    """Собственно удаление одного бота."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    if not get_bot_by_id_any_owner(bot_id):
        await callback.answer("⚠️ Бот уже удалён", show_alert=True)
        return

    try:
        name = await _delete_bot_forever(bot_id, child_manager)
    except Exception as e:
        await callback.answer("⚠️ Не удалось удалить", show_alert=True)
        if callback.message:
            await callback.message.answer(f"⚠️ Ошибка при удалении: {e}")
        return

    await render_callback(
        callback,
        f"🗑 Бот <b>{name}</b> удалён. Остальные боты продолжают работать.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="profile_show")]
        ]),
    )
    await callback.answer("🗑 Удалено")



@router.callback_query(F.data.startswith("dead_bots"))
async def cb_dead_bots(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    page = _parse_page(cb_data(callback), "dead_bots_p")
    text, kb = _dead_bots_payload(page=page)
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "dead_check")
async def cb_dead_check(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.answer("🔍 Проверяю ботов…")
    checked, dead, skipped = await _check_bots_alive()
    status = (
        f"🔍 <b>Проверка завершена:</b> опрошено <b>{checked}</b>, "
        f"нерабочих — <b>{dead}</b>."
    )
    if skipped:
        status += (
            f"\n⚠️ <b>{skipped}</b> ботов не удалось проверить — нет связи с "
            "Telegram. Их статус не менялся: повтори проверку при появлении сети."
        )
    text, kb = _dead_bots_payload(status)
    await render_callback(callback, text, kb)


@router.callback_query(F.data.regexp(r"^dead_del_\d+(_p\d+)?$"))
async def cb_dead_del(callback: CallbackQuery, child_manager: ChildManager) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    raw = cb_data(callback)
    head, _, page_tail = raw.partition("_p")
    bot_id = int(head.split("_")[-1])
    page = int(page_tail) if page_tail.isdigit() else 1
    name = await _delete_bot_forever(bot_id, child_manager)
    text, kb = _dead_bots_payload(f"🗑 <b>Бот удалён:</b> {name}.", page)
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "dead_del_all")
async def cb_dead_del_all_ask(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    dead = get_dead_bots()
    if not dead:
        text, kb = _dead_bots_payload("⚠️ Мёртвых ботов нет — удалять нечего.")
        await render_callback(callback, text, kb)
        return

    text = (
        "🗑 <b>Удалить всех мёртвых ботов?</b>\n\n"
        f"Будет удалено ботов: <b>{len(dead)}</b> — вместе с их данными "
        "(ПЗ, статистика, пользователи, привязки).\n\n"
        "⚠️ Действие необратимо."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить всех", callback_data="dead_del_all_yes",
                              style="danger")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="dead_bots", style="primary")],
    ])
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "dead_del_all_yes")
async def cb_dead_del_all_yes(callback: CallbackQuery, child_manager: ChildManager) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    for row in get_dead_bots():
        bot_id = int(row["bot_id"])
        if child_manager.is_running(bot_id):
            try:
                await child_manager.stop_child(bot_id)
            except Exception:
                pass

    removed = remove_dead_bots()
    text, kb = _dead_bots_payload(f"🗑 <b>Удалено мёртвых ботов:</b> {len(removed)}.")
    await render_callback(callback, text, kb)


def profiles_kb(users: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for u in users:
        rows.append([InlineKeyboardButton(
            text=_user_line(u), callback_data=f"profile_view_{u['user_id']}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="profile_admin", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_admin_kb(user_id: int) -> InlineKeyboardMarkup:
    banned = is_registry_user_banned(user_id)
    ban_btn = "🚫 Забанить" if not banned else "✅ Разбанить"
    ban_data = f"profile_ban_{user_id}" if not banned else f"profile_unban_{user_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ban_btn, callback_data=ban_data, style=("danger" if not banned else "success"))],
        [InlineKeyboardButton(text="📊 Статистика", callback_data=f"profile_stats_{user_id}", style="primary")],
        [InlineKeyboardButton(text="🗑 Удалить ботов", callback_data=f"profile_del_bots_{user_id}", style="danger")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="profiles_list", style="primary")],
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="profile_admin", style="primary")],
    ])


def _profile_payload(user_id: int, first_name: str) -> tuple[str, InlineKeyboardMarkup]:
    """Собирает текст и клавиатуру профиля (используется и для message, и для callback)."""
    bots = get_user_bots(user_id)
    admins = get_admins_all(user_id)

    lines = []
    total_pz = 0
    for b in bots:
        topics = get_all_topics_for_bot(b["id"])
        total_pz += len(topics)
        lines.append(f"  • {bot_display_name(b)} — 📋 ПЗ: <b>{len(topics)}</b>")
    bots_list = "\n".join(lines) if lines else "  — нет ботов —"

    work_chat = get_bound_chat(user_id, "work")
    admin_chat = get_bound_chat(user_id, "admin")
    work_line = f"<code>{work_chat}</code>" if work_chat else "не привязан"
    admin_line = f"<code>{admin_chat}</code>" if admin_chat else "не привязан"

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"📛 Имя: <b>{first_name}</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"
        f"🤖 Ботов: <b>{len(bots)}</b>\n"
        f"👥 Админов: <b>{len(admins)}</b>\n"
        f"📋 Всего ПЗ: <b>{total_pz}</b>\n\n"
        f"💼 Чат работы: {work_line}\n"
        f"🛡 Чат админов: {admin_line}\n\n"
        f"<b>По ботам:</b>\n{bots_list}\n\n"
        f"⚙️ Версия бота: <b>{BOT_VERSION}</b>"
    )

    # Кнопка чатов: «Привязать чаты» — пока ничего не привязано, иначе «Чаты».
    any_chat = work_chat or admin_chat
    chats_label = "📎 Чаты" if any_chat else "🔗 Привязать чаты"
    chats_data = "chats_info" if any_chat else "chats_bind"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        # «Боты», «Админы» и «ПЗ» переехали в reply-меню — в профиле оставляем
        # только то, что относится к настройкам владельца.
        [
            InlineKeyboardButton(text=chats_label, callback_data=chats_data),
            InlineKeyboardButton(text="📊 Норма", callback_data="norm"),
        ],
        # «Защита» — красная: внутри антирейд и антинакрутка.
        [InlineKeyboardButton(text="🛡 Защита", callback_data="profile_protection",
                              style="danger")],
        [
            InlineKeyboardButton(text="⏰ Напоминалка", callback_data="reminder_menu", style="primary"),
        ],
        [InlineKeyboardButton(text="🕐 Время работы", callback_data="work_hours", style="primary")],
        [
            InlineKeyboardButton(text="👑 Передать права", callback_data="transfer", style="danger"),
            InlineKeyboardButton(text="🔄 Полный перезапуск", callback_data="profile_restart_all", style="danger"),
        ],
        [
            InlineKeyboardButton(text="📂 Мои ссылки и конфиги", callback_data="my_links",
                                 style="success"),
        ],
    ])
    if is_super_admin(user_id):
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="🛡 Админ-панель", callback_data="profile_admin", style="primary")
        ])

    return text, kb


async def show_profile(message: Message) -> None:
    text, kb = _profile_payload(msg_uid(message), msg_firstname(message) or "—")
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "profile_show")
async def cb_profile_show(callback: CallbackQuery) -> None:
    """Открывает профиль из инлайн-колбэка (без нового приветствия)."""
    _PENDING_BINDS.pop(cb_uid(callback), None)
    set_pending_bind(cb_uid(callback), None)
    if callback.message is None:
        return
    text, kb = _profile_payload(cb_uid(callback), cb_firstname(callback) or "—")
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "profile_restart_all")
async def cb_profile_restart_all(callback: CallbackQuery,
                                 child_manager: ChildManager) -> None:
    """«🔄 Полный перезапуск» — перезапускает всех дочерних ботов владельца.

    Помогает, когда боты зависли или перестали отвечать: не нужно отвязывать
    и привязывать заново — просто перезапускаем всё одним нажатием.
    """
    user_id = cb_uid(callback)
    result = await child_manager.restart_all_for_owner(user_id)
    text, kb = _profile_payload(user_id, cb_firstname(callback) or "—")
    if result["total"]:
        status = (
            f"🔄 <b>Полный перезапуск завершён:</b> "
            f"{result['ok']} из {result['total']} ботов перезапущено."
        )
    else:
        status = "🔄 У тебя нет запущенных ботов для перезапуска."
    await render_callback(callback, f"{status}\n\n{text}", kb)


# ═══════════════ «🛡 Защита» — антирейд и антинакрутка в одном разделе ═══════════════

def _protection_kb() -> InlineKeyboardMarkup:
    """Клавиатура раздела «🛡 Защита»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Антирейд", callback_data="antiraid", style="primary")],
        [InlineKeyboardButton(text="🚨 Антинакрутка", callback_data="antinakrutka", style="primary")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")],
    ])


@router.callback_query(F.data == "profile_protection")
async def cb_profile_protection(callback: CallbackQuery, state: FSMContext) -> None:
    """«🛡 Защита» — один экран для антирейда и антинакрутки.

    Раньше это были две отдельные кнопки в профиле; теперь они внутри раздела,
    а сверху видно текущее состояние обеих защит.
    """
    await state.clear()
    owner_id = cb_uid(callback)

    raid = get_antiraid_settings(owner_id)
    raid_state = "🟢 включён" if raid["enabled"] else "🔴 выключен"

    nakr = get_antinakrutka_settings(owner_id)
    if not int(nakr.get("enabled", 1)):
        nakr_state = "🔴 выключена"
    elif nakr["triggered"]:
        nakr_state = "🚨 активна (защита от наплыва ПЗ)"
    else:
        nakr_state = "🟢 следит за новыми ПЗ"

    text = (
        "🛡 <b>Защита</b>\n\n"
        "Две независимые системы защиты — для «чата админов» и для ПЗ "
        "твоих ботов:\n\n"
        f"🛡 <b>Антирейд</b> — {raid_state}\n"
        "  ловит массовые заходы и спам в привязанном «чате админов».\n\n"
        f"🚨 <b>Антинакрутка</b> — {nakr_state}\n"
        "  ловит наплыв новых ПЗ (накрутку) и временно останавливает "
        "создание топиков.\n\n"
        "Выбери раздел 👇"
    )
    await render_callback(callback, text, _protection_kb())


# ═══════════════ Передача прав владельца ═══════════════

async def _master_bot_username(bot) -> str:
    """Возвращает username мастер-бота (YamoBot) для ссылки-приглашения."""
    try:
        me = await bot.get_me()
        return me.username or ""
    except Exception:
        return ""


def _user_display(user_id: int) -> str:
    """Имя пользователя YamoBot (для подписи «<ник> передаёт вам права»)."""
    u = get_user_registry(user_id)
    if u:
        return u.get("username") or u.get("first_name") or f"ID:{user_id}"
    return f"ID:{user_id}"


def _rights_lines_from(transfer: dict) -> list[str]:
    """Читаемый список того, что передаётся, по данным ссылки-передачи."""
    kind = transfer.get("kind")
    if kind == "bot" and transfer.get("bot_id"):
        bot = get_bot_by_id_any_owner(int(transfer["bot_id"]))
        if bot:
            return [f"• {bot_display_name(bot)}"]
        return ["• бот (удалён)"]
    bots = get_user_bots(transfer.get("from_user_id") or 0)
    if not bots:
        return ["• все права"]
    return [f"• {bot_display_name(b)}" for b in bots]


@router.callback_query(F.data == "transfer")
async def cb_transfer_open(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    bots = get_user_bots(user_id)
    if not bots:
        await render_callback(
            callback,
            "👑 <b>Передача прав</b>\n\nУ тебя нет ботов — передавать нечего.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")]
            ]),
        )
        return

    text = (
        "👑 <b>Передача прав</b>\n\n"
        "⚠️ <b>Внимание!</b> При передаче все привязанные данные "
        "(боты, админы, совладельцы, привязанные чаты и настройки) "
        "перейдут другому владельцу.\n\n"
        "Что передаём?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👑 Все права", callback_data="transfer_all", style="primary")],
        [InlineKeyboardButton(text="🤖 Только одного бота", callback_data="transfer_one", style="primary")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")],
    ])
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "transfer_one")
async def cb_transfer_one(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    bots = get_user_bots(user_id)
    if not bots:
        await callback.answer("У тебя нет ботов", show_alert=True)
        return

    rows = [
        [InlineKeyboardButton(text=bot_display_name(b), callback_data=f"transfer_pick_{b['id']}")]
        for b in bots
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="transfer", style="primary")])
    await render_callback(
        callback,
        "🤖 <b>Передать одного бота</b>\n\nВыбери бота, которого хочешь передать:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _send_confirm_link(callback: CallbackQuery, token: str) -> None:
    """Отправляет владельцу ссылку для передачи прав."""
    username = await _master_bot_username(callback.bot)
    if not username:
        await callback.answer("⚠️ Не удалось сформировать ссылку", show_alert=True)
        return
    link = f"https://t.me/{username}?start=transfer_{token}"
    text = (
        "🔗 <b>Ссылка для передачи прав готова!</b>\n\n"
        "Отправь её новому владельцу. После перехода он должен подтвердить принятие.\n\n"
        f"<code>{link}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")]
    ])
    if callback.message:
        await try_edit_answer(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "transfer_all")
async def cb_transfer_all(callback: CallbackQuery) -> None:
    token = create_transfer(cb_uid(callback), "all")
    await _send_confirm_link(callback, token)


@router.callback_query(F.data.startswith("transfer_pick_"))
async def cb_transfer_pick(callback: CallbackQuery) -> None:
    bot_id = int(cb_data(callback).split("_")[-1])
    token = create_transfer(cb_uid(callback), "bot", bot_id)
    await _send_confirm_link(callback, token)


async def handle_transfer_link(message: Message, token: str) -> None:
    """Обрабатывает переход нового владельца по ссылке `?start=transfer_<token>`."""
    transfer = get_transfer(token)
    if not transfer:
        await message.answer("❌ Ссылка на передачу прав недействительна.")
        return

    from_uid = transfer.get("from_user_id")
    to_uid = msg_uid(message)

    if to_uid == from_uid:
        await message.answer("⚠️ Ты не можешь передать права самому себе.")
        return
    if is_registry_user_banned(to_uid) and not is_super_admin(to_uid):
        await message.answer("🚫 Вы заблокированы администрацией.")
        return

    lines = _rights_lines_from(transfer)

    # Регистрируем нового владельца в реестре.
    if not get_user_registry(to_uid):
        from services.storage import register_user
        register_user(to_uid, msg_username(message) or "", msg_firstname(message) or "")

    text = (
        f"👑 <b>Вам передают права!</b>\n\n"
        f"Пользователь <b>{_user_display(int(from_uid or 0))}</b> передаёт вам следующие права:\n"
        + "\n".join(lines)
        + "\n\nПодтверди принятие, чтобы данные перешли к тебе навсегда."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"transfer_accept_{token}", style="success")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"transfer_reject_{token}", style="danger")],
    ])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("transfer_accept_"))
async def cb_transfer_accept(callback: CallbackQuery,
                             child_manager: ChildManager) -> None:
    token = cb_data(callback).split("transfer_accept_", 1)[1]
    transfer = get_transfer(token)
    if not transfer:
        await callback.answer("⚠️ Ссылка уже недействительна.", show_alert=True)
        return

    from_uid = transfer.get("from_user_id")
    to_uid = cb_uid(callback)
    kind = transfer.get("kind")

    if to_uid == from_uid:
        await callback.answer("⚠️ Нельзя принять у самого себя.", show_alert=True)
        return

    username = cb_username(callback) or ""
    first_name = cb_firstname(callback) or ""

    if kind == "bot":
        bot_id = int(transfer.get("bot_id") or 0)
        ok = transfer_bot(int(from_uid or 0), to_uid, bot_id, username, first_name)
        if not ok:
            await callback.answer("⚠️ Не удалось передать бота.", show_alert=True)
            return
        bot = get_bot_by_id_any_owner(bot_id)
        bot_name = bot_display_name(bot) if bot else f"бот {bot_id}"
        rights_text = f"• {bot_name}"
        summary = f"🤖 Теперь бот <b>{bot_name}</b> принадлежит тебе."
    else:
        count = transfer_all_rights(int(from_uid or 0), to_uid, username, first_name)
        rights_text = "все права"
        summary = f"👑 <b>Все права приняты!</b>\n\nПередано ботов: <b>{count}</b>."

    # Перезапускаем переданные дочерние боты, чтобы новый владелец сразу получил
    # актуальные настройки (приветствие, кнопки, анонимность) без ручного рестарта.
    restart_count = 0
    try:
        if kind == "bot":
            bot = get_bot_by_id_any_owner(bot_id)
            if bot and child_manager.is_running(bot_id):
                if await child_manager.restart_child(bot):
                    restart_count += 1
        else:
            for b in get_user_bots(to_uid):
                if child_manager.is_running(b["id"]):
                    if await child_manager.restart_child(b):
                        restart_count += 1
    except Exception:
        pass

    if restart_count:
        summary += f"\n\n🔄 Перезапущено ботов: <b>{restart_count}</b>"

    delete_transfer(token)

    if callback.message:
        await try_edit_answer(
            callback.message,
            summary,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile_show")]
            ]),
        )

    # Уведомляем старого владельца.
    try:
        bot = getattr(callback, "bot", None)
        if bot is not None:
            await bot.send_message(
                int(from_uid or 0),
                f"🔁 <b>Права переданы.</b>\n\n"
                f"<b>{_user_display(to_uid)}</b> принял ваши права: {rights_text}.",
            )
    except Exception:
        pass


@router.callback_query(F.data.startswith("transfer_reject_"))
async def cb_transfer_reject(callback: CallbackQuery) -> None:
    token = cb_data(callback).split("transfer_reject_", 1)[1]
    transfer = get_transfer(token)
    delete_transfer(token)

    if callback.message:
        await try_edit_answer(callback.message, "❌ <b>Вы отклонили передачу прав.</b>")

    if transfer:
        try:
            bot = getattr(callback, "bot", None)
            if bot is not None:
                await bot.send_message(
                    int(transfer.get("from_user_id") or 0),
                    "❌ Новый владелец отклонил передачу прав.",
                )
        except Exception:
            pass


# ═══════════════ Админ-панель пользователей ═══════════════

@router.callback_query(F.data == "profile_admin")
async def cb_profile_admin(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await render_callback(callback, "🛡 <b>Админ-панель</b>\n\nВыбери раздел:", admin_kb())


# ═══════════════ Настройки ссылок для «🟢 Прочее» (только супер-админ) ═══════════════

# Ключ параметра → (ключ в app_settings, заголовок, подсказка).
_LINK_FIELDS: dict[str, tuple[str, str, str]] = {
    "yamochan": (
        SETTING_YAMOCHAN_URL,
        "🤖 Проект YamoChan",
        "Ссылка на проект YamoChan. Появится кнопкой на экране «Проект YamoChan» "
        "в разделе «🟢 Прочее».",
    ),
    "donate": (
        SETTING_DONATE_URL,
        "💚 Поддержать проект (донат)",
        "Ссылка на донат. По ней ведёт кнопка «💚 Задонатить» в разделе "
        "«🟢 Прочее».",
    ),
    "testbot": (
        SETTING_TEST_BOT_URL,
        "🧪 Тестовый бот",
        "Ссылка на тестового бота. Используется кнопкой «➡️ Перейти» "
        "в разделе «✨ Прочее».",
    ),
}


def _links_settings_text() -> str:
    """Экран «Настройки ссылок»: текущие значения и подсказка."""
    lines = [
        "🔗 <b>Настройки ссылок</b>",
        "",
        "Здесь задаются ссылки для раздела «🟢 Прочее». Если ссылка не задана, "
        "кнопки в разделе покажут подсказку вместо перехода.",
        "",
    ]
    for key, (setting_key, title, _hint) in _LINK_FIELDS.items():
        value = get_app_setting(setting_key)
        shown = f"<code>{value}</code>" if value else "— не задана —"
        lines.append(f"{title}: {shown}")
    lines += ["", "Выбери, что изменить 👇"]
    return "\n".join(lines)


def links_settings_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, (_setting_key, title, _hint) in _LINK_FIELDS.items():
        rows.append([
            InlineKeyboardButton(text=f"✏️ {title}", callback_data=f"links_set_{key}",
                                 style="primary"),
            InlineKeyboardButton(text="🗑", callback_data=f"links_clear_{key}",
                                 style="danger"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="profile_admin",
                                      style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "links_settings")
async def cb_links_settings(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await render_callback(callback, _links_settings_text(), links_settings_kb())


@router.callback_query(F.data.startswith("links_set_"))
async def cb_links_set(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    key = cb_data(callback).split("links_set_", 1)[1]
    field = _LINK_FIELDS.get(key)
    if field is None:
        await callback.answer("⚠️ Неизвестный параметр", show_alert=True)
        return

    setting_key, title, hint = field
    await state.set_state(LinksFSM.waiting_link)
    await state.update_data(link_setting_key=setting_key, link_title=title)

    current = get_app_setting(setting_key)
    current_line = f"<code>{current}</code>" if current else "— не задана —"
    text = (
        f"✏️ <b>{title}</b>\n\n"
        f"{hint}\n\n"
        f"Сейчас: {current_line}\n\n"
        "Отправь новую ссылку одним сообщением: полной (https://…), "
        "короткой (t.me/…) или как @username."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="links_settings",
                              style="primary")]
    ])
    await render_callback(callback, text, kb)


@router.callback_query(F.data.startswith("links_clear_"))
async def cb_links_clear(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    key = cb_data(callback).split("links_clear_", 1)[1]
    field = _LINK_FIELDS.get(key)
    if field is None:
        await callback.answer("⚠️ Неизвестный параметр", show_alert=True)
        return

    setting_key, title, _hint = field
    set_app_setting(setting_key, "")
    await state.clear()
    await render_callback(callback, f"🗑 <b>{title}</b>: ссылка очищена.\n\n"
                                    + _links_settings_text(),
                           links_settings_kb())


@router.message(LinksFSM.waiting_link)
async def fsm_links_set(message: Message, state: FSMContext) -> None:
    """Сохраняет ссылку, введённую супер-админом."""
    user_id = msg_uid(message)
    if not is_super_admin(user_id):
        await state.clear()
        return

    data = await state.get_data()
    setting_key = str(data.get("link_setting_key") or "")
    title = str(data.get("link_title") or "")
    if not setting_key:
        await state.clear()
        await message.answer("⚠️ Не удалось определить параметр — открой настройки заново.")
        return

    from handlers.other import normalize_link

    link = normalize_link(message.text or "")
    if not link:
        await message.answer(
            "❌ Не похоже на ссылку.\n\n"
            "Отправь её ещё раз: полной (https://…), короткой (t.me/…) "
            "или как @username."
        )
        return

    set_app_setting(setting_key, link)
    await state.clear()
    await message.answer(
        f"✅ <b>{title}</b>: ссылка сохранена.\n\n{_links_settings_text()}",
        reply_markup=links_settings_kb(),
    )


@router.callback_query(F.data == "profiles_list")
async def cb_profiles_list(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    all_users = get_all_users_registry()
    owners = [int(u["user_id"]) for u in all_users if get_user_bots(u["user_id"])]
    # Админов считаем уникальными: один человек может быть админом у нескольких
    # владельцев (и раньше в счётчик попадали боты, а не админы).
    admin_ids: set[int] = set()
    for owner_id in owners:
        admin_ids.update(int(a["user_id"]) for a in get_admins_all(owner_id))
    text = (
        "👥 <b>Профили пользователей</b>\n\n"
        f"👤 Всего пользователей: <b>{len(all_users)}</b>\n"
        f"🤖 ВЛД (владельцы ботов): <b>{len(owners)}</b>\n"
        f"👥 Админов у владельцев: <b>{len(admin_ids)}</b>\n\n"
        "Выбери категорию:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 ВЛД (владельцы)", callback_data="profiles_vld", style="primary")],
        [InlineKeyboardButton(text="👥 Админы (по ботам)", callback_data="profiles_admins_bots", style="primary")],
        [InlineKeyboardButton(text="🧟 Мёртвые боты", callback_data="dead_bots", style="danger")],
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="profile_admin", style="primary")],
    ])
    await render_callback(callback, text, kb)


# ═══════════════ ВЛД — владельцы (у кого есть хотя бы один бот) ═══════════════

def _vld_kb(users: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Клавиатура списка ВЛД (постранично, компактные подписи)."""
    rows: list[list[InlineKeyboardButton]] = []
    for u in users:
        name = u.get("username") or u.get("first_name") or str(u["user_id"])
        status = "🚫" if u.get("blocked") else "🟢"
        rows.append([InlineKeyboardButton(
            text=_short(f"{status} {name} ⸱ 🤖 {len(get_user_bots(u['user_id']))}"),
            callback_data=f"profile_view_{u['user_id']}",
        )])
    rows.extend(_pager_rows("profiles_vld_p", page, total_pages))
    rows.append([InlineKeyboardButton(text="⬅️ Категории", callback_data="profiles_list", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("profiles_vld"))
async def cb_profiles_vld(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    vld = [u for u in get_all_users_registry() if get_user_bots(u["user_id"])]
    if not vld:
        await render_callback(
            callback, "🤖 <b>ВЛД</b>\n\nПока нет владельцев с ботами.", admin_kb()
        )
        return
    page = _parse_page(cb_data(callback), "profiles_vld_p")
    window, page, total_pages = _page_slice(vld, page)
    text = (
        f"🤖 <b>ВЛД — владельцы</b> ({len(vld)})\n\n"
        f"Выбери владельца:{_page_note(page, total_pages)}"
    )
    await render_callback(callback, text, _vld_kb(window, page, total_pages))


@router.callback_query(F.data.startswith("profile_view_"))
async def cb_profile_view(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(cb_data(callback).split("_")[-1])
    users = [u for u in get_all_users_registry() if u["user_id"] == uid]
    if not users:
        await callback.answer("Пользователь не найден")
        return
    u = users[0]
    bots = get_user_bots(uid)
    status = "🚫 заблокирован" if u.get("blocked") else "🟢 активен"
    text = (
        f"👤 <b>{u.get('username') or u.get('first_name') or uid}</b>\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📅 Регистрация: {utc_to_msk(u.get('created_at'))[:10]}\n"
        f"🤖 Ботов: <b>{len(bots)}</b>\n"
        f"📌 Статус: {status}"
    )
    await render_callback(callback, text, profile_admin_kb(uid))


@router.callback_query(F.data.startswith("profile_ban_"))
async def cb_profile_ban(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(cb_data(callback).split("_")[-1])
    set_registry_user_blocked(uid, True)
    await render_callback(callback, f"🚫 Пользователь <code>{uid}</code> забанен.", profile_admin_kb(uid))


@router.callback_query(F.data.startswith("profile_unban_"))
async def cb_profile_unban(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(cb_data(callback).split("_")[-1])
    set_registry_user_blocked(uid, False)
    await render_callback(callback, f"✅ Пользователь <code>{uid}</code> разбанен.", profile_admin_kb(uid))


# ═══════════════ Админы (по ботам) ═══════════════

def _owner_name(owner_id: int) -> str:
    """Короткое имя владельца для подписи кнопки (username, имя или ID)."""
    if not owner_id:
        return "—"
    u = get_user_registry(owner_id)
    if u:
        return u.get("username") or u.get("first_name") or f"ID:{owner_id}"
    return f"ID:{owner_id}"


def _bots_admins_kb(bots: list[dict], page: int,
                    total_pages: int) -> InlineKeyboardMarkup:
    """Список ботов с числом админов их владельца (постранично, компактно).

    Админы в панели — «на владельца» (работают со всеми его ботами), поэтому
    у ботов одного владельца число админов одинаковое: это и подписываем.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for b in bots:
        owner_id = int(b.get("owner_id") or 0)
        admins = get_admins_all(owner_id) if owner_id else []
        bot_name = b.get("first_name") or b.get("username") or f"bot_{b['id']}"
        label = f"{bot_name} ⸱ 👤 {_owner_name(owner_id)} ⸱ 👥 {len(admins)}"
        rows.append([InlineKeyboardButton(
            text=_short(label),
            callback_data=f"profiles_a_bot_{b['id']}",
        )])
    rows.extend(_pager_rows("profiles_admins_bots_p", page, total_pages))
    rows.append([InlineKeyboardButton(text="⬅️ Категории", callback_data="profiles_list", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("profiles_admins_bots"))
async def cb_profiles_admins_bots(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    bots = get_all_bots_flat()
    if not bots:
        await render_callback(callback, "👥 <b>Админы</b>\n\nБотов пока нет.", admin_kb())
        return

    # Считаем админов один раз на владельца — иначе для каждого бота это был бы
    # отдельный запрос, и на большом списке вкладка открывалась бы долго.
    admins_total = 0
    for owner_id in {int(b.get("owner_id") or 0) for b in bots}:
        if owner_id:
            admins_total += len(get_admins_all(owner_id))

    page = _parse_page(cb_data(callback), "profiles_admins_bots_p")
    window, page, total_pages = _page_slice(bots, page)
    text = (
        f"👥 <b>Админы — по ботам</b>\n\n"
        f"🤖 Ботов: <b>{len(bots)}</b> ⸱ 👥 Админов у владельцев: "
        f"<b>{admins_total}</b>\n\n"
        f"Выбери бот:{_page_note(page, total_pages)}"
    )
    await render_callback(callback, text, _bots_admins_kb(window, page, total_pages))


def _bot_admins_kb(bot_id: int, owner_id: int, admins: list[dict],
                   page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Карточки админов владельца бота: профиль · бан · удалить (постранично)."""
    rows: list[list[InlineKeyboardButton]] = []
    for a in admins:
        uname = f"@{a['username']}" if a.get("username") else f"ID:{a['user_id']}"
        banned = is_registry_user_banned(a["user_id"])
        ban_label = "✅" if banned else "🚫"
        ban_data = f"profiles_a_unban_{a['user_id']}" if banned else f"profiles_a_ban_{a['user_id']}"
        rows.append([
            InlineKeyboardButton(
                text=_short(f"👤 #{a['tag']} ⸱ {uname}"),
                callback_data=f"profile_view_{a['user_id']}",
            ),
            InlineKeyboardButton(text=ban_label, callback_data=ban_data,
                                 style=("success" if banned else "danger")),
            InlineKeyboardButton(text="🗑",
                                 callback_data=f"profiles_a_del_{owner_id}_{a['user_id']}",
                                 style="danger"),
        ])
    rows.extend(_pager_rows(f"profiles_a_bot_{bot_id}_p", page, total_pages))
    rows.append([InlineKeyboardButton(text="⬅️ Список ботов", callback_data="profiles_admins_bots", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(r"^profiles_a_bot_\d+(_p\d+)?$"))
async def cb_profiles_a_bot(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    raw = cb_data(callback)
    head, _, page_tail = raw.partition("_p")
    bot_id = int(head.split("_")[-1])
    page = int(page_tail) if page_tail.isdigit() else 1

    bot = get_bot_by_id_any_owner(bot_id)
    if not bot:
        await callback.answer("Бот не найден")
        return
    owner_id = int(bot.get("owner_id") or 0)
    admins = get_admins_all(owner_id)
    if not admins:
        await render_callback(
            callback,
            f"👥 <b>Админы — {bot_display_name(bot)}</b> (🆔 {bot_id})\n\n"
            "У владельца нет админов.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Список ботов", callback_data="profiles_admins_bots", style="primary")]
            ]),
        )
        return

    window, page, total_pages = _page_slice(admins, page, ADMINS_PAGE_SIZE)
    text = (
        f"👥 <b>Админы — {bot_display_name(bot)}</b> (🆔 <code>{bot_id}</code>)\n\n"
        f"👤 Владелец: <code>{owner_id}</code> ⸱ 👥 Админов: <b>{len(admins)}</b>\n\n"
        f"Выбери действие:{_page_note(page, total_pages)}"
    )
    await render_callback(callback, text, _bot_admins_kb(bot_id, owner_id, window, page, total_pages))


@router.callback_query(F.data.regexp(r"^profiles_a_del_\d+_\d+$"))
async def cb_profiles_a_del(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = cb_data(callback).split("_")
    owner_id = int(parts[3])
    admin_uid = int(parts[4])
    remove_admin(owner_id, admin_uid)
    await callback.answer("✅ Админ удалён")
    await cb_profiles_admins_bots(callback)


@router.callback_query(F.data.regexp(r"^profiles_a_ban_\d+$"))
async def cb_profiles_a_ban(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    admin_uid = int(cb_data(callback).split("_")[-1])
    set_registry_user_blocked(admin_uid, True)
    await callback.answer("🚫 Пользователь забанен")


@router.callback_query(F.data.regexp(r"^profiles_a_unban_\d+$"))
async def cb_profiles_a_unban(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    admin_uid = int(cb_data(callback).split("_")[-1])
    set_registry_user_blocked(admin_uid, False)
    await callback.answer("✅ Пользователь разбанен")


@router.callback_query(F.data.startswith("profile_stats_"))
async def cb_profile_stats(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(cb_data(callback).split("_")[-1])
    bots = get_user_bots(uid)
    lines = []
    total = {"users_total": 0, "messages_in": 0, "messages_out": 0}
    for b in bots:
        s = get_stats(b["id"])
        for k in total:
            total[k] += s[k]
        lines.append(f"  • {bot_display_name(b)} — 👥 {s['users_total']}")
    bot_lines = "\n".join(lines) if lines else "  — нет ботов —"
    text = (
        f"📊 <b>Статистика пользователя</b> <code>{uid}</code>\n\n"
        f"👥 Всего пользователей: <b>{total['users_total']}</b>\n"
        f"📩 Получено: <b>{total['messages_in']}</b>\n"
        f"📤 Отправлено: <b>{total['messages_out']}</b>\n\n"
        f"{bot_lines}"
    )
    await render_callback(callback, text, profile_admin_kb(uid))


@router.callback_query(F.data.startswith("profile_del_bots_"))
async def cb_profile_del_bots(callback: CallbackQuery) -> None:
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    uid = int(cb_data(callback).split("_")[-1])
    bots = get_user_bots(uid)
    for b in bots:
        remove_user_bot(uid, b["id"])
    await render_callback(callback, f"🗑 Удалены все боты пользователя <code>{uid}</code>.", profile_admin_kb(uid))


# ═══════════════ Привязка «чата работы» и «чата админов» ═══════════════

_BIND_LABELS = {
    "work": "💼 Чат работы",
    "admin": "🛡 Чат админов",
}

# ═══════════════ «Привязать чаты» / «Чаты» (страницы профиля) ═══════════════

def _chats_bind_payload() -> tuple[str, InlineKeyboardMarkup]:
    """Инструкция по привязке обоих чатов + кнопки выбора чата."""
    text = (
        "🔗 <b>Привязка чатов</b>\n\n"
        "YamoBot работает с двумя чатами:\n\n"
        "💼 <b>Чат работы</b> — чат с дочерним ботом, где админы общаются "
        "с пользователями по заявкам (ПЗ).\n"
        "🛡 <b>Чат админов</b> — общий чат админов, где они переписываются "
        "между собой.\n\n"
        "<b>Как привязать:</b>\n"
        "1️⃣ Добавь YamoBot в нужный чат.\n"
        "2️⃣ Нажми соответствующую кнопку ниже.\n"
        "3️⃣ Дождись подтверждения.\n\n"
        "Выбери, какой чат привязать:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Привязать чат админов", callback_data="bind_admin", style="primary")],
        [InlineKeyboardButton(text="💼 Привязать чат работы", callback_data="bind_work", style="primary")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")],
    ])
    return text, kb


def _chats_info_payload(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Инфо о привязанных чатах + привязка недостающего / отвязка / перезапуск."""
    work_chat = get_bound_chat(user_id, "work")
    admin_chat = get_bound_chat(user_id, "admin")
    work_line = f"<code>{work_chat}</code>" if work_chat else "— не привязан —"
    admin_line = f"<code>{admin_chat}</code>" if admin_chat else "— не привязан —"

    text = (
        "📎 <b>Чаты</b>\n\n"
        f"💼 <b>Чат работы:</b> {work_line}\n"
        f"🛡 <b>Чат админов:</b> {admin_line}\n\n"
        "Кнопки ниже позволяют привязать недостающий чат, отвязать "
        "привязанные или перезапустить привязку."
    )

    rows: list[list[InlineKeyboardButton]] = []
    # Непривязанные чаты предлагаем привязать прямо отсюда.
    if admin_chat is None:
        rows.append([
            InlineKeyboardButton(text="🛡 Привязать чат админов", callback_data="bind_admin", style="primary")
        ])
    if work_chat is None:
        rows.append([
            InlineKeyboardButton(text="💼 Привязать чат работы", callback_data="bind_work", style="primary")
        ])
    # Привязанные чаты можно отвязать.
    if admin_chat:
        rows.append([
            InlineKeyboardButton(text="❌ Отвязать чат админов", callback_data="unbind_admin", style="danger")
        ])
    if work_chat:
        rows.append([
            InlineKeyboardButton(text="❌ Отвязать чат работы", callback_data="unbind_work", style="danger")
        ])
    rows.append([InlineKeyboardButton(text="🔄 Перезапуск", callback_data="chats_restart", style="primary")])
    rows.append([InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")])

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "chats_bind")
async def cb_chats_bind(callback: CallbackQuery) -> None:
    text, kb = _chats_bind_payload()
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "chats_info")
async def cb_chats_info(callback: CallbackQuery) -> None:
    text, kb = _chats_info_payload(cb_uid(callback))
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "chats_restart")
async def cb_chats_restart(callback: CallbackQuery) -> None:
    """Перезапуск: перепривязывает бота к уже сохранённым чатам."""
    user_id = cb_uid(callback)
    work_chat = get_bound_chat(user_id, "work")
    admin_chat = get_bound_chat(user_id, "admin")

    set_bound_chat(user_id, "work", work_chat)
    set_bound_chat(user_id, "admin", admin_chat)

    statuses: list[str] = []
    bot = getattr(callback, "bot", None)

    if admin_chat:
        try:
            if bot is not None:
                await bot.send_message(admin_chat, ADMIN_CHAT_WELCOME)
            statuses.append("🛡 Чат админов: перепривязан, приветствие отправлено")
        except Exception:
            statuses.append("🛡 Чат админов: перепривязан (не удалось отправить приветствие)")
    if work_chat:
        try:
            if bot is not None:
                await bot.send_message(
                    work_chat, "💼 Чат работы привязан к YamoBot. Бот активен. ✅"
                )
            statuses.append("💼 Чат работы: перепривязан")
        except Exception:
            statuses.append("💼 Чат работы: привязка сохранена (бот не в чате)")

    status_text = "\n".join(statuses) if statuses else "Чат ещё не привязан."
    text, kb = _chats_info_payload(user_id)
    text = f"🔄 <b>Перезапуск привязки</b>\n\n{status_text}\n\n{text}"

    await render_callback(callback, text, kb)


def _bind_wait_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я добавил бота", callback_data="bind_done", style="success")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile_show")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="bind_cancel")],
    ])


async def _bind_instructions(callback: CallbackQuery, kind: str) -> str:
    try:
        bot = getattr(callback, "bot", None)
        me = await bot.get_me() if bot is not None else None
        bot_ref = f"@{me.username}" if me and me.username else "бота"
    except Exception:
        bot_ref = "бота"

    label = _BIND_LABELS[kind]
    if kind == "work":
        tail = "После привязки бот запомнит ID чата и <b>покинет</b> его."
    else:
        tail = "После привязки бот запомнит ID чата и <b>останется</b> в нём — "
        tail += "сюда будут приходить уведомления о новых ПЗ."

    admin_note = ""
    if kind == "admin":
        admin_note = (
            "3. Выдай боту <b>права администратора</b> в этом чате — иначе "
            "он не сможет в полной мере работать с уведомлениями.\n"
        )

    return (
        f"📌 <b>{label}</b>\n\n"
        f"1. Добавь <b>{bot_ref}</b> в групповой чат, который хочешь "
        f"использовать как «{label}».\n"
        f"2. Дождись подтверждения привязки.\n"
        f"{admin_note}\n"
        f"{tail}"
    )


@router.callback_query(F.data.in_({"bind_work", "bind_admin"}))
async def cb_bind_start(callback: CallbackQuery) -> None:
    kind = "work" if callback.data == "bind_work" else "admin"
    _PENDING_BINDS[cb_uid(callback)] = kind
    set_pending_bind(cb_uid(callback), kind)  # в БД — переживает рестарт бота
    text = await _bind_instructions(callback, kind)
    await render_callback(callback, text, _bind_wait_kb())


@router.callback_query(F.data == "bind_done")
async def cb_bind_done(callback: CallbackQuery) -> None:
    """Пользователь сообщил, что добавил бота. Привязываем сами, если событие не пришло."""
    user_id = cb_uid(callback)
    kind = get_pending_bind(user_id) or _PENDING_BINDS.get(user_id)

    # Если бот ещё ждёт привязку — попробуем привязать последний добавленный чат.
    if kind:
        chat_id = _LAST_ADDED.get(user_id)
        if chat_id:
            # Защита от путаницы: нельзя привязать «чат админов» как «чат работы»
            # (и наоборот) и тем более выйти из нужного чата. Иначе при отвязке/
            # привязке одного чата бот мог «уходить» из другого.
            other = "admin" if kind == "work" else "work"
            other_bound = get_bound_chat(user_id, other)
            if other_bound and chat_id == other_bound:
                set_pending_bind(user_id, None)
                await callback.answer(
                    "⚠️ Этот чат уже привязан как другой тип. Добавь бота в новый чат.",
                    show_alert=True,
                )
                return

            _PENDING_BINDS.pop(user_id, None)
            set_pending_bind(user_id, None)
            set_bound_chat(user_id, kind, chat_id)
            if kind == "work":
                # Чат работы — бот запоминает и покидает его.
                bot = getattr(callback, "bot", None)
                try:
                    if bot is not None:
                        await bot.leave_chat(chat_id)
                except Exception:
                    pass
            await callback.answer("✅ Привязано!")
            text, kb = _profile_payload(user_id, cb_firstname(callback) or "—")
            await render_callback(callback, text, kb)
        else:
            # Бот пока не видит добавление — короткое уведомление, без повтора инструкции.
            await callback.answer("⏳ Добавь бота в чат, затем нажми ещё раз")
            return
    else:
        # Уже привязано через событие — просто открываем профиль.
        await callback.answer()
        text, kb = _profile_payload(user_id, cb_firstname(callback) or "—")
        await render_callback(callback, text, kb)


@router.callback_query(F.data == "bind_cancel")
async def cb_bind_cancel(callback: CallbackQuery) -> None:
    _PENDING_BINDS.pop(cb_uid(callback), None)
    set_pending_bind(cb_uid(callback), None)
    await callback.answer("❌ Привязка отменена")
    if callback.message:
        text, kb = _profile_payload(cb_uid(callback), cb_firstname(callback) or "—")
        await render_callback(callback, text, kb)


@router.callback_query(F.data == "unbind_work")
async def cb_unbind_work(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    if get_bound_chat(user_id, "work") is None:
        await callback.answer("💼 Чат работы уже не привязан", show_alert=False)
    else:
        set_bound_chat(user_id, "work", None)
        await callback.answer("💼 Чат работы отвязан")
    text, kb = _chats_info_payload(user_id)
    await render_callback(callback, text, kb)


@router.callback_query(F.data == "unbind_admin")
async def cb_unbind_admin(callback: CallbackQuery) -> None:
    user_id = cb_uid(callback)
    chat_id = get_bound_chat(user_id, "admin")
    if chat_id is None:
        await callback.answer("🛡 Чат админов уже не привязан", show_alert=False)
    else:
        set_bound_chat(user_id, "admin", None)
        bot = getattr(callback, "bot", None)
        try:
            if bot is not None:
                await bot.leave_chat(chat_id)
        except Exception:
            pass
        await callback.answer("🛡 Чат админов отвязан")
    text, kb = _chats_info_payload(user_id)
    await render_callback(callback, text, kb)


# Событие: YamoBot добавили в группу/супергруппу.
@router.my_chat_member()
async def on_bot_added_to_chat(event) -> None:
    adder = getattr(event, "from_user", None)
    if adder is not None and not getattr(adder, "is_bot", False):
        # Запоминаем последний чат, куда добавили бота (для кнопки «я добавил бота»).
        chat = event.chat
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            _LAST_ADDED[adder.id] = chat.id

    adder = getattr(event, "from_user", None)
    if adder is None or getattr(adder, "is_bot", False):
        return

    # Антирейд: если бота повысили до администратора в уже привязанном
    # «чате админов» с включённой защитой — уведомляем владельца о том,
    # что защита теперь полностью работает (иначе бот не видит заходы и
    # сообщения, а владелец думает, что «антирейд сломан»).
    if event.chat and event.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        from handlers.antiraid import notify_antiraid_promoted_if_bound
        await notify_antiraid_promoted_if_bound(event)

    kind = get_pending_bind(adder.id) or _PENDING_BINDS.pop(adder.id, None)
    if not kind:
        return

    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        # Если это не группа — оставляем ожидание в БД, чтобы не потерять запрос.
        return

    new_status = getattr(event.new_chat_member, "status", None)
    old_status = getattr(event.old_chat_member, "status", None)
    was_member = old_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    is_member = new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    if was_member or not is_member:
        return

    # Какой бы чат ни добавил бота — это и есть привязываемый чат, он точный.
    set_bound_chat(adder.id, kind, chat.id)
    set_pending_bind(adder.id, None)

    bot = event.bot
    chat_name = chat.title or f"чат {chat.id}"
    if kind == "work":
        try:
            await bot.send_message(
                chat.id,
                "💼 Чат работы привязан. YamoBot запомнил его и покидает чат. 👋",
            )
        except Exception:
            pass
        try:
            await bot.leave_chat(chat.id)
        except Exception:
            pass
        confirm_text = (
            f"✅ <b>Чат работы привязан!</b>\n\n"
            f"📎 Чат: <b>{chat_name}</b>\n"
            f"🆔 ID: <code>{chat.id}</code>\n\n"
            f"Бот запомнил чат и вышел из него."
        )
    else:
        welcome_admin = ADMIN_CHAT_WELCOME
        try:
            await bot.send_message(chat.id, welcome_admin)
        except Exception:
            pass
        confirm_text = (
            f"✅ <b>Чат админов привязан!</b>\n\n"
            f"📎 Чат: <b>{chat_name}</b>\n"
            f"🆔 ID: <code>{chat.id}</code>\n\n"
            f"⚠️ <b>Выдай боту права администратора</b> в этом чате — "
            f"иначе он не сможет в полной мере работать с уведомлениями.\n"
            f"Сделай это через: «Управление чатом → Администраторы → YamoBot → "
            f"Назначить администратором».\n\n"
            f"Теперь сюда будут приходить уведомления о новых ПЗ. "
            f"Я отправил в чат приветствие со списком команд."
        )

    try:
        await bot.send_message(adder.id, confirm_text)
    except Exception:
        pass

