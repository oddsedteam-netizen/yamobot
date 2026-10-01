"""Раздел «🏆 Рейтинг»: топ админов и топ ботов.

Один экран с двумя вкладками (админы / боты) и переключателем между ними.
Рейтинг считается заново при каждом открытии — см. ``services.db.rating``,
там же объяснено, почему кэшировать его нельзя.

Что видно в топе
----------------
* админы — тегом (без юза и ID) и активностью: ответы ПЗ и сколько
  обращений за ним закреплено сейчас;
* боты — ``@username``, количеством обращений и числом ответов на них.

Кнопка «🚫 Вне рейтинга» убирает из топа самого пользователя (как админа) и
его ботов: светиться в общем топе — право, а не обязанность.
"""

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from handlers._common import cb_data, cb_uid, html_escape, render_callback
from services.config import is_super_admin
from services.storage import (
    RATING_ADMIN,
    RATING_BOT,
    RATING_LIMIT,
    admin_rating,
    bot_rating,
    get_manual_hides,
    get_rating_optouts,
    get_user_bots,
    is_hidden_from_rating,
    set_manual_hide,
    set_rating_optout,
)

router = Router()

# Вкладки рейтинга.
TAB_ADMINS = "admins"
TAB_BOTS = "bots"

# Медали для первых трёх мест.
_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _medal(place: int) -> str:
    return _MEDALS.get(place, f"{place}.")


def admins_lines() -> list[str]:
    """Строки топа админов."""
    rows = admin_rating(RATING_LIMIT)
    if not rows:
        return ["🏆 <b>Рейтинг админов</b>\n\n"
                "Пока пусто: рейтинг появляется, когда админы начнут "
                "отвечать на обращения."]
    lines = ["👨‍💼 <b>Топ админов</b> по активности\n"]
    for place, row in enumerate(rows, start=1):
        lines.append(
            f"\n{_medal(place)} <b>#{html_escape(row['tag'])}</b>"
            f" — 💬 ответов: <b>{row['replies']}</b>"
            f" · 📋 ведёт ПЗ: <b>{row['pz_current']}</b>"
        )
    if len(rows) == RATING_LIMIT:
        lines.append(f"\n\n<i>Показаны первые {RATING_LIMIT}.</i>")
    return lines


def bots_lines() -> list[str]:
    """Строки топа ботов."""
    rows = bot_rating(RATING_LIMIT)
    if not rows:
        return ["🤖 <b>Рейтинг ботов</b>\n\n"
                "Пока пусто: сюда попадают боты, в которых уже были "
                "обращения."]
    lines = ["🤖 <b>Топ ботов</b> по количеству обращений\n"]
    for place, row in enumerate(rows, start=1):
        username = f"@{html_escape(row['username'])}"
        lines.append(
            f"\n{_medal(place)} <b>{username}</b>"
            f" — 📋 ПЗ: <b>{row['pz_count']}</b>"
            f" · 💬 ответов админов: <b>{row['replies']}</b>"
        )
    if len(rows) == RATING_LIMIT:
        lines.append(f"\n\n<i>Показаны первые {RATING_LIMIT}.</i>")
    return lines


def rating_kb(user_id: int, tab: str) -> InlineKeyboardMarkup:
    """Переключатель вкладок + кнопки «вне рейтинга» и «назад»."""
    self_hidden = is_hidden_from_rating(RATING_ADMIN, user_id)
    hidden_bots = get_rating_optouts(RATING_BOT)
    bots = get_user_bots(user_id)
    # Переключатель показываем «скрыты», только если скрыты ВСЕ боты: иначе
    # для одного скрытого и трёх видимых надпись вводила бы в заблуждение.
    own_bots_hidden = bool(bots) and all(int(b["id"]) in hidden_bots for b in bots)

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="👨‍💼 Админы",
                              callback_data=f"rating_tab_{TAB_ADMINS}",
                              style="success" if tab == TAB_ADMINS else "primary"),
         InlineKeyboardButton(text="🤖 Боты",
                              callback_data=f"rating_tab_{TAB_BOTS}",
                              style="success" if tab == TAB_BOTS else "primary")],
        [InlineKeyboardButton(
            text="🔙 Вернуть меня в рейтинг" if self_hidden else "🚫 Вне рейтинга (я)",
            callback_data="rating_out_me", style="primary")],
        [InlineKeyboardButton(
            text="👁 Показать моих ботов" if own_bots_hidden
            else "🙈 Скрыть моих ботов",
            callback_data="rating_out_bots", style="primary")],
        [InlineKeyboardButton(text="⬅️ YID", callback_data="yid_card",
                              style="primary")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "yid_rating")
