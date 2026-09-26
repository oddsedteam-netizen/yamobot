"""Глобальная конфигурация бота из переменных окружения."""

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
_ENV_PATH = _BASE_DIR / ".env"

if _ENV_PATH.exists():
    load_dotenv(dotenv_path=_ENV_PATH, override=True, encoding="utf-8-sig")


def _clean_token(raw: str) -> str:
    return raw.strip().strip('"').strip("'")


BOT_TOKEN = _clean_token(os.getenv("BOT_TOKEN", ""))

# Супер-админ платформы (принимает жалобы, видит профили пользователей).
# Задаётся через переменную ADMIN (мой Telegram ID). Для обратной совместимости
# также читается OWNER_ID, но приоритет у ADMIN.
def _parse_id(raw: str) -> int:
    try:
        return int(str(raw).strip())
    except ValueError:
        return 0


ADMIN_ID = _parse_id(os.getenv("ADMIN", os.getenv("OWNER_ID", "") or "0"))
# Алиас для совместимости со старым кодом (complaints.py использует OWNER_ID).
OWNER_ID = ADMIN_ID


def is_super_admin(user_id: int) -> bool:
    """Является ли пользователь супер-админом платформы."""
    return bool(ADMIN_ID) and user_id == ADMIN_ID


# ═══════════════ Прокси для Telegram (необязательно) ═══════════════
# Нужен там, где api.telegram.org напрямую недоступен: без него бот падает
# при старте с «Превышен таймаут семафора» / ClientConnectorError.
#   http://host:port    — работает «из коробки» (aiohttp);
#   socks5://host:port  — дополнительно нужен пакет aiohttp[socks].
PROXY_URL = _clean_token(os.getenv("PROXY_URL", "") or "")


def proxy_settings() -> dict[str, Any]:
    """Аргументы для ``Bot(...)``: прокси, если он задан в .env.

    Пустой словарь — работать без прокси (обычный случай).
    """
    return {"proxy": PROXY_URL} if PROXY_URL else {}


def proxy_hint() -> str:
    """Подсказка для пользователя, если Telegram недоступен из сети."""
    hint = (
        "Проверь интернет, VPN/прокси и правила файрвола.\n"
        "Если api.telegram.org напрямую недоступен, укажи прокси в .env: "
        "PROXY_URL=http://host:port (либо socks5://host:port, для socks5 "
        "дополнительно выполни: pip install aiohttp[socks])."
    )
    if PROXY_URL:
        hint = f"Сейчас настроен прокси: {PROXY_URL}\n{hint}"
    return hint