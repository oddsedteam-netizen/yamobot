"""Хранилище: внутренние номера пользователей (YID) и приветствия админов.

Зачем этот модуль
-----------------
Раздел «🆔 YID» отвечает на вопрос «кто я как админ», а не «кто я как
владелец». Поэтому здесь лежат только личные данные сотрудника:

* ``yid``          — короткий внутренний номер (Y100, Y101…) вместо
  длинного Telegram ID, который неудобно диктовать вслух;
* ``admin_greetings`` — заготовленное приветщение, которое бот отправляет ПЗ
  автоматически, когда этот админ берёт обращение в конкретном боте.

Чего здесь нет и не должно быть: статистики чужих ботов. Раздел показывает
только то, что сделал сам пользователь.
"""

import sqlite3

from services.db.connection import _get_conn, _lock
from services.db.users import get_user_registry

# Счётчик номеров. Отсчёт с 100 — чтобы «Y104» не путался с порядковым
# номером юзера в базе и не выглядел как служебный код бота.
YID_START = 100
_YID_SEQ_KEY = "yid_seq"


def _next_yid(conn: sqlite3.Connection) -> int:
    """Выдаёт следующий свободный номер и сохраняет новый счётчик.

    Счётчик только растёт: номера удалённых юзеров не переиспользуются,
    иначе «Y104» внезапно стал бы другим человеком.
    """
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (_YID_SEQ_KEY,)
    ).fetchone()
    current = YID_START - 1
    if row and str(row[0]).isdigit():
        current = int(row[0])
    nxt = max(current, YID_START - 1) + 1

    # Учитываем уже выданные номера: счётчик мог отстать (например, его
    # сбросили вручную при переносе базы).
    top = conn.execute(
        "SELECT COALESCE(MAX(yid), 0) FROM users_registry WHERE yid > 0"
    ).fetchone()[0]
    if top and int(top) >= nxt:
        nxt = int(top) + 1

    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, "
        "datetime('now')) ON CONFLICT(key) DO UPDATE SET "
        "value = excluded.value, updated_at = excluded.updated_at",
        (_YID_SEQ_KEY, str(nxt)),
    )
    return nxt


