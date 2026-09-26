import json
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

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


def _migrate_bot_welcome_media(conn: sqlite3.Connection) -> None:
    """Добавляет колонки медиа-приветствия (фото/rich-«статья») в таблицу bots.

    Нужно для поддержки красивых приветствий: фото и rich-сообщений (статей,
    которые Telegram отдаёт как ``message.rich_message``). У уже существующих
    ботов колонки создаются пустыми — поведение остаётся прежним.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bots)").fetchall()]
    if "welcome_photo" not in cols:
        conn.execute("ALTER TABLE bots ADD COLUMN welcome_photo TEXT DEFAULT ''")
    if "welcome_rich" not in cols:
        conn.execute("ALTER TABLE bots ADD COLUMN welcome_rich TEXT DEFAULT ''")
    conn.commit()


def _migrate_bot_category_ask(conn: sqlite3.Connection) -> None:
    """Добавляет колонки «уточнения категории ПЗ» в таблицу bots.

    ``cat_ask_enabled`` — включена ли функция у бота,
    ``cat_ask_categories`` — какие категории предлагать ПЗ (пустая строка =
    набор по умолчанию). У уже существующих ботов значения пустые — поведение
    остаётся прежним, функция просто выключена.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bots)").fetchall()]
    if "cat_ask_enabled" not in cols:
        conn.execute("ALTER TABLE bots ADD COLUMN cat_ask_enabled INTEGER DEFAULT 0")
    if "cat_ask_categories" not in cols:
        conn.execute("ALTER TABLE bots ADD COLUMN cat_ask_categories TEXT DEFAULT ''")
    if "cat_ask_custom" not in cols:
        # Свои категории ПЗ (до 3) с пометкой выключенных через префикс «-».
        conn.execute("ALTER TABLE bots ADD COLUMN cat_ask_custom TEXT DEFAULT ''")
    conn.commit()


def _migrate_bot_admin_change(conn: sqlite3.Connection) -> None:
    """Лимит смен админа для ПЗ (в сутки) + журнал смен.

    ``admin_change_enabled`` — включено ли ограничение у бота,
    ``admin_change_limit`` — сколько смен разрешено ПЗ за сутки (по умолчанию 3).
    Сами смены пишем в ``admin_change_log``, чтобы считать их за текущие сутки.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bots)").fetchall()]
    if "admin_change_enabled" not in cols:
        conn.execute("ALTER TABLE bots ADD COLUMN admin_change_enabled INTEGER DEFAULT 0")
    if "admin_change_limit" not in cols:
        conn.execute("ALTER TABLE bots ADD COLUMN admin_change_limit INTEGER DEFAULT 3")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_change_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id        INTEGER NOT NULL,
            user_chat_id  INTEGER NOT NULL,
            changed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_change_log "
        "ON admin_change_log (bot_id, user_chat_id, changed_at)"
    )
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


def _migrate_antinakrutka_enabled(conn: sqlite3.Connection) -> None:
    """Добавляет колонку enabled в antinakrutka_settings.

    Раньше антинакрутку нельзя было выключить — она всегда следила за наплывом.
    Теперь у защиты есть переключатель «🟢 Включить / 🔴 Выключить», поэтому
    существующим владельцам ставим enabled = 1 (поведение не меняется).
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(antinakrutka_settings)").fetchall()]
    if "enabled" not in cols:
        conn.execute("ALTER TABLE antinakrutka_settings ADD COLUMN enabled INTEGER DEFAULT 1")
    conn.commit()


