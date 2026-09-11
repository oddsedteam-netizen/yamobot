import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "yamobot.db"

_lock = Lock()
_conn: sqlite3.Connection | None = None

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


def _migrate_bot_type(conn: sqlite3.Connection) -> None:
    """Добавляет колонку bot_type в существующую таблицу bots."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bots)").fetchall()]
    if "bot_type" not in cols:
        conn.execute("ALTER TABLE bots ADD COLUMN bot_type TEXT DEFAULT 'standard'")
    conn.commit()


def _migrate_admin_scope(conn: sqlite3.Connection) -> None:
    """Добавляет owner_id в admins и меняет уникальность на (owner_id, user_id)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(admins)").fetchall()]
    if "owner_id" in cols:
        return
    conn.executescript("""
        CREATE TABLE admins_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id    INTEGER NOT NULL DEFAULT 0,
            user_id     INTEGER NOT NULL,
            username    TEXT DEFAULT '',
            tag         TEXT NOT NULL,
            active      INTEGER DEFAULT 1,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(owner_id, user_id)
        );
        INSERT INTO admins_new (owner_id, user_id, username, tag, active, created_at)
            SELECT 0, user_id, username, tag, active, created_at FROM admins;
        DROP TABLE admins;
        ALTER TABLE admins_new RENAME TO admins;
    """)
    conn.commit()


def _migrate_bot_anonymous(conn: sqlite3.Connection) -> None:
    """Добавляет колонку anonymous_mode в таблицу bots."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bots)").fetchall()]
    if "anonymous_mode" not in cols:
        conn.execute("ALTER TABLE bots ADD COLUMN anonymous_mode INTEGER DEFAULT 0")
    conn.commit()


def _migrate_registry_chats(conn: sqlite3.Connection) -> None:
    """Добавляет колонки привязанных чатов в users_registry."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users_registry)").fetchall()]
    if "work_chat_id" not in cols:
        conn.execute("ALTER TABLE users_registry ADD COLUMN work_chat_id INTEGER DEFAULT 0")
    if "admin_chat_id" not in cols:
        conn.execute("ALTER TABLE users_registry ADD COLUMN admin_chat_id INTEGER DEFAULT 0")
    conn.commit()


def _migrate_registry_pending_bind(conn: sqlite3.Connection) -> None:
    """Добавляет колонку активной привязки в users_registry.

    Сделано для того, чтобы привязка «чата работы»/«чата админов» переживала
    перезапуск бота и не зависела от in-memory словаря (иначе бот «не видит»
    добавление и привязка теряется со временем).
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users_registry)").fetchall()]
    if "pending_bind_kind" not in cols:
        conn.execute("ALTER TABLE users_registry ADD COLUMN pending_bind_kind TEXT DEFAULT ''")
    conn.commit()


def _migrate_admin_invites(conn: sqlite3.Connection) -> None:
    """Добавляет колонки лимита использования в таблицу admin_invites.

    Старые ссылки (до этого обновления) считаются одноразовыми: max_uses = 1.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(admin_invites)").fetchall()]
    if "max_uses" not in cols:
        conn.execute("ALTER TABLE admin_invites ADD COLUMN max_uses INTEGER DEFAULT 1")
    if "used" not in cols:
        conn.execute("ALTER TABLE admin_invites ADD COLUMN used INTEGER DEFAULT 0")
    conn.commit()



