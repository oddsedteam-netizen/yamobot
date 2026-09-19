import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from handlers._common import (render_callback, safe_edit, cb_data, cb_uid,
                              msg_uid, try_edit_answer, try_edit,
                              normalize_link)
from services.storage import (
    get_bot_by_id,
    update_bot_field,
    bot_display_name,
    get_bot_links,
    set_bot_links,
    get_user_bots,
    set_welcome_for_all,
    set_welcome_bundle_for_all,
    set_links_for_all,
)
from services.child_manager import ChildManager
from services.premium_emoji import prepare_welcome
from services.rich import count_media_blocks, extract_rich_json, has_rich, rich_to_plain_text

_logger = logging.getLogger(__name__)

router = Router()


class EditorFSM(StatesGroup):
    waiting_welcome_text = State()
    waiting_link_name = State()
    waiting_link_url = State()
    waiting_link_style = State()
    waiting_global_welcome = State()
    waiting_global_link_name = State()
    waiting_global_link_url = State()
    waiting_global_link_style = State()


# Стили кнопок-ссылок: владелец сам выбирает цвет при добавлении линка.
LINK_STYLE_LABELS = {
    "primary": "🔵 синий",
    "success": "🟢 зелёный",
    "danger": "🔴 красный",
    "": "⬜ без цвета",
}


def _safe_link_style(link: dict) -> str | None:
    """Валидный стиль кнопки-ссылки (None — если стиль не задан/некорректен)."""
    value = str(link.get("style") or "").strip().lower()
    return value if value in ("primary", "success", "danger") else None


def style_pick_kb(prefix: str, cancel_data: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора цвета для инлайн-кнопки-ссылки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔵 Синий", callback_data=f"{prefix}_primary",
                                 style="primary"),
            InlineKeyboardButton(text="🟢 Зелёный", callback_data=f"{prefix}_success",
                                 style="success"),
            InlineKeyboardButton(text="🔴 Красный", callback_data=f"{prefix}_danger",
                                 style="danger"),
        ],
        [InlineKeyboardButton(text="⬜ Без цвета", callback_data=f"{prefix}_none")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_data)],
    ])


def _rich_json_from_message(message: Message) -> str:
    """Сохраняет статью владельца (rich-сообщение) в виде JSON.

    Telegram отдаёт статью как ``message.rich_message`` (в новых версиях
    aiogram — объектом, в старых — словарём в ``model_extra``); модуль
    ``services.rich`` умеет и то, и другое.
    """
    return extract_rich_json(message)


def _rich_fallback_text(message: Message) -> str:
    """Текст-заглушка для статьи (если её придётся отправить обычным текстом)."""
    return rich_to_plain_text(extract_rich_json(message))


async def _finish_welcome_edit(message: Message, state: FSMContext,
                               child_manager: ChildManager, user_id: int,
                               bot_id: int, note: str) -> None:
    """Завершает редактирование приветствия: сброс состояния, рестарт, ответ."""
    await state.clear()
    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info and child_manager.is_running(bot_id):
        await child_manager.restart_child(bot_info)
        status = "🟢 Бот перезапущен"
    else:
        status = "💾 Сохранено"

    await message.answer(
        f"{note}\n\n{status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактор", callback_data=f"editor_{bot_id}",
                                  style="primary")],
            [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
        ])
    )


# ═══════════════ Одиночный редактор ═══════════════

def editor_kb(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Изменить приветствие", callback_data=f"edit_welcome_{bot_id}", style="primary")],
        [
            InlineKeyboardButton(text="🔗 Линки", callback_data=f"edit_links_{bot_id}", style="primary"),
            InlineKeyboardButton(text="⬅️ Назад к боту", callback_data=f"bot_{bot_id}"),
        ],
    ])


