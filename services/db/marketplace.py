"""Анкеты и поиск: раздел «🔎 Поиск» в YID.

Зачем этот модуль
----------------
Владелец бота ищет админа, админ ищет бот. Обе стороны описывают себя
анкетой, но вопросов у них разных:

* **анкета бота** (заполняет владелец) — кого он ждёт: возраст админа
  (можно диапазоном «12-15»), категория, пол и текст со своими условиями;
* **анкета админа** (заполняет сам админ) — сколько лет, категория, сколько
  ПЗ ему комфортно вести, часовой пояс, сколько времени он готов уделять
  боту и текст.

Каждый вопрос можно пропустить, и тогда в анкете стоит прочерк. Прочерк —
это НЕ «анкета не заполнена»: анкета из одного прочерка тоже считается
заполненной, её видно другим.

Кто кого ищет
-------------
* ``OFFER_BOT_TO_ADMIN`` — владелец пригласил админа в свой бот;
* ``OFFER_ADMIN_TO_BOT`` — админ подал заявку на вступление в бот.

Обоим сценариям служит одна таблица ``search_offers``: различаются они тем,
кто кому пишет, а набор статусов и подсчёт откликов одинаков.

Боты-анкетницы (``bot_type='anketa'``) здесь не участвуют: у них нет ПЗ и
админской работы, о них не ищут админов.
"""

from datetime import datetime, timezone

from services.db.connection import _get_conn, _lock

# Направления предложений.
OFFER_BOT_TO_ADMIN = "bot_to_admin"
OFFER_ADMIN_TO_BOT = "admin_to_bot"

# Что показываем вместо пропущенного вопроса.
DASH = "—"

# Статусы предложения.
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"

# Сколько предложений в сутки можно разослать. Окно нужно, чтобы раздел
# «Поиск» не превратился в массовую рассылку рекламы по платформе.
DAILY_OFFER_LIMIT = 50

# Через сколько часов после отказа можно написать этому же человеку снова.
# Отсчёт идёт от момента отказа (``decided_at``).
#
# Раньше была пауза в сутки, и это делало отказ почти бессмысленным: человек
# ждал бы следующего дня только ради того, чтобы отправить то же самое ещё
# раз. Три часа достаточно, чтобы владелец не засыпался одинаковыми
# заявками, и не заставляют ждать до следующего дня.
REOFFER_COOLDOWN_HOURS = 3

# Типы ботов, которые не участвуют в поиске.
_BOT_TYPE_EXCLUDED = ("anketa",)

_UTC = timezone.utc


def _hours_ago_str(hours: int) -> str:
    """Момент «N часов назад» в формате даты SQLite (UTC)."""
    from datetime import timedelta

    return (datetime.now(_UTC) - timedelta(hours=int(hours))).strftime(
        "%Y-%m-%d %H:%M:%S")


def _today() -> str:
    """Сегодняшняя дата в UTC — ключ дневного счётчика."""
    return datetime.now(_UTC).strftime("%Y-%m-%d")


def _clean(value, limit: int = 1000) -> str:
    """Обрезает и подчищает значение вопроса анкеты.

    Текст «к анкете» человек пишет сам, а он уходит в сообщение другому
    человеку — длинный текст Telegram бы отверг.
    """
    return str(value or "").strip()[:limit]


# ═══════════════ Анкета бота ═══════════════

def get_bot_profile(bot_id: int) -> dict | None:
    """Анкета бота (None, если она не заполнена)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM bot_profiles WHERE bot_id = ?", (int(bot_id),),
    ).fetchone()
    return dict(row) if row else None


def set_bot_profile(bot_id: int, owner_id: int, age_range: str = "",
                    category: str = "", gender: str = "",
                    text: str = "") -> bool:
    """Сохраняет анкету бота (создаёт или обновляет)."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO bot_profiles "
            "(bot_id, owner_id, age_range, category, gender, text) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bot_id) DO UPDATE SET "
            "owner_id=excluded.owner_id, age_range=excluded.age_range, "
            "category=excluded.category, gender=excluded.gender, "
            "text=excluded.text, updated_at=CURRENT_TIMESTAMP",
            (int(bot_id), int(owner_id), _clean(age_range, 60),
             _clean(category, 40), _clean(gender, 40), _clean(text)),
        )
        conn.commit()
    return True


