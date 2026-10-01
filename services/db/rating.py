"""Рейтинг админов и ботов.

Зачем этот модуль
----------------
Раздел «🏆 Рейтинг» в YID показывает два топа: по админам и по ботам. Оба
считаются ЗДЕСЬ и каждый раз заново — кэша нет намеренно:

* рейтинг должен быть динамическим: он меняется, когда кто-то взял ПЗ или
  ответил. Кэш неизбежно «отставал» бы и показывал неверный порядок;
* считать его надо по всей платформе, а данные разбросаны по таблицам
  (``admins``, ``bots``, ``admin_messages``, ``feedback_topics``,
  ``feedback_messages``) — в одном месте собирать их дешевле и прозрачнее.

Что показываем
--------------
* **Админы** — тег (без юза и ID: он виден владельцу бота по топикам) и
  активность: сколько ответов ПЗ и сколько обращений за ним закреплено.
* **Боты** — ``@username`` (у бота он и есть «визитка») и пара цифр:
  сколько обращений у бота и сколько на них ответили админы.

Кто не попадает в топ
--------------------
* админ, скрывший себя кнопкой «🚫 Вне рейтинга»;
* бот, скрытый владельцем;
* **бот-анкетница** (``bot_type='anketa'``) — всегда: анкетницы не держат ПЗ,
  им не нужны админы в топах, и в поиске они тоже не участвуют.
"""

from services.db.connection import _get_conn, _lock

# Виды сущностей в таблице rating_optout.
RATING_ADMIN = "admin"
RATING_BOT = "bot"

# Сколько позиций показываем в топе.
RATING_LIMIT = 20

# Тип бота, который в рейтинг не попадает никогда.
_BOT_TYPE_EXCLUDED = ("anketa",)


# ═══════════════ Скрытие из рейтинга ═══════════════


def get_rating_optouts(kind: str) -> set[int]:
    """Кто скрыт из рейтинга данного вида (``admin`` / ``bot``)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT entity_id FROM rating_optout WHERE kind = ?", (str(kind),),
    ).fetchall()
    return {int(r[0]) for r in rows}


def set_rating_optout(kind: str, entity_id: int, hidden: bool) -> bool:
    """Скрывает (или возвращает в рейтинг) админа/бота.

    ``hidden=True`` — убрать из рейтинга, ``False`` — вернуть.
    """
    if not entity_id:
        return False
    conn = _get_conn()
    with _lock:
        if hidden:
            conn.execute(
                "INSERT OR IGNORE INTO rating_optout (kind, entity_id) VALUES (?, ?)",
                (str(kind), int(entity_id)),
            )
        else:
            conn.execute(
                "DELETE FROM rating_optout WHERE kind = ? AND entity_id = ?",
                (str(kind), int(entity_id)),
            )
        conn.commit()
    return True


def is_hidden_from_rating(kind: str, entity_id: int) -> bool:
    """Скрыт ли админ/бот из рейтинга."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM rating_optout WHERE kind = ? AND entity_id = ?",
        (str(kind), int(entity_id)),
    ).fetchone()
    return row is not None


# ═══════════════ Ручное скрытие (решение администратора) ═══════════════


def get_manual_hides(kind: str) -> set[int]:
    """Кого администратор убрал из рейтинга вручную."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT entity_id FROM rating_manual_hide WHERE kind = ?", (str(kind),),
    ).fetchall()
    return {int(r[0]) for r in rows}


def set_manual_hide(kind: str, entity_id: int, hidden: bool,
                    hidden_by: int = 0) -> bool:
    """Убирает (или возвращает) админа/бота из рейтинга по решению админа.

    Отличие от ``set_rating_optout``: скрытый здесь не может вернуть себя
    сам — его кнопка «Вернуть в рейтинг» действует только на ``rating_optout``
    и эту запись не трогает. Иначе «убрать из рейтинга» было бы невозможно:
    человек нажимал бы «Вернуть меня в рейтинг» и снова попадал в топ.
    """
    if not entity_id:
        return False
    conn = _get_conn()
    with _lock:
        if hidden:
            conn.execute(
                "INSERT OR REPLACE INTO rating_manual_hide "
                "(kind, entity_id, hidden_by) VALUES (?, ?, ?)",
                (str(kind), int(entity_id), int(hidden_by or 0)),
            )
        else:
            conn.execute(
                "DELETE FROM rating_manual_hide WHERE kind = ? AND entity_id = ?",
                (str(kind), int(entity_id)),
            )
        conn.commit()
    return True


def is_manually_hidden(kind: str, entity_id: int) -> bool:
    """Убран ли админ/бот из рейтинга решением администратора."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM rating_manual_hide WHERE kind = ? AND entity_id = ?",
        (str(kind), int(entity_id)),
    ).fetchone()
    return row is not None


# ═══════════════ Рейтинг админов ═══════════════