def links_kb(bot_id: int, links: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, link in enumerate(links):
        rows.append([
            InlineKeyboardButton(text=f"🔗 {link['text']} {LINK_STYLE_LABELS.get(str(link.get('style') or ''), '')}".strip(),
                callback_data=f"viewlink_{bot_id}_{i}", style=_safe_link_style(link)),
            InlineKeyboardButton(text="🗑", callback_data=f"dellink_{bot_id}_{i}", style="danger"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить линк", callback_data=f"addlink_{bot_id}", style="success")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к редактору", callback_data=f"editor_{bot_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("editor_"))
async def cb_editor(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    bot_id = int(cb_data(callback).split("_", 1)[1])
    user_id = cb_uid(callback)

    bot_info = get_bot_by_id(user_id, bot_id)
    if not bot_info:
        await callback.answer("⚠️ Бот не найден")
        return

    name = bot_display_name(bot_info)
    welcome = bot_info.get("welcome_text", "") or "— не задано —"
    links = get_bot_links(user_id, bot_id)

    if links:
        links_text = "\n🔗 Линки:\n" + "\n".join(f"  • {l['text']} → {l['url']}" for l in links)
    else:
        links_text = "\n🔗 Линки: — нет —"

    text = (
        f"✏️ <b>Редактор — {name}</b>\n\n"
        f"💬 Приветствие:\n{welcome}\n"
        f"{links_text}\n\n"
        f"Выбери что изменить:"
    )

    await render_callback(callback, text, editor_kb(bot_id))


@router.callback_query(F.data.startswith("edit_welcome_"))
async def cb_edit_welcome(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(cb_data(callback).split("_")[-1])

    await state.set_state(EditorFSM.waiting_welcome_text)
    await state.update_data(editing_bot_id=bot_id)

    text = (
        "💬 <b>Новое приветствие</b>\n\n"
        "Отправь <b>текст</b>, <b>фото</b> с подписью или готовую "
        "<b>статью</b> (пост с форматированием) — всё это станет приветствием.\n\n"
        "🖼 Фото поддерживается, 📰 статья сохраняет своё оформление.\n\n"
    )

    if callback.message:
        await try_edit_answer(callback.message, text,
                              InlineKeyboardMarkup(inline_keyboard=[
                                  [InlineKeyboardButton(text="❌ Отмена", callback_data=f"editor_{bot_id}", style="primary")]
                              ]))
    await callback.answer()


@router.message(EditorFSM.waiting_welcome_text)
async def fsm_welcome_text(message: Message, state: FSMContext, child_manager: ChildManager) -> None:
    data = await state.get_data()
    bot_id = int(data.get("editing_bot_id") or 0)
    user_id = msg_uid(message)

    if not bot_id:
        await state.clear()
        return

    rich_json = _rich_json_from_message(message)
    photo_id = message.photo[-1].file_id if message.photo else ""

    # ── 1. Приветствие-«статья» (rich-сообщение) ──
    if rich_json:
        new_welcome = _rich_fallback_text(message) or "Привет!"
        update_bot_field(user_id, bot_id, "welcome_rich", rich_json)
        update_bot_field(user_id, bot_id, "welcome_photo", "")
        update_bot_field(user_id, bot_id, "welcome_text", new_welcome)
        media = count_media_blocks(rich_json)
        note = "📰 Приветствие-статья сохранено!"
        if media:
            note += (
                f"\n\n⚠️ Медиа внутри статьи ({media} шт.) не пересылается: "
                "его файлы привязаны к основному боту. Пришли фото отдельным "
                "сообщением — получится приветствие с фото."
            )
        await _finish_welcome_edit(message, state, child_manager, user_id,
                                   bot_id, note)
        return

    # ── 2. Приветствие с фото ──
    if photo_id:
        new_welcome = prepare_welcome(message)
        update_bot_field(user_id, bot_id, "welcome_photo", photo_id)
        update_bot_field(user_id, bot_id, "welcome_rich", "")
        update_bot_field(user_id, bot_id, "welcome_text", new_welcome)
        await _finish_welcome_edit(message, state, child_manager, user_id, bot_id,
                                   "🖼 Приветствие с фото сохранено!")
        return

    # ── 3. Обычный текст ─
    new_welcome = prepare_welcome(message)
    if not new_welcome.strip():
        _logger.warning(
            "Приветствие: пустое сообщение (content_type=%s, photo=%s, rich=%s)",
            getattr(message, "content_type", "?"), bool(message.photo),
            has_rich(message),
        )
        await message.answer(
            "❌ Не увидел ни текста, ни фото, ни статьи.\n\n"
            f"🛠 Тип полученного сообщения: "
            f"<code>{getattr(message, 'content_type', '?')}</code>\n\n"
            "Пришли приветствие <b>текстом</b>, <b>фото с подписью</b> или "
            "<b>статьёй</b>."
        )
        return

    update_bot_field(user_id, bot_id, "welcome_text", new_welcome)
    # Текстовое приветствие заменяет прежнее фото/статью.
    update_bot_field(user_id, bot_id, "welcome_photo", "")
    update_bot_field(user_id, bot_id, "welcome_rich", "")
    await state.clear()

    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info and child_manager.is_running(bot_id):
        await child_manager.restart_child(bot_info)
        status = "🟢 Бот перезапущен"
    else:
        status = "💾 Сохранено"

    await message.answer(
        f"✅ Приветствие обновлено!\n\n{status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактор", callback_data=f"editor_{bot_id}", style="primary")],
            [InlineKeyboardButton(text="⬅️ К боту", callback_data=f"bot_{bot_id}")],
        ])
    )


@router.callback_query(F.data.startswith("edit_links_"))
async def cb_edit_links(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    bot_id = int(cb_data(callback).split("_")[-1])
    user_id = cb_uid(callback)

    links = get_bot_links(user_id, bot_id)

    if links:
        text = f"🔗 <b>Линки</b> ({len(links)})\n\n"
        for i, link in enumerate(links, 1):
            text += f"{i}. {link['text']} → {link['url']}\n"
    else:
        text = "🔗 <b>Линки</b>\n\nПока нет ни одного линка."

    await render_callback(callback, text, links_kb(bot_id, links))


@router.callback_query(F.data.startswith("addlink_"))
async def cb_add_link(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(cb_data(callback).split("_", 1)[1])

    await state.set_state(EditorFSM.waiting_link_name)
    await state.update_data(link_bot_id=bot_id)

    text = "🔗 <b>Новый линк</b>\n\nОтправь <b>название кнопки</b>.\nПример: <code>Наш ТГК</code>"

    if callback.message:
        await try_edit_answer(callback.message, text,
                              InlineKeyboardMarkup(inline_keyboard=[
                                  [InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_links_{bot_id}")]
                              ]))
    await callback.answer()


@router.message(EditorFSM.waiting_link_name)
async def fsm_link_name(message: Message, state: FSMContext) -> None:
    link_name = (message.text or "").strip()
    if not link_name:
        await message.answer("❌ Название не может быть пустым.")
        return

    await state.update_data(link_name=link_name)
    await state.set_state(EditorFSM.waiting_link_url)

    data = await state.get_data()
    bot_id = data.get("link_bot_id")

    await message.answer(
        f"✅ Название: <b>{link_name}</b>\n\nТеперь отправь <b>ссылку</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_links_{bot_id}")]
        ])
    )


@router.message(EditorFSM.waiting_link_url)
async def fsm_link_url(message: Message, state: FSMContext) -> None:
    # Ссылку принимаем в любом виде (@username, t.me/..., example.com) и сами
    # приводим к формату, который понимает Telegram в инлайн-кнопке.
    link_url = normalize_link(message.text or "")

    if not link_url:
        await message.answer(
            "❌ Не похоже на ссылку.\n\n"
            "Отправь её в любом виде: <code>@username</code>, "
            "<code>t.me/канал</code> или полную <code>https://…</code> — "
            "бот сам приведёт её к нужному формату."
        )
        return

    data = await state.get_data()
    bot_id = int(data.get("link_bot_id") or 0)
    link_name = data.get("link_name", "Кнопка")

    # Цвет кнопки спрашиваем отдельным шагом — линк запишем после выбора.
    await state.set_state(EditorFSM.waiting_link_style)
    await state.update_data(link_url=link_url)

    await message.answer(
        f"✅ Название: <b>{link_name}</b>\n"
        f"🔗 Ссылка: {link_url}\n\n"
        "🎨 Какой цвет присвоить кнопке?",
        reply_markup=style_pick_kb(f"linkstyle_{bot_id}", f"edit_links_{bot_id}"),
    )


def _style_from_cb(raw: str) -> str:
    """Преобразует суффикс колбэка в стиль кнопки (пусто — без цвета)."""
    return "" if raw == "none" else raw


@router.callback_query(F.data.startswith("linkstyle_"))
async def cb_link_style(callback: CallbackQuery, state: FSMContext,
                        child_manager: ChildManager) -> None:
    raw = cb_data(callback).split("_", 1)[1]          # <bot_id>_<style>
    bot_id_str, _, style_raw = raw.partition("_")
    bot_id = int(bot_id_str or 0)
    style = _style_from_cb(style_raw)

    data = await state.get_data()
    link_name = data.get("link_name", "Кнопка")
    link_url = data.get("link_url", "")
    user_id = cb_uid(callback)
    await state.clear()

    if not link_url:
        await callback.answer("⚠️ Ссылка потерялась — добавь линк заново.",
                              show_alert=True)
        return

    links = get_bot_links(user_id, bot_id)
    links.append({"text": link_name, "url": link_url, "style": style})
    set_bot_links(user_id, bot_id, links)

    bot_info = get_bot_by_id(user_id, bot_id)
    if bot_info and child_manager.is_running(bot_id):
        await child_manager.restart_child(bot_info)
        status = "🟢 Бот перезапущен — кнопка уже в приветствии."
    else:
        status = "⚠️ Бот не запущен — кнопка применится при запуске бота."

    if callback.message:
        await try_edit_answer(
            callback.message,
            f"✅ Линк добавлен!\n\n🔗 {link_name} → {link_url}\n"
            f"🎨 Цвет: {LINK_STYLE_LABELS.get(style, style)}\n\n{status}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Линки",
                                      callback_data=f"edit_links_{bot_id}",
                                      style="primary")],
                [InlineKeyboardButton(text="⬅️ К боту",
                                      callback_data=f"bot_{bot_id}")],
            ]),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("dellink_"))
