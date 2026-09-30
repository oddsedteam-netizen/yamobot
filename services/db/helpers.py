"""Общие приватные помощники для доменных модулей ``services.db``.

Нужны сразу нескольким доменам (``_owner_bot_ids`` — почти всем), но
тематически принадлежат другим модулям. Если оставить их в ``bots.py`` и
``admins.py``, возникнет циклический импорт (проверяется
``tools/check_db_refs.py``). Поэтому такие помощники живут здесь: модуль
зависит только от ``connection`` и ни от кого больше.
"""

from datetime import datetime, timezone

from services.db.connection import _get_conn


def _now_utc_str() -> str:
    """Текущее время в формате CURRENT_TIMESTAMP (наивный UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _owner_bot_ids(owner_id: int) -> list[int]:
    """id всех ботов владельца — основа почти всех выборок по его ПЗ."""
    conn = _get_conn()
    rows = conn.execute("SELECT id FROM bots WHERE owner_id = ?", (owner_id,)).fetchall()
    return [int(r[0]) for r in rows]
