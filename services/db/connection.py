"""Подключение к базе: путь, соединение, блокировка, часовой пояс.

Единственное место в проекте, где создаётся ``sqlite3.Connection``.
Остальные модули берут соединение через ``_get_conn()`` и пишут под
``_lock``: aiogram обрабатывает апдейты в потоках, а SQLite не любит
параллельную запись.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "yamobot.db"

# Соединение одно на процесс: открывать его на каждый запрос дорого,
# а пул ради одной базы избыточен.
_lock = Lock()
_conn: sqlite3.Connection | None = None

# Московское время (UTC+3) — почти всё в интерфейсе считается по нему.
_MSK_TZ = timezone(timedelta(hours=3))

def utc_to_msk(utc_str: str | None) -> str:
    """Переводит сохранённое в БД UTC-время (строку) в МСК (UTC+3).

    SQLite хранит CURRENT_TIMESTAMP в UTC как наивную строку
    'YYYY-MM-DD HH:MM:SS'. Интерпретируем её как UTC и переводим в МСК.
    """
    if not utc_str:
        return "—"
    s = str(utc_str)
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", ""))
        except ValueError:
            return s
    return (dt.replace(tzinfo=timezone.utc).astimezone(_MSK_TZ)).strftime("%Y-%m-%d %H:%M:%S")

def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
    return _conn