async def cb_delete_link(callback: CallbackQuery, child_manager: ChildManager) -> None:
    parts = cb_data(callback).split("_")
    bot_id = int(parts[1])
    link_idx = int(parts[2])
    user_id = cb_uid(callback)

    links = get_bot_links(user_id, bot_id)
    if 0 <= link_idx < len(links):
        removed = links.pop(link_idx)
        set_bot_links(user_id, bot_id, links)

        bot_info = get_bot_by_id(user_id, bot_id)
        if bot_info and child_manager.is_running(bot_id):
            await child_manager.restart_child(bot_info)

        await callback.answer(f"🗑 Удалён: {removed['text']}")
    else:
        await callback.answer("⚠️ Не найден")

    if links:
        text = f"🔗 <b>Линки</b> ({len(links)})\n\n"
        for i, link in enumerate(links, 1):
            text += f"{i}. {link['text']} → {link['url']}\n"
    else:
        text = "🔗 <b>Линки</b>\n\nВсе линки удалены."

    await safe_edit(callback.message, text, reply_markup=links_kb(bot_id, links))


@router.callback_query(F.data.startswith("viewlink_"))
async def cb_view_link(callback: CallbackQuery) -> None:
    parts = cb_data(callback).split("_")
    bot_id = int(parts[1])
    link_idx = int(parts[2])
    user_id = cb_uid(callback)

    links = get_bot_links(user_id, bot_id)
    if 0 <= link_idx < len(links):
        link = links[link_idx]
        await callback.answer(f"{link['text']}: {link['url']}", show_alert=True)
    else:
        await callback.answer("⚠️ Не найден")