def _migrate_admin_norms(conn: sqlite3.Connection) -> None:
    """Создаёт таблицу недельных норм админов (раздел «📊 Норма» в профиле).

    Хранит по владельцу: саму норму, первый/последний день подсчёта (0=Пн … 6=Вс),
    включены ли уведомления и метку последнего отправленного периода — чтобы
    уведомление о недоборе приходило ровно один раз за период.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_norms (
            owner_id        INTEGER PRIMARY KEY,
            norm            INTEGER DEFAULT 0,
            start_day       INTEGER DEFAULT 0,
            end_day         INTEGER DEFAULT 4,
            notify_enabled  INTEGER DEFAULT 1,
            last_notified   TEXT DEFAULT '',
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
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


def _migrate_antiraid_del_links_default(conn: sqlite3.Connection) -> None:
    """Одноразово включает отзыв ссылок в антирейде у существующих владельцев.

    Раньше «Удаление ссылок» (del_links) по умолчанию было выключено, из-за чего
    даже при срабатывании антирейда ссылка-приглашение оставалась активной и
    рейдеры могли вернуться по ней же. Миграция включается ОДИН раз (флаг в _meta),
    чтобы потом не перетирать осознанный выбор владельца в профиле.
    """
    try:
        done = conn.execute(
            "SELECT 1 FROM _meta WHERE key = 'antiraid_del_links_default'"
        ).fetchone()
    except sqlite3.OperationalError:
        # _meta мог не существовать на самом старом наборе БД — пересоздадим.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT DEFAULT '')"
        )
        done = conn.execute(
            "SELECT 1 FROM _meta WHERE key = 'antiraid_del_links_default'"
        ).fetchone()
    if done:
        return
    conn.execute("UPDATE antiraid_settings SET del_links = 1 WHERE del_links = 0")
    conn.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES ('antiraid_del_links_default', '1')"
    )
    conn.commit()



def _migrate_single_channel_owner(conn: sqlite3.Connection) -> None:
    """Убирает «двойные» привязки одного ТГК разным людям.

    Старые версии бота не проверяли, кому принадлежит канал: два пользователя
    могли привязать один и тот же ТГК, и любой из них публиковал посты от лица
    канала. Оставляем на канал одну (самую свежую) привязку, остальные снимаем
    вместе с их отложенными постами: дальше ``bind_channel`` не даст появиться
    дублям, а владелец канала может забрать привязку себе.
    """
    dupes = conn.execute(
        "SELECT channel_id FROM tg_channels GROUP BY channel_id "
        "HAVING COUNT(*) > 1"
    ).fetchall()
    for row in dupes:
        channel_id = int(row[0])
        keep = conn.execute(
            "SELECT owner_id FROM tg_channels WHERE channel_id = ? "
            "ORDER BY bound_at DESC, owner_id DESC LIMIT 1",
            (channel_id,),
        ).fetchone()
        if keep is None:
            continue
        keep_owner = int(keep[0])
        others = [
            int(r[0]) for r in conn.execute(
                "SELECT owner_id FROM tg_channels "
                "WHERE channel_id = ? AND owner_id != ?",
                (channel_id, keep_owner),
            ).fetchall()
        ]
        for owner_id in others:
            _drop_channel_binding(conn, owner_id)
        logger.warning(
            "ТГК %s был привязан к нескольким пользователям (%s) — оставляю "
            "привязку %s, у остальных снимаю", channel_id,
            ", ".join(str(o) for o in [keep_owner, *others]), keep_owner,
        )
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
            welcome_photo TEXT DEFAULT '',
            welcome_rich TEXT DEFAULT '',
            cat_ask_enabled INTEGER DEFAULT 0,
            cat_ask_categories TEXT DEFAULT '',
            cat_ask_custom TEXT DEFAULT '',
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

        -- Настройки антирейда для «чата админов» владельца.
        CREATE TABLE IF NOT EXISTS antiraid_settings (
            owner_id        INTEGER PRIMARY KEY,
            enabled         INTEGER DEFAULT 0,
            threshold       INTEGER DEFAULT 10,
            del_links       INTEGER DEFAULT 1,
            del_members     INTEGER DEFAULT 0,
            triggered       INTEGER DEFAULT 0,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Служебные флаги миграций (одноразовые действия).
        CREATE TABLE IF NOT EXISTS _meta (
            key     TEXT PRIMARY KEY,
            value   TEXT DEFAULT ''
        );

        -- Антинакрутка ПЗ: защита владельца от наплыва фейковых «новых ПЗ».
        -- Настраивается в профиле владельца и действует на всех его ботов.
        CREATE TABLE IF NOT EXISTS antinakrutka_settings (
            owner_id        INTEGER PRIMARY KEY,
            count           INTEGER DEFAULT 10,
            window_minutes  INTEGER DEFAULT 5,
            block_topics    INTEGER DEFAULT 1,
            triggered       INTEGER DEFAULT 0,
            snapshot        TEXT DEFAULT '',
            triggered_at    TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Смещения статистики: сколько «накрученных» сообщений/юзеров вычесть
        -- из статистики бота, если владелец подтвердил, что это была накрутка.
        CREATE TABLE IF NOT EXISTS stats_offsets (
            bot_id       INTEGER PRIMARY KEY,
            messages_in  INTEGER DEFAULT 0,
            messages_out INTEGER DEFAULT 0,
            users_total  INTEGER DEFAULT 0
        );

        -- Тихие часы напоминалок: в этот интервал (МСК) напоминания в «чат
        -- админов» не отправляются, чтобы не спамить, когда админы спят.
        CREATE TABLE IF NOT EXISTS reminder_quiet (
            owner_id   INTEGER PRIMARY KEY,
            enabled    INTEGER DEFAULT 1,
            from_time  TEXT DEFAULT '21:00',
            to_time    TEXT DEFAULT '09:00',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Конфиги ботов: сохранённый «слепок» настроек (приветствие, инлайны,
        -- тип бота, антиспам, анонимность и т.д.) под коротким кодом, чтобы
        -- перенести настройки на другого бота или восстановить после сбоя.
        CREATE TABLE IF NOT EXISTS bot_configs (
            code       TEXT PRIMARY KEY,
            owner_id   INTEGER NOT NULL,
            bot_id     INTEGER NOT NULL,
            bot_name   TEXT DEFAULT '',
            data       TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Напоминалки: «авточек ответа админа» и «напоминание про ПЗ».
        CREATE TABLE IF NOT EXISTS reminders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id         INTEGER NOT NULL,
            mode             TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL,
            enabled          INTEGER DEFAULT 1,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Когда последний раз слали напоминание по конкретному топику
        -- (защита от дублирования уведомлений при каждом сканировании).
        CREATE TABLE IF NOT EXISTS reminder_ticks (
            reminder_id  INTEGER NOT NULL,
            topic_key    TEXT NOT NULL,
            last_sent_at TIMESTAMP,
            PRIMARY KEY (reminder_id, topic_key)
        );

        -- Словарь премиум-эмодзи: «обычный эмодзи» → его custom_emoji_id в Telegram.
        -- Заполняется из сообщений владельцев (см. services/premium_emoji.py) и
        -- используется, чтобы автоматически возвращать премиум-эмодзи в уже
        -- сохранённых приветствиях без удаления и повторной привязки ботов.
        CREATE TABLE IF NOT EXISTS emoji_map (
            emoji           TEXT PRIMARY KEY,
            custom_emoji_id TEXT NOT NULL,
            uses            INTEGER DEFAULT 1,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Системные настройки (ссылки, параметры платформы)
        CREATE TABLE IF NOT EXISTS app_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL DEFAULT '',
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- ТГК (Telegram-канал) пользователя: бот добавляется в канал админом
        -- и публикует посты от лица канала. У пользователя один канал, и один
        -- канал — только у одного пользователя (проверяет bind_channel).
        CREATE TABLE IF NOT EXISTS tg_channels (
            owner_id   INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            title      TEXT DEFAULT '',
            username   TEXT DEFAULT '',
            bound_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Кто прямо сейчас просит привязать ТГК. Нужно на случай, когда
        -- Telegram не сообщил инициатора добавления бота в канал (анонимный
        -- админ): привязываем канал владельцу с самой свежей заявкой.
        CREATE TABLE IF NOT EXISTS channel_bind_requests (
            owner_id   INTEGER PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Посты ТГК: отложенные (status='scheduled') и опубликованные.
        -- publish_at — UTC 'YYYY-MM-DD HH:MM:SS' (в интерфейсе показываем МСК).
        CREATE TABLE IF NOT EXISTS channel_posts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id   INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            text       TEXT DEFAULT '',
            photo      TEXT DEFAULT '',
            buttons    TEXT DEFAULT '[]',
            status     TEXT DEFAULT 'scheduled',
            publish_at TEXT DEFAULT '',
            message_id INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Розыгрыши ТГК: для сводки в разделе «📢 Мой ТГК»
        -- (сколько проведено, какие победители).
        CREATE TABLE IF NOT EXISTS channel_giveaways (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id   INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            title      TEXT DEFAULT '',
            winners    TEXT DEFAULT '',
            status     TEXT DEFAULT 'finished',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Мёртвые боты (авто-детект): токен отозван, бот удалён в BotFather
        -- или не запускается. Храним причину и время обнаружения, чтобы
        -- админ-панель показала список и дала удалить их пачкой.
        CREATE TABLE IF NOT EXISTS dead_bots (
            bot_id      INTEGER PRIMARY KEY,
            reason      TEXT DEFAULT '',
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Время работы бота (раздел «🕐 Время работы» в профиле):
        -- если ПЗ пишет в нерабочее время, бот отвечает ему сам.
        -- Логи переписки: Telegram не отдаёт историю, поэтому пишем сами.
        -- Ошибки дочерних ботов: пишем всё, что упало в обработчике,
        -- чтобы пользователь мог прислать их в поддержку, а владелец платформы —
        -- посмотреть по конкретному боту.
        CREATE TABLE IF NOT EXISTS bot_errors (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id      INTEGER NOT NULL,
            owner_id    INTEGER DEFAULT 0,
            source      TEXT DEFAULT '',
            message     TEXT DEFAULT '',
            detail      TEXT DEFAULT '',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_bot_errors_bot
            ON bot_errors (bot_id, id);

        CREATE TABLE IF NOT EXISTS user_logs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id        INTEGER DEFAULT 0,
            user_chat_id  INTEGER NOT NULL,
            direction     TEXT DEFAULT 'in',
            username      TEXT DEFAULT '',
            text          TEXT DEFAULT '',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_user_logs_user
            ON user_logs (user_chat_id, id);

        -- Тикеты поддержки (бывшие жалобы).
        CREATE TABLE IF NOT EXISTS tickets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            username    TEXT DEFAULT '',
            first_name  TEXT DEFAULT '',
            category    TEXT DEFAULT 'other',
            text        TEXT DEFAULT '',
            photos      TEXT DEFAULT '[]',
            has_logs    INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'open',
            answer      TEXT DEFAULT '',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at   TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_tickets_status
            ON tickets (status, id);

        CREATE TABLE IF NOT EXISTS work_hours (
            owner_id     INTEGER PRIMARY KEY,
            enabled      INTEGER DEFAULT 0,
            start        TEXT DEFAULT '09:00',
            end          TEXT DEFAULT '21:00',
            msg_text     TEXT DEFAULT '',
            msg_photo    TEXT DEFAULT '',
            msg_entities TEXT DEFAULT '[]',
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    _migrate_bot_type(conn)
    _migrate_admin_scope(conn)
    _migrate_bot_anonymous(conn)
    _migrate_bot_welcome_media(conn)
    _migrate_bot_category_ask(conn)
    _migrate_bot_admin_change(conn)
    _migrate_registry_chats(conn)
    _migrate_registry_pending_bind(conn)
    _migrate_admin_invites(conn)
    _migrate_antiraid_del_links_default(conn)
    _migrate_antinakrutka_enabled(conn)
    _migrate_admin_norms(conn)
    # Один ТГК — один владелец: снимаем дубли привязок, оставшиеся от старых
    # версий (см. handlers/channels.py — там же проверка прав на привязку).
    _migrate_single_channel_owner(conn)
    _drop_admin_chat_members(conn)
    conn.commit()


def _drop_admin_chat_members(conn: sqlite3.Connection) -> None:
    """Удаляет таблицу учёта участников «чата админов».

    Раздел «Не в списке» удалён: собрать полный состав чата бот не может
    (метода getChatMembers в Bot API нет), и раздел только вводил в
    заблуждение. Таблица больше не нужна — убираем, чтобы не осталось
    мёртвых данных.
    """
    conn.execute("DROP TABLE IF EXISTS admin_chat_members")


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


def add_user_bot(user_id: int, bot_info: dict) -> bool:
    """Привязывает бота к владельцу. False — если бот занят другим владельцем."""
    conn = _get_conn()
    existing = conn.execute("SELECT owner_id FROM bots WHERE id = ?", (bot_info["id"],)).fetchone()
    if existing and existing["owner_id"] != user_id:
        # Токен даёт полный доступ к боту, но привязка уже занята другим владельцем
        # панели: не даём «увести» бота к себе. Передавать права нужно через
        # «👑 Передать права» (или сначала удалить бота у текущего владельца).
        logger.warning(
            "Отклонена привязка бота %s к %s: бот уже привязан к %s",
            bot_info["id"], user_id, existing["owner_id"],
        )
        return False

    with _lock:
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
    return True


def remove_user_bot(user_id: int, bot_id: int) -> bool:
    """Удаляет бота владельца вместе со всеми данными по нему."""
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
        # Удалённый бот не должен «висеть» в списке мёртвых.
        conn.execute("DELETE FROM dead_bots WHERE bot_id = ?", (bot_id,))
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
    "welcome_photo", "welcome_rich", "cat_ask_enabled", "cat_ask_categories",
    "cat_ask_custom", "admin_change_enabled", "admin_change_limit",
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
#  Уточнение категории ПЗ (настройка бота)
# ═══════════════════════════════════════════════════════════
# Если функция включена, а ПЗ не указал категорию в первом сообщении, бот
# спрашивает её инлайн-кнопками и только потом уведомляет «чат админов».

# Категории, которые предлагаются по умолчанию (пока владелец не настроил свои).
DEFAULT_PZ_CATEGORIES: tuple[str, ...] = ("поддержка", "универсал", "общение")


def get_cat_ask_settings(bot_id: int) -> tuple[bool, list[str]]:
    """Настройки «уточнения категории»: (включено, список категорий).

    Категории хранятся одной строкой через запятую; пусто — набор по умолчанию.
    Список никогда не бывает пустым: без категорий вопрос ПЗ не имеет смысла.
    """
    bot = get_bot_by_id_any_owner(bot_id)
    if not bot:
        return False, list(DEFAULT_PZ_CATEGORIES)

    enabled = bool(bot.get("cat_ask_enabled", 0))
    raw = str(bot.get("cat_ask_categories") or "").strip()
    if raw:
        categories = [c.strip().lower() for c in raw.split(",") if c.strip()]
    else:
        categories = []
    return enabled, categories or list(DEFAULT_PZ_CATEGORIES)


def set_cat_ask_enabled(user_id: int, bot_id: int, enabled: bool) -> bool:
    """Включает/выключает «уточнение категории» у бота."""
    return update_bot_field(user_id, bot_id, "cat_ask_enabled", 1 if enabled else 0)


def set_cat_ask_categories(user_id: int, bot_id: int, categories: list[str]) -> bool:
    """Сохраняет список категорий, которые предлагать ПЗ (не пустой)."""
    clean: list[str] = []
    for raw in categories:
        name = str(raw or "").strip().lower().lstrip("#")
        if name and name not in clean:
            clean.append(name)
    if not clean:
        clean = list(DEFAULT_PZ_CATEGORIES)
    return update_bot_field(user_id, bot_id, "cat_ask_categories", ",".join(clean))


# ═══════════════════════════════════════════════════════════
#  Свои категории ПЗ (до 3): добавление, вкл/выкл, удаление
# ═══════════════════════════════════════════════════════════

# Больше трёх своих категорий не добавляем: иначе сообщение ПЗ с кнопками
# растягивается и выглядит «простынёй».
MAX_CUSTOM_CATEGORIES = 3


def get_cat_custom(user_id: int, bot_id: int) -> list[dict]:
    """Свои категории ПЗ: ``[{"name": ..., "active": bool}, ...]``.

    Храним в колонке ``cat_ask_custom`` списком через запятую; выключенные
    помечаем префиксом ``-`` (например ``-реклама, жалоба``).
    """
    bot = get_bot_by_id(user_id, bot_id)
    if not bot:
        return []
    raw = str(bot.get("cat_ask_custom") or "").strip()
    result: list[dict] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        active = True
        if name.startswith("-"):
            active, name = False, name[1:].strip()
        if name:
            result.append({"name": name.lower(), "active": active})
    return result


def set_cat_custom(user_id: int, bot_id: int, items: list[dict]) -> bool:
    """Сохраняет список своих категорий с их состоянием (вкл/выкл)."""
    parts: list[str] = []
    for item in items:
        name = str(item.get("name") or "").strip().lower().lstrip("#")
        if not name:
            continue
        parts.append(name if item.get("active", True) else f"-{name}")
    return update_bot_field(
        user_id, bot_id, "cat_ask_custom", ",".join(parts[:MAX_CUSTOM_CATEGORIES])
    )


def add_custom_category(user_id: int, bot_id: int, name: str) -> tuple[bool, str]:
    """Добавляет свою категорию. Возвращает ``(получилось, текст ответа)``."""
    clean = str(name or "").strip().lstrip("#").lower()
    if not clean:
        return False, "❌ Название не может быть пустым."
    if len(clean) > 24:
        clean = clean[:24]

    current = get_cat_custom(user_id, bot_id)
    if any(item["name"] == clean for item in current):
        return False, f"⚠️ Категория «{clean}» уже есть."
    if len(current) >= MAX_CUSTOM_CATEGORIES:
        return False, (
            f"⚠️ Больше {MAX_CUSTOM_CATEGORIES} своих категорий добавить нельзя.\n"
            "Удали или переименуй одну из текущих."
        )

    current.append({"name": clean, "active": True})
    set_cat_custom(user_id, bot_id, current)
    return True, f"✅ Категория «{clean}» добавлена."


def remove_custom_category(user_id: int, bot_id: int, name: str) -> bool:
    """Удаляет свою категорию по имени."""
    target = str(name or "").strip().lstrip("#").lower()
    current = get_cat_custom(user_id, bot_id)
    left = [item for item in current if item["name"] != target]
    if len(left) == len(current):
        return False
    set_cat_custom(user_id, bot_id, left)
    return True


def toggle_custom_category(user_id: int, bot_id: int, name: str) -> tuple[bool, bool]:
    """Включает/выключает свою категорию. Возвращает ``(нашлась, активна)``."""
    target = str(name or "").strip().lstrip("#").lower()
    current = get_cat_custom(user_id, bot_id)
    if not any(item["name"] == target for item in current):
        return False, False
    for item in current:
        if item["name"] == target:
            item["active"] = not item["active"]
            break
    set_cat_custom(user_id, bot_id, current)
    return True, bool(next(i["active"] for i in current if i["name"] == target))


def get_categories_for_pz(user_id: int, bot_id: int) -> list[str]:
    """Итоговый список категорий, которые бот предложит ПЗ."""
    _enabled, base = get_cat_ask_settings(bot_id)
    result = list(base)
    for item in get_cat_custom(user_id, bot_id):
        if item["active"] and item["name"] not in result:
            result.append(item["name"])
    return result


# ═══════════════════════════════════════════════════════════
#  Лимит смен админа для ПЗ (в сутки)
# ═══════════════════════════════════════════════════════════

DEFAULT_ADMIN_CHANGE_LIMIT = 3
MAX_ADMIN_CHANGE_LIMIT = 20


def get_admin_change_settings(bot_id: int) -> tuple[bool, int]:
    """Настройки лимита смен админа: ``(включено, сколько смен в сутки)``."""
    bot = get_bot_by_id_any_owner(bot_id)
    if not bot:
        return False, DEFAULT_ADMIN_CHANGE_LIMIT
    enabled = bool(bot.get("admin_change_enabled", 0))
    try:
        limit = int(bot.get("admin_change_limit") or DEFAULT_ADMIN_CHANGE_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_ADMIN_CHANGE_LIMIT
    return enabled, max(1, min(limit, MAX_ADMIN_CHANGE_LIMIT))


def set_admin_change_enabled(user_id: int, bot_id: int, enabled: bool) -> bool:
    return update_bot_field(user_id, bot_id, "admin_change_enabled", 1 if enabled else 0)


def set_admin_change_limit(user_id: int, bot_id: int, limit: int) -> bool:
    value = max(1, min(int(limit), MAX_ADMIN_CHANGE_LIMIT))
    return update_bot_field(user_id, bot_id, "admin_change_limit", value)


def count_admin_changes_today(bot_id: int, user_chat_id: int) -> int:
    """Сколько раз ПЗ менял админа за последние сутки."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM admin_change_log "
        "WHERE bot_id = ? AND user_chat_id = ? "
        "AND changed_at >= datetime('now', '-1 day')",
        (bot_id, user_chat_id),
    ).fetchone()
    return int(row[0]) if row else 0


