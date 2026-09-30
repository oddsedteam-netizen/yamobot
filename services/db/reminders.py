"""Хранилище: напоминалки (авточек ответа админа / напоминание про ПЗ).

Часть пакета ``services.db`` — доступ к базе. Создано разбиением
``services/storage.py`` по доменам; публичный API не изменился, поэтому
``from services.storage import ...`` продолжает работать (см. фасад).
"""


from datetime import datetime, timezone

from services.db.connection import (
    _get_conn,
    _lock,
)


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
    """Время последнего ответа админа в топике (direction='out'), UTC-строка.

    Учитывает ТОЛЬКО метку активности топика (``last_activity_dir``) и
    ``feedback_messages`` — тем же, что и остальной код напоминалки.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT last_activity_at FROM feedback_topics "
        "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ? "
        "AND last_activity_dir = 'out'",
        (bot_id, topic_id, group_chat_id)
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        "SELECT created_at FROM feedback_messages "
        "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ? AND direction = 'out' "
        "ORDER BY created_at DESC LIMIT 1",
        (bot_id, topic_id, group_chat_id)
    ).fetchone()
    return row[0] if row else None

def get_topics_without_admin(bot_id: int, older_than_iso: str) -> list[dict]:
    """Топики бота, которые висят БЕЗ админа (режим «напоминание про ПЗ»).

    Отбираем только те, где последним писал ПЗ (``last_activity_dir='in'``)
    и с тех пор прошёл срок. Старые заброшенные ПЗ больше не напоминаются
    вечно: отсекаем по ``last_activity_at``, а если метки нет (топик из
    старой базы) — берём время создания.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM feedback_topics "
        "WHERE bot_id = ? AND status != 'assigned' "
        "AND COALESCE(NULLIF(last_activity_dir, ''), 'in') = 'in' "
        "AND closed_at IS NULL "
        "AND COALESCE(last_activity_at, created_at) <= ? "
        "ORDER BY COALESCE(last_activity_at, created_at) ASC",
        (bot_id, older_than_iso)
    ).fetchall()
    return [dict(r) for r in rows]

def get_topics_waiting_admin(bot_id: int, older_than_iso: str) -> list[dict]:
    """Топики с назначенным админом, где ПЗ ждёт ответа (режим «авточек»).

    Условия, ВСЕ из которых нужны одновременно:

    * админ назначен (``status='assigned'``);
    * последним писал ПЗ, а не админ — если последним писал админ, ПЗ
      отвечено, и уведомлять нельзя (это и вызывало жалобы);
    * и ПЗ, и назначение админа состоялись раньше отсечки по сроку.

    Отсчёт идёт от более позднего из двух событий: назначения админа и
    последнего сообщения ПЗ. Поэтому админ, взявший ПЗ «только что», получает
    полный срок на ответ, а не мгновенное уведомление.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM feedback_topics "
        "WHERE bot_id = ? AND status = 'assigned' "
        "AND COALESCE(NULLIF(last_activity_dir, ''), 'in') = 'in' "
        "AND closed_at IS NULL "
        "AND COALESCE(last_activity_at, created_at) <= ? "
        "AND COALESCE(admin_assigned_at, created_at) <= ? "
        "ORDER BY COALESCE(admin_assigned_at, last_activity_at, created_at) ASC",
        (bot_id, older_than_iso, older_than_iso)
    ).fetchall()
    return [dict(r) for r in rows]

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


# ═══════════════════════════════════════════════════════════════════
# Заглушки: «не напоминать мне про этот топик»
# ═══════════════════════════════════════════════════════════════════

# Режимы заглушки. ``forever`` не имеет срока (expires_at IS NULL) и снимается
# только вручную из раздела «⏰ Напоминалка» → «🔇 Заглушённые».
MUTE_DAY = "day"
MUTE_FOREVER = "forever"

# Сколько часов длится заглушка «на сутки».
MUTE_DAY_HOURS = 24


def add_reminder_mute(owner_id: int, bot_id: int, topic_id: int,
                      group_chat_id: int, forever: bool = False) -> int:
    """Заглушает напоминалку по конкретному топику. Возвращает id записи.

    ``forever=True`` — заглушка без срока (снимается вручную), иначе заглушка
    действует ``MUTE_DAY_HOURS`` часов. Повторный вызов по тому же топику
    ОБНОВЛЯЕТ срок, а не создаёт вторую запись (UNIQUE в схеме).
    """
    mode = MUTE_FOREVER if forever else MUTE_DAY
    expires_sql = "NULL" if forever else f"datetime('now', '+{int(MUTE_DAY_HOURS)} hours')"
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO reminder_mutes "
            "(owner_id, bot_id, topic_id, group_chat_id, mode, expires_at) "
            f"VALUES (?, ?, ?, ?, ?, {expires_sql}) "
            "ON CONFLICT(owner_id, bot_id, topic_id, group_chat_id) DO UPDATE SET "
            f"mode=excluded.mode, expires_at={expires_sql}, "
            "created_at=CURRENT_TIMESTAMP",
            (owner_id, bot_id, topic_id, group_chat_id, mode),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM reminder_mutes "
            "WHERE owner_id = ? AND bot_id = ? AND topic_id = ? AND group_chat_id = ?",
            (owner_id, bot_id, topic_id, group_chat_id),
        ).fetchone()
    return int(row[0]) if row else 0


def get_reminder_mutes(owner_id: int, active_only: bool = True) -> list[dict]:
    """Заглушки владельца (новые сверху).

    ``active_only=True`` — только те, что ещё действуют (срок не истёк).
    """
    conn = _get_conn()
    sql = "SELECT * FROM reminder_mutes WHERE owner_id = ?"
    if active_only:
        sql += " AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)"
    sql += " ORDER BY created_at DESC, id DESC"
    rows = conn.execute(sql, (owner_id,)).fetchall()
    return [dict(r) for r in rows]


def count_active_mutes(owner_id: int) -> int:
    """Сколько заглушек у владельца сейчас действует (для кнопки в меню)."""
    return len(get_reminder_mutes(owner_id))


def get_muted_topic_keys(owner_id: int) -> set[str]:
    """Ключи ``bot_id:topic_id:group_chat_id`` заглушенных топиков.

    Сделано одной выборкой, а не по одному запросу на топик: иначе на каждом
    скане напоминалок (раз в минуту, по всем владельцам) получился бы N+1.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT bot_id, topic_id, group_chat_id FROM reminder_mutes "
        "WHERE owner_id = ? "
        "AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)",
        (owner_id,),
    ).fetchall()
    return {f"{int(r[0])}:{int(r[1])}:{int(r[2])}" for r in rows}