# ═══════════════ ГЛОБАЛЬНЫЙ редактор для всех ботов ═══════════════

def global_editor_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Приветствие для всех", callback_data="all_edit_welcome")],
        [InlineKeyboardButton(text="🔗 Добавить линк всем", callback_data="all_edit_link")],
        [InlineKeyboardButton(text="🗑 Удалить все линки", callback_data="all_clear_links")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="select_all", style="primary")],
    ])


@router.callback_query(F.data == "all_editor")
async def cb_all_editor(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user_id = cb_uid(callback)
    bots = get_user_bots(user_id)

    text = (
        f"✏️ <b>Редактор для всех ботов</b>\n\n"
        f"Ботов: <b>{len(bots)}</b>\n\n"
        f"Изменения применятся ко <b>всем</b> твоим ботам сразу."
    )

    if callback.message:
        await try_edit_answer(callback.message, text, reply_markup=global_editor_kb())
    await callback.answer()


@router.callback_query(F.data == "all_edit_welcome")
async def cb_all_edit_welcome(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditorFSM.waiting_global_welcome)

    text = (
        "💬 <b>Приветствие для всех ботов</b>\n\n"
        "Отправь <b>текст</b>, <b>фото</b> с подписью или готовую "
        "<b>статью</b> — всё это станет приветствием всех твоих ботов.\n\n"
        "🖼 Фото и 📰 статья сохранят своё оформление. "
        "Поддерживается HTML и премиум-эмодзи."
    )

    if callback.message:
        await try_edit_answer(callback.message, text,
                              InlineKeyboardMarkup(inline_keyboard=[
                                  [InlineKeyboardButton(text="❌ Отмена", callback_data="all_editor", style="primary")]
                              ]))
    await callback.answer()


@router.message(EditorFSM.waiting_global_welcome)
async def fsm_global_welcome(message: Message, state: FSMContext, child_manager: ChildManager) -> None:
    user_id = msg_uid(message)
    rich_json = _rich_json_from_message(message)
    photo_id = message.photo[-1].file_id if message.photo else ""

    if rich_json:
        new_welcome = _rich_fallback_text(message) or "Привет!"
    else:
        new_welcome = prepare_welcome(message)

    if not new_welcome.strip() and not photo_id:
        _logger.warning(
            "Приветствие (все боты): пустое сообщение (content_type=%s, photo=%s, rich=%s)",
            getattr(message, "content_type", "?"), bool(message.photo),
            has_rich(message),
        )
        await message.answer(
            "❌ Не увидел ни текста, ни фото, ни статьи.\n\n"
            f"🛠 Тип полученного сообщения: "
            f"<code>{getattr(message, 'content_type', '?')}</code>\n\n"
            "Пришли приветствие <b>текстом</b>, <b>фото с подписью</b> или "
            "<b>статьёй</b>."
        )
        return

    count = set_welcome_bundle_for_all(user_id, new_welcome, photo_id, rich_json)
    await state.clear()

    # Перезапускаем все дочерки
    bots = get_user_bots(user_id)
    restarted = 0
    for b in bots:
        if child_manager.is_running(b["id"]):
            await child_manager.restart_child(b)
            restarted += 1

    await message.answer(
        f"✅ <b>Приветствие обновлено для всех!</b>\n\n"
        f"📊 Изменено: <b>{count}</b> ботов\n"
        f"🔄 Перезапущено: <b>{restarted}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактор всех", callback_data="all_editor", style="primary")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="back_main")],
        ])
    )