def ensure_db() -> None:
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bots (
            id           INTEGER PRIMARY KEY,
            owner_id     INTEGER NOT NULL,
            token        TEXT NOT NULL,
            username     TEXT DEFAULT '',
            first_name   TEXT DEFAULT '',
            welcome_text TEXT DEFAULT '',
            links        TEXT DEFAULT '[]',
            stopped      INTEGER DEFAULT 0,
            antispam_mode TEXT DEFAULT 'off',
            bot_type     TEXT DEFAULT 'standard',
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

        CREATE TABLE IF NOT EXISTS admins (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            owner_id     INTEGER NOT NULL DEFAULT 0,
            username    TEXT DEFAULT '',
            tag         TEXT NOT NULL,
            active      INTEGER DEFAULT 1,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(owner_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS admin_tag_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_user_id   INTEGER NOT NULL,
            old_tag         TEXT DEFAULT '',
            new_tag         TEXT NOT NULL,
            changed_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS admin_messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id          INTEGER NOT NULL,
            admin_user_id   INTEGER NOT NULL,
            direction       TEXT DEFAULT 'out',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS coowners (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id    INTEGER NOT NULL,
            coowner_id  INTEGER NOT NULL,
            username    TEXT DEFAULT '',
            added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(owner_id, coowner_id)
        );

        CREATE TABLE IF NOT EXISTS feedback_chats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id          INTEGER NOT NULL,
            group_chat_id   INTEGER NOT NULL,
            UNIQUE(bot_id, group_chat_id)
        );

        CREATE TABLE IF NOT EXISTS feedback_topics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id          INTEGER NOT NULL,
            user_chat_id    INTEGER NOT NULL,
            group_chat_id   INTEGER NOT NULL,
            topic_id        INTEGER NOT NULL,
            admin_user_id   INTEGER DEFAULT 0,
            admin_tag       TEXT DEFAULT '',
            status          TEXT DEFAULT 'open',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(bot_id, user_chat_id)
        );

        CREATE TABLE IF NOT EXISTS feedback_messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id          INTEGER NOT NULL,
            topic_id        INTEGER NOT NULL,
            group_chat_id   INTEGER NOT NULL,
            user_chat_id    INTEGER NOT NULL,
            direction       TEXT DEFAULT 'in',
            group_msg_id    INTEGER DEFAULT 0,
            user_msg_id     INTEGER DEFAULT 0,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bot_keyboards (
            bot_id       INTEGER PRIMARY KEY,
            owner_id     INTEGER NOT NULL,
            buttons      TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS admin_invites (
            token       TEXT PRIMARY KEY,
            owner_id    INTEGER NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users_registry (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            blocked    INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS transfers (
            token          TEXT PRIMARY KEY,
            from_user_id   INTEGER NOT NULL,
            kind           TEXT NOT NULL,
            bot_id         INTEGER,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS complaints (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            user_username TEXT DEFAULT '',
            category      TEXT NOT NULL,
            screenshot_id TEXT DEFAULT '',
            comment       TEXT DEFAULT '',
            status        TEXT DEFAULT 'new',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at   TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_restrictions (
            bot_id         INTEGER NOT NULL,
            user_chat_id   INTEGER NOT NULL,
            ban_until      TEXT,
            mute_until     TEXT,
            warns          INTEGER DEFAULT 0,
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (bot_id, user_chat_id)
        );

        CREATE TABLE IF NOT EXISTS banned_topics (
            bot_id         INTEGER NOT NULL,
            user_chat_id   INTEGER NOT NULL,
            group_chat_id  INTEGER NOT NULL,
            topic_id       INTEGER NOT NULL,
            banned_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(bot_id, group_chat_id, topic_id)
        );

        CREATE TABLE IF NOT EXISTS warn_settings (
            owner_id         INTEGER PRIMARY KEY,
            max_warns        INTEGER DEFAULT 5,
            punish_type      TEXT DEFAULT 'mute',
            punish_duration  INTEGER DEFAULT 60
        );

        CREATE TABLE IF NOT EXISTS admin_chat_moderators (
            owner_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            username    TEXT DEFAULT '',
            first_name  TEXT DEFAULT '',
            added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (owner_id, user_id)
        );
    """)
    _migrate_bot_type(conn)
    _migrate_admin_scope(conn)
    _migrate_bot_anonymous(conn)
    _migrate_registry_chats(conn)
    _migrate_registry_pending_bind(conn)
    _migrate_admin_invites(conn)
    conn.commit()


# ═══════════════════════════════════════════════════════════
#  Боты
# ═══════════════════════════════════════════════════════════

def get_user_bots(user_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM bots WHERE owner_id = ?", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_accessible_bots(user_id: int) -> list[dict]:
    conn = _get_conn()
    own = conn.execute("SELECT * FROM bots WHERE owner_id = ?", (user_id,)).fetchall()
    co_owners = conn.execute("SELECT owner_id FROM coowners WHERE coowner_id = ?", (user_id,)).fetchall()
    result = [dict(r) for r in own]
    for co in co_owners:
        co_bots = conn.execute("SELECT * FROM bots WHERE owner_id = ?", (co["owner_id"],)).fetchall()
        result.extend([dict(r) for r in co_bots])
    seen = set()
    unique = []
    for b in result:
        if b["id"] not in seen:
            seen.add(b["id"])
            unique.append(b)
    return unique


def add_user_bot(user_id: int, bot_info: dict) -> None:
    conn = _get_conn()
    with _lock:
        existing = conn.execute("SELECT id FROM bots WHERE id = ?", (bot_info["id"],)).fetchone()
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
                 bot_info.get("welcome_text", ""), json.dumps(bot_info.get("links", [])), 0)
            )
        conn.commit()


def remove_user_bot(user_id: int, bot_id: int) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM bots WHERE id = ? AND owner_id = ?", (bot_id, user_id))
        conn.execute("DELETE FROM users WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM stats WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM mailings WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM admin_messages WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM feedback_chats WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM feedback_topics WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM feedback_messages WHERE bot_id = ?", (bot_id,))
        conn.commit()
        return cur.rowcount > 0


def get_bot_by_id(user_id: int, bot_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM bots WHERE id = ? AND owner_id = ?", (bot_id, user_id)).fetchone()
    if row:
        return dict(row)
    co = conn.execute("SELECT owner_id FROM coowners WHERE coowner_id = ?", (user_id,)).fetchall()
    for c in co:
        row = conn.execute("SELECT * FROM bots WHERE id = ? AND owner_id = ?", (bot_id, c["owner_id"])).fetchone()
        if row:
            return dict(row)
    return None


def get_bot_by_id_any_owner(bot_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    return dict(row) if row else None


_BOT_FIELDS_WHITELIST = {
    "token", "username", "first_name", "welcome_text",
    "links", "stopped", "antispam_mode", "bot_type", "anonymous_mode",
}


def update_bot_field(user_id: int, bot_id: int, field: str, value) -> bool:
    if field not in _BOT_FIELDS_WHITELIST:
        raise ValueError(f"Недопустимое поле бота: {field!r}")
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            f"UPDATE bots SET {field} = ? WHERE id = ?",
            (value, bot_id),
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
#  Анонимный режим
# ═══════════════════════════════════════════════════════════

def is_bot_anonymous(bot_id: int) -> bool:
    """Включён ли анонимный режим для бота (по данным любого владельца)."""
    bot = get_bot_by_id_any_owner(bot_id)
    if not bot:
        return False
    return bool(bot.get("anonymous_mode", 0))


def set_bot_anonymous(user_id: int, bot_id: int, enabled: bool) -> bool:
    """Включает/выключает анонимный режим бота."""
    return update_bot_field(user_id, bot_id, "anonymous_mode", 1 if enabled else 0)


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
        conn.execute("UPDATE users SET blocked = 1 WHERE bot_id = ? AND chat_id = ?", (bot_id, chat_id))
        conn.commit()


def get_child_users(bot_id: int, only_active: bool = True) -> list[dict]:
    conn = _get_conn()
    if only_active:
        rows = conn.execute("SELECT * FROM users WHERE bot_id = ? AND blocked = 0", (bot_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM users WHERE bot_id = ?", (bot_id,)).fetchall()
    return [dict(r) for r in rows]


def get_child_users_count(bot_id: int) -> dict:
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM users WHERE bot_id = ?", (bot_id,)).fetchone()[0]
    blocked = conn.execute("SELECT COUNT(*) FROM users WHERE bot_id = ? AND blocked = 1", (bot_id,)).fetchone()[0]
    return {"total": total, "blocked": blocked, "active": total - blocked}


# ═══════════════════════════════════════════════════════════
#  Статистика
# ═══════════════════════════════════════════════════════════

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

    return {
        "users_total": users["total"],
        "users_blocked": users["blocked"],
        "users_active": users["active"],
        "messages_in": messages_in,
        "messages_out": messages_out,
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

def save_mailing(bot_id: int, text: str, media_type: str, media_id: str, sent: int, failed: int) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO mailings (bot_id, text, media_type, media_id, sent, failed) VALUES (?, ?, ?, ?, ?, ?)",
            (bot_id, text, media_type, media_id, sent, failed)
        )
        conn.commit()


# ═══════════════════════════════════════════════════════════
#  Антиспам
# ═══════════════════════════════════════════════════════════

def get_antispam_mode(bot_id: int) -> str:
    conn = _get_conn()
    row = conn.execute("SELECT antispam_mode FROM bots WHERE id = ?", (bot_id,)).fetchone()
    return row[0] if row else "off"


def set_antispam_mode(user_id: int, bot_id: int, mode: str) -> bool:
    return update_bot_field(user_id, bot_id, "antispam_mode", mode)


# ═══════════════════════════════════════════════════════════
#  Админы (глобальные — привязаны ко всем ботам)
# ═══════════════════════════════════════════════════════════

def add_admin(owner_id: int, user_id: int, username: str, tag: str) -> bool:
    conn = _get_conn()
    with _lock:
        try:
            conn.execute(
                "INSERT INTO admins (owner_id, user_id, username, tag) VALUES (?, ?, ?, ?)",
                (owner_id, user_id, username, tag)
            )
            conn.execute(
                "INSERT INTO admin_tag_history (admin_user_id, old_tag, new_tag) VALUES (?, ?, ?)",
                (user_id, "", tag)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_admin(owner_id: int, user_id: int) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM admins WHERE owner_id = ? AND user_id = ?", (owner_id, user_id))
        conn.commit()
        return cur.rowcount > 0


def ensure_admin(owner_id: int, user_id: int, username: str = "") -> bool:
    """Гарантирует, что юзер записан админом владельца (тег = username).

    Нужно после передачи прав: новый владелец автоматически становится админом
    со своим тегом, чтобы при взятии ПЗ название топика было админским тегом,
    а не личным именем/юзером. Существующую запись (в т.ч. легаси owner_id=0)
    не перезаписывает.
    """
    if get_admin_by_user_id(owner_id, user_id):
        return True
    existing_tag = ""
    if owner_id != 0:
        legacy = get_admin_by_user_id(0, user_id)
        if legacy and legacy.get("tag"):
            existing_tag = legacy["tag"]
    tag = existing_tag or username or f"id{user_id}"
    conn = _get_conn()
    with _lock:
        try:
            conn.execute(
                "INSERT INTO admins (owner_id, user_id, username, tag) VALUES (?, ?, ?, ?)",
                (owner_id, user_id, username or "", tag),
            )
            conn.execute(
                "INSERT INTO admin_tag_history (admin_user_id, old_tag, new_tag) VALUES (?, ?, ?)",
                (user_id, "", tag),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Запись уже появилась (гонка) — считаем успехом.
            pass
    return True


def get_admins_all(owner_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM admins WHERE owner_id = ?", (owner_id,)).fetchall()
    return [dict(r) for r in rows]


def get_admin_by_tag(owner_id: int, tag: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM admins WHERE owner_id = ? AND tag = ?", (owner_id, tag)).fetchone()
    if row:
        return dict(row)
    # Легаси-админы, добавленные до появления owner_id, хранятся с owner_id = 0.
    if owner_id != 0:
        row = conn.execute("SELECT * FROM admins WHERE owner_id = 0 AND tag = ?", (tag,)).fetchone()
        if row:
            return dict(row)
    return None


def get_admin_by_user_id(owner_id: int, user_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM admins WHERE owner_id = ? AND user_id = ?", (owner_id, user_id)).fetchone()
    if row:
        return dict(row)
    # Легаси-админы, добавленные до появления owner_id, хранятся с owner_id = 0.
    if owner_id != 0:
        row = conn.execute("SELECT * FROM admins WHERE owner_id = 0 AND user_id = ?", (user_id,)).fetchone()
        if row:
            return dict(row)
    return None


def update_admin_tag(owner_id: int, user_id: int, new_tag: str) -> bool:
    conn = _get_conn()
    with _lock:
        old = conn.execute(
            "SELECT tag FROM admins WHERE owner_id = ? AND user_id = ?", (owner_id, user_id)
        ).fetchone()
        if not old:
            return False
        old_tag = old[0]
        conn.execute(
            "UPDATE admins SET tag = ? WHERE owner_id = ? AND user_id = ?",
            (new_tag, owner_id, user_id)
        )
        conn.execute(
            "INSERT INTO admin_tag_history (admin_user_id, old_tag, new_tag) VALUES (?, ?, ?)",
            (user_id, old_tag, new_tag)
        )
        conn.commit()
        return True


def get_admin_tag_history(owner_id: int, user_id: int) -> list[dict]:
    conn = _get_conn()
    admin = conn.execute(
        "SELECT id FROM admins WHERE owner_id = ? AND user_id = ?", (owner_id, user_id)
    ).fetchone()
    if not admin:
        return []
    rows = conn.execute(
        "SELECT * FROM admin_tag_history WHERE admin_user_id = ? ORDER BY changed_at",
        (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_admin_message(bot_id: int, admin_user_id: int, direction: str = "out") -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO admin_messages (bot_id, admin_user_id, direction) VALUES (?, ?, ?)",
            (bot_id, admin_user_id, direction)
        )
        conn.commit()


def get_bot_owner(bot_id: int) -> int | None:
    conn = _get_conn()
    row = conn.execute("SELECT owner_id FROM bots WHERE id = ?", (bot_id,)).fetchone()
    return row[0] if row else None


def _owner_bot_ids(owner_id: int) -> list[int]:
    conn = _get_conn()
    rows = conn.execute("SELECT id FROM bots WHERE owner_id = ?", (owner_id,)).fetchall()
    return [r[0] for r in rows]


def get_admin_message_stats(owner_id: int, admin_user_id: int) -> dict:
    conn = _get_conn()
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # наивный UTC (как CURRENT_TIMESTAMP)
    day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return {"total": 0, "day": 0, "week": 0, "month": 0}
    placeholders = ",".join("?" for _ in bot_ids)
    params = bot_ids

    def _count(since: str) -> int:
        row = conn.execute(
            f"SELECT COUNT(*) FROM admin_messages WHERE admin_user_id = ? AND created_at >= ? AND bot_id IN ({placeholders})",
            (admin_user_id, since, *params)
        ).fetchone()
        return row[0]

    total = conn.execute(
        f"SELECT COUNT(*) FROM admin_messages WHERE admin_user_id = ? AND bot_id IN ({placeholders})",
        (admin_user_id, *params)
    ).fetchone()[0]

    return {
        "total": total,
        "day": _count(day_ago),
        "week": _count(week_ago),
        "month": _count(month_ago),
    }


def get_admin_active_topics(owner_id: int, admin_user_id: int) -> int:
    conn = _get_conn()
    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return 0
    placeholders = ",".join("?" for _ in bot_ids)
    row = conn.execute(
        f"SELECT COUNT(*) FROM feedback_topics WHERE admin_user_id = ? AND status = 'assigned' AND bot_id IN ({placeholders})",
        (admin_user_id, *bot_ids)
    ).fetchone()
    return row[0]


def get_all_admins_stats(owner_id: int) -> list[dict]:
    admins = get_admins_all(owner_id)
    result = []
    for a in admins:
        stats = get_admin_message_stats(owner_id, a["user_id"])
        topics = get_admin_active_topics(owner_id, a["user_id"])
        result.append({
            "admin": a,
            "stats": stats,
            "active_topics": topics,
        })
    return result


# ═══════════════════════════════════════════════════════════
#  Совладельцы
# ═══════════════════════════════════════════════════════════

def add_coowner(owner_id: int, coowner_id: int, username: str = "") -> bool:
    conn = _get_conn()
    with _lock:
        try:
            conn.execute(
                "INSERT INTO coowners (owner_id, coowner_id, username) VALUES (?, ?, ?)",
                (owner_id, coowner_id, username)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_coowner(owner_id: int, coowner_id: int) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM coowners WHERE owner_id = ? AND coowner_id = ?", (owner_id, coowner_id))
        conn.commit()
        return cur.rowcount > 0


def get_coowners(owner_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM coowners WHERE owner_id = ?", (owner_id,)).fetchall()
    return [dict(r) for r in rows]


def is_coowner(owner_id: int, user_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id FROM coowners WHERE owner_id = ? AND coowner_id = ?",
        (owner_id, user_id)
    ).fetchone()
    return row is not None


# ═══════════════════════════════════════════════════════════
#  Топики обратной связи
# ═══════════════════════════════════════════════════════════

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
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM feedback_topics WHERE bot_id = ? AND user_chat_id = ?",
            (bot_id, user_chat_id)
        )
        conn.commit()


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
            "UPDATE feedback_topics SET admin_user_id = ?, admin_tag = ?, status = 'assigned' "
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
            "UPDATE feedback_topics SET admin_user_id = 0, admin_tag = '', status = 'open' "
            "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ?",
            (bot_id, topic_id, group_chat_id)
        )
        conn.commit()
        return cur.rowcount > 0


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

# ═══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════
#  Клавиатура ботов (настраиваемые кнопки)
# ═══════════════════════════════════════════════════════════

ACTION_ADMIN = "admin"

def default_bot_keyboard() -> list[dict]:
    """Клавиатура по умолчанию: только «сменить админа»."""
    return [{"kind": "admin", "text": "сменить админа"}]


def get_bot_keyboard_raw(bot_id: int) -> str:
    conn = _get_conn()
    row = conn.execute("SELECT buttons FROM bot_keyboards WHERE bot_id = ?", (bot_id,)).fetchone()
    return row[0] if row else ""


def get_bot_keyboard(owner_id: int, bot_id: int) -> list[dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT buttons FROM bot_keyboards WHERE bot_id = ? AND owner_id = ?", (bot_id, owner_id)
    ).fetchone()
    if not row:
        return default_bot_keyboard()
    try:
        buttons = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return default_bot_keyboard()
    return buttons if isinstance(buttons, list) else default_bot_keyboard()


def get_bot_keyboard_by_bot(bot_id: int) -> list[dict]:
    """Для дочернего бота: клавиатура вне зависимости от того, кто владелец."""
    conn = _get_conn()
    row = conn.execute("SELECT buttons FROM bot_keyboards WHERE bot_id = ?", (bot_id,)).fetchone()
    if not row:
        return default_bot_keyboard()
    try:
        buttons = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return default_bot_keyboard()
    return buttons if isinstance(buttons, list) else default_bot_keyboard()
# ═══════════════════════════════════════════════════════════
#  Приглашения админов по ссылке
# ═══════════════════════════════════════════════════════════

def create_admin_invite(owner_id: int, max_uses: int = 1) -> str:
    """Создаёт токен-приглашение админа на `max_uses` человек (по умолчанию — 1)."""
    token = secrets.token_urlsafe(16)
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO admin_invites (token, owner_id, max_uses, used) VALUES (?, ?, ?, 0)",
            (token, owner_id, max_uses)
        )
        conn.commit()
    return token


def get_admin_invite(token: str) -> dict | None:
    """Возвращает инфо о приглашении: {'owner_id', 'max_uses', 'used'} или None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT owner_id, max_uses, used FROM admin_invites WHERE token = ?", (token,)
    ).fetchone()
    return dict(row) if row else None


def get_admin_invite_owner(token: str) -> int | None:
    """Возвращает владельца приглашения (или None). Совместимость со старым кодом."""
    invite = get_admin_invite(token)
    return invite["owner_id"] if invite else None


def consume_admin_invite(token: str) -> int | None:
    """Использует один «слот» приглашения.

    Возвращает количество оставшихся мест (0 — ссылка исчерпана и удалена),
    либо None, если приглашения не существует.
    """
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT max_uses, used FROM admin_invites WHERE token = ?", (token,)
        ).fetchone()
        if not row:
            return None
        new_used = row["used"] + 1
        if new_used >= row["max_uses"]:
            conn.execute("DELETE FROM admin_invites WHERE token = ?", (token,))
            conn.commit()
            return 0
        conn.execute(
            "UPDATE admin_invites SET used = ? WHERE token = ?", (new_used, token)
        )
        conn.commit()
        return row["max_uses"] - new_used


def set_bot_keyboard(owner_id: int, bot_id: int, buttons: list[dict]) -> bool:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO bot_keyboards (bot_id, owner_id, buttons) VALUES (?, ?, ?)",
            (bot_id, owner_id, json.dumps(buttons, ensure_ascii=False))
        )
        conn.commit()
        return True
#  Импорт пользователей + баны
# ═══════════════════════════════════════════════════════════

def import_users_bulk(bot_id: int, users_list: list[dict]) -> int:
    """
    Массовый импорт пользователей.
    users_list: [{"chat_id": 123, "username": "x", "first_name": "Y"}, ...]
    Возвращает количество добавленных.
    """
    conn = _get_conn()
    added = 0
    with _lock:
        for u in users_list:
            try:
                chat_id = int(u.get("chat_id", 0))
                if not chat_id:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO users (bot_id, chat_id, username, first_name) VALUES (?, ?, ?, ?)",
                    (bot_id, chat_id, u.get("username", ""), u.get("first_name", ""))
                )
                if cur.rowcount > 0:
                    added += 1
            except (ValueError, TypeError):
                continue
        conn.commit()
    return added


def ban_user(bot_id: int, chat_id: int) -> None:
    """Помечает юзера как заблокированного (не будет получать рассылки)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO users (bot_id, chat_id, username, first_name, blocked) VALUES (?, ?, '', '', 1)",
            (bot_id, chat_id)
        )
        conn.execute(
            "UPDATE users SET blocked = 1 WHERE bot_id = ? AND chat_id = ?",
            (bot_id, chat_id)
        )
        conn.commit()


def is_user_banned(bot_id: int, chat_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT blocked FROM users WHERE bot_id = ? AND chat_id = ?",
        (bot_id, chat_id)
    ).fetchone()
    if row and row[0]:
        return True
    # Временный бан из «чата админов».
    r = conn.execute(
        "SELECT ban_until FROM user_restrictions WHERE bot_id = ? AND user_chat_id = ?",
        (bot_id, chat_id)
    ).fetchone()
    if r and r[0]:
        return r[0] > _now_utc_str()
    return False


def unban_user(bot_id: int, chat_id: int) -> bool:
    """Снимает бан с юзера."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE users SET blocked = 0 WHERE bot_id = ? AND chat_id = ?",
            (bot_id, chat_id)
        )
        conn.commit()
        return cur.rowcount > 0


# ═══════════════════════════════════════════════════════════
#  Забаненные топики (для рабочего /unban в топике)
# ═══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
#  Наказания из «чата админов»: бан/мут/преды
# ═══════════════════════════════════════════════════════════

def _now_utc_str() -> str:
    """Текущее время в формате CURRENT_TIMESTAMP (наивный UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _expire_time(minutes: int | None) -> str | None:
    """Время истечения в ISO-строках БД; None означает вечный бан."""
    if not minutes:
        return None
    return (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _restriction_upsert(conn: sqlite3.Connection, bot_id: int, chat_id: int, col: str, value) -> None:
    conn.execute(
        f"INSERT INTO user_restrictions (bot_id, user_chat_id, {col}) VALUES (?, ?, ?) "
        f"ON CONFLICT(bot_id, user_chat_id) DO UPDATE SET {col}=excluded.{col}",
        (bot_id, chat_id, value)
    )


def get_user_restriction(bot_id: int, chat_id: int) -> dict:
    """Текущее состояние ограничений юзера (бан/мут/преды)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT ban_until, mute_until, warns FROM user_restrictions "
        "WHERE bot_id = ? AND user_chat_id = ?",
        (bot_id, chat_id)
    ).fetchone()
    if not row:
        return {"ban_until": None, "mute_until": None, "warns": 0}
    return {"ban_until": row[0], "mute_until": row[1], "warns": row[2]}


