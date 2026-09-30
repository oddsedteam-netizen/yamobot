"""Схема базы и миграции.

Все изменения схемы живут здесь и только здесь. Каждая миграция
идемпотентна (``IF NOT EXISTS`` + проверка наличия колонки), поэтому
``ensure_db()`` безопасно вызывать при каждом старте бота.
"""

import logging
import sqlite3

from services.db.connection import _get_conn

logger = logging.getLogger(__name__)

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


def _migrate_yid(conn: sqlite3.Connection) -> None:
    """Внутренний номер пользователя вида Y100, Y101… (раздел «🆔 YID»).

    Зачем
    ----
    Чтобы сотрудника можно было называть коротким номером («Y104, посмотри
    ПЗ»), не выдавая его настоящий Telegram ID. Отсчёт начинается со 100 —
    так номера не путаются с порядковыми номерами ботов и юзеров.

    Как выдаётся
    -----------
    Номер присваивается любому, кто открыл YID, и больше не меняется.
    Счётчик лежит в ``app_settings['yid_seq']`` и только растёт: номера
    удалённых юзеров не переиспользуются, иначе «Y104» внезапно стал бы
    другим человеком.

    Старые записи (yid = 0) получают номера при первом обращении к разделу,
    а не массово при обновлении — иначе номера получат те, кто их не открывал.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users_registry)").fetchall()]
    if "yid" not in cols:
        conn.execute("ALTER TABLE users_registry ADD COLUMN yid INTEGER DEFAULT 0")
    # Частичный уникальный индекс: у 0 (номера нет) ограничения нет, поэтому
    # сколько угодно юзеров без номера уживаются, а реальные номера не
    # дублируются.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_registry_yid
            ON users_registry (yid) WHERE yid > 0
    """)
    conn.commit()


def _migrate_admin_greetings(conn: sqlite3.Connection) -> None:
    """Заготовленные приветствия админа — по одному на пару (бот, админ).

    Когда админ берёт ПЗ в этом боте, приветствие уходит ПЗ автоматически.
    Хранится именно для конкретного бота: стиль общения у каждого бота свой,
    и одно приветствие на все боты смотрелось бы чужеродно.

    Текст и фото — отдельными колонками. Премиум-эмодзи и форматирование
    лежат в ``text_entities`` как сущности Telegram — их нельзя хранить в
    HTML, Telegram их вырезает.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_greetings (
            bot_id        INTEGER NOT NULL,
            admin_user_id INTEGER NOT NULL,
            text          TEXT DEFAULT '',
            photo_id      TEXT DEFAULT '',
            text_entities TEXT DEFAULT '[]',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (bot_id, admin_user_id)
        )
    """)
    conn.commit()

