"""Единая карточка человека для админ-панели.

Зачем этот модуль
-----------------
Владелец пожаловался: «поищу ВЛД через Профиль+ — показывает не всю инфу,
а поиск по ботам даёт полную». Причина в том, что карточек человека было
ДВЕ, и они разошлись:

* ``profile_plus._person_text`` — ID, дата, бан, боты (без привязок чатов);
* ``profile_plus._owner_text`` — то же плюс чат работы и чат админов.

Обе остались бы надолго и разъехались ещё сильнее. Здесь одна функция
``person_card_text`` — источник правды, а оба прежних места просто
вызывают её.

Что показываем
--------------
Всё, что помогает ответить «кто это и что с ним не так»: имя, юзернейм,
**YID** (номер, по которому человека называют вслух), дату регистрации,
бан, **оба чата** (работы и админов), боты с их состоянием, сколько
админок у его ботов и где у него сейчас бан в ПЗ.

Доступ
------
Вызывающий обязан проверить ``is_super_admin`` — здесь видны ID всех
пользователей и их привязки, это данные администрации.
"""

from handlers._common import html_escape
from services.storage import (
    bot_display_name,
    get_admins_all,
    get_bound_chat,
    get_user_banned_bots,
    get_user_bots,
    get_user_registry,
    get_yid,
)


def person_name(user_id: int, row: dict | None = None) -> str:
    """Подпись человека: юзернейм, иначе имя, иначе ``ID:``.

    Единая подпись нужна всем экранам: раньше каждый рисовал человека
    по-своему, и в списке и в карточке один и тот же человек выглядел
    по-разному.
    """
    row = row if row is not None else (get_user_registry(user_id) or {})
    name = row.get("username") or row.get("first_name") or ""
    return f"@{html_escape(str(name))}" if name else f"ID:{user_id}"


def person_card_text(user_id: int) -> str:
    """Полная карточка человека: одна на все экраны админ-панели."""
    row = get_user_registry(user_id) or {}
    bots = get_user_bots(user_id)
    work = get_bound_chat(user_id, "work")
    admin_chat = get_bound_chat(user_id, "admin")
    yid = get_yid(user_id)
    banned_bots = get_user_banned_bots(user_id)

    def _chat(value: int | None) -> str:
        return f"<code>{value}</code>" if value else "не привязан"

    # Админок считаем по его ботам: админка работает с ботами владельца,
    # и их количество — самая полезная цифра в этом разделе.
    admins_total = 0
    for b in bots:
        owner_id = int(b.get("owner_id") or 0)
        if owner_id:
            admins_total += len(get_admins_all(owner_id))

    bot_lines = "\n".join(
        f"   • {bot_display_name(b)} <code>{b['id']}</code>"
        + ("" if b.get("stopped") else " — 🟢 работает")
        for b in bots
    ) or "   — нет —"

    head = [
        f"👤 <b>Профиль {person_name(user_id, row)}</b>",
        "",
        f"🆔 <b>ID:</b> <code>{user_id}</code>",
        f"🏷 <b>YID:</b> <b>Y{yid}</b>" if yid else "🏷 <b>YID:</b> номер не выдан",
        f"📛 Имя: {html_escape(str(row.get('first_name') or '—'))}",
        f"📅 В системе с: {str(row.get('created_at') or '—')[:19].replace('T', ' ')}",
        f"📌 Забанен администрацией: {'да' if row.get('blocked') else 'нет'}",
    ]
    if banned_bots:
        head.append(
            f"🚫 <b>Бан в ПЗ:</b> боты "
            f"{', '.join(f'#{b}' for b in banned_bots)}"
        )

    return (
        "\n".join(head)
        + f"\n\n💼 Чат работы: {_chat(work)}"
        + f"\n🛡 Чат админов: {_chat(admin_chat)}"
        + f"\n\n🤖 <b>Ботов: {len(bots)}</b> · 👥 админов у них: "
          f"<b>{admins_total}</b>\n{bot_lines}"
    )