def set_user_ban(bot_id: int, chat_id: int, until_iso: str | None = None) -> None:
    """Устанавливает бан. None — навсегда (постоянный флаг), строка — до даты."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO users (bot_id, chat_id, username, first_name) "
            "VALUES (?, ?, '', '')",
            (bot_id, chat_id)
        )
        if until_iso is None:
            # Вечный бан — постоянный флаг, чтобы is_user_banned ловил его всегда.
            conn.execute(
                "UPDATE users SET blocked = 1 WHERE bot_id = ? AND chat_id = ?",
                (bot_id, chat_id)
            )
            _restriction_upsert(conn, bot_id, chat_id, "ban_until", None)
        else:
            conn.execute(
                "UPDATE users SET blocked = 0 WHERE bot_id = ? AND chat_id = ?",
                (bot_id, chat_id)
            )
            _restriction_upsert(conn, bot_id, chat_id, "ban_until", until_iso)
        conn.commit()


def set_user_mute(bot_id: int, chat_id: int, until_iso: str | None) -> None:
    """Устанавливает мут до until_iso."""
    conn = _get_conn()
    with _lock:
        _restriction_upsert(conn, bot_id, chat_id, "mute_until", until_iso)
        conn.execute(
            "INSERT OR IGNORE INTO users (bot_id, chat_id, username, first_name) "
            "VALUES (?, ?, '', '')",
            (bot_id, chat_id)
        )
        conn.commit()


def clear_user_restriction(bot_id: int, chat_id: int) -> None:
    """Снимает бан, мут и преды."""
    conn = _get_conn()
    with _lock:
        _restriction_upsert(conn, bot_id, chat_id, "ban_until", None)
        _restriction_upsert(conn, bot_id, chat_id, "mute_until", None)
        _restriction_upsert(conn, bot_id, chat_id, "warns", 0)
        conn.execute(
            "UPDATE users SET blocked = 0 WHERE bot_id = ? AND chat_id = ?",
            (bot_id, chat_id)
        )
        conn.commit()


def add_user_warn(bot_id: int, chat_id: int) -> int:
    """Записывает один пред. Возвращает новое количество предов юзера."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO user_restrictions (bot_id, user_chat_id, warns) VALUES (?, ?, 1) "
            "ON CONFLICT(bot_id, user_chat_id) DO UPDATE SET warns=warns+1",
            (bot_id, chat_id)
        )
        conn.commit()
        row = conn.execute(
            "SELECT warns FROM user_restrictions WHERE bot_id = ? AND user_chat_id = ?",
            (bot_id, chat_id)
        ).fetchone()
        return row[0] if row else 1


