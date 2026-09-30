"""Хранилище: топики обратной связи.

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""


from services.db.connection import (
    _get_conn,
    _lock,
)
from services.db.helpers import _owner_bot_ids
from services.db.reminders import purge_topic_reminder_state


def set_topic_closed(bot_id: int, topic_id: int, group_chat_id: int,
                     closed: bool) -> bool:
    """Помечает топик закрытым (или снимает пометку). True, если запись была.

    Зачем это нужно
    ---------------
    Жалоба: «топик удалили, ПЗ не пишет, новый не создаётся — бот продолжает
    присылать "ПЗ без админа" по нему вечно». Проверить существование топика
    через Bot API нельзя (метода ``getForumTopic`` нет), а сервисное сообщение
    ``forum_topic_closed`` приходит не всегда — например, если бот не админ
    чата. Поэтому здесь МЯГКАЯ МЕТКА, а не удаление: запись ПЗ и её история
    остаются целы, топик просто перестаёт попадать в напоминалку.

    Если админ откроет топик снова, придёт ``forum_topic_reopened``, метка
    снимется — и напоминания по нему возобновятся. Именно поэтому здесь не
    ``DELETE``: терять обращение из-за чужего клика по «Удалить топик» нельзя.
    """
    conn = _get_conn()
    with _lock:
        if closed:
            cur = conn.execute(
                "UPDATE feedback_topics SET closed_at = datetime('now') "
                "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ?",
                (bot_id, topic_id, group_chat_id),
            )
        else:
            cur = conn.execute(
                "UPDATE feedback_topics SET closed_at = NULL "
                "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ?",
                (bot_id, topic_id, group_chat_id),
            )
        conn.commit()
        return cur.rowcount > 0


def get_pinned_message_id(bot_id: int, topic_id: int, group_chat_id: int) -> int:
    """id закреплённой шапки ПЗ (0 — шапка ещё не закреплялась)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT pinned_message_id FROM feedback_topics "
        "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ?",
        (bot_id, topic_id, group_chat_id),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def set_pinned_message_id(bot_id: int, topic_id: int, group_chat_id: int,
                          message_id: int) -> None:
    """Запоминает, какая шапка ПЗ сейчас закреплена в топике.

    Нужно, чтобы перед закрепом новой шапки снять старую: иначе в закрепе
    осталось бы устаревшее «никто не взял», и админ не нашёл бы актуальные
    кнопки «✋ Я беру» / «🚫 Отказ».
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE feedback_topics SET pinned_message_id = ? "
            "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ?",
            (int(message_id), bot_id, topic_id, group_chat_id),
        )
        conn.commit()


def set_feedback_chat(bot_id: int, group_chat_id: int) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO feedback_chats (bot_id, group_chat_id) VALUES (?, ?)",
            (bot_id, group_chat_id)
        )
        conn.commit()

def get_feedback_chat(bot_id: int) -> int | None:
    conn = _get_conn()
    row = conn.execute("SELECT group_chat_id FROM feedback_chats WHERE bot_id = ?", (bot_id,)).fetchone()
    return row[0] if row else None

def clear_feedback_chat(bot_id: int) -> bool:
    """Отвязывает бота от рабочего чата (кнопка «🔗 Перепривязка»).

    Возвращает True, если привязка была и теперь снята. История ПЗ и сами
    топики не трогаем — бот просто сможет подключиться к новому чату.
    """
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM feedback_chats WHERE bot_id = ?", (bot_id,))
        conn.commit()
        return cur.rowcount > 0

def get_topic_by_user(bot_id: int, user_chat_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM feedback_topics WHERE bot_id = ? AND user_chat_id = ?",
        (bot_id, user_chat_id)
    ).fetchone()
    return dict(row) if row else None

def get_topic_by_topic_id(bot_id: int, group_chat_id: int, topic_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM feedback_topics WHERE bot_id = ? AND group_chat_id = ? AND topic_id = ?",
        (bot_id, group_chat_id, topic_id)
    ).fetchone()
    return dict(row) if row else None

def create_topic_record(bot_id: int, user_chat_id: int, group_chat_id: int, topic_id: int) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO feedback_topics "
            "(bot_id, user_chat_id, group_chat_id, topic_id, admin_user_id, admin_tag, status) "
            "VALUES (?, ?, ?, ?, 0, '', 'open')",
            (bot_id, user_chat_id, group_chat_id, topic_id)
        )
        conn.commit()

def delete_topic_record(bot_id: int, user_chat_id: int) -> None:
    """Удаляет запись топика (используется, когда топик устарел/удалили/закрыли).

    Удалённый топик больше не отображается ни в «ПЗ», ни в сводке «ПЗ без админов».

    Вместе с записью чистится и состояние напоминалок по этому топику (тики и
    заглушки). Без этого оставались бы «хвосты» по уже несуществующему топику:
    он не показывался бы в списках, но продолжал занимать место в БД, а по
    нему нельзя было бы снять заглушку. Чистка точечная — по тройке id, чужие
    топики не затрагиваются.
    """
    topic = get_topic_by_user(bot_id, user_chat_id)
    conn = _get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM feedback_topics WHERE bot_id = ? AND user_chat_id = ?",
            (bot_id, user_chat_id)
        )
        conn.commit()
    if topic:
        purge_topic_reminder_state(
            bot_id, int(topic["topic_id"]), int(topic["group_chat_id"])
        )

def delete_topics_for_owner_user(owner_id: int, user_chat_id: int) -> int:
    """Удаляет ПЗ пользователя по всем ботам владельца (например, после бана).

    Возвращает количество удалённых записей.
    """
    removed = 0
    for bot_id in _owner_bot_ids(owner_id):
        conn = _get_conn()
        with _lock:
            cur = conn.execute(
                "DELETE FROM feedback_topics WHERE bot_id = ? AND user_chat_id = ?",
                (bot_id, user_chat_id)
            )
            conn.commit()
            removed += cur.rowcount
    return removed

def assign_admin_to_topic(bot_id: int, topic_id: int, group_chat_id: int,
                          admin_user_id: int, admin_tag: str) -> dict:
    """
    Возвращает {'ok': bool, 'prev_admin_id': int, 'is_change': bool}
    """
    conn = _get_conn()
    with _lock:
        # Смотрим текущего админа
        prev = conn.execute(
            "SELECT admin_user_id FROM feedback_topics "
            "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ?",
            (bot_id, topic_id, group_chat_id)
        ).fetchone()

        prev_admin_id = prev[0] if prev else 0
        is_change = prev_admin_id != 0 and prev_admin_id != admin_user_id

        cur = conn.execute(
            "UPDATE feedback_topics SET admin_user_id = ?, admin_tag = ?, status = 'assigned', "
            # Отсчёт «админ молчит» начинается с момента назначения, а не с
            # момента создания топика — иначе на старом ПЗ прилетало
            # уведомление в первую же минуту после назначения.
            "admin_assigned_at = datetime('now') "
            "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ?",
            (admin_user_id, admin_tag, bot_id, topic_id, group_chat_id)
        )
        conn.commit()

        return {
            "ok": cur.rowcount > 0,
            "prev_admin_id": prev_admin_id,
            "is_change": is_change,
        }

def reset_topic_admin(bot_id: int, topic_id: int, group_chat_id: int) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE feedback_topics SET admin_user_id = 0, admin_tag = '', "
            "status = 'open', admin_assigned_at = NULL "
            "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ?",
            (bot_id, topic_id, group_chat_id)
        )
        conn.commit()
        return cur.rowcount > 0

def touch_topic_activity(bot_id: int, topic_id: int, group_chat_id: int,
                         direction: str) -> None:
    """Отмечает, что в топике было сообщение, и КТО его написал.

    ``direction``: ``in`` — писал ПЗ, ``out`` — писал админ.

    Напоминалка читает эту метку: если последним писал админ, ПЗ считается
    отвеченным и уведомление «без ответа» не отправляется.
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE feedback_topics SET last_activity_at = datetime('now'), "
            "last_activity_dir = ? "
            "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ?",
            ("out" if direction == "out" else "in", bot_id, topic_id, group_chat_id)
        )
        conn.commit()

