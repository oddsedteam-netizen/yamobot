"""Хранилище: очередь недоставленных сообщений (см. services/delivery.py).

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""

import json

from services.db.connection import (
    _get_conn,
    _lock,
)


OUTBOX_PENDING = "pending"    # ждёт отправки (или повторной попытки)

OUTBOX_SENT = "sent"          # доставлено — успех

OUTBOX_FAILED = "failed"      # доставка невозможна (юзер заблокировал бота)

def enqueue_outbox(bot_id: int, chat_id: int, kind: str, payload: dict,
                   thread_id: int = 0) -> int:
    """Кладёт сообщение в очередь на отправку и возвращает id записи."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO outbox (bot_id, chat_id, thread_id, kind, payload, "
            "status, next_attempt_at) VALUES (?, ?, ?, ?, ?, ?, "
            "datetime('now'))",
            (int(bot_id or 0), int(chat_id), int(thread_id or 0), kind,
             json.dumps(payload, ensure_ascii=False), OUTBOX_PENDING),
        )
        conn.commit()
        return int(cur.lastrowid or 0)

def fetch_due_outbox(limit: int = 20) -> list[dict]:
    """Записи, которые пора отправить (или повторить), с самыми старыми вперёд.

    Берём записи, у которых ``next_attempt_at`` наступил. У только что
    добавленных он сразу равен текущему времени, поэтому они тоже попадают
    в выборку без задержки.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM outbox WHERE status = ? AND "
        "(next_attempt_at IS NULL OR next_attempt_at <= datetime('now')) "
        "ORDER BY id ASC LIMIT ?",
        (OUTBOX_PENDING, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]

def mark_outbox_sent(row_id: int) -> None:
    """Сообщение доставлено — запись убирается из активной очереди."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE outbox SET status = ?, last_error = '' WHERE id = ?",
            (OUTBOX_SENT, int(row_id)),
        )
        conn.commit()

def reschedule_outbox(row_id: int, attempts: int, error: str,
                      delay_seconds: float) -> None:
    """Отправка не удалась — повторить позже, причину сохранить.

    Паузу принудительно ограничиваем снизу одной секундой: иначе при
    ошибке «повторить через 0с» воркер устроил бы горячий цикл.
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE outbox SET attempts = ?, last_error = ?, "
            "next_attempt_at = datetime('now', ?) WHERE id = ?",
            (int(attempts), (error or "")[:300],
             f"+{max(1, int(delay_seconds))} seconds", int(row_id)),
        )
        conn.commit()

def fail_outbox(row_id: int, attempts: int, error: str) -> None:
    """Доставка невозможна окончательно (юзер заблокировал бота и т.п.)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE outbox SET status = ?, attempts = ?, last_error = ? WHERE id = ?",
            (OUTBOX_FAILED, int(attempts), (error or "")[:300], int(row_id)),
        )
        conn.commit()

def outbox_counts() -> dict:
    """Сколько сообщений ждёт, сколько доставлено, сколько не дошло."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM outbox GROUP BY status"
    ).fetchall()
    counts = {OUTBOX_PENDING: 0, OUTBOX_SENT: 0, OUTBOX_FAILED: 0}
    for status, total in rows:
        counts[str(status)] = int(total)
    return counts

def outbox_failed_list(limit: int = 10) -> list[dict]:
    """Последние недоставленные сообщения — чтобы понять, что сломалось."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM outbox WHERE status = ? ORDER BY id DESC LIMIT ?",
        (OUTBOX_FAILED, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]

def purge_outbox(keep_days: int = 7) -> int:
    """Чистит старые записи, чтобы таблица не росла бесконечно.

    Записи в статусе ``pending`` НЕ трогаем: это ещё не доставленные
    сообщения, они должны дойти.
    """
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "DELETE FROM outbox WHERE status != ? AND "
            "created_at < datetime('now', ?)",
            (OUTBOX_PENDING, f"-{int(keep_days)} days"),
        )
        conn.commit()
        return cur.rowcount