@router.callback_query(F.data == "all_edit_link")
async def cb_all_edit_link(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditorFSM.waiting_global_link_name)

    text = (
        "🔗 <b>Добавить линк ко всем ботам</b>\n\n"
        "Отправь <b>название кнопки</b>."
    )

    if callback.message:
        await try_edit_answer(callback.message, text,
                              InlineKeyboardMarkup(inline_keyboard=[
                                  [InlineKeyboardButton(text="❌ Отмена", callback_data="all_editor", style="primary")]
                              ]))
    await callback.answer()


@router.message(EditorFSM.waiting_global_link_name)
async def fsm_global_link_name(message: Message, state: FSMContext) -> None:
    link_name = (message.text or "").strip()
    if not link_name:
        await message.answer("❌ Название не может быть пустым.")
        return

    await state.update_data(global_link_name=link_name)
    await state.set_state(EditorFSM.waiting_global_link_url)

    await message.answer(
        f"✅ Название: <b>{link_name}</b>\n\nТеперь отправь <b>ссылку</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="all_editor", style="primary")]
        ])
    )


@router.message(EditorFSM.waiting_global_link_url)
async def fsm_global_link_url(message: Message, state: FSMContext) -> None:
    # Ссылку принимаем в любом виде (@username, t.me/..., example.com) и сами
    # приводим к формату, который понимает Telegram в инлайн-кнопке.
    link_url = normalize_link(message.text or "")

    if not link_url:
        await message.answer(
            "❌ Не похоже на ссылку.\n\n"
            "Отправь её в любом виде: <code>@username</code>, "
            "<code>t.me/канал</code> или полную <code>https://…</code> — "
            "бот сам приведёт её к нужному формату."
        )
        return

    data = await state.get_data()
    link_name = data.get("global_link_name", "Кнопка")

    # Спрашиваем цвет — применим ко всем ботам после выбора.
    await state.set_state(EditorFSM.waiting_global_link_style)
    await state.update_data(global_link_url=link_url)

    await message.answer(
        f"✅ Название: <b>{link_name}</b>\n"
        f"🔗 Ссылка: {link_url}\n\n"
        "🎨 Какой цвет присвоить кнопке?",
        reply_markup=style_pick_kb("glinkstyle", "all_editor"),
    )


