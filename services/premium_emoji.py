"""Премиум-эмодзи (custom emoji): обучение, разметка и мягкий ремонт.

В Telegram премиум-эмодзи передаются не «символом», а сущностью ``custom_emoji``
(в HTML-разметке — тегом ``<tg-emoji emoji-id="...">🙂</tg-emoji>``). Если эмодзи
сохранён в текст БЕЗ тега, Telegram отправит обычный эмодзи — и это тихий сбой:
ошибки нет, премиум просто пропадает. Именно так ломается приветствие, если в
редактор вставить эмодзи копированием из сообщения (клиент Telegram превращает
премиум в обычный символ).

Модуль решает это тремя шагами:

1. **Обучение.** Когда владелец присылает сообщение с премиум-эмодзи, Telegram
   отдаёт сущности ``custom_emoji`` — мы запоминаем пару
   «обычный эмодзи → custom_emoji_id» в таблице ``emoji_map``.
2. **Мягкий ремонт.** ``upgrade_markup`` оборачивает «потерянные» эмодзи в
   ``<tg-emoji>`` тем идентификатором, который уже известен по словарю. Это
   чинит приветствия СТАРЫХ ботов без удаления бота и без потери данных:
   вызывается при сохранении приветствия и кнопкой «🔄 Полный перезапуск».
3. **Страховка на отправке.** ``markup_to_entities`` превращает теги
   ``<tg-emoji>`` в отдельные сущности — благодаря этому премиум-эмодзи
   доходят до пользователя даже тогда, когда HTML-разметка почему-то не
   распарсилась и сообщение ушло без форматирования.
"""

import logging
import re
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, MessageEntity, TelegramObject

from services.storage import get_emoji_map, remember_custom_emoji

logger = logging.getLogger(__name__)

# Тег премиум-эмодзи, который присылает aiogram в Message.html_text
EMOJI_TAG_RE = re.compile(r'<tg-emoji\s+emoji-id="(\d+)">(.+?)</tg-emoji>', re.DOTALL)

# Фрагменты, которые НЕ трогаем при автооборачивании эмодзи:
#   • уже готовый тег премиум-эмодзи;
#   • блоки кода (там эмодзи должны остаться обычным текстом).
PROTECTED_RE = re.compile(
    r'<tg-emoji\s+emoji-id="(\d+)">.+?</tg-emoji>'
    r'|<(code|pre)(?:\s[^>]*)?>.*?</\2>',
    re.DOTALL | re.IGNORECASE,
)

# Эмодзи, которые НЕЛЬЗЯ заворачивать в HTML-тег: внутри тега ломают разметку.
_UNSAFE_IN_TAG = ("<", ">", "&", '"')
# Слишком длинная «сущность» эмодзи — не эмодзи, а что-то постороннее.
MAX_EMOJI_LEN = 12


def utf16_len(text: str) -> int:
    """Длина строки в UTF-16 юнитах — так считает смещения Telegram."""
    return len(text.encode("utf-16-le")) // 2


def slice_utf16(text: str, offset: int, length: int) -> str:
    """Вырезает подстроку по смещению/длине в UTF-16 юнитах (семантика Telegram)."""
    data = text.encode("utf-16-le")
    return data[offset * 2:(offset + length) * 2].decode("utf-16-le", "ignore")


def is_valid_emoji(emoji: str) -> bool:
    """Годится ли строка как «обычный эмодзи» внутри тега <tg-emoji>."""
    if not emoji or len(emoji) > MAX_EMOJI_LEN:
        return False
    if any(ch in emoji for ch in _UNSAFE_IN_TAG):
        return False
    # Пробелы/переводы строк — точно не один эмодзи.
    return not any(ch.isspace() for ch in emoji)


def has_premium_markup(text: str | None) -> bool:
    """Есть ли в тексте разметка премиум-эмодзи."""
    return bool(text) and bool(EMOJI_TAG_RE.search(text or ""))


def count_premium_markup(text: str | None) -> int:
    """Сколько премиум-эмодзи размечено в тексте тегами ``<tg-emoji>``."""
    if not text:
        return 0
    return len(EMOJI_TAG_RE.findall(text))


def build_markup(emoji: str, emoji_id: str) -> str:
    """Собирает тег премиум-эмодзи."""
    return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'


def learn_from_message(message: Message) -> int:
    """Запоминает пары «эмодзи → custom_emoji_id» из сообщения. Возвращает число новых.

    Учимся только в личных чатах: приветствия и рассылки владелец пишет именно
    там, а посторонние сообщения (например, в «чате админов») словарь не портят.
    """
    chat = getattr(message, "chat", None)
    chat_type = getattr(chat, "type", None)
    if chat_type is not None and str(getattr(chat_type, "value", chat_type)) != "private":
        return 0

    learned = 0
    pairs = (
        (getattr(message, "entities", None), getattr(message, "text", None)),
        (getattr(message, "caption_entities", None), getattr(message, "caption", None)),
    )
    for entities, text in pairs:
        if not text or not entities:
            continue
        for entity in entities:
            if str(getattr(entity, "type", "")) != "custom_emoji":
                continue
            emoji_id = getattr(entity, "custom_emoji_id", None)
            if not emoji_id:
                continue
            emoji = slice_utf16(text, entity.offset, entity.length)
            if not is_valid_emoji(emoji):
                continue
            if remember_custom_emoji(emoji, str(emoji_id)):
                learned += 1
    if learned:
        logger.info("Запомнил премиум-эмодзи: %d", learned)
    return learned


