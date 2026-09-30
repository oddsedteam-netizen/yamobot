"""Хранилище: логи переписки (раздел «Логи» и техподдержка).

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""


from services.db.connection import (
    _get_conn,
    _lock,
)


LOG_MAX_CHARS = 2500

LOG_MAX_MESSAGES = 40

def save_log_message(bot_id: int, user_chat_id: int, direction: str,
                     text: str, username: str = "") -> None:
    """Сохраняет текст сообщения (in — от ПЗ, out — ответ админа)."""
    clean = (text or "").strip()
    if not clean:
        return
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO user_logs (bot_id, user_chat_id, direction, text, username) "
            "VALUES (?, ?, ?, ?, ?)",
            (bot_id, user_chat_id, direction, clean[:2000], username or ""),
        )
        # Подчищаем старое, чтобы таблица не росла бесконечно.
        conn.execute(
            "DELETE FROM user_logs WHERE id NOT IN "
            "(SELECT id FROM user_logs ORDER BY id DESC LIMIT 2000)"
        )
        conn.commit()

def get_user_logs(user_chat_id: int, limit: int = LOG_MAX_MESSAGES) -> list[dict]:
    """Последние сообщения пользователя (его тексты и ответы админов)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM user_logs WHERE user_chat_id = ? ORDER BY id DESC LIMIT ?",
        (user_chat_id, int(limit)),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]

def format_user_logs(user_chat_id: int, limit: int = LOG_MAX_MESSAGES) -> str:
    """Готовый текст логов для отправки в цитировании и свёрнутом виде."""
    logs = get_user_logs(user_chat_id, limit)
    if not logs:
        return "Логов пока нет: в этом боте ещё не было переписки."

    lines: list[str] = []
    size = 0
    for item in logs:
        who = "Пользователь" if item["direction"] == "in" else "Админ"
        text = " ".join(str(item["text"]).split())[:300]
        line = f"{who}: {text}"
        if size + len(line) > LOG_MAX_CHARS:
            lines.append("…")
            break
        lines.append(line)
        size += len(line)
    return "\n\n".join(lines)

ERRORS_PER_BOT = 25

def save_bot_error(bot_id: int, message: str, detail: str = "",
                   owner_id: int = 0, source: str = "") -> None:
    """Записывает ошибку обработчика конкретного бота."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO bot_errors (bot_id, owner_id, source, message, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(bot_id), int(owner_id or 0), (source or "")[:60],
             (message or "")[:300], (detail or "")[:1500]),
        )
        # Держим только последние записи по каждому боту.
        conn.execute(
            "DELETE FROM bot_errors WHERE bot_id = ? AND id NOT IN "
            "(SELECT id FROM bot_errors WHERE bot_id = ? ORDER BY id DESC LIMIT ?)",
            (int(bot_id), int(bot_id), 100),
        )
        conn.commit()

def get_bot_errors(bot_id: int, limit: int = ERRORS_PER_BOT) -> list[dict]:
    """Последние ошибки бота — свежие сверху."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM bot_errors WHERE bot_id = ? ORDER BY id DESC LIMIT ?",
        (int(bot_id), int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]

def get_owner_bot_errors(owner_id: int, limit: int = ERRORS_PER_BOT) -> list[dict]:
    """Ошибки всех ботов пользователя (свежие сверху)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM bot_errors WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
        (int(owner_id), int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]

def format_bot_errors(entries: list[dict]) -> str:
    """Готовый текст журнала ошибок для свёрнутого блока."""
    if not entries:
        return "Ошибок не зафиксировано — бот работает штатно."

    lines: list[str] = []
    size = 0
    for item in entries:
        head = f"{str(item.get('created_at') or '')[:19]} · {item.get('message') or ''}"
        block = f"⚠️ {head}"
        detail = " ".join(str(item.get("detail") or "").split())[:400]
        if detail:
            block += f"\n    {detail}"
        if size + len(block) > LOG_MAX_CHARS:
            lines.append("…")
            break
        lines.append(block)
        size += len(block)
    return "\n\n".join(lines)