@router.callback_query(F.data.startswith("glinkstyle_"))
async def cb_global_link_style(callback: CallbackQuery, state: FSMContext,
                               child_manager: ChildManager) -> None:
    style_raw = cb_data(callback).split("_", 1)[1]
    style = _style_from_cb(style_raw)

    data = await state.get_data()
    link_name = data.get("global_link_name", "Кнопка")
    link_url = data.get("global_link_url", "")
    user_id = cb_uid(callback)
    await state.clear()

    if not link_url:
        await callback.answer("⚠️ Ссылка потерялась — добавь линк заново.",
                              show_alert=True)
        return

    # Добавляем линк ко всем ботам с выбранным цветом.
    bots = get_user_bots(user_id)
    for b in bots:
        links = get_bot_links(user_id, b["id"])
        links.append({"text": link_name, "url": link_url, "style": style})
        set_bot_links(user_id, b["id"], links)

    restarted = 0
    for b in bots:
        if child_manager.is_running(b["id"]):
            await child_manager.restart_child(b)
            restarted += 1

    if callback.message:
        await try_edit_answer(
            callback.message,
            f"✅ <b>Линк добавлен ко всем ботам!</b>\n\n"
            f"🔗 {link_name} → {link_url}\n"
            f"🎨 Цвет: {LINK_STYLE_LABELS.get(style, style)}\n\n"
            f"📊 Ботов: <b>{len(bots)}</b>\n"
            f"🔄 Перезапущено: <b>{restarted}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Редактор всех",
                                      callback_data="all_editor", style="primary")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="back_main")],
            ]),
        )
    await callback.answer()


@router.callback_query(F.data == "all_clear_links")
async def cb_all_clear_links(callback: CallbackQuery, child_manager: ChildManager) -> None:
    user_id = cb_uid(callback)
    bots = get_user_bots(user_id)

    set_links_for_all(user_id, [])

    restarted = 0
    for b in bots:
        if child_manager.is_running(b["id"]):
            await child_manager.restart_child(b)
            restarted += 1

    if callback.message:
        await try_edit(
            callback.message,
            f"✅ <b>Все линки удалены!</b>\n\n"
            f"📊 Ботов: <b>{len(bots)}</b>\n"
            f"🔄 Перезапущено: <b>{restarted}</b>",
            reply_markup=global_editor_kb(),
        )
    await callback.answer("Все линки удалены")