def log_admin_change(bot_id: int, user_chat_id: int) -> None:
    """Записывает смену админа (для подсчёта суточного лимита)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO admin_change_log (bot_id, user_chat_id) VALUES (?, ?)",
            (bot_id, user_chat_id),
        )
        # Подчищаем старые записи, чтобы таблица не росла бесконечно.
        conn.execute("DELETE FROM admin_change_log WHERE changed_at < datetime('now', '-7 day')")
        conn.commit()


def admin_changes_left(bot_id: int, user_chat_id: int) -> int:
    """Сколько смен осталось ПЗ сегодня; -1 — ограничение выключено."""
    enabled, limit = get_admin_change_settings(bot_id)
    if not enabled:
        return -1
    return max(0, limit - count_admin_changes_today(bot_id, user_chat_id))


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


# ═══════════════════════════════════════════════════════════
#  Смещения статистики (антинакрутка)
# ══════════════════════════════════════════════════════════

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
#  Время работы бота (профиль владельца)
# ═══════════════════════════════════════════════════════════
# Если ПЗ пишет в нерабочее время, бот сам отвечает ему: «бот работает с …,
# если кто-то из админов свободен — обязательно ответит». Настройка общая
# для всех ботов владельца, как и остальные разделы профиля.

DEFAULT_WORK_START = "09:00"
DEFAULT_WORK_END = "21:00"
DEFAULT_WORK_MESSAGE = (
    "Извините, наш бот работает с {start} до {end}! "
    "Многие админы заняты или уже спят, но если кто-то будет свободен — "
    "обязательно вам напишет 🤍"
)


def get_work_hours(owner_id: int) -> dict:
    """Настройки времени работы: включено, начало, конец, текст/фото ответа."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM work_hours WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    if not row:
        return {
            "enabled": 0,
            "start": DEFAULT_WORK_START,
            "end": DEFAULT_WORK_END,
            "msg_text": DEFAULT_WORK_MESSAGE,
            "msg_photo": "",
            "msg_entities": "[]",
        }
    return dict(row)