def delete_reminder_mute(mute_id: int, owner_id: int) -> bool:
    """Снимает заглушку (кнопка «♾ Снять»). True, если заглушка была.

    ``owner_id`` в условии — не формальность: без него можно было бы снять
    чужую заглушку, зная её id (id видно в callback_data).
    """
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "DELETE FROM reminder_mutes WHERE id = ? AND owner_id = ?",
            (mute_id, owner_id),
        )
        conn.commit()
        return cur.rowcount > 0


def purge_expired_mutes() -> int:
    """Удаляет истёкшие заглушки. Возвращает число удалённых записей.

    Вызывается из фонового сканера: без чистки таблица росла бы бесконечно.
    """
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "DELETE FROM reminder_mutes "
            "WHERE expires_at IS NOT NULL AND expires_at <= CURRENT_TIMESTAMP"
        )
        conn.commit()
        return cur.rowcount


def purge_topic_reminder_state(bot_id: int, topic_id: int, group_chat_id: int) -> None:
    """Чистит ВСЁ напоминалковое состояние по одному топику.

    Вызывается, когда запись топика удаляется (бан, пересоздание, ручное
    удаление из списка). Иначе остаются «хвосты»: тик по несуществующему
    топику и заглушка, которую уже нечем снять. Записи ДРУГИХ топиков не
    затрагиваются — условие DELETE точно по тройке id.
    """
    key = f"{bot_id}:{topic_id}:{group_chat_id}"
    conn = _get_conn()
    with _lock:
        conn.execute("DELETE FROM reminder_ticks WHERE topic_key = ?", (key,))
        conn.execute(
            "DELETE FROM reminder_mutes "
            "WHERE bot_id = ? AND topic_id = ? AND group_chat_id = ?",
            (bot_id, topic_id, group_chat_id),
        )
        conn.commit()


def owner_topic_keys(owner_id: int) -> set[str]:
    """Ключи ``bot_id:topic_id:group_chat_id`` по всем топикам владельца.

    Нужен для сброса отсчёта: только эти ключи «живые», всё остальное — мусор.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT t.bot_id, t.topic_id, t.group_chat_id FROM feedback_topics t "
        "JOIN bots b ON b.id = t.bot_id WHERE b.owner_id = ?",
        (owner_id,),
    ).fetchall()
    return {f"{int(r[0])}:{int(r[1])}:{int(r[2])}" for r in rows}


def reset_reminder_countdown(owner_id: int, reminder_id: int) -> tuple[int, int]:
    """Сбрасывает отсчёт напоминалки: ``(обновлено тиков, удалено мусорных)``.

    Что именно делает «Сбросить» и зачем
    -----------------------------------
    Жалоба: «напоминаний накопилось много, при включении функции бот начинает
    спамить ими». Причина в ``reminder_ticks``: там по каждому топику лежит
    старая отметка, поэтому при очередном скане ВСЕ просроченные обращения
    одновременно признаются «пора слать» и вываливаются в чат.

    Поэтому сброс НЕ удаляет отметки (пусто — тоже значит «пора слать», стало
    бы только хуже), а ПРИСВАИВАЕТ им текущее время. Отсчёт начинается заново
    с полного срока, и уведомления приходят по одному, по мере старения ПЗ.

    Заодно вычищаются осиротевшие тики по несуществующим топикам (например,
    топик удалили вручную) — иначе они копятся и занимают место в базе.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    keys = owner_topic_keys(owner_id)
    conn = _get_conn()
    with _lock:
        for key in keys:
            conn.execute(
                "INSERT OR REPLACE INTO reminder_ticks "
                "(reminder_id, topic_key, last_sent_at) VALUES (?, ?, ?)",
                (reminder_id, key, now),
            )
        # Мусорные тики — только среди НАПОМИНАЛОК ЭТОГО владельца: чужие и
        # тики по другим его напоминалкам не трогаем.
        stale = 0
        rows = conn.execute(
            "SELECT t.reminder_id, t.topic_key FROM reminder_ticks t "
            "JOIN reminders r ON r.id = t.reminder_id "
            "WHERE r.owner_id = ? AND t.reminder_id = ?",
            (owner_id, reminder_id),
        ).fetchall()
        for row in rows:
            if row[1] not in keys:
                conn.execute(
                    "DELETE FROM reminder_ticks "
                    "WHERE reminder_id = ? AND topic_key = ?",
                    (row[0], row[1]),
                )
                stale += 1
        conn.commit()
    return len(keys), stale
