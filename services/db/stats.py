"""Хранилище: статистика.

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""


from services.db.connection import (
    _get_conn,
    _lock,
)
from services.db.users import get_child_users_count


def add_stat(bot_id: int, event: str, count: int = 1) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute("INSERT INTO stats (bot_id, event, count) VALUES (?, ?, ?)", (bot_id, event, count))
        conn.commit()

def get_stats(bot_id: int) -> dict:
    conn = _get_conn()

    users = get_child_users_count(bot_id)
    mailings_count = conn.execute("SELECT COUNT(*) FROM mailings WHERE bot_id = ?", (bot_id,)).fetchone()[0]
    mailings_sent = conn.execute("SELECT COALESCE(SUM(sent), 0) FROM mailings WHERE bot_id = ?", (bot_id,)).fetchone()[0]
    mailings_failed = conn.execute("SELECT COALESCE(SUM(failed), 0) FROM mailings WHERE bot_id = ?", (bot_id,)).fetchone()[0]

    # Сообщения считаем ТОЛЬКО из таблицы переписки feedback_messages — там каждая
    # реальная переписка сохраняется ровно один раз (входящее от юзера и ответ
    # админа). Это гарантирует 100% точность без «накрутки»: раньше первое
    # сообщение юзера (создание топика) считалось и как «получено», и как
    # «отправлено», завышая цифры.
    messages_in = conn.execute(
        "SELECT COUNT(*) FROM feedback_messages WHERE bot_id = ? AND direction = 'in'",
        (bot_id,),
    ).fetchone()[0]
    messages_out = conn.execute(
        "SELECT COUNT(*) FROM feedback_messages WHERE bot_id = ? AND direction = 'out'",
        (bot_id,),
    ).fetchone()[0]

    # Смещения антинакрутки: если владелец подтвердил, что наплыв ПЗ был спамом,
    # «накрученные» сообщения/юзеры вычитаются из статистики — цифры снова
    # показывают только реальную работу.
    offsets = get_stats_offsets(bot_id)

    users_total = max(0, users["total"] - offsets["users_total"])
    users_blocked = min(max(0, users["blocked"]), users_total)
    return {
        "users_total": users_total,
        "users_blocked": users_blocked,
        "users_active": max(0, users_total - users_blocked),
        "messages_in": max(0, messages_in - offsets["messages_in"]),
        "messages_out": max(0, messages_out - offsets["messages_out"]),
        "mailings_count": mailings_count,
        "mailings_sent": mailings_sent,
        "mailings_failed": mailings_failed,
    }

def get_raw_counts(bot_id: int) -> dict:
    """«Сырые» счётчики статистики бота (без смещений антинакрутки).

    Используется защитой от накрутки: при срабатывании запоминаются актуальные
    цифры, чтобы владелец мог решить, засчитывать их или нет.
    """
    conn = _get_conn()
    users = get_child_users_count(bot_id)
    messages_in = conn.execute(
        "SELECT COUNT(*) FROM feedback_messages WHERE bot_id = ? AND direction = 'in'",
        (bot_id,),
    ).fetchone()[0]
    messages_out = conn.execute(
        "SELECT COUNT(*) FROM feedback_messages WHERE bot_id = ? AND direction = 'out'",
        (bot_id,),
    ).fetchone()[0]
    return {
        "users_total": users["total"],
        "users_blocked": users["blocked"],
        "messages_in": messages_in,
        "messages_out": messages_out,
    }

def get_stats_offsets(bot_id: int) -> dict:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM stats_offsets WHERE bot_id = ?", (bot_id,)
    ).fetchone()
    if not row:
        return {"messages_in": 0, "messages_out": 0, "users_total": 0}
    return {
        "messages_in": max(0, int(row["messages_in"] or 0)),
        "messages_out": max(0, int(row["messages_out"] or 0)),
        "users_total": max(0, int(row["users_total"] or 0)),
    }

def set_stats_offsets(bot_id: int, messages_in: int, messages_out: int,
                      users_total: int = 0) -> None:
    """Ставит абсолютные смещения статистики бота (см. get_stats)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO stats_offsets (bot_id, messages_in, messages_out, users_total) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(bot_id) DO UPDATE SET "
            "messages_in=excluded.messages_in, messages_out=excluded.messages_out, "
            "users_total=excluded.users_total",
            (bot_id, max(0, int(messages_in)), max(0, int(messages_out)),
             max(0, int(users_total))),
        )
        conn.commit()

def clear_stats_offsets(bot_id: int) -> None:
    """Сбрасывает смещения статистики (наплыв ПЗ признан реальным)."""
    set_stats_offsets(bot_id, 0, 0, 0)

def get_all_stats(bot_ids: list[int]) -> dict:
    totals = {
        "users_total": 0, "users_blocked": 0, "users_active": 0,
        "messages_in": 0, "messages_out": 0,
        "mailings_count": 0, "mailings_sent": 0, "mailings_failed": 0,
    }
    for bid in bot_ids:
        s = get_stats(bid)
        for k in totals:
            totals[k] += s[k]
    return totals

def save_mailing(bot_id: int, text: str, media_type: str, media_id: str, sent: int, failed: int) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO mailings (bot_id, text, media_type, media_id, sent, failed) VALUES (?, ?, ?, ?, ?, ?)",
            (bot_id, text, media_type, media_id, sent, failed)
        )
        conn.commit()