def set_work_hours_enabled(owner_id: int, enabled: bool) -> None:
    _save_work_hours(owner_id, {"enabled": 1 if enabled else 0})


def set_work_hours_time(owner_id: int, start: str, end: str) -> None:
    _save_work_hours(owner_id, {"start": start, "end": end})


def set_work_hours_message(owner_id: int, text: str, photo: str = "",
                            entities: str = "[]") -> None:
    _save_work_hours(owner_id, {"msg_text": text, "msg_photo": photo, "msg_entities": entities})


def _save_work_hours(owner_id: int, values: dict) -> None:
    """Обновляет только переданные поля записи времени работы."""
    if not values:
        return
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO work_hours (owner_id) VALUES (?) "
            "ON CONFLICT(owner_id) DO NOTHING",
            (owner_id,),
        )
        assignments = ", ".join(f"{key} = ?" for key in values)
        conn.execute(
            f"UPDATE work_hours SET {assignments} WHERE owner_id = ?",
            (*values.values(), owner_id),
        )
        conn.commit()


def is_within_work_hours(owner_id: int, now: datetime | None = None) -> bool:
    """Сейчас «рабочее» время бота? Поддерживается интервал через полночь."""
    settings = get_work_hours(owner_id)
    if not settings.get("enabled"):
        return True

    def _parse(value: str) -> tuple[int, int] | None:
        try:
            hh, mm = str(value or "").strip().split(":")
            return int(hh), int(mm)
        except (ValueError, AttributeError):
            return None

    start = _parse(settings.get("start", ""))
    end = _parse(settings.get("end", ""))
    if start is None or end is None:
        return True  # кривые настройки — работаем всегда

    current = (now or datetime.now()).hour * 60 + (now or datetime.now()).minute
    start_min = start[0] * 60 + start[1]
    end_min = end[0] * 60 + end[1]

    if start_min == end_min:
        return True  # круглосуточно
    if start_min < end_min:
        return start_min <= current < end_min
    # Интервал через полночь, например 22:00–08:00.
    return current >= start_min or current < end_min

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


def get_owner_admin_invites(owner_id: int) -> list[dict]:
    """Все действующие ссылки-приглашения админов владельца (новые сверху)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT token, max_uses, used, created_at FROM admin_invites "
        "WHERE owner_id = ? ORDER BY created_at DESC",
        (owner_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_admin_invite(token: str) -> bool:
    """Аннулирует ссылку-приглашение (после этого она не действует)."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM admin_invites WHERE token = ?", (token,))
        conn.commit()
        return cur.rowcount > 0


def update_admin_invite_uses(token: str, max_uses: int) -> bool:
    """Меняет лимит приглашения (для кнопки «Пересоздать»)."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE admin_invites SET max_uses = ?, used = 0 WHERE token = ?",
            (max(1, int(max_uses)), token),
        )
        conn.commit()
        return cur.rowcount > 0


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


def set_welcome_bundle_for_all(user_id: int, welcome_text: str,
                               welcome_photo: str = "",
                               welcome_rich: str = "") -> int:
    """Устанавливает текст + медиа-приветствие (фото/статью) всем ботам юзера."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE bots SET welcome_text = ?, welcome_photo = ?, welcome_rich = ? "
            "WHERE owner_id = ?",
            (welcome_text, welcome_photo or "", welcome_rich or "", user_id)
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


# ═══════════════════════════════════════════════════════════
#  Антирейд «чата админов»
# ═══════════════════════════════════════════════════════════

_ANTIRAID_DEFAULTS = {
    "enabled": 0,
    "threshold": 10,
    # «Удаление ссылок» включено по умолчанию: при рейде ссылку-приглашение
    # надо отзывать, чтобы рейдеры не вернулись по ней же.
    "del_links": 1,
    "del_members": 0,
    "triggered": 0,
}


