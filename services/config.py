"""Глобальная конфигурация бота из переменных окружения."""

import os
from pathlib import Path

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
_ENV_PATH = _BASE_DIR / ".env"

if _ENV_PATH.exists():
    load_dotenv(dotenv_path=_ENV_PATH, override=True, encoding="utf-8-sig")


def _clean_token(raw: str) -> str:
    return raw.strip().strip('"').strip("'")


BOT_TOKEN = _clean_token(os.getenv("BOT_TOKEN", ""))

# Супер-админ платформы (принимает жалобы, видит профили пользователей).
# Если не задан — супер-админские разделы недоступны.
_owner_raw = os.getenv("OWNER_ID", "0").strip()
try:
    OWNER_ID = int(_owner_raw)
except ValueError:
    OWNER_ID = 0


def is_super_admin(user_id: int) -> bool:
    """Является ли пользователь супер-админом платформы."""
    return bool(OWNER_ID) and user_id == OWNER_ID