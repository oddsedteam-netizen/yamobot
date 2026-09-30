"""Хранилище: передача прав владельца.

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""

import secrets

from services.db.connection import (
    _get_conn,
    _lock,
)
from services.db.helpers import _owner_bot_ids
from services.db.admins import ensure_admin
from services.db.users import get_bound_chat, register_user, set_bound_chat


def create_transfer(from_user_id: int, kind: str, bot_id: int | None = None) -> str:
    """Создаёт токен-ссылку на передачу прав.

    kind = "all" — передача всех прав владельца.
    kind = "bot" — передача одного бота (bot_id).
    """
    token = secrets.token_urlsafe(20)
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO transfers (token, from_user_id, kind, bot_id) VALUES (?, ?, ?, ?)",
            (token, from_user_id, kind, bot_id),
        )
        conn.commit()
    return token

def get_transfer(token: str) -> dict | None:
    """Возвращает инфо о передаче прав или None."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM transfers WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None

def delete_transfer(token: str) -> None:
    """Удаляет ссылку-передачу (после принятия или отклонения)."""
    conn = _get_conn()
    with _lock:
        conn.execute("DELETE FROM transfers WHERE token = ?", (token,))
        conn.commit()

def transfer_all_rights(
    from_id: int,
    to_id: int,
    to_username: str = "",
    to_first_name: str = "",
) -> int:
    """Полная передача всех прав владельца новому юзеру.

    Мигрируют: боты (приветствие, линки/инлайн-кнопки, тип, анонимность,
    антиспам, клавиатуры), админы, совладельцы, привязанные чаты
    (работа/админов), настройки предов (warn_settings), модераторы «чата
    админов», настройки антирейда, напоминалки.
    Возвращает количество переданных ботов.
    """
    register_user(to_id, to_username, to_first_name)

    bot_ids = _owner_bot_ids(from_id)
    conn = _get_conn()
    with _lock:
        # Боты
        conn.execute("UPDATE bots SET owner_id = ? WHERE owner_id = ?", (to_id, from_id))
        # Админы (избегаем конфликта, если новый владелец уже был админом старика)
        conn.execute(
            "DELETE FROM admins WHERE owner_id = ? AND user_id = ?", (from_id, to_id)
        )
        conn.execute("UPDATE admins SET owner_id = ? WHERE owner_id = ?", (to_id, from_id))
        # Совладельцы
        conn.execute(
            "DELETE FROM coowners WHERE owner_id = ? AND coowner_id = ?", (from_id, to_id)
        )
        conn.execute("UPDATE coowners SET owner_id = ? WHERE owner_id = ?", (to_id, from_id))
        # Настройки предов
        conn.execute(
            "INSERT OR REPLACE INTO warn_settings (owner_id, max_warns, punish_type, punish_duration) "
            "SELECT ?, max_warns, punish_type, punish_duration "
            "FROM warn_settings WHERE owner_id = ?",
            (to_id, from_id),
        )
        conn.execute(
            "DELETE FROM warn_settings WHERE owner_id = ?", (from_id,)
        )
        # Модераторы «чата админов»
        conn.execute(
            "DELETE FROM admin_chat_moderators WHERE owner_id = ? AND user_id = ?",
            (from_id, to_id),
        )
        conn.execute(
            "UPDATE admin_chat_moderators SET owner_id = ? WHERE owner_id = ?",
            (to_id, from_id),
        )
        # Напоминалки
        conn.execute(
            "UPDATE reminders SET owner_id = ? WHERE owner_id = ?", (to_id, from_id)
        )
        # Клавиатуры дочерних ботов (кнопки) — переезжают к новому владельцу,
        # чтобы «настройки редактора» не сбрасывались после передачи прав.
        conn.execute(
            "UPDATE bot_keyboards SET owner_id = ? WHERE owner_id = ?", (to_id, from_id)
        )
        # Настройки антирейда «чата админов» — полностью переезжают вместе с чатом.
        conn.execute(
            "INSERT OR REPLACE INTO antiraid_settings "
            "(owner_id, enabled, threshold, del_links, del_members, triggered, updated_at) "
            "SELECT ?, enabled, threshold, del_links, del_members, triggered, updated_at "
            "FROM antiraid_settings WHERE owner_id = ?",
            (to_id, from_id),
        )
        conn.execute("DELETE FROM antiraid_settings WHERE owner_id = ?", (from_id,))
        conn.commit()

    # Привязанные чаты передаём новому владельцу (только если у старого они были).
    # Не затираем собственные привязки нового владельца, если у старого их нет,
    # иначе при передаче могли пропасть уведомления/стата у нового владельца.
    for kind in ("work", "admin"):
        src = get_bound_chat(from_id, kind)
        if src:
            set_bound_chat(to_id, kind, src)
    set_bound_chat(from_id, "work", None)
    set_bound_chat(from_id, "admin", None)

    # Новый владелец становится админом со своим тегом — чтобы при взятии ПЗ
    # название топика было админским тегом, а не личным именем/юзером.
    ensure_admin(to_id, to_id, to_username)

    return len(bot_ids)

def transfer_bot(
    from_id: int,
    to_id: int,
    bot_id: int,
    to_username: str = "",
    to_first_name: str = "",
) -> bool:
    """Передаёт только одного бота новому владельцу (вместе с его данными)."""
    register_user(to_id, to_username, to_first_name)
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE bots SET owner_id = ? WHERE id = ? AND owner_id = ?",
            (to_id, bot_id, from_id),
        )
        # Клавиатура (кнопки) бота переезжает вместе с ним — иначе у нового
        # владельца настройки бота «сбрасывались» бы в значения по умолчанию.
        if cur.rowcount > 0:
            conn.execute(
                "UPDATE bot_keyboards SET owner_id = ? WHERE bot_id = ? AND owner_id = ?",
                (to_id, bot_id, from_id),
            )
        conn.commit()
        ok = cur.rowcount > 0
        if not ok:
            return False

    # Переносим привязку «чата админов» новому владельцу, если у него своей ещё нет
    # (иначе после одиночной передачи бота пропадают уведомления о новых ПЗ и /стата).
    if get_bound_chat(to_id, "admin") is None:
        src_admin = get_bound_chat(from_id, "admin")
        if src_admin:
            set_bound_chat(to_id, "admin", src_admin)

    # Новый владелец бота становится его админом со своим тегом.
    ensure_admin(to_id, to_id, to_username)
    return True