def admin_rating(limit: int = RATING_LIMIT) -> list[dict]:
    """Топ админов по активности: ответы ПЗ и закреплённые обращения.

    Порядок — по сумме двух показателей, поэтому человек, который отвечает
    много, поднимается в топ независимо от того, сколько у него ПЗ «висит»
    прямо сейчас (это число меняется каждый день и само по себе активности
    не означает).

    Один админ может вести несколько ботов: считаем по ``user_id``, а не по
    паре (владелец, админ) — иначе человек показался бы в топе несколько раз.

    Пустые теги пропускаем: в топике такой админ виден как ``ID:123``, и в
    рейтинге это выглядело бы ошибкой.

    Кто именно попадает в топ
    -----------------------
    Только те, кто РЕАЛЬНО сейчас числится админом. Проверяем это по таблице
    ``bots``: если у владельца не осталось ни одного бота (все удалены),
    запись в ``admins`` могла уцелеть (удаление бота чистит его данные, но
    не список админов), и такой «админ без бота» попадал в рейтинг со всей
    старой статистикой. Теперь он из выдачи выпадает.

    Владелец бота тоже админ в своём боте, но он не лежит в таблице ``admins``
    автоматически — поэтому его в топ не берём: считаем именно назначенных
    админов, как и просил владелец.
    """
    hidden = get_rating_optouts(RATING_ADMIN) | get_manual_hides(RATING_ADMIN)
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT a.user_id  AS user_id,
               MIN(a.tag) AS tag,
               COALESCE(SUM(CASE WHEN am.direction = 'out' THEN 1 ELSE 0 END), 0)
                   AS replies,
               (SELECT COUNT(*) FROM feedback_topics t
                 WHERE t.admin_user_id = a.user_id AND t.status = 'assigned')
                   AS pz_current
          FROM admins a
     LEFT JOIN admin_messages am ON am.admin_user_id = a.user_id
         WHERE a.tag != ''
           AND a.active != 0
           -- Главное условие: админ есть только если у его владельца
           -- остался хотя бы один бот. Иначе запись в admins — «хвост».
           AND EXISTS (
                 SELECT 1 FROM bots b WHERE b.owner_id = a.owner_id
               )
      GROUP BY a.user_id
      ORDER BY (replies + pz_current) DESC, replies DESC, pz_current DESC
         LIMIT ?
        """,
        (int(limit) * 4,),
    ).fetchall()

    result: list[dict] = []
    for row in rows:
        user_id = int(row["user_id"])
        if user_id in hidden:
            continue
        replies = int(row["replies"])
        pz_current = int(row["pz_current"])
        # Совсем «мёртвый» админ (ноль ответов и ноль ПЗ) в топе не интересен:
        # иначе при нехватке активных людей в выдаче окажутся пустые строки.
        if replies == 0 and pz_current == 0:
            continue
        result.append({
            "user_id": user_id,
            "tag": str(row["tag"]),
            "replies": replies,
            "pz_current": pz_current,
            "total": replies + pz_current,
        })
        if len(result) >= int(limit):
            break
    return result


# ═══════════════ Рейтинг ботов ═══════════════


def bot_rating(limit: int = RATING_LIMIT) -> list[dict]:
    """Топ ботов: сколько обращений и сколько на них ответили админы.

    Порядок — по обращениям (это и есть «нагрузка» бота), а ответы админов
    идут вторым показателем в строке.

    Анкетницы (``bot_type='anketa'``) и скрытые владельцем боты не попадают
    в выдачу никогда.
    """
    hidden = get_rating_optouts(RATING_BOT) | get_manual_hides(RATING_BOT)
    conn = _get_conn()
    placeholders = ",".join("?" for _ in _BOT_TYPE_EXCLUDED)
    rows = conn.execute(
        f"""
        SELECT b.id         AS bot_id,
               b.username   AS username,
               b.first_name AS first_name,
               b.owner_id   AS owner_id,
               (SELECT COUNT(*) FROM feedback_topics t WHERE t.bot_id = b.id)
                   AS pz_count,
               (SELECT COUNT(*) FROM feedback_messages m
                 WHERE m.bot_id = b.id AND m.direction = 'out')
                   AS replies
          FROM bots b
         WHERE b.bot_type NOT IN ({placeholders})
           AND b.username != ''
      ORDER BY pz_count DESC, replies DESC, b.id
         LIMIT ?
        """,
        (*_BOT_TYPE_EXCLUDED, int(limit) * 3),
    ).fetchall()

    result: list[dict] = []
    for row in rows:
        bot_id = int(row["bot_id"])
        if bot_id in hidden:
            continue
        pz = int(row["pz_count"])
        replies = int(row["replies"])
        if pz == 0 and replies == 0:
            continue
        result.append({
            "bot_id": bot_id,
            "username": str(row["username"]),
            "first_name": str(row["first_name"] or ""),
            "owner_id": int(row["owner_id"] or 0),
            "pz_count": pz,
            "replies": replies,
        })
        if len(result) >= int(limit):
            break
    return result