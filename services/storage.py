import json
import logging
from pathlib import Path
from threading import Lock

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "bots.json"

_lock = Lock()


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(exist_ok=True)


def load_bots() -> dict:
    _ensure_data_dir()
    if not DB_PATH.exists():
        return {}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.warning("Ошибка чтения bots.json: %s", e)
        return {}


def save_bots(data: dict) -> None:
    _ensure_data_dir()
    with _lock:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_bots(user_id: int) -> list[dict]:
    data = load_bots()
    return data.get(str(user_id), [])


def add_user_bot(user_id: int, bot_info: dict) -> None:
    data = load_bots()
    key = str(user_id)
    if key not in data:
        data[key] = []

    for b in data[key]:
        if b["id"] == bot_info["id"]:
            b.update(bot_info)
            save_bots(data)
            return

    data[key].append(bot_info)
    save_bots(data)


def remove_user_bot(user_id: int, bot_id: int) -> bool:
    data = load_bots()
    key = str(user_id)
    if key not in data:
        return False

    before = len(data[key])
    data[key] = [b for b in data[key] if b["id"] != bot_id]
    if len(data[key]) < before:
        save_bots(data)
        return True
    return False


def get_bot_by_id(user_id: int, bot_id: int) -> dict | None:
    bots = get_user_bots(user_id)
    return next((b for b in bots if b["id"] == bot_id), None)


def update_bot_field(user_id: int, bot_id: int, field: str, value) -> bool:
    data = load_bots()
    key = str(user_id)
    if key not in data:
        return False

    for b in data[key]:
        if b["id"] == bot_id:
            b[field] = value
            save_bots(data)
            return True
    return False


def get_all_bots_flat() -> list[dict]:
    """Все боты всех юзеров — для запуска дочерок."""
    data = load_bots()
    result = []
    for user_id, bots in data.items():
        for b in bots:
            b_copy = dict(b)
            b_copy["owner_id"] = int(user_id)
            result.append(b_copy)
    return result


def bot_display_name(b: dict) -> str:
    name = b.get("first_name") or b.get("username") or f"bot_{b['id']}"
    username = f" (@{b['username']})" if b.get("username") else ""
    return f"{name}{username}"