def reset_user_warns(bot_id: int, chat_id: int) -> None:
    conn = _get_conn()
    with _lock:
        _restriction_upsert(conn, bot_id, chat_id, "warns", 0)
        conn.commit()


def get_warn_settings(owner_id: int) -> dict:
    """Порог предов до наказания и само наказание для владельца."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT max_warns, punish_type, punish_duration FROM warn_settings WHERE owner_id = ?",
        (owner_id,)
    ).fetchone()
    if not row:
        return {"max_warns": 5, "punish_type": "mute", "punish_duration": 60}
    return {"max_warns": row[0], "punish_type": row[1], "punish_duration": row[2]}


def set_warn_settings(owner_id: int, max_warns: int, punish_type: str, punish_duration: int) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO warn_settings (owner_id, max_warns, punish_type, punish_duration) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET "
            "max_warns=excluded.max_warns, punish_type=excluded.punish_type, "
            "punish_duration=excluded.punish_duration",
            (owner_id, max_warns, punish_type, punish_duration)
        )
        conn.commit()


def is_user_muted(bot_id: int, chat_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT mute_until FROM user_restrictions WHERE bot_id = ? AND user_chat_id = ?",
        (bot_id, chat_id)
    ).fetchone()
    if row and row[0]:
        return row[0] > _now_utc_str()
    return False


def get_owner_users(owner_id: int) -> list[dict]:
    """Все пользователи дочерних ботов владельца (bot_id, chat_id, username, first_name)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT u.bot_id AS bot_id, u.chat_id AS chat_id, "
        "u.username AS username, u.first_name AS first_name "
        "FROM users u JOIN bots b ON b.id = u.bot_id "
        "WHERE b.owner_id = ?",
        (owner_id,)
    ).fetchall()
    return [dict(r) for r in rows]