def get_antiraid_settings(owner_id: int) -> dict:
    """Настройки антирейда владельца (с значениями по умолчанию)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM antiraid_settings WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    settings = dict(_ANTIRAID_DEFAULTS)
    if row:
        for k in settings:
            if k in row.keys():
                settings[k] = row[k]
    settings["enabled"] = int(settings.get("enabled") or 0)
    settings["threshold"] = max(1, int(settings.get("threshold") or 10))
    settings["del_links"] = int(settings.get("del_links") or 0)
    settings["del_members"] = int(settings.get("del_members") or 0)
    settings["triggered"] = int(settings.get("triggered") or 0)
    return settings


def set_antiraid_field(owner_id: int, field: str, value) -> bool:
    """Обновляет одно поле настроек антирейда владельца."""
    if field not in _ANTIRAID_DEFAULTS:
        return False
    conn = _get_conn()
    with _lock:
        conn.execute(
            f"INSERT INTO antiraid_settings (owner_id, {field}) VALUES (?, ?) "
            f"ON CONFLICT(owner_id) DO UPDATE SET {field}=excluded.{field}, "
            "updated_at=CURRENT_TIMESTAMP",
            (owner_id, int(value)),
        )
        conn.commit()
    return True


def set_antiraid_enabled(owner_id: int, enabled: bool) -> bool:
    return set_antiraid_field(owner_id, "enabled", 1 if enabled else 0)


def set_antiraid_threshold(owner_id: int, threshold: int) -> bool:
    return set_antiraid_field(owner_id, "threshold", max(1, int(threshold)))


def set_antiraid_del_links(owner_id: int, value: bool) -> bool:
    return set_antiraid_field(owner_id, "del_links", 1 if value else 0)


def set_antiraid_del_members(owner_id: int, value: bool) -> bool:
    return set_antiraid_field(owner_id, "del_members", 1 if value else 0)


def set_antiraid_triggered(owner_id: int, value: bool) -> bool:
    return set_antiraid_field(owner_id, "triggered", 1 if value else 0)


def reset_all_antiraid_triggered() -> None:
    """Сбрасывает флаг сработавшего антирейда у всех владельцев (при старте бота)."""
    conn = _get_conn()
    conn.execute("UPDATE antiraid_settings SET triggered = 0")
    conn.commit()


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
#  Системные настройки (ссылки на ботов, донат и т.д.)
# ═══════════════════════════════════════════════════════════

def get_app_setting(key: str, default: str = "") -> str:
    conn = _get_conn()
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row and row[0] is not None:
        return str(row[0]).strip()
    return default


def set_app_setting(key: str, value: str) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (key, str(value).strip()),
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

    Мигрируют: боты (приветствие, линки/инлайн-кнопки, тип, анонимность,
    антиспам, клавиатуры), админы, совладельцы, привязанные чаты
    (работа/админов), настройки предов (warn_settings), модераторы «чата
    админов», настройки антирейда, напоминалки.
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
            "INSERT OR REPLACE INTO warn_settings (owner_id, max_warns, punish_type, punish_duration) "
            "SELECT ?, max_warns, punish_type, punish_duration "
            "FROM warn_settings WHERE owner_id = ?",
            (to_id, from_id),
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
        # Напоминалки
        conn.execute(
            "UPDATE reminders SET owner_id = ? WHERE owner_id = ?", (to_id, from_id)
        )
        # Клавиатуры дочерних ботов (кнопки) — переезжают к новому владельцу,
        # чтобы «настройки редактора» не сбрасывались после передачи прав.
        conn.execute(
            "UPDATE bot_keyboards SET owner_id = ? WHERE owner_id = ?", (to_id, from_id)
        )
        # Настройки антирейда «чата админов» — полностью переезжают вместе с чатом.
        conn.execute(
            "INSERT OR REPLACE INTO antiraid_settings "
            "(owner_id, enabled, threshold, del_links, del_members, triggered, updated_at) "
            "SELECT ?, enabled, threshold, del_links, del_members, triggered, updated_at "
            "FROM antiraid_settings WHERE owner_id = ?",
            (to_id, from_id),
        )
        conn.execute("DELETE FROM antiraid_settings WHERE owner_id = ?", (from_id,))
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
        # Клавиатура (кнопки) бота переезжает вместе с ним — иначе у нового
        # владельца настройки бота «сбрасывались» бы в значения по умолчанию.
        if cur.rowcount > 0:
            conn.execute(
                "UPDATE bot_keyboards SET owner_id = ? WHERE bot_id = ? AND owner_id = ?",
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


# ═══════════════════════════════════════════════════════════
#  Напоминалки (авточек ответа админа / напоминание про ПЗ)
# ═══════════════════════════════════════════════════════════

def get_reminders(owner_id: int) -> list[dict]:
    """Все настройки напоминалок владельца (новые сверху)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE owner_id = ? ORDER BY id DESC",
        (owner_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_reminder(owner_id: int, mode: str, duration_seconds: int) -> int:
    """Создаёт напоминалку и возвращает её id."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO reminders (owner_id, mode, duration_seconds) VALUES (?, ?, ?)",
            (owner_id, mode, duration_seconds)
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def set_reminder_enabled(reminder_id: int, enabled: bool) -> bool:
    """Включает/выключает напоминалку."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE reminders SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, reminder_id)
        )
        conn.commit()
        return cur.rowcount > 0


def delete_reminder(reminder_id: int) -> bool:
    """Удаляет напоминалку вместе с историей отправок."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.execute("DELETE FROM reminder_ticks WHERE reminder_id = ?", (reminder_id,))
        conn.commit()
        return cur.rowcount > 0


def get_all_enabled_reminders() -> list[dict]:
    """Все включённые напоминалки всех владельцев (для фонового сканера)."""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM reminders WHERE enabled = 1").fetchall()
    return [dict(r) for r in rows]


def get_reminder_tick(reminder_id: int, topic_key: str) -> str | None:
    """UTC-время последней отправки напоминания по топику (или None)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT last_sent_at FROM reminder_ticks WHERE reminder_id = ? AND topic_key = ?",
        (reminder_id, topic_key)
    ).fetchone()
    return row[0] if row else None


def set_reminder_tick(reminder_id: int, topic_key: str, last_sent_at: str) -> None:
    """Записывает время последней отправки напоминания по топику."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO reminder_ticks (reminder_id, topic_key, last_sent_at) "
            "VALUES (?, ?, ?)",
            (reminder_id, topic_key, last_sent_at)
        )
        conn.commit()


def get_last_admin_reply_at(bot_id: int, topic_id: int, group_chat_id: int) -> str | None:
    """Время последнего ответа админа в топике (direction='out'), UTC-строка."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT created_at FROM feedback_messages "
        "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ? AND direction = 'out' "
        "ORDER BY created_at DESC LIMIT 1",
        (bot_id, topic_id, group_chat_id)
    ).fetchone()
    return row[0] if row else None


# ══════════════════════════════════════════════════════════
#  Словарь премиум-эмодзи (emoji → custom_emoji_id)
# ══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
#  Антинакрутка ПЗ (защита от наплыва фейковых «новых ПЗ»)
# ═══════════════════════════════════════════════════════════

_ANTINAKRUTKA_DEFAULTS = {
    # Включена ли защита (переключатель «🟢 Включить» / «🔴 Выключить»).
    # Когда выключена — бот не следит за наплывом и не присылает уведомлений.
    "enabled": 1,
    "count": 10,
    "window_minutes": 5,
    # «Пропускать ли топики ПЗ при защите»: 1 — да (топики создаются,
    # уведомления приостанавливаются), 0 — нет (бот не создаёт топики и пишет
    # пользователю, что временно не может принять обращение).
    "block_topics": 1,
    "triggered": 0,
    "snapshot": "",
    "triggered_at": None,
}