def delete_bot_profile(bot_id: int) -> bool:
    """Убирает анкету бота."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "DELETE FROM bot_profiles WHERE bot_id = ?", (int(bot_id),),
        )
        conn.commit()
        return cur.rowcount > 0


def bot_profiles_feed(exclude_owner_id: int = 0,
                      limit: int = 50, offset: int = 0) -> list[dict]:
    """Лента ботов для раздела «🤖 Просмотр ботов».

    Анкетниц нет, свои боты не показываем: человеку неинтересно предлагать
    себя же. Каждая строка идёт вместе со статистикой бота — её показывают
    даже когда анкеты нет.

    Ботов без ``username`` больше НЕ отсекаем: такой бот тоже зарегистрирован
    на площадке, и раньше он просто исчезал из поиска вместе с возможностью
    подать на него заявку. Покажем его по ``first_name``, а если и его нет —
    как «бот<id>» (см. ``bot_display_name``).
    """
    conn = _get_conn()
    placeholders = ",".join("?" for _ in _BOT_TYPE_EXCLUDED)
    params: list = [*_BOT_TYPE_EXCLUDED]
    exclude = ""
    if exclude_owner_id:
        exclude = "AND b.owner_id != ?"
        params.append(int(exclude_owner_id))
    params.extend([int(limit), int(offset)])

    rows = conn.execute(
        f"""
        SELECT b.id AS bot_id, b.username AS username,
               b.first_name AS first_name, b.owner_id AS owner_id,
               p.age_range, p.category, p.gender, p.text,
               (SELECT COUNT(*) FROM feedback_topics t WHERE t.bot_id = b.id)
                   AS pz_count,
               (SELECT COUNT(*) FROM feedback_messages m
                 WHERE m.bot_id = b.id AND m.direction = 'out')
                   AS replies
          FROM bots b
     LEFT JOIN bot_profiles p ON p.bot_id = b.id
         WHERE b.bot_type NOT IN ({placeholders})
           {exclude}
      ORDER BY (p.bot_id IS NOT NULL) DESC, pz_count DESC, b.id
         LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def bot_profiles_feed_count(exclude_owner_id: int = 0) -> int:
    """Сколько всего ботов попадёт в ленту (без ``LIMIT``).

    Зачем отдельный счётчик
    -----------------------
    Раньше число страниц считалось как ``ceil(len(окно) / FEED_PAGE)``, то есть
    по УЖЕ ОБРЕЗАННОМУ окну запроса. При ``limit=15`` и ``FEED_PAGE=5`` это
    всегда давало 3 страницы: сколько бы ботов ни было на площадке, дальше
    пятнадцатого они были недостижимы — а владелец видел пустые «хвосты»
    списка и думал, что заявку отправляет в пустоту. Теперь количество
    страниц считается по реальному ``COUNT(*)`` с тем же WHERE.
    """
    conn = _get_conn()
    placeholders = ",".join("?" for _ in _BOT_TYPE_EXCLUDED)
    params: list = [*_BOT_TYPE_EXCLUDED]
    exclude = ""
    if exclude_owner_id:
        exclude = "AND b.owner_id != ?"
        params.append(int(exclude_owner_id))

    row = conn.execute(
        f"""
        SELECT COUNT(*)
          FROM bots b
         WHERE b.bot_type NOT IN ({placeholders})
           {exclude}
        """,
        params,
    ).fetchone()
    return int(row[0] or 0) if row else 0
# ═══════════════ Анкета админа ═══════════════