def clear_user_restriction_for_owner(owner_id: int, chat_id: int) -> None:
    """Снимает бан, мут и преды у юзера по всем ботам владельца."""
    for bot_id in _owner_bot_ids(owner_id):
        clear_user_restriction(bot_id, chat_id)


def clear_user_mute_for_owner(owner_id: int, chat_id: int) -> None:
    """Снимает только мут у юзера по всем ботам владельца."""
    conn = _get_conn()
    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return
    with _lock:
        for bot_id in bot_ids:
            _restriction_upsert(conn, bot_id, chat_id, "mute_until", None)
        conn.commit()


def reset_user_warns_for_owner(owner_id: int, chat_id: int) -> None:
    """Сбрасывает преды у юзера по всем ботам владельца."""
    conn = _get_conn()
    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return
    with _lock:
        for bot_id in bot_ids:
            _restriction_upsert(conn, bot_id, chat_id, "warns", 0)
        conn.commit()


def add_admin_chat_moderator(owner_id: int, user_id: int, username: str = "", first_name: str = "") -> bool:
    conn = _get_conn()
    with _lock:
        try:
            conn.execute(
                "INSERT INTO admin_chat_moderators (owner_id, user_id, username, first_name) "
                "VALUES (?, ?, ?, ?)",
                (owner_id, user_id, username, first_name)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_admin_chat_moderator(owner_id: int, user_id: int) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "DELETE FROM admin_chat_moderators WHERE owner_id = ? AND user_id = ?",
            (owner_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0


def get_admin_chat_moderators(owner_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM admin_chat_moderators WHERE owner_id = ? ORDER BY added_at ASC",
        (owner_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def is_admin_chat_moderator(owner_id: int, user_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT user_id FROM admin_chat_moderators WHERE owner_id = ? AND user_id = ?",
        (owner_id, user_id)
    ).fetchone()
    return row is not None

# ═══════════════════════════════════════════════════════════
#  Глобальные приветствие и линки для всех ботов юзера
# ═══════════════════════════════════════════════════════════

def set_welcome_for_all(user_id: int, welcome_text: str) -> int:
    """Устанавливает приветствие для всех ботов юзера."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE bots SET welcome_text = ? WHERE owner_id = ?",
            (welcome_text, user_id)
        )
        conn.commit()
        return cur.rowcount


def set_links_for_all(user_id: int, links: list[dict]) -> int:
    """Устанавливает линки для всех ботов юзера."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE bots SET links = ? WHERE owner_id = ?",
            (json.dumps(links, ensure_ascii=False), user_id)
        )
        conn.commit()
        return cur.rowcount

# ═══════════════════════════════════════════════════════════
#  ПЗ (топики) — расширенные функции
# ═══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
#  Тип бота (standard / anketa)
# ═══════════════════════════════════════════════════════════

def set_bot_type(user_id: int, bot_id: int, bot_type: str) -> bool:
    """Устанавливает тип бота: 'standard' или 'anketa'."""
    if bot_type not in ("standard", "anketa"):
        return False
    return update_bot_field(user_id, bot_id, "bot_type", bot_type)


def get_bot_type(bot_id: int) -> str:
    conn = _get_conn()
    row = conn.execute("SELECT bot_type FROM bots WHERE id = ?", (bot_id,)).fetchone()
    return row[0] if row else "standard"


def get_user_bot_types(user_id: int) -> list[str]:
    """Список типов ботов, которые есть у юзера (например, ['standard', 'anketa'])."""
    conn = _get_conn()
    rows = conn.execute("SELECT DISTINCT bot_type FROM bots WHERE owner_id = ?", (user_id,)).fetchall()
    return [r[0] for r in rows if r[0]]


# ═══════════════════════════════════════════════════════════
#  Реестр пользователей YamoBot
# ═══════════════════════════════════════════════════════════

def register_user(user_id: int, username: str = "", first_name: str = "") -> None:
    """Регистрирует/обновляет пользователя мастер-бота."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO users_registry (user_id, username, first_name) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
            "first_name=excluded.first_name",
            (user_id, username, first_name)
        )
        conn.commit()


def get_user_registry(user_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users_registry WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


# ═══════════════════════════════════════════════════════════
#  Привязка чатов работы и админов (в реестре YamoBot)
# ═══════════════════════════════════════════════════════════

_CHAT_BIND_COLUMNS = {"work": "work_chat_id", "admin": "admin_chat_id"}


def get_bound_chat(user_id: int, kind: str) -> int | None:
    """Возвращает ID привязанного чата ('work' или 'admin'), либо None."""
    col = _CHAT_BIND_COLUMNS.get(kind)
    if not col:
        return None
    row = get_user_registry(user_id)
    if not row:
        return None
    chat_id = row.get(col) or 0
    return int(chat_id) if chat_id else None


def set_bound_chat(user_id: int, kind: str, chat_id: int | None) -> bool:
    """Привязывает/отвязывает чат ('work' или 'admin') для пользователя."""
    col = _CHAT_BIND_COLUMNS.get(kind)
    if not col:
        return False
    conn = _get_conn()
    with _lock:
        conn.execute(
            f"INSERT INTO users_registry (user_id, {col}) VALUES (?, ?) "
            f"ON CONFLICT(user_id) DO UPDATE SET {col}=excluded.{col}",
            (user_id, chat_id or 0)
        )
        conn.commit()
    return True


def get_all_users_registry() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM users_registry ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_owner_by_admin_chat(chat_id: int) -> int | None:
    """Возвращает владельца, к которому привязан данный «чат админов» (или None)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT user_id FROM users_registry WHERE admin_chat_id = ?",
        (chat_id,)
    ).fetchone()
    return row[0] if row else None


def set_pending_bind(user_id: int, kind: str | None) -> bool:
    """Отмечает, какой чат пользователь сейчас привязывает ('work'/'admin').

    Хранится в БД, чтобы привязка переживала перезапуск бота и не терялась
    (раньше это был in-memory словарь, из-за чего бот со временем «не видел»
    добавление и привязка ломалась).
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO users_registry (user_id, pending_bind_kind) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET pending_bind_kind=excluded.pending_bind_kind",
            (user_id, kind or ""),
        )
        conn.commit()
    return True


def get_pending_bind(user_id: int) -> str | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT pending_bind_kind FROM users_registry WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row:
        return None
    val = (row[0] or "").strip()
    return val if val else None


def get_admin_active_topics_list(owner_id: int, admin_user_id: int) -> list[dict]:
    """Активные топики (ПЗ), закреплённые за конкретным админом."""
    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return []
    placeholders = ",".join("?" for _ in bot_ids)
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT * FROM feedback_topics "
        f"WHERE admin_user_id = ? AND status = 'assigned' AND bot_id IN ({placeholders})",
        (admin_user_id, *bot_ids)
    ).fetchall()
    return [dict(r) for r in rows]


def is_registry_user_banned(user_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT blocked FROM users_registry WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row[0])


def set_registry_user_blocked(user_id: int, blocked: bool) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO users_registry (user_id, blocked) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET blocked=excluded.blocked",
            (user_id, 1 if blocked else 0)
        )
        conn.commit()


# ═══════════════════════════════════════════════════════════
#  Жалобы
# ═══════════════════════════════════════════════════════════

def create_complaint(user_id: int, username: str, category: str,
                     screenshot_id: str, comment: str) -> int:
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO complaints (user_id, user_username, category, screenshot_id, comment) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, category, screenshot_id, comment)
        )
        conn.commit()
        return cur.lastrowid or 0


def get_complaints(status: str | None = None) -> list[dict]:
    conn = _get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM complaints WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM complaints ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_complaint(complaint_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM complaints WHERE id = ?", (complaint_id,)).fetchone()
    return dict(row) if row else None


def set_complaint_status(complaint_id: int, status: str) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE complaints SET status = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, complaint_id)
        )
        conn.commit()
        return cur.rowcount > 0


def complaints_count(status: str | None = None) -> int:
    conn = _get_conn()
    if status:
        row = conn.execute("SELECT COUNT(*) FROM complaints WHERE status = ?", (status,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) FROM complaints").fetchone()
    return row[0]


# ═══════════════════════════════════════════════════════════
#  Передача прав владельца
# ═══════════════════════════════════════════════════════════

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

    Мигрируют: боты, админы, совладельцы, привязанные чаты (работа/админов),
    настройки предов (warn_settings) и модераторы «чата админов».
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
            "UPDATE warn_settings SET owner_id = ? WHERE owner_id = ?", (to_id, from_id)
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