def get_antinakrutka_settings(owner_id: int) -> dict:
    """Настройки антинакрутки владельца (со значениями по умолчанию)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM antinakrutka_settings WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    settings = dict(_ANTINAKRUTKA_DEFAULTS)
    if row:
        for k in settings:
            if k in row.keys():
                settings[k] = row[k]
    settings["count"] = max(1, int(settings.get("count") or 10))
    settings["window_minutes"] = max(1, int(settings.get("window_minutes") or 5))
    settings["enabled"] = 1 if settings.get("enabled") is None else int(settings["enabled"])
    settings["block_topics"] = int(settings.get("block_topics") or 0)
    settings["triggered"] = int(settings.get("triggered") or 0)
    settings["snapshot"] = settings.get("snapshot") or ""
    return settings


def set_antinakrutka_field(owner_id: int, field: str, value) -> bool:
    """Обновляет одно поле настроек антинакрутки владельца."""
    if field not in _ANTINAKRUTKA_DEFAULTS:
        return False
    conn = _get_conn()
    with _lock:
        conn.execute(
            f"INSERT INTO antinakrutka_settings (owner_id, {field}) VALUES (?, ?) "
            f"ON CONFLICT(owner_id) DO UPDATE SET {field}=excluded.{field}, "
            "updated_at=CURRENT_TIMESTAMP",
            (owner_id, value),
        )
        conn.commit()
    return True


def set_antinakrutka_triggered(owner_id: int, value: bool,
                               snapshot: str | None = None) -> bool:
    """Переключает состояние тревоги антинакрутки (с опциональным снимком статы)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO antinakrutka_settings (owner_id, triggered) VALUES (?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET triggered=excluded.triggered, "
            "updated_at=CURRENT_TIMESTAMP",
            (owner_id, 1 if value else 0),
        )
        if value:
            conn.execute(
                "UPDATE antinakrutka_settings SET snapshot = ?, "
                "triggered_at = CURRENT_TIMESTAMP WHERE owner_id = ?",
                (snapshot or "", owner_id),
            )
        else:
            conn.execute(
                "UPDATE antinakrutka_settings SET snapshot = '', triggered_at = NULL "
                "WHERE owner_id = ?",
                (owner_id,),
            )
        conn.commit()
    return True


def reset_all_antinakrutka_triggered() -> None:
    """Сбрасывает тревогу антинакрутки у всех владельцев (при старте бота)."""
    conn = _get_conn()
    conn.execute("UPDATE antinakrutka_settings SET triggered = 0, snapshot = ''")
    conn.commit()


# ═══════════════════════════════════════════════════════════
#  Норма админов (раздел «📊 Норма» в профиле)
# ═══════════════════════════════════════════════════════════

# Дни недели: 0 — понедельник, 6 — воскресенье.
# По умолчанию период подсчёта — с понедельника (0) по пятницу (4).
_NORM_DEFAULTS = {
    "norm": 0,             # 0 — норма выключена
    "start_day": 0,
    "end_day": 4,
    "notify_enabled": 1,
    "last_notified": "",
}


def get_norm_settings(owner_id: int) -> dict:
    """Настройки нормы владельца (со значениями по умолчанию)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM admin_norms WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    settings = dict(_NORM_DEFAULTS)
    if row:
        for k in settings:
            if k in row.keys():
                settings[k] = row[k]
    settings["norm"] = max(0, int(settings.get("norm") or 0))
    settings["start_day"] = min(6, max(0, int(settings.get("start_day") or 0)))
    settings["end_day"] = min(6, max(0, int(settings.get("end_day") or 0)))
    settings["notify_enabled"] = 1 if settings.get("notify_enabled") is None \
        else int(settings["notify_enabled"])
    settings["last_notified"] = str(settings.get("last_notified") or "")
    return settings


def set_norm_field(owner_id: int, field: str, value) -> bool:
    """Обновляет одно поле настроек нормы владельца."""
    if field not in _NORM_DEFAULTS:
        return False
    conn = _get_conn()
    with _lock:
        conn.execute(
            f"INSERT INTO admin_norms (owner_id, {field}) VALUES (?, ?) "
            f"ON CONFLICT(owner_id) DO UPDATE SET {field}=excluded.{field}, "
            "updated_at=CURRENT_TIMESTAMP",
            (owner_id, value),
        )
        conn.commit()
    return True


def get_all_norm_settings() -> list[dict]:
    """Настройки нормы всех владельцев (для фоновой проверки недобора).

    В каждую запись добавляется ``owner_id`` — по нему отправляется уведомление.
    """
    conn = _get_conn()
    rows = conn.execute("SELECT owner_id FROM admin_norms").fetchall()
    result: list[dict] = []
    for row in rows:
        owner_id = int(row["owner_id"])
        settings = get_norm_settings(owner_id)
        settings["owner_id"] = owner_id
        result.append(settings)
    return result


def get_admin_period_messages(owner_id: int, admin_user_id: int,
                              since: str, until: str) -> int:
    """Сколько сообщений админ отправил за период [since, until) (UTC).

    Считаем все записи admin_messages: и ответы админа в топиках, и его
    действия по кнопкам — это и есть «активность» админа за период.
    """
    bot_ids = _owner_bot_ids(owner_id)
    if not bot_ids:
        return 0
    placeholders = ",".join("?" for _ in bot_ids)
    conn = _get_conn()
    row = conn.execute(
        f"SELECT COUNT(*) FROM admin_messages WHERE admin_user_id = ? "
        f"AND created_at >= ? AND created_at < ? AND bot_id IN ({placeholders})",
        (admin_user_id, since, until, *bot_ids),
    ).fetchone()
    return int(row[0]) if row else 0


def get_norm_period_stats(owner_id: int, since: str, until: str) -> list[dict]:
    """Активность всех админов владельца за период, отсортированная по убыванию.

    Возвращает список словарей: admin, period (сообщений за период),
    stats (день/неделя/месяц/всего), active_topics, reached (набрана ли норма).
    """
    norm = get_norm_settings(owner_id)["norm"]
    result: list[dict] = []
    for a in get_admins_all(owner_id):
        period = get_admin_period_messages(owner_id, a["user_id"], since, until)
        result.append({
            "admin": a,
            "period": period,
            "stats": get_admin_message_stats(owner_id, a["user_id"]),
            "active_topics": get_admin_active_topics(owner_id, a["user_id"]),
            "reached": bool(norm) and period >= norm,
        })
    result.sort(key=lambda item: (-item["period"], str(item["admin"].get("tag") or "")))
    return result


def clear_antinakrutka_snapshot(owner_id: int) -> None:
    """Убирает снимок статистики (решение по наплыву уже принято).

    Флаг ``triggered`` при этом НЕ трогаем: защита снимается отдельно —
    кнопкой «Снять защиту»/«Сброс защиты».
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE antinakrutka_settings SET snapshot = '' WHERE owner_id = ?",
            (owner_id,),
        )
        conn.commit()