def save_feedback_message(bot_id: int, topic_id: int, group_chat_id: int,
                          user_chat_id: int, direction: str,
                          group_msg_id: int = 0, user_msg_id: int = 0) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO feedback_messages "
            "(bot_id, topic_id, group_chat_id, user_chat_id, direction, group_msg_id, user_msg_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bot_id, topic_id, group_chat_id, user_chat_id, direction, group_msg_id, user_msg_id)
        )
        conn.commit()

def get_feedback_msg_by_group_msg(bot_id: int, group_chat_id: int, group_msg_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM feedback_messages WHERE bot_id = ? AND group_chat_id = ? AND group_msg_id = ?",
        (bot_id, group_chat_id, group_msg_id)
    ).fetchone()
    return dict(row) if row else None

def get_feedback_msg_by_user_msg(bot_id: int, user_chat_id: int, user_msg_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM feedback_messages WHERE bot_id = ? AND user_chat_id = ? AND user_msg_id = ?",
        (bot_id, user_chat_id, user_msg_id)
    ).fetchone()
    return dict(row) if row else None

def save_banned_topic(bot_id: int, user_chat_id: int, group_chat_id: int, topic_id: int) -> None:
    """Сохраняет маппинг «топик → юзер» при бане.

    Запись ПЗ при бане удаляется, чтобы он не светился в списках, но
    сохраняем маппинг, чтобы потом можно было разбанить прямо из топика.
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO banned_topics "
            "(bot_id, user_chat_id, group_chat_id, topic_id) VALUES (?, ?, ?, ?)",
            (bot_id, user_chat_id, group_chat_id, topic_id),
        )
        conn.commit()

def get_banned_topic_user(bot_id: int, group_chat_id: int, topic_id: int) -> int | None:
    """Возвращает user_chat_id забаненного топика (или None)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT user_chat_id FROM banned_topics "
        "WHERE bot_id = ? AND group_chat_id = ? AND topic_id = ?",
        (bot_id, group_chat_id, topic_id),
    ).fetchone()
    return row[0] if row else None