async def cb_yid_rating(callback: CallbackQuery) -> None:
    """Открывает рейтинг (по умолчанию — вкладка админов)."""
    await render_callback(callback, "\n".join(admins_lines()),
                          rating_kb(cb_uid(callback), TAB_ADMINS))


@router.callback_query(F.data == "rating_out_me")
async def cb_rating_out_me(callback: CallbackQuery) -> None:
    """«🚫 Вне рейтинга» для самого пользователя (как админа).

    Тумблер, а не односторонняя кнопка: нажать второй раз — вернуться в топ.
    Иначе скрыться можно было бы навсегда и без возможности вернуться.
    """
    user_id = cb_uid(callback)
    hidden = is_hidden_from_rating(RATING_ADMIN, user_id)
    set_rating_optout(RATING_ADMIN, user_id, not hidden)

    if hidden:
        await callback.answer("✅ Ты снова в рейтинге")
    else:
        await callback.answer(
            "🙈 Ты убран из рейтинга админов.\n\n"
            "Твои цифры по-прежнему видны тебе в карточке YID.",
            show_alert=True,
        )
    await render_callback(callback, "\n".join(admins_lines()),
                          rating_kb(user_id, TAB_ADMINS))


@router.callback_query(F.data == "rating_out_bots")
async def cb_rating_out_bots(callback: CallbackQuery) -> None:
    """Скрывает/возвращает в рейтинг ботов пользователя.

    Переключаем ВСЕХ своих ботов разом: у людей их обычно один-два, а по
    одному переключать — лишние клики без выигрыша. Анкетницы в рейтинг не
    попадают и так, поэтому на них переключатель не влияет.
    """
    user_id = cb_uid(callback)
    bots = get_user_bots(user_id)
    if not bots:
        await callback.answer("У тебя нет ботов", show_alert=True)
        return

    hidden_bots = get_rating_optouts(RATING_BOT)
    now_hidden = not all(int(b["id"]) in hidden_bots for b in bots)
    for bot in bots:
        set_rating_optout(RATING_BOT, int(bot["id"]), now_hidden)

    if now_hidden:
        await callback.answer(
            f"🙈 {len(bots)} бот(а) скрыто из рейтинга.\n\n"
            "Боты-анкетницы в рейтинг не попадают никогда.",
            show_alert=True,
        )
    else:
        await callback.answer("✅ Боты снова в рейтинге")
    await render_callback(callback, "\n".join(bots_lines()),
                          rating_kb(user_id, TAB_BOTS))
@router.callback_query(F.data.regexp(r"^rating_tab_(admins|bots)$"))
async def cb_rating_tab(callback: CallbackQuery) -> None:
    """Переключение между топами админов и ботов."""
    tab = cb_data(callback).rsplit("_", 1)[-1]
    lines = bots_lines() if tab == TAB_BOTS else admins_lines()
    await render_callback(callback, "\n".join(lines),
                          rating_kb(cb_uid(callback), tab))


# ═══════════════ Управление рейтингом из админ-панели ═══════════════
#
# Зачем
# -----
# Владельцу платформы нужно убрать конкретного админа или бота из топа —
# например, за спам или потому, что человек не хочет там светиться, а сам
# «Вне рейтинга» нажать не догадывается.
#
# Чем это отличается от «Вне рейтинга» у пользователя
# ---------------------------------------------------
# Его кнопка пишет в ``rating_optout`` — это ЕГО добровольный отказ, и он
# может его отменить. Здесь пишем в ``rating_manual_hide``: решение
# администратора, которое кнопка «Вернуть меня в рейтинг» не снимает.
# Иначе «убрать» и «запретить вернуться» были бы неразличимы.

TAB_ADMIN_MANAGE = "admins"
TAB_BOT_MANAGE = "bots"


def admin_rating_admin_rows() -> list[list[InlineKeyboardButton]]:
    """Кнопки «убрать/вернуть» под каждым админом топа."""
    manual = get_manual_hides(RATING_ADMIN)
    rows: list[list[InlineKeyboardButton]] = []
    for row in admin_rating(RATING_LIMIT * 2):
        user_id = int(row["user_id"])
        tag = html_escape(row["tag"])
        if user_id in manual:
            btn = InlineKeyboardButton(
                text=f"↩️ Вернуть #{tag}",
                callback_data=f"ratingadm_unhide_{user_id}", style="success")
        else:
            btn = InlineKeyboardButton(
                text=f"🚫 Убрать #{tag}",
                callback_data=f"ratingadm_hide_{user_id}", style="danger")
        rows.append([btn])
    return rows