def _migrate_topic_activity(conn: sqlite3.Connection) -> None:
    """Добавляет в топики отметки о времени назначения админа и последнего
    сообщения.

    Зачем это нужно
    ---------------
    Напоминалка «авточек ответа админа» раньше брала точку отсчёта из
    ``created_at`` топика (момент создания ПЗ) и из последнего ОТВЕТА админа.
    Из-за этого получалось ровно то, на что жаловались пользователи:

    * админ ответил, но ПЗ висит — через ``duration`` после его ответа
      приходило «ПЗ без ответа!», и так бесконечно;
    * админ только что взял ПЗ, а таймер шёл от создания топика — пришло
      уведомление в первую же минуту.

    Теперь у топика есть два честных поля:

    * ``admin_assigned_at`` — когда назначили админа (с этого момента идёт
      отсчёт «он молчит»);
    * ``last_activity_at`` / ``last_activity_dir`` — когда было последнее
      сообщение и кто его написал (``in`` — ПЗ, ``out`` — админ). Если
      последним писал админ, ПЗ считается отвеченным и напоминание не
      отправляется вовсе.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(feedback_topics)").fetchall()]
    if "admin_assigned_at" not in cols:
        conn.execute("ALTER TABLE feedback_topics "
                     "ADD COLUMN admin_assigned_at TIMESTAMP")
    if "last_activity_at" not in cols:
        conn.execute("ALTER TABLE feedback_topics "
                     "ADD COLUMN last_activity_at TIMESTAMP")
    if "last_activity_dir" not in cols:
        conn.execute("ALTER TABLE feedback_topics "
                     "ADD COLUMN last_activity_dir TEXT DEFAULT ''")
    conn.commit()

    # Подтягиваем историю для уже существующих топиков: иначе после
    # обновления бот решит, что ответа не было, и разом пришлёт «без ответа»
    # по всем старым ПЗ.
    conn.execute("""
        UPDATE feedback_topics
           SET last_activity_at = COALESCE((
                   SELECT MAX(fm.created_at) FROM feedback_messages fm
                    WHERE fm.bot_id = feedback_topics.bot_id
                      AND fm.topic_id = feedback_topics.topic_id
                      AND fm.group_chat_id = feedback_topics.group_chat_id
               ), created_at)
    """)
    conn.execute("""
        UPDATE feedback_topics
           SET last_activity_dir = COALESCE((
                   SELECT fm.direction FROM feedback_messages fm
                    WHERE fm.bot_id = feedback_topics.bot_id
                      AND fm.topic_id = feedback_topics.topic_id
                      AND fm.group_chat_id = feedback_topics.group_chat_id
                    ORDER BY fm.created_at DESC, fm.id DESC LIMIT 1
               ), '')
    """)
    # Админ, назначенный до обновления, считается взявшим ПЗ «сейчас», чтобы
    # не прилетело мгновенное уведомление на всех старых ПЗ.
    conn.execute("""
        UPDATE feedback_topics
           SET admin_assigned_at = created_at
         WHERE admin_user_id != 0 AND admin_assigned_at IS NULL
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_feedback_topics_activity
            ON feedback_topics (bot_id, last_activity_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_feedback_messages_topic
            ON feedback_messages (bot_id, topic_id, group_chat_id, id)
    """)
    conn.commit()

def _migrate_topic_closed(conn: sqlite3.Connection) -> None:
    """Добавляет в топики метку ``closed_at`` — «топик закрыт или удалён».

    Зачем это нужно
    ---------------
    Жалоба: «топик удалили, ПЗ не пишет, новый не создаётся — а бот продолжает
    присылать "ПЗ без админа" по нему вечно». Удалить топик в Telegram бот может
    не заметить (сервисное сообщение приходит не всегда, например если бот не
    админ чата), а проверять существование топика через Bot API нельзя —
    метода ``getForumTopic`` не существует.

    Поэтому здесь не удаление, а МЯГКАЯ МЕТКА: топик помечается закрытым и
    перестаёт попадать в напоминалку. Сама запись ПЗ и её история остаются
    целы — если админ откроет топик снова, придёт ``forum_topic_reopened``,
    метка снимется и напоминания возобновятся. Это ровно то «не удалять ничего
    лишнего», о котором просил владелец.

    У уже существующих топиков значение ``NULL`` (т.е. «не закрыт») — поведение
    после обновления не меняется.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(feedback_topics)").fetchall()]
    if "closed_at" not in cols:
        conn.execute("ALTER TABLE feedback_topics ADD COLUMN closed_at TIMESTAMP")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_feedback_topics_closed
            ON feedback_topics (bot_id, closed_at)
    """)
    conn.commit()

def _migrate_topic_pinned_message(conn: sqlite3.Connection) -> None:
    """Добавляет в топик ссылку на ЗАКРЕПЛЁННУЮ шапку ПЗ.

    Шапка — это сообщение с инфой о ПЗ и кнопками «✋ Я беру» / «🚫 Отказ».
    В переписке ПЗ и админа она быстро уезжает вверх, и админ не находит,
    от чего отказываться. Поэтому шапку закрепляем, а здесь храним её
    ``message_id``: когда придёт новая шапка (смена админа, отказ, повторный
    запрос) — снимаем старый закреп и закрепляем новый, чтобы в закрепе не
    осталось устаревшего «никто не взял».

    У уже существующих топиков значение ``0`` (не закреплено) — поведение
    после обновления не меняется.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(feedback_topics)").fetchall()]
    if "pinned_message_id" not in cols:
        conn.execute(
            "ALTER TABLE feedback_topics ADD COLUMN pinned_message_id INTEGER DEFAULT 0"
        )
    conn.commit()


def _migrate_reminder_mutes(conn: sqlite3.Connection) -> None:
    """Создаёт таблицу заглушек напоминалок (``reminder_mutes``).

    Заглушка — это «не напоминать мне про этот топик»: ровно та проблема, из-за
    которой появляются уведомления по давно неактуальным (или уже удалённым)
    обращениям. Владелец выбирает срок: на сутки (``expires_at``) или навсегда
    (``expires_at IS NULL``, снимается вручную в разделе «Напоминалка»).

    UNIQUE по (owner_id, bot_id, topic_id, group_chat_id) — чтобы повторное
    нажатие «заглушить» обновляло срок, а не плодило дубли.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reminder_mutes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id      INTEGER NOT NULL,
            bot_id        INTEGER NOT NULL,
            topic_id      INTEGER NOT NULL,
            group_chat_id INTEGER NOT NULL,
            mode          TEXT NOT NULL DEFAULT 'day',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at    TIMESTAMP,
            UNIQUE(owner_id, bot_id, topic_id, group_chat_id)
        );

        CREATE INDEX IF NOT EXISTS idx_reminder_mutes_owner
            ON reminder_mutes (owner_id, expires_at);
    """)
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

        -- Очередь недоставленных сообщений (services/delivery.py).
        -- Telegram не даёт гарантий доставки: при flood-control, обрыве сети
        -- или падении процесса сообщение могло просто потеряться, и
        -- владелец/админ об этом не узнавал. Здесь каждая такая отправка
        -- живёт до успеха (или до окончательного отказа — тогда ставится
        -- причина, и её видно в «Диагностике»).
        CREATE TABLE IF NOT EXISTS outbox (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id        INTEGER NOT NULL DEFAULT 0,
            chat_id       INTEGER NOT NULL,
            thread_id     INTEGER DEFAULT 0,
            kind          TEXT DEFAULT 'message',
            payload       TEXT DEFAULT '{}',
            attempts      INTEGER DEFAULT 0,
            last_error    TEXT DEFAULT '',
            status        TEXT DEFAULT 'pending',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            next_attempt_at TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_outbox_pending
            ON outbox (status, next_attempt_at);
    """)
    _migrate_bot_type(conn)
    _migrate_admin_scope(conn)
    _migrate_bot_anonymous(conn)
    _migrate_bot_welcome_media(conn)
    _migrate_bot_category_ask(conn)
    _migrate_bot_admin_change(conn)
    _migrate_registry_chats(conn)
    _migrate_registry_pending_bind(conn)
    _migrate_yid(conn)
    _migrate_admin_greetings(conn)
    _migrate_topic_activity(conn)
    # Метка «топик закрыт/удалён» и заглушки напоминалок: без них бот продолжал
    # присылать «ПЗ без админа» по несуществующим топикам (жалоба владельца).
    _migrate_topic_closed(conn)
    _migrate_reminder_mutes(conn)
    _migrate_topic_pinned_message(conn)
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