def delete_banned_topic(bot_id: int, group_chat_id: int, topic_id: int) -> None:
    """Удаляет запись о забаненном топике (после разбана)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM banned_topics "
            "WHERE bot_id = ? AND group_chat_id = ? AND topic_id = ?",
            (bot_id, group_chat_id, topic_id),
        )
        conn.commit()

def get_all_topics_for_bot(bot_id: int) -> list[dict]:
    """Все топики бота."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM feedback_topics WHERE bot_id = ? ORDER BY created_at DESC",
        (bot_id,)
    ).fetchall()
    return [dict(r) for r in rows]

def get_topic_by_user_id_search(bot_id: int, user_chat_id: int) -> dict | None:
    """Ищет топик по user_chat_id."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM feedback_topics WHERE bot_id = ? AND user_chat_id = ?",
        (bot_id, user_chat_id)
    ).fetchone()
    return dict(row) if row else None

def get_pz_stats(bot_id: int, user_chat_id: int) -> dict:
    """Полная стата по ПЗ."""
    conn = _get_conn()

    # Кол-во сообщений от юзера
    from_user = conn.execute(
        "SELECT COUNT(*) FROM feedback_messages WHERE bot_id = ? AND user_chat_id = ? AND direction = 'in'",
        (bot_id, user_chat_id)
    ).fetchone()[0]

    # Кол-во сообщений админам (в ответ)
    to_user = conn.execute(
        "SELECT COUNT(*) FROM feedback_messages WHERE bot_id = ? AND user_chat_id = ? AND direction = 'out'",
        (bot_id, user_chat_id)
    ).fetchone()[0]

    # Первое сообщение
    first = conn.execute(
        "SELECT created_at FROM feedback_messages WHERE bot_id = ? AND user_chat_id = ? ORDER BY created_at ASC LIMIT 1",
        (bot_id, user_chat_id)
    ).fetchone()

    # Последнее сообщение
    last = conn.execute(
        "SELECT created_at FROM feedback_messages WHERE bot_id = ? AND user_chat_id = ? ORDER BY created_at DESC LIMIT 1",
        (bot_id, user_chat_id)
    ).fetchone()

    return {
        "messages_from_user": from_user,
        "messages_to_user": to_user,
        "first_message_at": first[0] if first else None,
        "last_message_at": last[0] if last else None,
    }

def get_user_info_from_pz(bot_id: int, user_chat_id: int) -> dict | None:
    """Инфа о юзере из таблицы users."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE bot_id = ? AND chat_id = ?",
        (bot_id, user_chat_id)
    ).fetchone()
    return dict(row) if row else None

def reserve_topic_slot(bot_id: int, user_chat_id: int, group_chat_id: int) -> bool:
    """Атомарно «бронирует» ПЗ за пользователем ДО создания топика в Telegram.

    Возвращает True, если слот свободен и забронирован этим вызовом, и False,
    если ПЗ у пользователя уже есть (тогда новый топик создавать нельзя).

    Это страховка от гонок: даже если два обработчика (или два процесса)
    одновременно начнут создавать топик, топик будет создан ровно один —
    в БД есть UNIQUE(bot_id, user_chat_id), а INSERT OR IGNORE атомарен.
    """
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT OR IGNORE INTO feedback_topics "
            "(bot_id, user_chat_id, group_chat_id, topic_id, admin_user_id, admin_tag, status) "
            "VALUES (?, ?, ?, 0, 0, '', 'open')",
            (bot_id, user_chat_id, group_chat_id),
        )
        conn.commit()
        return cur.rowcount > 0

def set_topic_id(bot_id: int, user_chat_id: int, group_chat_id: int,
                 topic_id: int) -> None:
    """Проставляет реальный topic_id у ранее забронированного ПЗ.

    Заодно сбрасывает ``pinned_message_id``: топик новый, закреплённой шапки
    в нём ещё нет. Без сброса бот после пересоздания топика пытался бы снять
    закреп по ``message_id`` из СТАРОГО топика.
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE feedback_topics SET topic_id = ?, group_chat_id = ?, "
            "pinned_message_id = 0 "
            "WHERE bot_id = ? AND user_chat_id = ?",
            (topic_id, group_chat_id, bot_id, user_chat_id),
        )
        conn.commit()

def is_topic_reserved(bot_id: int, user_chat_id: int) -> bool:
    """Есть ли запись ПЗ (в том числе «забронированная» без topic_id)."""
    return get_topic_by_user(bot_id, user_chat_id) is not None
