"""Persistent bot data: API keys and authorized users (never log secrets)."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent
DATA_DIR = _ROOT / "data"
API_KEYS_FILE = DATA_DIR / "api_keys.json"
AUTH_USERS_FILE = DATA_DIR / "authorized_users.json"

PLACEHOLDER_API_KEYS = frozenset({"", "your_search4faces_api_key", "YOUR_SEARCH4FACES_API_KEY"})


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return dict(default)
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else dict(default)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", path.name, exc)
        return dict(default)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_data_dir()
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def _is_placeholder_key(key: str) -> bool:
    k = key.strip()
    return not k or k.lower() in {x.lower() for x in PLACEHOLDER_API_KEYS if x}


def mask_api_key(key: str) -> str:
    k = key.strip()
    if len(k) <= 8:
        return "****"
    if "-" in k:
        prefix, suffix = k.rsplit("-", 1)
        return f"{'*' * min(12, len(prefix))}-{suffix[-4:]}"
    return f"****{k[-4:]}"


def get_owner_id() -> int | None:
    raw = (os.environ.get("OWNER_USER_ID") or os.environ.get("BOT_OWNER_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def load_api_keys() -> list[str]:
    data = _read_json(API_KEYS_FILE, {"keys": []})
    keys = data.get("keys") or []
    out = [str(k).strip() for k in keys if isinstance(k, str) and not _is_placeholder_key(str(k))]
    return out


def save_api_keys(keys: list[str]) -> None:
    clean = [k.strip() for k in keys if k.strip() and not _is_placeholder_key(k)]
    _write_json(API_KEYS_FILE, {"keys": clean})


def add_api_key(key: str) -> tuple[bool, str]:
    k = key.strip()
    if _is_placeholder_key(k):
        return False, "Invalid or empty API key."
    keys = load_api_keys()
    if k in keys:
        return False, "That key is already in the pool."
    keys.append(k)
    save_api_keys(keys)
    return True, f"Added key slot {len(keys)} ({mask_api_key(k)})."


def remove_api_key(index: int) -> tuple[bool, str]:
    keys = load_api_keys()
    if index < 0 or index >= len(keys):
        return False, "Invalid key slot."
    removed = keys.pop(index)
    save_api_keys(keys)
    return True, f"Removed slot {index + 1} ({mask_api_key(removed)})."


def migrate_env_api_key_if_needed() -> None:
    """One-time import from SEARCH4FACES_API_KEY when data file is empty."""
    if load_api_keys():
        return
    env_key = (os.environ.get("SEARCH4FACES_API_KEY") or "").strip()
    if not _is_placeholder_key(env_key):
        save_api_keys([env_key])
        logger.info("Imported API key from .env into data/api_keys.json")


def load_authorized_users() -> dict[str, Any]:
    return _read_json(AUTH_USERS_FILE, {"users": [], "labels": {}})


def save_authorized_users(users: list[int], labels: dict[str, str]) -> None:
    _write_json(
        AUTH_USERS_FILE,
        {"users": sorted(set(users)), "labels": labels},
    )


def list_authorized_user_ids() -> list[int]:
    data = load_authorized_users()
    users = data.get("users") or []
    return [int(u) for u in users]


def user_label(user_id: int) -> str:
    data = load_authorized_users()
    labels = data.get("labels") or {}
    return str(labels.get(str(user_id), user_id))


def authorize_user(user_id: int, label: str = "") -> tuple[bool, str]:
    owner = get_owner_id()
    if owner is not None and user_id == owner:
        return False, "The owner is always allowed; no need to /auth."

    users = list_authorized_user_ids()
    labels = dict(load_authorized_users().get("labels") or {})
    if user_id in users:
        return False, f"User <code>{user_id}</code> is already authorized."

    users.append(user_id)
    if label:
        labels[str(user_id)] = label
    save_authorized_users(users, labels)
    return True, f"Authorized user <code>{user_id}</code>."


def unauthorize_user(user_id: int) -> tuple[bool, str]:
    owner = get_owner_id()
    if owner is not None and user_id == owner:
        return False, "Cannot remove the owner."

    users = list_authorized_user_ids()
    if user_id not in users:
        return False, f"User <code>{user_id}</code> is not on the list."

    users.remove(user_id)
    labels = dict(load_authorized_users().get("labels") or {})
    labels.pop(str(user_id), None)
    save_authorized_users(users, labels)
    return True, f"Removed user <code>{user_id}</code>."


def is_owner(user_id: int | None) -> bool:
    if user_id is None:
        return False
    owner = get_owner_id()
    return owner is not None and user_id == owner


def is_authorized(user_id: int | None) -> bool:
    if user_id is None:
        return False
    if is_owner(user_id):
        return True
    return user_id in list_authorized_user_ids()


def parse_user_target(text: str) -> str:
    """Return 'id:123' or 'user:@name' for resolution."""
    t = text.strip()
    if t.startswith("@"):
        return f"user:{t}"
    if re.fullmatch(r"\d+", t):
        return f"id:{t}"
    return ""
