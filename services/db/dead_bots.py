"""Хранилище: мёртвые боты (авто-детект + удаление пачкой).

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""


from services.db.connection import (
    _get_conn,
    _lock,
)
from services.db.admins import get_bot_owner
from services.db.bots import remove_user_bot


def mark_bot_dead(bot_id: int, reason: str = "unauthorized") -> None:
    """Помечает бота «мёртвым»: токен не работает / бот удалён.

    Повторный вызов обновляет причину и время обнаружения (бот «ожил» —
    см. :func:`clear_bot_dead`).
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO dead_bots (bot_id, reason, detected_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(bot_id) DO UPDATE SET reason=excluded.reason, "
            "detected_at=CURRENT_TIMESTAMP",
            (bot_id, reason or "unauthorized"),
        )
        conn.commit()

def clear_bot_dead(bot_id: int) -> None:
    """Снимает пометку «мёртвый» (бот снова отвечает)."""
    conn = _get_conn()
    with _lock:
        conn.execute("DELETE FROM dead_bots WHERE bot_id = ?", (bot_id,))
        conn.commit()

def is_bot_dead(bot_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM dead_bots WHERE bot_id = ?", (bot_id,)).fetchone()
    return bool(row)

def get_dead_bots() -> list[dict]:
    """Список помеченных «мёртвых» ботов вместе с данными бота и владельца.

    Боты, уже удалённые из панели, в список не попадают (запись без бота
    бесполезна) — такие «хвосты» вычищаются на месте.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT d.bot_id, d.reason, d.detected_at, "
        "       b.username, b.first_name, b.owner_id "
        "FROM dead_bots AS d JOIN bots AS b ON b.id = d.bot_id "
        "ORDER BY d.detected_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]

def remove_dead_bots() -> list[int]:
    """Удаляет всех помеченных «мёртвых» ботов вместе с их данными.

    Возвращает список удалённых bot_id (чтобы вызывающий код мог остановить
    их опрос в менеджере дочерних ботов).
    """
    removed = [int(row["bot_id"]) for row in get_dead_bots()]
    for bot_id in removed:
        owner_id = get_bot_owner(bot_id)
        if owner_id:
            remove_user_bot(int(owner_id), bot_id)
        else:
            conn = _get_conn()
            with _lock:
                conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
                conn.commit()
        clear_bot_dead(bot_id)
    return removed
