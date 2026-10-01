"""Уведомления по конкретным ботам (раздел «🔔 Мои уведомления»).

Зачем этот модуль
----------------
Реальная жалоба владельцев: в «чат админов» прилетают уведомления о новых ПЗ
и напоминания по ботам поддержки и анкетниц, которые в «чат админов» вообще не
нужны. Уведомления выключаются ПО БОТУ, а не целиком: у кого-то лишним
окажется один бот, а у кого-то половина.

Что подчиняется настройке
-------------------------
* ``notify_new_pz``      — уведомление «🆕 Новый ПЗ» в чат админов;
* ``notify_reminders``   — напоминалки «ПЗ без ответа» и «ПЗ без админа».

Что НЕ подчиняется намеренно: антинакрутка, смена админа, норма админов,
защита — это разовые тревоги, молчать о них нельзя.

Значения по умолчанию
---------------------
Отсутствие строки = уведомления **включены**. Иначе у всех, кто ни разу не
открывал раздел, молча выключились бы уведомления после переезда на новую
версию — такой регресс заметили бы все и не поняли бы почему.
"""

from services.db.connection import _get_conn, _lock

# Что можно включать/выключать (белый список для SQL).
_FIELDS = ("notify_new_pz", "notify_reminders")

# Значения по умолчанию: обе галочки включены.
_DEFAULTS = {"notify_new_pz": 1, "notify_reminders": 1}


def get_bot_notify_settings(owner_id: int, bot_id: int) -> dict:
    """Настройки уведомлений по одному боту (с прочерками по умолчанию)."""
    settings = dict(_DEFAULTS)
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM bot_notify_settings WHERE owner_id = ? AND bot_id = ?",
        (owner_id, bot_id),
    ).fetchone()
    if row:
        for key in _FIELDS:
            if key in row.keys():
                settings[key] = int(row[key] or 0)
    return settings


def get_notify_settings_map(owner_id: int, bot_ids: list[int]) -> dict[int, dict]:
    """Настройки сразу по нескольким ботам — одним запросом.

    Нужна экрану списка и фоновому сканеру напоминалок: по запросу на бота
    получилось бы N+1, а список ботов у крупного владельца — десятки.
    """
    ids = [int(b) for b in bot_ids]
    if not ids:
        return {}
    conn = _get_conn()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM bot_notify_settings WHERE owner_id = ? "
        f"AND bot_id IN ({placeholders})",
        (owner_id, *ids),
    ).fetchall()

    result = {bot_id: dict(_DEFAULTS) for bot_id in ids}
    for row in rows:
        item = dict(_DEFAULTS)
        for key in _FIELDS:
            if key in row.keys():
                item[key] = int(row[key] or 0)
        result[int(row["bot_id"])] = item
    return result


def set_bot_notify_field(owner_id: int, bot_id: int, field: str, value: bool) -> bool:
    """Включает/выключает один вид уведомлений по боту."""
    if field not in _FIELDS:
        raise ValueError(f"Недопустимый параметр уведомлений: {field!r}")
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO bot_notify_settings (owner_id, bot_id) VALUES (?, ?)",
            (owner_id, bot_id),
        )
        conn.execute(
            f"UPDATE bot_notify_settings SET {field} = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE owner_id = ? AND bot_id = ?",
            (1 if value else 0, owner_id, bot_id),
        )
        conn.commit()
    return True


def is_bot_notify_enabled(owner_id: int, bot_id: int, field: str = "notify_new_pz") -> bool:
    """Разрешено ли слать этот вид уведомлений по боту (быстрая проверка).

    Точечный вызов для мест, где настройки уже не собраны пачкой.
    """
    if field not in _FIELDS:
        raise ValueError(f"Недопустимый параметр уведомлений: {field!r}")
    conn = _get_conn()
    row = conn.execute(
        f"SELECT {field} FROM bot_notify_settings WHERE owner_id = ? AND bot_id = ?",
        (owner_id, bot_id),
    ).fetchone()
    # Строки нет — уведомления включены (см. модульный докстринг).
    return True if not row else bool(row[0])


def delete_bot_notify_settings(owner_id: int, bot_id: int) -> bool:
    """Убирает настройки бота — уведомления возвращаются к «включены»."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "DELETE FROM bot_notify_settings WHERE owner_id = ? AND bot_id = ?",
            (owner_id, bot_id),
        )
        conn.commit()
        return cur.rowcount > 0