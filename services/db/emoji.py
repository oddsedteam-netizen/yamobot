"""Хранилище: словарь премиум-эмодзи (emoji → custom_emoji_id).

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""


from services.db.connection import (
    _get_conn,
    _lock,
)


def remember_custom_emoji(emoji: str, custom_emoji_id: str) -> bool:
    """Запоминает премиум-эмодзи по его обычному символу.

    Возвращает True, если пара новая (или обновилась) — то есть словарь пополнился.
    Если для этого символа уже сохранён ДРУГОЙ id, оставляем прежний: главное,
    чтобы владелец получил премиум-эмодзи, а не «перескок» на чужой эмодзи.
    """
    emoji = (emoji or "").strip("\u200b")
    custom_emoji_id = str(custom_emoji_id or "")
    if not emoji or not custom_emoji_id:
        return False

    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT custom_emoji_id FROM emoji_map WHERE emoji = ?", (emoji,)
        ).fetchone()
        if row is not None:
            if row["custom_emoji_id"] == custom_emoji_id:
                # Уже знаем — просто отмечаем использование (для статистики).
                conn.execute(
                    "UPDATE emoji_map SET uses = uses + 1, updated_at = CURRENT_TIMESTAMP "
                    "WHERE emoji = ?",
                    (emoji,),
                )
                conn.commit()
                return False
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO emoji_map (emoji, custom_emoji_id, uses) VALUES (?, ?, 1)",
            (emoji, custom_emoji_id),
        )
        conn.commit()
        return True

def get_emoji_map() -> dict[str, str]:
    """Весь словарь премиум-эмодзи: «обычный эмодзи» → custom_emoji_id."""
    conn = _get_conn()
    rows = conn.execute("SELECT emoji, custom_emoji_id FROM emoji_map").fetchall()
    return {r["emoji"]: r["custom_emoji_id"] for r in rows}