def get_admin_profile(user_id: int) -> dict | None:
    """Анкета админа (None, если она не заполнена)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM admin_profiles WHERE user_id = ?", (int(user_id),),
    ).fetchone()
    return dict(row) if row else None


def set_admin_profile(user_id: int, username: str = "", first_name: str = "",
                      age: str = "", category: str = "", pz_limit: str = "",
                      timezone: str = "", hours: str = "",
                      text: str = "", tag: str = "") -> bool:
    """Сохраняет анкету админа (создаёт или обновляет).

    ``tag`` — как админа зовут в анкетах и карточках («Ваш тег?» — первый
    вопрос анкеты). Это НЕ админский тег из ``admins``: он принадлежит
    владельцу бота и у одного человека в разных ботах он разный. Здесь
    человек называет себя сам, и именно это имя видно в «Просмотре
    профилей» вместо юзернейма.
    """
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO admin_profiles "
            "(user_id, username, first_name, age, category, pz_limit, "
            "timezone, hours, text, tag) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username=excluded.username, first_name=excluded.first_name, "
            "age=excluded.age, category=excluded.category, "
            "pz_limit=excluded.pz_limit, timezone=excluded.timezone, "
            "hours=excluded.hours, text=excluded.text, tag=excluded.tag, "
            "updated_at=CURRENT_TIMESTAMP",
            (int(user_id), _clean(username, 64), _clean(first_name, 64),
             _clean(age, 40), _clean(category, 40), _clean(pz_limit, 60),
             _clean(timezone, 60), _clean(hours, 60), _clean(text),
             _clean(tag, 40)),
        )
        conn.commit()
    return True


def delete_admin_profile(user_id: int) -> bool:
    """Убирает анкету админа."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "DELETE FROM admin_profiles WHERE user_id = ?", (int(user_id),),
        )
        conn.commit()
        return cur.rowcount > 0


def admin_profiles_feed(exclude_user_id: int = 0,
                        limit: int = 50, offset: int = 0) -> list[dict]:
    """Лента анкет админов для раздела «👥 Просмотр профилей».

    Свою анкету не показываем — предлагать себя самому себе незачем.
    """
    conn = _get_conn()
    exclude = ""
    params: list = []
    if exclude_user_id:
        exclude = "AND user_id != ?"
        params.append(int(exclude_user_id))
    params.extend([int(limit), int(offset)])

    rows = conn.execute(
        f"""
        SELECT * FROM admin_profiles
         WHERE 1 = 1 {exclude}
      ORDER BY updated_at DESC, user_id
         LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def admin_profiles_feed_count(exclude_user_id: int = 0) -> int:
    """Сколько всего анкет админов попадёт в ленту (без ``LIMIT``).

    Та же причина, что и в ``bot_profiles_feed_count``: число страниц нельзя
    считать по обрезанному окну запроса — иначе лента обрывается на пятнадцатом
    админе и дальше не листается в принципе.
    """
    conn = _get_conn()
    exclude = ""
    params: list = []
    if exclude_user_id:
        exclude = "AND user_id != ?"
        params.append(int(exclude_user_id))

    row = conn.execute(
        f"SELECT COUNT(*) FROM admin_profiles WHERE 1 = 1 {exclude}",
        params,
    ).fetchone()
    return int(row[0] or 0) if row else 0
# ═══════════════ Предложения и отклики ═══════════════

def offer_send_block(kind: str, sender_id: int, recipient_id: int,
                     bot_id: int = 0) -> str:
    """Почему предложение отправить НЕЛЬЗЯ (пустая строка — можно).

    Правила, которые ввёл владелец:
      * отправил — второй раз недоступно;
      * приняли — недоступно навсегда;
      * отказали — снова можно, но не раньше чем через
        ``REOFFER_COOLDOWN_HOURS`` часов (отсчёт от момента отказа).

    Раньше повторных ограничений не было вовсе: ``add_offer`` на повторе
    просто перезаписывал запись в ``pending``, и человек мог слать анкету
    бесконечно — и владельцу сыпались одинаковые заявки в личку.

    Возвращаем текст-причину, а не флаг: интерфейсу нужно сказать человеку
    ПОЧЕМУ кнопка не работает, иначе он будет думать, что сломалось.
    """
    offer = find_offer(kind, sender_id, recipient_id, bot_id)
    if not offer:
        return ""

    status = str(offer.get("status") or "")
    if status == STATUS_PENDING:
        return "⏳ Заявка уже отправлена и ждёт ответа. Вторую отправить нельзя."
    if status == STATUS_ACCEPTED:
        return ("✅ Вас уже приняли. Повторная заявка этому боту не нужна — "
                "просто напишите владельцу, когда он позовёт в админы.")

    # Отказ: повторно можно только после паузы. Отсчёт — от момента отказа,
    # поэтому ждать приходится ровно столько, сколько прошло с тех пор.
    decided_at = str(offer.get("decided_at") or "")
    if decided_at and decided_at > _hours_ago_str(REOFFER_COOLDOWN_HOURS):
        hours_left = max(1, int((
            datetime.fromisoformat(decided_at.replace("T", " "))
            - datetime.now(_UTC).replace(tzinfo=None)
        ).total_seconds() // 3600) + 1)
        return (f"⏳ Вам отказали в этом боте. Повторно можно будет через "
                f"{hours_left} ч (пауза {REOFFER_COOLDOWN_HOURS} ч после отказа).")
    return ""


def add_offer(kind: str, sender_id: int, recipient_id: int,
              bot_id: int = 0) -> int:
    """Создаёт предложение. Возвращает id записи (0 — не создано).

    Повторное предложение тому же человеку по тому же боту НЕ создаёт вторую
    запись, а обновляет существующую (и снова делает «ожидает»): иначе в
    «Откликах» накапливались бы дубли одного и того же приглашения.
    """
    if not sender_id or not recipient_id or sender_id == recipient_id:
        return 0
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO search_offers "
            "(kind, sender_id, recipient_id, bot_id, status) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, sender_id, recipient_id, bot_id) DO UPDATE SET "
            "status='pending', created_at=CURRENT_TIMESTAMP, decided_at=NULL",
            (str(kind), int(sender_id), int(recipient_id), int(bot_id),
             STATUS_PENDING),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM search_offers WHERE kind = ? AND sender_id = ? "
            "AND recipient_id = ? AND bot_id = ?",
            (str(kind), int(sender_id), int(recipient_id), int(bot_id)),
        ).fetchone()
    return int(row[0]) if row else 0


def get_offer(offer_id: int) -> dict | None:
    """Одно предложение по id."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM search_offers WHERE id = ?", (int(offer_id),),
    ).fetchone()
    return dict(row) if row else None