# ═══════════════════════════════════════════════════════════
#  Резервация топика (защита от дубликатов ПЗ)
# ═══════════════════════════════════════════════════════════

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
    """Проставляет реальный topic_id у ранее забронированного ПЗ."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE feedback_topics SET topic_id = ?, group_chat_id = ? "
            "WHERE bot_id = ? AND user_chat_id = ?",
            (topic_id, group_chat_id, bot_id, user_chat_id),
        )
        conn.commit()


def is_topic_reserved(bot_id: int, user_chat_id: int) -> bool:
    """Есть ли запись ПЗ (в том числе «забронированная» без topic_id)."""
    return get_topic_by_user(bot_id, user_chat_id) is not None


# ═══════════════════════════════════════════════════════════
#  Тихие часы напоминалок (МСК)
# ═══════════════════════════════════════════════════════════

_REMINDER_QUIET_DEFAULTS = {"enabled": 1, "from_time": "21:00", "to_time": "09:00"}


def get_reminder_quiet(owner_id: int) -> dict:
    """Настройки тихих часов владельца: интервал, когда напоминания молчат.

    По умолчанию: с 21:00 до 09:00 по МСК.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM reminder_quiet WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    s = dict(_REMINDER_QUIET_DEFAULTS)
    if row:
        for k in s:
            if k in row.keys() and row[k] not in (None, ""):
                s[k] = row[k]
    s["enabled"] = int(s.get("enabled") or 0)
    s["from_time"] = str(s.get("from_time") or "21:00")
    s["to_time"] = str(s.get("to_time") or "09:00")
    return s


def set_reminder_quiet(owner_id: int, from_time: str | None = None,
                       to_time: str | None = None, enabled: bool | None = None) -> bool:
    """Обновляет тихие часы (частично: что передали, то и меняем)."""
    current = get_reminder_quiet(owner_id)
    from_time = str(from_time or current["from_time"])
    to_time = str(to_time or current["to_time"])
    if enabled is None:
        enabled_val = int(current["enabled"] or 0)
    else:
        enabled_val = 1 if enabled else 0

    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO reminder_quiet (owner_id, enabled, from_time, to_time) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(owner_id) DO UPDATE SET "
            "enabled=excluded.enabled, from_time=excluded.from_time, "
            "to_time=excluded.to_time, updated_at=CURRENT_TIMESTAMP",
            (owner_id, enabled_val, from_time, to_time),
        )
        conn.commit()
    return True


# ═══════════════════════════════════════════════════════════
#  Конфиги ботов (сохранение/перенос настроек под кодом)
# ═══════════════════════════════════════════════════════════

def _make_config_code() -> str:
    """Короткий код конфига вида ``YM-7F3A-91C2``."""
    raw = secrets.token_hex(4).upper()
    return f"YM-{raw[:4]}-{raw[4:]}"


def normalize_config_code(raw: str) -> str:
    """Приводит введённый код к каноническому виду ``YM-XXXX-XXXX``."""
    text = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if text.startswith("YM"):
        text = text[2:]
    if len(text) == 8:
        return f"YM-{text[:4]}-{text[4:]}"
    return (raw or "").strip().upper()


def create_bot_config(owner_id: int, bot_id: int, bot_name: str,
                      data: dict) -> str:
    """Сохраняет конфиг бота и возвращает его код."""
    conn = _get_conn()
    payload = json.dumps(data, ensure_ascii=False)
    for _ in range(10):
        code = _make_config_code()
        try:
            with _lock:
                conn.execute(
                    "INSERT INTO bot_configs (code, owner_id, bot_id, bot_name, data) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (code, owner_id, bot_id, bot_name, payload),
                )
                conn.commit()
            return code
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Не удалось создать уникальный код конфига")


def get_bot_config(code: str) -> dict | None:
    """Конфиг по коду (или None). Код можно писать без дефисов/в нижнем регистре."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM bot_configs WHERE code = ?", (normalize_config_code(code),)
    ).fetchone()
    return dict(row) if row else None


def get_user_bot_configs(owner_id: int) -> list[dict]:
    """Все конфиги владельца (новые сверху)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM bot_configs WHERE owner_id = ? ORDER BY created_at DESC",
        (owner_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_bot_config_by_bot(bot_id: int) -> dict | None:
    """Последний конфиг, сохранённый именно для этого бота."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM bot_configs WHERE bot_id = ? ORDER BY created_at DESC LIMIT 1",
        (bot_id,),
    ).fetchone()
    return dict(row) if row else None


def update_bot_config(code: str, owner_id: int, bot_id: int, bot_name: str,
                      data: dict) -> bool:
    """Перезаписывает сохранённый конфиг (кнопка «Сохранить» повторно)."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE bot_configs SET owner_id = ?, bot_id = ?, bot_name = ?, data = ? "
            "WHERE code = ?",
            (owner_id, bot_id, bot_name, json.dumps(data, ensure_ascii=False),
             normalize_config_code(code)),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_bot_config(code: str, owner_id: int | None = None) -> bool:
    """Удаляет конфиг («очистить конфиг»)."""
    conn = _get_conn()
    with _lock:
        if owner_id is None:
            cur = conn.execute("DELETE FROM bot_configs WHERE code = ?",
                               (normalize_config_code(code),))
        else:
            cur = conn.execute(
                "DELETE FROM bot_configs WHERE code = ? AND owner_id = ?",
                (normalize_config_code(code), owner_id),
            )
        conn.commit()
        return cur.rowcount > 0
# ═══════════════════════════════════════════════════════════
#  ТГК (Telegram-каналы) и посты канала
# ═══════════════════════════════════════════════════════════
# Раздел «📢 Мой ТГК»: пользователь добавляет YamoBot
# в свой канал админом, бот публикует посты от лица канала. Канал — один на
# пользователя (owner_id — владелец в YamoBot).


def _drop_channel_binding(conn: sqlite3.Connection, owner_id: int) -> int:
    """Удаляет привязку канала пользователя вместе с её «хвостами».

    Вызывается только под ``_lock``. Возвращает число удалённых привязок
    (0 — у пользователя канала не было). Отложенные посты канала снимаем:
    публиковать их больше некому.
    """
    cur = conn.execute("DELETE FROM tg_channels WHERE owner_id = ?", (owner_id,))
    conn.execute(
        "DELETE FROM channel_posts WHERE owner_id = ? AND status = 'scheduled'",
        (owner_id,),
    )
    conn.execute(
        "DELETE FROM channel_bind_requests WHERE owner_id = ?", (owner_id,)
    )
    return cur.rowcount


def bind_channel(owner_id: int, channel_id: int, title: str = "",
                 username: str = "", *, takeover: bool = False) -> bool:
    """Привязывает канал к пользователю. ``True`` — привязка сделана.

    Права человека («владелец или админ канала») проверяются в обработчиках —
    там есть доступ к Telegram. Здесь страхуем второе правило: у одного ТГК
    не может быть двух хозяев.

    * ``False`` — канал уже привязан к другому владельцу YamoBot: чужой ТГК
      не отдаём (обработчик объясняет это пользователю);
    * ``takeover=True`` — разрешено забрать канал у прежнего владельца. Так
      делает только владелец (создатель) канала в Telegram: настоящий хозяин
      канала не должен остаться без доступа из-за чужой привязки. Прежний
      владелец теряет привязку и отложенные посты этого канала (как при
      «🔴 Отвязать ТГК»), обработчик уведомляет его об этом.
    """
    conn = _get_conn()
    with _lock:
        others = [
            int(r[0]) for r in conn.execute(
                "SELECT owner_id FROM tg_channels WHERE channel_id = ? "
                "AND owner_id != ?",
                (channel_id, owner_id),
            ).fetchall()
        ]
        if others:
            # Канал уже чей-то: чужой ТГК не отдаём. Исключение — takeover:
            # владелец канала в Telegram возвращает привязку себе.
            if not takeover:
                return False
            for other_owner in others:
                _drop_channel_binding(conn, other_owner)
        conn.execute(
            "INSERT INTO tg_channels (owner_id, channel_id, title, username) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET channel_id=excluded.channel_id, "
            "title=excluded.title, username=excluded.username",
            (owner_id, channel_id, title or "", username or ""),
        )
        conn.commit()
        return True


def get_bound_channel(owner_id: int) -> dict | None:
    """Привязанный канал пользователя (или None)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM tg_channels WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    return dict(row) if row else None


def get_channel_owner(channel_id: int) -> int | None:
    """Чей канал привязан (owner_id) — по ID канала."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT owner_id FROM tg_channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    return int(row[0]) if row else None


def update_channel_info(channel_id: int, title: str = "", username: str = "") -> None:
    """Обновляет название/username канала (например, после переименования)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE tg_channels SET title = ?, username = ? WHERE channel_id = ?",
            (title or "", username or "", channel_id),
        )
        conn.commit()


def unbind_channel(owner_id: int) -> bool:
    """Отвязывает канал пользователя (вместе с его отложенными постами)."""
    conn = _get_conn()
    with _lock:
        removed = _drop_channel_binding(conn, owner_id)
        conn.commit()
        return removed > 0


def add_channel_post(owner_id: int, channel_id: int, text: str = "",
                     photo: str = "", buttons: str = "[]",
                     status: str = "scheduled", publish_at: str = "") -> int:
    """Добавляет пост канала. Возвращает его id."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO channel_posts "
            "(owner_id, channel_id, text, photo, buttons, status, publish_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_id, channel_id, text or "", photo or "", buttons or "[]",
             status, publish_at or ""),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def get_channel_post(post_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM channel_posts WHERE id = ?", (post_id,)
    ).fetchone()
    return dict(row) if row else None


