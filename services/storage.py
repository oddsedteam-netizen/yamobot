import json
import logging
import sqlite3
from pathlib import Path
from threading import Lock

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "yamobot.db"

_lock = Lock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def ensure_db() -> None:
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bots (
            id          INTEGER PRIMARY KEY,
            owner_id    INTEGER NOT NULL,
            token       TEXT NOT NULL,
            username    TEXT DEFAULT '',
            first_name  TEXT DEFAULT '',
            welcome_text TEXT DEFAULT '',
            links       TEXT DEFAULT '[]',
            stopped     INTEGER DEFAULT 0,
            antispam_mode TEXT DEFAULT 'off',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id      INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL,
            username    TEXT DEFAULT '',
            first_name  TEXT DEFAULT '',
            blocked     INTEGER DEFAULT 0,
            first_seen  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(bot_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id      INTEGER NOT NULL,
            event       TEXT NOT NULL,
            count       INTEGER DEFAULT 1,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS mailings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id      INTEGER NOT NULL,
            text        TEXT DEFAULT '',
            media_type  TEXT DEFAULT '',
            media_id    TEXT DEFAULT '',
            sent        INTEGER DEFAULT 0,
            failed      INTEGER DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()


# ═══════════════════════════════════════════════════════════
#  Боты
# ═══════════════════════════════════════════════════════════

def get_user_bots(user_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM bots WHERE owner_id = ?", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_user_bot(user_id: int, bot_info: dict) -> None:
    conn = _get_conn()
    with _lock:
        existing = conn.execute(
            "SELECT id FROM bots WHERE id = ?", (bot_info["id"],)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE bots SET token=?, username=?, first_name=?, owner_id=? WHERE id=?",
                (bot_info["token"], bot_info.get("username", ""),
                 bot_info.get("first_name", ""), user_id, bot_info["id"])
            )
        else:
            conn.execute(
                "INSERT INTO bots (id, owner_id, token, username, first_name, welcome_text, links, stopped) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bot_info["id"], user_id, bot_info["token"],
                 bot_info.get("username", ""), bot_info.get("first_name", ""),
                 bot_info.get("welcome_text", ""), json.dumps(bot_info.get("links", [])),
                 0)
            )
        conn.commit()


def remove_user_bot(user_id: int, bot_id: int) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "DELETE FROM bots WHERE id = ? AND owner_id = ?", (bot_id, user_id)
        )
        conn.execute("DELETE FROM users WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM stats WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM mailings WHERE bot_id = ?", (bot_id,))
        conn.commit()
        return cur.rowcount > 0


def get_bot_by_id(user_id: int, bot_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM bots WHERE id = ? AND owner_id = ?", (bot_id, user_id)
    ).fetchone()
    return dict(row) if row else None


def get_bot_by_id_any_owner(bot_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    return dict(row) if row else None


def update_bot_field(user_id: int, bot_id: int, field: str, value) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            f"UPDATE bots SET {field} = ? WHERE id = ? AND owner_id = ?",
            (value, bot_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0


def get_all_bots_flat() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM bots").fetchall()
    return [dict(r) for r in rows]


def bot_display_name(b: dict) -> str:
    name = b.get("first_name") or b.get("username") or f"bot_{b['id']}"
    username = f" (@{b['username']})" if b.get("username") else ""
    return f"{name}{username}"


# ═══════════════════════════════════════════════════════════
#  Линки
# ═══════════════════════════════════════════════════════════

def get_bot_links(user_id: int, bot_id: int) -> list[dict]:
    bot = get_bot_by_id(user_id, bot_id)
    if not bot:
        return []
    try:
        return json.loads(bot.get("links", "[]"))
    except (json.JSONDecodeError, TypeError):
        return []


def set_bot_links(user_id: int, bot_id: int, links: list[dict]) -> bool:
    return update_bot_field(user_id, bot_id, "links", json.dumps(links, ensure_ascii=False))


# ═══════════════════════════════════════════════════════════
#  Пользователи дочерних ботов
# ═══════════════════════════════════════════════════════════

def add_child_user(bot_id: int, chat_id: int, username: str = "", first_name: str = "") -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO users (bot_id, chat_id, username, first_name) VALUES (?, ?, ?, ?)",
            (bot_id, chat_id, username, first_name)
        )
        conn.commit()


def mark_user_blocked(bot_id: int, chat_id: int) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE users SET blocked = 1 WHERE bot_id = ? AND chat_id = ?",
            (bot_id, chat_id)
        )
        conn.commit()


def get_child_users(bot_id: int, only_active: bool = True) -> list[dict]:
    conn = _get_conn()
    if only_active:
        rows = conn.execute(
            "SELECT * FROM users WHERE bot_id = ? AND blocked = 0", (bot_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM users WHERE bot_id = ?", (bot_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_child_users_count(bot_id: int) -> dict:
    conn = _get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM users WHERE bot_id = ?", (bot_id,)
    ).fetchone()[0]
    blocked = conn.execute(
        "SELECT COUNT(*) FROM users WHERE bot_id = ? AND blocked = 1", (bot_id,)
    ).fetchone()[0]
    return {"total": total, "blocked": blocked, "active": total - blocked}


# ═══════════════════════════════════════════════════════════
#  Статистика
# ═══════════════════════════════════════════════════════════

def add_stat(bot_id: int, event: str, count: int = 1) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO stats (bot_id, event, count) VALUES (?, ?, ?)",
            (bot_id, event, count)
        )
        conn.commit()


def get_stats(bot_id: int) -> dict:
    conn = _get_conn()

    def _sum(evt: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM stats WHERE bot_id = ? AND event = ?",
            (bot_id, evt)
        ).fetchone()
        return row[0]

    users = get_child_users_count(bot_id)

    mailings_count = conn.execute(
        "SELECT COUNT(*) FROM mailings WHERE bot_id = ?", (bot_id,)
    ).fetchone()[0]

    mailings_sent = conn.execute(
        "SELECT COALESCE(SUM(sent), 0) FROM mailings WHERE bot_id = ?", (bot_id,)
    ).fetchone()[0]

    mailings_failed = conn.execute(
        "SELECT COALESCE(SUM(failed), 0) FROM mailings WHERE bot_id = ?", (bot_id,)
    ).fetchone()[0]

    return {
        "users_total": users["total"],
        "users_blocked": users["blocked"],
        "users_active": users["active"],
        "messages_in": _sum("message_in"),
        "messages_out": _sum("message_out"),
        "mailings_count": mailings_count,
        "mailings_sent": mailings_sent,
        "mailings_failed": mailings_failed,
    }


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


# ═══════════════════════════════════════════════════════════
#  Рассылки
# ═══════════════════════════════════════════════════════════

def save_mailing(bot_id: int, text: str, media_type: str, media_id: str,
                 sent: int, failed: int) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO mailings (bot_id, text, media_type, media_id, sent, failed) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bot_id, text, media_type, media_id, sent, failed)
        )
        conn.commit()


# ═══════════════════════════════════════════════════════════
#  Антиспам
# ═══════════════════════════════════════════════════════════

def get_antispam_mode(bot_id: int) -> str:
    conn = _get_conn()
    row = conn.execute(
        "SELECT antispam_mode FROM bots WHERE id = ?", (bot_id,)
    ).fetchone()
    return row[0] if row else "off"


def set_antispam_mode(user_id: int, bot_id: int, mode: str) -> bool:
    return update_bot_field(user_id, bot_id, "antispam_mode", mode)