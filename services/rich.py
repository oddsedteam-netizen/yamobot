"""Работа с rich-сообщениями («статьями») Telegram независимо от версии aiogram.

Telegram Bot API умеет «статьи» (rich messages): сообщение приходит как
``message.rich_message``, а отправляется методом ``sendRichMessage``. Разные
версии aiogram поддерживают это по-разному:

  • 3.31+ — есть поле ``Message.rich_message``, ``Bot.send_rich_message`` и
    ``Message.answer_rich``;
  • 3.28 и раньше — таких полей нет, но aiogram разрешает «лишние» поля
    (``extra="allow"``), поэтому статья лежит в ``Message.model_extra``
    обычным словарём.

Модуль даёт единый способ и читать статью (получаем JSON), и отправлять её
(собираем метод ``sendRichMessage`` вручную), чтобы приветствие-статья
работало на любой из этих версий.
"""

import json
import logging

logger = logging.getLogger(__name__)

# Медиа-блоки внутри статьи: при пересылке дочерним ботом их отбрасываем —
# file_id принадлежит тому боту, который принял файл, чужой бот его не отправит.
_MEDIA_BLOCK_TYPES = {
    "photo", "video", "audio", "document", "animation", "voice_note",
    "collage", "slideshow",
}

# Чаты, куда уже не получилось отправить статью — чтобы не спамить в лог.
_RICH_WARNED: set[int] = set()


def _rich_raw(message) -> object | None:
    """Достаёт rich-сообщение из апдейта (объект или словарь — по версии aiogram)."""
    rich = getattr(message, "rich_message", None)
    if rich:
        return rich
    extra = getattr(message, "model_extra", None) or {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            if "rich" in str(key).lower() and value:
                return value
    return None


def has_rich(message) -> bool:
    """Есть ли у сообщения rich-содержимое («статья»)."""
    return _rich_raw(message) is not None


def rich_json_of(rich) -> str:
    """Превращает rich-сообщение (объект или словарь) в JSON-строку.

    Не бросает исключений: при любой ошибке возвращает пустую строку, чтобы
    некорректная статья не ломала обработку сообщения.
    """
    if rich is None:
        return ""
    if isinstance(rich, (dict, list)):
        if not rich:
            # Пустой словарь/список — считаем, что статьи нет.
            return ""
        try:
            return json.dumps(rich, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    dump = getattr(rich, "model_dump", None)
    if callable(dump):
        try:
            return json.dumps(dump(), ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return ""
    return ""


def extract_rich_json(message) -> str:
    """Возвращает статью из сообщения в виде JSON (или пустую строку)."""
    return rich_json_of(_rich_raw(message))


def _text_to_plain(value) -> str:
    """Собирает обычный текст из RichTextUnion (строка/список/объект/словарь)."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_text_to_plain(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") == "custom_emoji":
            # Премиум-эмодзи в обычном тексте показываем его символом.
            return str(value.get("alternative_text") or "")
        if "text" in value:
            return _text_to_plain(value.get("text"))
        return ""
    plain = getattr(value, "text", None)
    if plain is not None:
        return _text_to_plain(plain)
    return getattr(value, "alternative_text", "") or ""


def _blocks_of(data) -> list:
    """Достаёт список блоков из разобранного rich-сообщения или его объекта."""
    if isinstance(data, dict):
        blocks = data.get("blocks")
    elif isinstance(data, list):
        blocks = data
    else:
        blocks = getattr(data, "blocks", None)
    return blocks if isinstance(blocks, list) else []


def _block_field(block, name):
    """Читает поле блока у словаря или у pydantic-объекта."""
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def rich_to_plain_text(rich_json: str) -> str:
    """Обычный текст статьи (превью в панели и запасной вариант отправки)."""
    if not rich_json:
        return ""
    try:
        data = json.loads(rich_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    parts: list[str] = []
    for block in _blocks_of(data):
        text = _block_field(block, "text")
        if text is None:
            caption = _block_field(block, "caption")
            if isinstance(caption, dict):
                text = caption.get("text")
            elif caption is not None:
                text = getattr(caption, "text", None)
        plain = _text_to_plain(text).strip()
        if plain:
            parts.append(plain)
    return "\n\n".join(parts)[:4000]


def count_media_blocks(rich_json: str) -> int:
    """Сколько медиа-блоков (фото/видео/…) в статье — для предупреждений."""
    if not rich_json:
        return 0
    try:
        data = json.loads(rich_json)
    except (json.JSONDecodeError, TypeError):
        return 0
    return sum(
        1 for block in _blocks_of(data)
        if str(_block_field(block, "type") or "") in _MEDIA_BLOCK_TYPES
    )


def rich_payload_from_json(rich_json: str) -> dict | None:
    """Готовит ``rich_message`` для отправки (без неподдерживаемых медиа-блоков).

    Медиа-блоки выкидываем: их file_id привязан к боту, который принял файл.
    Если после фильтрации не осталось ни одного блока — возвращаем None, тогда
    вызывающий код отправит приветствие обычным текстом.
    """
    if not rich_json:
        return None
    try:
        data = json.loads(rich_json)
    except (json.JSONDecodeError, TypeError):
        return None

    blocks: list = []
    for block in _blocks_of(data):
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "") in _MEDIA_BLOCK_TYPES:
            continue
        blocks.append(block)
    if not blocks:
        return None
    return {"blocks": blocks}


_SEND_RICH_METHOD = None


def send_rich_method():
    """Метод ``sendRichMessage`` для любой версии aiogram (создаём один раз).

    В aiogram 3.31+ есть готовый ``Bot.send_rich_message``, но он есть не везде,
    поэтому собираем запрос вручную: Bot API принимает те же поля, а aiogram
    корректно сериализует pydantic-модель вместе со стилями кнопок.
    """
    global _SEND_RICH_METHOD
    if _SEND_RICH_METHOD is not None:
        return _SEND_RICH_METHOD

    from aiogram.methods.base import TelegramMethod
    from aiogram.types import Message as TgMessage

    class SendRichMessageMethod(TelegramMethod):
        """Отправка rich-сообщения (статьи) — Bot API ``sendRichMessage``."""

        __returning__ = TgMessage
        __api_method__ = "sendRichMessage"

        chat_id: int | str
        rich_message: dict
        message_thread_id: int | None = None
        reply_markup: object | None = None

    _SEND_RICH_METHOD = SendRichMessageMethod
    return _SEND_RICH_METHOD


async def send_rich(bot, chat_id: int, payload: dict,
                    message_thread_id: int | None = None,
                    reply_markup=None):
    """Отправляет статью. Возвращает Message или None, если не получилось."""
    method_cls = send_rich_method()
    method = method_cls(
        chat_id=chat_id,
        rich_message=payload,
        message_thread_id=message_thread_id,
        reply_markup=reply_markup,
    )
    try:
        return await bot(method)
    except Exception as e:
        if chat_id not in _RICH_WARNED:
            _RICH_WARNED.add(chat_id)
            logger.warning(
                "Не удалось отправить статью (sendRichMessage) в %s: %s — "
                "отправляю приветствие обычным текстом", chat_id, e,
            )
        return None