def _build_pattern(mapping: dict[str, str]) -> re.Pattern[str] | None:
    """Регэксп по известным эмодзи (длинные раньше коротких: ❤️ важнее ❤)."""
    keys = [k for k in mapping if k]
    if not keys:
        return None
    keys.sort(key=len, reverse=True)
    return re.compile("|".join(re.escape(k) for k in keys))


def _wrap_plain_emoji(piece: str, pattern: re.Pattern[str] | None,
                      mapping: dict[str, str]) -> tuple[str, int]:
    """Оборачивает известные обычные эмодзи в тег премиум-эмодзи."""
    if not piece or pattern is None:
        return piece, 0

    wrapped = 0

    def _repl(match: re.Match[str]) -> str:
        nonlocal wrapped
        emoji = match.group(0)
        emoji_id = mapping.get(emoji)
        if not emoji_id:
            return emoji
        wrapped += 1
        return build_markup(emoji, emoji_id)

    return pattern.sub(_repl, piece), wrapped


def upgrade_markup(text: str | None, mapping: dict[str, str] | None = None) -> tuple[str, int]:
    """Восстанавливает премиум-эмодзи в сохранённом тексте.

    Известные по словарю обычные эмодзи оборачиваются в ``<tg-emoji>``. Уже
    размеченные эмодзи и блоки кода не трогаются (операция идемпотентна).

    Возвращает (новый текст, сколько эмодзи восстановлено).
    """
    if not text:
        return text or "", 0

    mapping = mapping if mapping is not None else get_emoji_map()
    pattern = _build_pattern(mapping)
    if pattern is None:
        return text, 0

    result: list[str] = []
    total = 0
    position = 0
    for protected in PROTECTED_RE.finditer(text):
        piece, wrapped = _wrap_plain_emoji(text[position:protected.start()], pattern, mapping)
        total += wrapped
        result.append(piece)
        result.append(protected.group(0))  # защищённый фрагмент — как есть
        position = protected.end()

    piece, wrapped = _wrap_plain_emoji(text[position:], pattern, mapping)
    total += wrapped
    result.append(piece)

    return "".join(result), total


def prepare_welcome(message: Message) -> str:
    """Готовит текст приветствия из сообщения владельца.

    Сохраняет HTML-разметку (как раньше) и дополнительно «дотягивает» премиум
    до известных эмодзи из словаря — это чинит случаи, когда владелец вставил
    эмодзи копированием и Telegram превратил его в обычный.
    """
    html = message.html_text or message.text or ""
    learn_from_message(message)
    upgraded, _ = upgrade_markup(html)
    return upgraded


def markup_to_entities(text: str | None) -> tuple[str, list[MessageEntity]]:
    """Превращает теги ``<tg-emoji>`` в сущности ``custom_emoji``.

    Нужна как страховка: если HTML-разметка не распарсилась, сообщение всё
    равно можно отправить без parse_mode, передав сущности руками — тогда
    премиум-эмодзи (и текст) не потеряются.
    """
    if not text:
        return "", []
    if not EMOJI_TAG_RE.search(text):
        return text, []

    raw_parts: list[str] = []
    entities: list[MessageEntity] = []
    position = 0
    plain_len = 0
    for match in EMOJI_TAG_RE.finditer(text):
        prefix = text[position:match.start()]
        raw_parts.append(prefix)
        plain_len += utf16_len(prefix)

        emoji = match.group(2)
        entities.append(MessageEntity(
            type="custom_emoji",
            offset=plain_len,
            length=utf16_len(emoji),
            custom_emoji_id=match.group(1),
        ))
        raw_parts.append(emoji)
        plain_len += utf16_len(emoji)
        position = match.end()

    raw_parts.append(text[position:])
    return "".join(raw_parts), entities


class PremiumEmojiLearningMiddleware(BaseMiddleware):
    """Пополняет словарь премиум-эмодзи по сообщениям, которые получает YamoBot.

    Владелец отправляет премиум-эмодзи (в приветствии, рассылке и т.п.) —
    Telegram присылает сущности ``custom_emoji``, и мы запоминаем пару
    «обычный эмодзи → custom_emoji_id». Дальше этой парой можно «лечить»
    приветствия ботов, где эмодзи потерял премиум (см. ``upgrade_markup``).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            try:
                learn_from_message(event)
            except Exception as e:  # обучение не должно мешать обработке
                logger.debug("Не удалось запомнить премиум-эмодзи: %s", e)
        return await handler(event, data)