def bot_rating_admin_rows() -> list[list[InlineKeyboardButton]]:
    """Кнопки «убрать/вернуть» под каждым ботом топа."""
    manual = get_manual_hides(RATING_BOT)
    rows: list[list[InlineKeyboardButton]] = []
    for row in bot_rating(RATING_LIMIT * 2):
        bot_id = int(row["bot_id"])
        name = html_escape(str(row["username"]))
        if bot_id in manual:
            btn = InlineKeyboardButton(
                text=f"↩️ Вернуть {name}",
                callback_data=f"ratingbot_unhide_{bot_id}", style="success")
        else:
            btn = InlineKeyboardButton(
                text=f"🚫 Убрать {name}",
                callback_data=f"ratingbot_hide_{bot_id}", style="danger")
        rows.append([btn])
    return rows


def _admin_rating_kb(tab: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(
            text="👨‍💼 Админы", callback_data="ratingadm_tab_admins",
            style="success" if tab == TAB_ADMIN_MANAGE else "primary"),
        InlineKeyboardButton(
            text="🤖 Боты", callback_data="ratingadm_tab_bots",
            style="success" if tab == TAB_BOT_MANAGE else "primary"),
    ]]
    rows += (admin_rating_admin_rows() if tab == TAB_ADMIN_MANAGE
             else bot_rating_admin_rows())
    rows.append([InlineKeyboardButton(text="⬅️ Админ-панель",
                                      callback_data="profile_admin",
                                      style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_admin_rating(callback: CallbackQuery, tab: str) -> None:
    """Экран «🏆 Рейтинг» из админ-панели."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    lines = (admins_lines() if tab == TAB_ADMIN_MANAGE else bots_lines())
    await render_callback(callback, "\n".join(lines), _admin_rating_kb(tab))


@router.callback_query(F.data == "rating_admin")
async def cb_rating_admin(callback: CallbackQuery) -> None:
    """«🏆 Рейтинг» в админ-панели: ручное управление топом."""
    await _render_admin_rating(callback, TAB_ADMIN_MANAGE)


@router.callback_query(F.data.regexp(r"^ratingadm_tab_(admins|bots)$"))
async def cb_rating_admin_tab(callback: CallbackQuery) -> None:
    """Переключение вкладок в админском рейтинге."""
    tab = cb_data(callback).rsplit("_", 1)[-1]
    await _render_admin_rating(callback, tab)


@router.callback_query(F.data.regexp(r"^ratingadm_hide_(\d+)$"))
async def cb_rating_admin_hide(callback: CallbackQuery) -> None:
    """Убирает админа из рейтинга — до решения владельца он не вернётся."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    admin_id = int(cb_data(callback).rsplit("_", 1)[-1])
    set_manual_hide(RATING_ADMIN, admin_id, True, hidden_by=cb_uid(callback))
    await callback.answer("🚫 Убран из рейтинга. Вернуть можно тут же.")
    await _render_admin_rating(callback, TAB_ADMIN_MANAGE)


@router.callback_query(F.data.regexp(r"^ratingadm_unhide_(\d+)$"))
async def cb_rating_admin_unhide(callback: CallbackQuery) -> None:
    """Возвращает админа в рейтинг."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    admin_id = int(cb_data(callback).rsplit("_", 1)[-1])
    set_manual_hide(RATING_ADMIN, admin_id, False)
    await callback.answer("✅ Снова в рейтинге")
    await _render_admin_rating(callback, TAB_ADMIN_MANAGE)


@router.callback_query(F.data.regexp(r"^ratingbot_hide_(\d+)$"))
async def cb_rating_bot_hide(callback: CallbackQuery) -> None:
    """Убирает бота из рейтинга — до решения владельца он не вернётся."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    set_manual_hide(RATING_BOT, bot_id, True, hidden_by=cb_uid(callback))
    await callback.answer("🚫 Убран из рейтинга. Вернуть можно тут же.")
    await _render_admin_rating(callback, TAB_BOT_MANAGE)


@router.callback_query(F.data.regexp(r"^ratingbot_unhide_(\d+)$"))
async def cb_rating_bot_unhide(callback: CallbackQuery) -> None:
    """Возвращает бота в рейтинг."""
    if not is_super_admin(cb_uid(callback)):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    bot_id = int(cb_data(callback).rsplit("_", 1)[-1])
    set_manual_hide(RATING_BOT, bot_id, False)
    await callback.answer("✅ Снова в рейтинге")
    await _render_admin_rating(callback, TAB_BOT_MANAGE)