def get_channel_posts(owner_id: int, status: str | None = None) -> list[dict]:
    """Посты канала пользователя (свежие сверху). status=None — все."""
    conn = _get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM channel_posts WHERE owner_id = ? AND status = ? "
            "ORDER BY id DESC",
            (owner_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM channel_posts WHERE owner_id = ? ORDER BY id DESC",
            (owner_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_channel_posts(owner_id: int, status: str) -> int:
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM channel_posts WHERE owner_id = ? AND status = ?",
        (owner_id, status),
    ).fetchone()
    return int(row[0]) if row else 0


_CHANNEL_POST_FIELDS = {"text", "photo", "buttons", "status", "publish_at", "message_id"}


def update_channel_post(post_id: int, **fields) -> bool:
    """Обновляет поля поста (только из белого списка)."""
    data = {k: v for k, v in fields.items() if k in _CHANNEL_POST_FIELDS}
    if not data:
        return False
    conn = _get_conn()
    with _lock:
        columns = ", ".join(f"{k} = ?" for k in data)
        cur = conn.execute(
            f"UPDATE channel_posts SET {columns} WHERE id = ?",
            (*data.values(), post_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_channel_post(post_id: int) -> bool:
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM channel_posts WHERE id = ?", (post_id,))
        conn.commit()
        return cur.rowcount > 0


def get_due_channel_posts(now_utc: str) -> list[dict]:
    """Отложенные посты, время которых уже наступило (UTC-строка)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM channel_posts "
        "WHERE status = 'scheduled' AND publish_at != '' AND publish_at <= ? "
        "ORDER BY publish_at ASC",
        (now_utc,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_channel_giveaway(owner_id: int, channel_id: int, title: str = "",
                         winners: str = "", status: str = "finished") -> int:
    """Добавляет запись о розыгрыше канала (для сводки в «Мой ТГК»)."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO channel_giveaways (owner_id, channel_id, title, winners, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_id, channel_id, title or "", winners or "", status),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def get_channel_giveaways(owner_id: int) -> list[dict]:
    """Розыгрыши канала пользователя (свежие сверху)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM channel_giveaways WHERE owner_id = ? ORDER BY id DESC",
        (owner_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_channel_bind_request(owner_id: int) -> None:
    """Отмечает, что пользователь просит привязать ТГК.

    Нужно, когда Telegram не сообщает инициатора добавления бота в канал
    (анонимный админ): тогда привязываем канал владельцу со свежей заявкой.
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO channel_bind_requests (owner_id, created_at) "
            "VALUES (?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(owner_id) DO UPDATE SET created_at=CURRENT_TIMESTAMP",
            (owner_id,),
        )
        conn.commit()


def clear_channel_bind_request(owner_id: int) -> None:
    """Снимает заявку на привязку ТГК (привязали или отменили)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM channel_bind_requests WHERE owner_id = ?", (owner_id,)
        )
        conn.commit()


def get_channel_bind_request(max_age_seconds: int = 900) -> int | None:
    """Владелец со свежей заявкой «привяжи ТГК» (или None).

    Берём только заявки не старше ``max_age_seconds`` (по умолчанию 15 минут):
    так старые ожидания не перехватят чужой канал.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT owner_id FROM channel_bind_requests "
        "WHERE created_at >= datetime('now', ?) "
        "ORDER BY created_at DESC LIMIT 1",
        (f"-{int(max_age_seconds)} seconds",),
    ).fetchone()
    return int(row[0]) if row else None


# ═══════════════════════════════════════════════════════════
#  Мёртвые боты (авто-детект + удаление пачкой)
# ═══════════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════════
#  Логи переписки (раздел «Логи» и техподдержка)
# ═══════════════════════════════════════════════════════════
# Telegram не умеет отдавать историю сообщений, поэтому тексты ПЗ и ответов
# админов пишем сами — иначе «пришли логи» было бы нечем.

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


# ═══════════════════════════════════════════════════════════
#  Тикеты поддержки (бывшие жалобы)
# ═══════════════════════════════════════════════════════════

TICKET_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("tech", "❓ Тех. вопрос"),
    ("complaint", "⚠️ Жалоба"),
    ("review", "⭐ Отзыв"),
    ("other", "📦 Другое"),
)

TICKET_CATEGORY_TITLES: dict[str, str] = dict(TICKET_CATEGORIES)


def create_ticket(user_id: int, category: str, text: str,
                  photos: list[str] | None = None, has_logs: bool = False,
                  username: str = "", first_name: str = "") -> int:
    """Создаёт тикет и возвращает его ID."""
    conn = _get_conn()
    with _lock:
        cursor = conn.execute(
            "INSERT INTO tickets (user_id, username, first_name, category, text, "
            "photos, has_logs, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (user_id, username or "", first_name or "", category or "other",
             text or "", json.dumps(photos or [], ensure_ascii=False),
             1 if has_logs else 0),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)


def get_ticket(ticket_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return dict(row) if row else None


def ticket_photos(ticket: dict) -> list[str]:
    """Фото тикета (могут прийти как file_id — их бот видит в своей БД)."""
    try:
        data = json.loads(ticket.get("photos") or "[]")
    except (TypeError, ValueError):
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


def get_user_tickets(user_id: int, status: str = "open") -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tickets WHERE user_id = ? AND status = ? ORDER BY id DESC LIMIT 50",
        (user_id, status),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_tickets(status: str = "open", limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tickets WHERE status = ? ORDER BY id DESC LIMIT ?",
        (status, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def close_ticket(ticket_id: int, answer: str = "") -> bool:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE tickets SET status = 'closed', answer = ?, "
            "closed_at = datetime('now') WHERE id = ?",
            (answer or "", ticket_id),
        )
        conn.commit()
    return True


def ticket_counts() -> dict[str, int]:
    conn = _get_conn()
    rows = conn.execute("SELECT status, COUNT(*) FROM tickets GROUP BY status").fetchall()
    counts = {"open": 0, "closed": 0}
    for status, total in rows:
        counts[str(status)] = int(total)
    return counts


# ═══════════════════════════════════════════════════════════
#  Журнал ошибок ботов
# ═══════════════════════════════════════════════════════════

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