def find_offer(kind: str, sender_id: int, recipient_id: int,
               bot_id: int = 0) -> dict | None:
    """Предложение по ключу (без id — он нужен не всегда)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM search_offers WHERE kind = ? AND sender_id = ? "
        "AND recipient_id = ? AND bot_id = ?",
        (str(kind), int(sender_id), int(recipient_id), int(bot_id)),
    ).fetchone()
    return dict(row) if row else None


def set_offer_status(offer_id: int, status: str) -> bool:
    """Меняет статус предложения (одобрить / отклонить).

    Проверку прав на переход делает вызывающий обработчик; здесь мы лишь
    фиксируем решение.
    """
    if status not in (STATUS_ACCEPTED, STATUS_DECLINED):
        return False
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE search_offers SET status = ?, "
            "decided_at = CURRENT_TIMESTAMP WHERE id = ?",
            (str(status), int(offer_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def offer_message_left(offer_id: int) -> bool:
    """Отправлял ли уже владелец сообщение админу по этой заявке.

    Сообщение одно: это запасной канал, когда юзернейма нет и написать
    напрямую нечем. Повторные отправки превратили бы бота в массовую рассылку
    чужим людям, поэтому факт отправки запоминается в БД.
    """
    offer = get_offer(offer_id)
    return bool(offer and str(offer.get("contact_msg_sent_at") or "").strip())


def mark_offer_message_sent(offer_id: int) -> bool:
    """Помечает, что одно сообщение по заявке уже отправлено."""
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE search_offers SET contact_msg_sent_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (int(offer_id),),
        )
        conn.commit()
        return cur.rowcount > 0


def offers_inbox(kind: str, recipient_id: int,
                 status: str = STATUS_PENDING) -> list[dict]:
    """Входящие предложения получателя («Отклики» / «Приглашения»).

    Каждое предложение дополнено анкетой отправителя и названием бота: без
    них в ответе владельцу нечего было бы показать.

    Для «Откликов» анкета отправителя — это анкета АДМИНА
    (``admin_profiles``). Для «Приглашений» всё наоборот: отправитель —
    владелец бота, свою анкету он не заполняет, и вместо неё нужен его
    username из реестра (``owner_username``).

    ``a.tag`` и ``a.username`` тянутся из анкеты админа: карточка «Откликов»
    показывает админа по его тегу, а не по юзернейму.

    ``bot_text`` — текст из анкеты БОТА. Нужен для «Приглашений»: там
    показывается то, что владелец обещал админу приглашением («Этот текст
    увидят админы, когда ты их пригласишь»). Раньше там брался ``a.text`` —
    анкета АДМИНА, то есть приглашал владелец, а текст показывался чужой.
    """
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT o.*,
               b.username AS bot_username,
               b.first_name AS bot_first_name,
               a.age, a.category, a.pz_limit, a.timezone, a.hours, a.text,
               a.tag, a.username AS admin_username,
               p.text AS bot_text,
               r.username AS owner_username,
               r.first_name AS owner_first_name
          FROM search_offers o
     LEFT JOIN bots b ON b.id = o.bot_id
     LEFT JOIN admin_profiles a ON a.user_id = o.sender_id
     LEFT JOIN bot_profiles p ON p.bot_id = o.bot_id
     LEFT JOIN users_registry r ON r.user_id = o.sender_id
         WHERE o.kind = ? AND o.recipient_id = ? AND o.status = ?
      ORDER BY o.id
        """,
        (str(kind), int(recipient_id), str(status)),
    ).fetchall()
    return [dict(r) for r in rows]