def ensure_yid(user_id: int) -> int:
    """Возвращает внутренний номер юзера, выдавая его при первом обращении.

    Номер выдаётся любому, кто открыл раздел YID, и больше не меняется.
    """
    if not user_id:
        return 0
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT yid FROM users_registry WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row and int(row[0] or 0) > 0:
            return int(row[0])

        # Юзера может не быть в реестре (например, он пришёл по инлайн-ссылке).
        conn.execute(
            "INSERT OR IGNORE INTO users_registry (user_id) VALUES (?)",
            (user_id,),
        )
        # Перечитываем: между SELECT и INSERT номер мог выдать другой запрос.
        row = conn.execute(
            "SELECT yid FROM users_registry WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row and int(row[0] or 0) > 0:
            return int(row[0])

        assigned = _next_yid(conn)
        conn.execute(
            "UPDATE users_registry SET yid = ? WHERE user_id = ?",
            (assigned, user_id),
        )
        conn.commit()
        return assigned


def get_yid(user_id: int) -> int:
    """Текущий номер юзера или 0, если номер ещё не выдавался."""
    if not user_id:
        return 0
    conn = _get_conn()
    row = conn.execute(
        "SELECT yid FROM users_registry WHERE user_id = ?", (user_id,)
    ).fetchone()
    return int(row[0] or 0) if row else 0


def yid_label(user_id: int) -> str:
    """Строковый номер для показа в интерфейсе: «Y104» или «—»."""
    number = get_yid(user_id)
    return f"Y{number}" if number else "—"


def admin_bots_of(user_id: int) -> list[dict]:
    """Боты, в которых пользователь выступает админом.

    Сюда попадают и боты, где он есть в таблице ``admins``, и его собственные
    боты: владелец де-факто админ в своём боте (см. ``cb_take_user`` в
    child_manager — владельцу без записи админа тегом становится его ник).

    Каждая запись помечена ``is_owner`` — по ней интерфейс решает, можно ли
    предлагать «отвязаться» (своё от себя не отвяжешь).
    """
    if not user_id:
        return []

    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT b.id, b.username, b.first_name, b.owner_id,
               EXISTS(SELECT 1 FROM admins a
                       WHERE a.owner_id = b.owner_id AND a.user_id = ?)
                   AS in_admins,
               (SELECT MIN(a2.created_at) FROM admins a2
                 WHERE a2.owner_id = b.owner_id AND a2.user_id = ?) AS since
          FROM bots b
         WHERE b.owner_id = ?
            OR EXISTS(SELECT 1 FROM admins a3
                       WHERE a3.owner_id = b.owner_id AND a3.user_id = ?)
         ORDER BY b.id
        """,
        (user_id, user_id, user_id, user_id),
    ).fetchall()

    result: list[dict] = []
    for row in rows:
        item = dict(row)
        item["is_owner"] = int(item.get("owner_id") or 0) == user_id
        # Легаси-админы хранятся с owner_id = 0 — учитываем и их.
        if not item["in_admins"] and not item["is_owner"]:
            check = conn.execute(
                "SELECT MIN(created_at) FROM admins WHERE owner_id = 0 AND user_id = ?",
                (user_id,),
            ).fetchone()
            if check and check[0]:
                item["in_admins"] = 1
                item["since"] = check[0]
        result.append(item)
    return result


def admin_stats_in_bot(user_id: int, bot_id: int) -> dict:
    """Личная статистика админа в одном боте.

    Считается ТОЛЬКО то, что сделал сам пользователь. Статистику бота целиком
    (сколько у него ПЗ и юзеров) здесь быть не должно: раздел YID — про
    сотрудника, а не про бот.

    * ``pz_current`` — обращений, взятых этим админом прямо сейчас;
    * ``pz_taken``   — сколько раз он брал обращения (всего);
    * ``replies``    — сколько ответов он дал в топиках;
    * ``since``      — когда он стал админом этого бота.
    """
    conn = _get_conn()

    pz_current = conn.execute(
        "SELECT COUNT(*) FROM feedback_topics "
        "WHERE bot_id = ? AND admin_user_id = ?",
        (int(bot_id), int(user_id)),
    ).fetchone()[0]

    pz_taken = conn.execute(
        "SELECT COUNT(*) FROM admin_messages "
        "WHERE bot_id = ? AND admin_user_id = ? AND direction = 'action'",
        (int(bot_id), int(user_id)),
    ).fetchone()[0]

    replies = conn.execute(
        "SELECT COUNT(*) FROM admin_messages "
        "WHERE bot_id = ? AND admin_user_id = ? AND direction = 'out'",
        (int(bot_id), int(user_id)),
    ).fetchone()[0]

    since = conn.execute(
        "SELECT MIN(created_at) FROM admins "
        "WHERE owner_id = (SELECT owner_id FROM bots WHERE id = ?) "
        "AND user_id = ?",
        (int(bot_id), int(user_id)),
    ).fetchone()
    if not since or not since[0]:
        since = conn.execute(
            "SELECT MIN(created_at) FROM admins WHERE owner_id = 0 AND user_id = ?",
            (user_id,),
        ).fetchone()

    return {
        "pz_current": int(pz_current),
        "pz_taken": int(pz_taken),
        "replies": int(replies),
        "since": (since[0] if since else "") or "",
    }


def yid_card(user_id: int) -> dict:
    """Данные для карточки раздела YID.

    Это вход в раздел, поэтому номер здесь и выдаётся (если его ещё нет) —
    так «открыл YID» не зависит от того, вспомнил ли вызывающий код вызвать
    ``ensure_yid`` отдельно.

    Заголовок — личный номер и итоги по всем ботам сразу; ниже — разбивка по
    ботам, где пользователь админ. Чужие боты в выдаче не участвуют.
    """
    number = ensure_yid(user_id)
    bots = admin_bots_of(user_id)
    rows: list[dict] = []
    total_taken = total_replies = total_current = 0
    for bot in bots:
        stats = admin_stats_in_bot(user_id, int(bot["id"]))
        rows.append({**bot, **stats})
        total_taken += stats["pz_taken"]
        total_replies += stats["replies"]
        total_current += stats["pz_current"]

    registry = get_user_registry(user_id) or {}
    return {
        "user_id": user_id,
        "yid": number,
        "username": str(registry.get("username") or ""),
        "first_name": str(registry.get("first_name") or ""),
        "registered_at": str(registry.get("created_at") or ""),
        "bots": rows,
        "total_pz_current": total_current,
        "total_pz_taken": total_taken,
        "total_replies": total_replies,
    }



def get_yid_owner(yid: int) -> int | None:
    """Обратный поиск: по номеру Y104 найти user_id (для админ-панели)."""
    if not yid or yid <= 0:
        return None
    conn = _get_conn()
    row = conn.execute(
        "SELECT user_id FROM users_registry WHERE yid = ?", (int(yid),)
    ).fetchone()
    return int(row[0]) if row else None


# ═══════════════ Приветствия админов ══════════════════════════════════


def get_admin_greeting(bot_id: int, admin_user_id: int) -> dict | None:
    """Приветствие админа для конкретного бота (None, если не задано)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM admin_greetings WHERE bot_id = ? AND admin_user_id = ?",
        (int(bot_id), int(admin_user_id)),
    ).fetchone()
    return dict(row) if row else None


def set_admin_greeting(bot_id: int, admin_user_id: int, text: str = "",
                       photo_id: str = "", text_entities: str = "[]") -> None:
    """Сохраняет приветствие админа для бота (создаёт или обновляет)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO admin_greetings "
            "(bot_id, admin_user_id, text, photo_id, text_entities) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(bot_id, admin_user_id) DO UPDATE SET "
            "text = excluded.text, photo_id = excluded.photo_id, "
            "text_entities = excluded.text_entities, "
            "updated_at = datetime('now')",
            (int(bot_id), int(admin_user_id), text or "", photo_id or "",
             text_entities or "[]"),
        )
        conn.commit()


def delete_admin_greeting(bot_id: int, admin_user_id: int) -> bool:
    """Убирает приветствие админа для бота."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "DELETE FROM admin_greetings WHERE bot_id = ? AND admin_user_id = ?",
            (int(bot_id), int(admin_user_id)),
        )
        conn.commit()
        return cur.rowcount > 0