def count_offers(kind: str, user_id: int, status: str | None = None) -> int:
    """Сколько предложений человек ОТПРАВИЛ (для счётчиков карточки поиска)."""
    conn = _get_conn()
    sql = "SELECT COUNT(*) FROM search_offers WHERE kind = ? AND sender_id = ?"
    params: list = [str(kind), int(user_id)]
    if status:
        sql += " AND status = ?"
        params.append(str(status))
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0)


def count_offers_received(kind: str, user_id: int,
                          status: str | None = None) -> int:
    """Сколько предложений человек ПОЛУЧИЛ."""
    conn = _get_conn()
    sql = "SELECT COUNT(*) FROM search_offers WHERE kind = ? AND recipient_id = ?"
    params: list = [str(kind), int(user_id)]
    if status:
        sql += " AND status = ?"
        params.append(str(status))
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0)


# ═══════════════ Просмотры карточек ═══════════════

def mark_profile_viewed(viewer_id: int, target_kind: str,
                        target_id: int) -> None:
    """Отмечает, что человек посмотрел карточку.

    Повторный просмотр той же карточки счётчик НЕ увеличивает: иначе один
    человек, листая туда-сюда, выглядел бы как десяток разных.
    """
    if not viewer_id or not target_id:
        return
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO search_views "
            "(viewer_id, target_kind, target_id) VALUES (?, ?, ?)",
            (int(viewer_id), str(target_kind), int(target_id)),
        )
        conn.commit()


def count_profile_views(target_kind: str, target_id: int) -> int:
    """Сколько человек посмотрели карточку."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM search_views WHERE target_kind = ? AND target_id = ?",
        (str(target_kind), int(target_id)),
    ).fetchone()
    return int(row[0] or 0)


# ═══════════════ Дневной счётчик рассылки ═══════════════

def get_daily_sent(user_id: int) -> int:
    """Сколько предложений человек разослал сегодня.

    Счётчик обнуляется сам за сутки: строка привязана к дате, поэтому
    вчерашняя просто не читается.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT sent FROM search_daily WHERE user_id = ? AND day = ?",
        (int(user_id), _today()),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def bump_daily_sent(user_id: int) -> int:
    """Учитывает ещё одно отправленное предложение. Возвращает счётчик дня."""
    today = _today()
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO search_daily (user_id, day, sent) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET sent = sent + 1",
            (int(user_id), today),
        )
        conn.commit()
        row = conn.execute(
            "SELECT sent FROM search_daily WHERE user_id = ? AND day = ?",
            (int(user_id), today),
        ).fetchone()
    return int(row[0] or 0) if row else 0


def purge_old_search_days(keep_days: int = 7) -> int:
    """Убирает дневные счётчики старше ``keep_days``.

    Вызывается при старте: без чистки таблица росла бы по строке на
    пользователя в сутки.
    """
    from datetime import timedelta

    cutoff = (datetime.now(_UTC) - timedelta(days=int(keep_days))).strftime(
        "%Y-%m-%d")
    conn = _get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM search_daily WHERE day < ?", (cutoff,))
        conn.commit()
        return cur.rowcount