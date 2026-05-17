"""
Tele4Faces — Telegram bot for search4faces.com (detectFaces → searchFace).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import subprocess
import sys
import traceback
from io import BytesIO
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_REQUIREMENTS = _ROOT / "requirements.txt"


def _ensure_runtime_dependencies() -> None:
    if (os.environ.get("TELE4FACES_NO_AUTO_PIP") or "").strip().lower() in ("1", "true", "yes"):
        return

    def _need_install() -> bool:
        for mod in ("httpx", "telegram", "dotenv"):
            if importlib.util.find_spec(mod) is None:
                return True
        return False

    if not _need_install():
        return

    if not _REQUIREMENTS.is_file():
        print("Missing packages and requirements.txt not found.", file=sys.stderr)
        sys.exit(1)

    print("Tele4Faces: installing dependencies…", flush=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], cwd=str(_ROOT))
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-r", str(_REQUIREMENTS)],
        cwd=str(_ROOT),
    )


_ensure_runtime_dependencies()

import asyncio
import html

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from api_pool import ApiKeyPool, build_api_pool
from bot_storage import (
    add_api_key,
    authorize_user,
    get_owner_id,
    is_authorized,
    is_owner,
    migrate_env_api_key_if_needed,
    parse_user_target,
    remove_api_key,
    unauthorize_user,
)
from search4faces_client import (
    SEARCH4FACES_DEFAULT_URL,
    Search4FacesError,
)

load_dotenv(_ROOT / ".env")

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or ""
).strip()
PLACEHOLDER_BOT_TOKENS = frozenset({"your_telegram_bot_token", "YOUR_TELEGRAM_BOT_TOKEN"})

API_URL = (os.environ.get("SEARCH4FACES_API_URL") or "").strip() or SEARCH4FACES_DEFAULT_URL
MOCK_ENV = (os.environ.get("MOCK_API") or "").lower() in ("1", "true", "yes")

IMAGE_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "image/bmp",
        "image/gif",
        "image/heic",
        "image/heif",
    }
)
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".heic", ".heif"})
EXPECT_KEY = "expecting_face_photo"
PENDING_KEY = "pending_detection"
AWAITING_API_KEY = "awaiting_api_key"
DENIED_HTML = "⛔ You are not authorized to use this bot."
OWNER_ONLY_HTML = "⛔ This command is only available to the bot owner."

# Search sources (everywhere = all of these, one API call each)
SEARCH_SOURCE_KEYS = (
    "vk_wall",
    "vkokn_avatar",
    "vkok_avatar",
    "tt_avatar",
    "ch_avatar",
    "sb_photo",
)

SOURCE_LABELS: dict[str, str] = {
    "vk_wall": "🔵 Vkontakte",
    "vkokn_avatar": "🟠 OK & VK (2022-2024)",
    "vkok_avatar": "🟡 OK & VK (2019-2020)",
    "tt_avatar": "🎵 TikTok Search",
    "ch_avatar": "🎙 ClubHouse Search",
    "sb_photo": "⭐ Famous People",
}

MAX_SCORE = 100.0
CONFIDENCE_OPTIONS = (80, 60, 40)
# Fetch a few extra from API, then keep only 80–100% matches (saves Telegram spam, not API calls)
API_RESULTS_FETCH = max(5, min(30, int(os.environ.get("API_RESULTS_FETCH", "10"))))
SOURCE_DELAY_SEC = max(0.0, float(os.environ.get("SOURCE_DELAY_SEC", "1")))
SEARCH_LANG = (os.environ.get("SEARCH_LANG") or "en").strip() or "en"
INCLUDE_HIDDEN = (os.environ.get("INCLUDE_HIDDEN_PROFILES") or "true").lower() in (
    "1",
    "true",
    "yes",
)


WELCOME_HTML = (
    "<b>Tele4Faces</b> — face search via search4faces.com\n\n"
    "Send any <b>photo</b> to start. Pick sources (✅), press <b>Finished</b>, "
    "then choose a confidence band. Each match is an album: result + your original photo.\n\n"
    "Type /help for all commands."
)

HELP_USER_HTML = (
    "<b>📖 Commands</b>\n\n"
    "/start — welcome message\n"
    "/help — this list\n"
    "/face — search a photo (or reply <code>/face</code> to an old one)\n"
    "/cancel — drop the current face-search session\n"
    "/quota — API usage limits (no secrets shown)\n\n"
    "<b>How to search</b>\n"
    "1. Send a photo (or image file).\n"
    "2. If several faces appear, pick one.\n"
    "3. Toggle sources, then <b>Finished</b>.\n"
    "4. Pick minimum score (80%, 60%, or 40%).\n"
    "5. Matches arrive as albums (match + your upload).\n\n"
    "If nothing matches, you can pick another confidence band for the same photo."
)

HELP_OWNER_HTML = (
    "\n\n<b>🔐 Owner only</b>\n"
    "/auth &lt;user_id&gt; — allow a Telegram user by numeric ID\n"
    "/auth @username — allow by @username\n"
    "/unauth &lt;id|@user&gt; — revoke access\n"
    "/api — add, list, or remove Search4Faces API keys (rotation when several)\n\n"
    "Only the owner can use /api and /auth. Authorized users can search and use /quota."
)


def document_is_image(document) -> bool:
    mime = (document.mime_type or "").lower()
    if mime in IMAGE_MIME_TYPES or mime.startswith("image/"):
        return True
    name = (document.file_name or "").lower()
    return any(name.endswith(ext) for ext in IMAGE_EXTENSIONS)


def message_has_image(msg) -> bool:
    if msg is None:
        return False
    if msg.photo:
        return True
    return bool(msg.document and document_is_image(msg.document))


def resolve_image_file_id(msg) -> str | None:
    if not message_has_image(msg):
        return None
    if msg.photo:
        return msg.photo[-1].file_id
    if msg.document:
        return msg.document.file_id
    return None


def parse_score(profile: dict[str, Any]) -> float | None:
    raw = profile.get("score")
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", ".").strip())
    except ValueError:
        return None


def score_in_range(
    profile: dict[str, Any],
    min_score: float,
    max_score: float = MAX_SCORE,
) -> bool:
    s = parse_score(profile)
    if s is None:
        return False
    return min_score <= s <= max_score


def _parse_detect_scale(detected: dict[str, Any]) -> float:
    raw = detected.get("scale", 1)
    try:
        scale = float(raw)
    except (TypeError, ValueError):
        scale = 1.0
    return scale if scale > 0 else 1.0


def map_face_to_image_coords(face: dict[str, Any], detect_scale: float) -> dict[str, Any]:
    """Map API face coordinates to the uploaded image pixel space."""
    if abs(detect_scale - 1.0) < 0.001:
        return face
    factor = 1.0 / detect_scale
    mapped: dict[str, Any] = dict(face)
    for key, value in face.items():
        if key in ("x", "y", "width", "height") or key.startswith("lm"):
            try:
                mapped[key] = int(round(float(value) * factor))
            except (TypeError, ValueError):
                mapped[key] = value
    return mapped


def _face_landmarks(face: dict[str, Any]) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for i in range(1, 6):
        try:
            points.append((int(face[f"lm{i}_x"]), int(face[f"lm{i}_y"])))
        except (KeyError, TypeError, ValueError):
            continue
    return points


def crop_face_bytes(image_bytes: bytes, face: dict[str, Any]) -> bytes:
    """Crop the detected face with landmark-centered box, EXIF fix, and padding."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("Pillow not installed — using full uploaded image for comparison album")
        return image_bytes

    img = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes)))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    img_w, img_h = img.size
    landmarks = _face_landmarks(face)

    if len(landmarks) >= 3:
        xs = [p[0] for p in landmarks]
        ys = [p[1] for p in landmarks]
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        span = max(span * 2.4, float(face.get("width", span)), float(face.get("height", span)))
    else:
        cx = float(face.get("x", 0)) + float(face.get("width", 1)) / 2
        cy = float(face.get("y", 0)) + float(face.get("height", 1)) / 2
        span = max(float(face.get("width", 1)), float(face.get("height", 1))) * 1.35

    half = span / 2
    left = int(round(cx - half))
    top = int(round(cy - half))
    right = int(round(cx + half))
    bottom = int(round(cy + half))

    # Clamp while keeping square-ish size when possible
    box_w = right - left
    box_h = bottom - top
    if box_w > img_w:
        left, right = 0, img_w
    if box_h > img_h:
        top, bottom = 0, img_h
    left = max(0, left)
    top = max(0, top)
    right = min(img_w, right)
    bottom = min(img_h, bottom)
    if right <= left + 1 or bottom <= top + 1:
        # Fallback to padded bounding box
        pad = 0.25
        x = int(face.get("x", 0))
        y = int(face.get("y", 0))
        w = int(face.get("width", img_w // 4))
        h = int(face.get("height", img_h // 4))
        px = int(w * pad)
        py = int(h * pad)
        left = max(0, x - px)
        top = max(0, y - py)
        right = min(img_w, x + w + px)
        bottom = min(img_h, y + h + py)

    cropped = img.crop((left, top, right, bottom))
    out = BytesIO()
    cropped.save(out, format="JPEG", quality=92)
    return out.getvalue()


async def download_telegram_file(bot, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    try:
        data = await tg_file.download_as_bytearray()
        if data:
            return bytes(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("download_as_bytearray failed: %s", exc)

    buffer = BytesIO()
    await tg_file.download_to_memory(out=buffer)
    payload = buffer.getvalue()
    if not payload:
        raise ValueError("Downloaded image is empty")
    return payload


async def download_url_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


def _quota_status_line(info: dict[str, Any]) -> str:
    disabled = info.get("disabled")
    if str(disabled).lower() in ("no", "false", "0"):
        return "✅ Active"
    if str(disabled).lower() in ("yes", "true", "1"):
        return "🚫 Disabled"
    return f"ℹ️ {html.escape(str(disabled))}"


def format_quota_slot(slot: int, total: int, masked_key: str, info: dict[str, Any]) -> str:
    allowed = info.get("allowed") or []
    if isinstance(allowed, list):
        allowed_text = ", ".join(html.escape(str(x)) for x in allowed)
    else:
        allowed_text = html.escape(str(allowed))

    return (
        f"<b>Key {slot}/{total}</b> — <code>{html.escape(masked_key)}</code>\n"
        f"📈 <b>Limit</b>: <code>{html.escape(str(info.get('limit', '—')))}</code> requests\n"
        f"⏳ <b>Remaining</b>: <code>{html.escape(str(info.get('remaining', '—')))}</code>\n"
        f"📅 <b>Valid until</b>: <code>{html.escape(str(info.get('enddate', '—')))}</code>\n"
        f"⚡ <b>Speed</b>: <code>{html.escape(str(info.get('speed', '—')))}</code> req/min\n"
        f"🧩 <b>Allowed methods</b>: {allowed_text}\n"
        f"🚦 <b>Status</b>: {_quota_status_line(info)}"
    )


def reload_api_pool(context: ContextTypes.DEFAULT_TYPE) -> ApiKeyPool:
    pool = build_api_pool(API_URL, force_mock=MOCK_ENV)
    context.application.bot_data["api_pool"] = pool
    return pool


def get_api_pool(context: ContextTypes.DEFAULT_TYPE) -> ApiKeyPool:
    pool = context.application.bot_data.get("api_pool")
    if pool is None:
        return reload_api_pool(context)
    return pool


async def reply_access_denied(update: Update, *, owner_only: bool = False) -> None:
    text = OWNER_ONLY_HTML if owner_only else DENIED_HTML
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
        return
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def ensure_access(update: Update, *, owner_only: bool = False) -> bool:
    user = update.effective_user
    if user is None:
        return False
    if owner_only:
        if is_owner(user.id):
            return True
        await reply_access_denied(update, owner_only=True)
        return False
    if is_authorized(user.id):
        return True
    await reply_access_denied(update)
    return False


async def resolve_target_user_id(bot, target: str) -> tuple[int | None, str]:
    if target.startswith("id:"):
        return int(target[3:]), ""
    if target.startswith("user:"):
        username = target[5:]
        try:
            chat = await bot.get_chat(username)
            return chat.id, username
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
    return None, "invalid target"


def api_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 List keys", callback_data="api:list")],
            [InlineKeyboardButton("➕ Add key", callback_data="api:add")],
            [InlineKeyboardButton("➖ Remove key", callback_data="api:remove_menu")],
            [InlineKeyboardButton("✖️ Close", callback_data="api:close")],
        ]
    )


def api_remove_keyboard(pool: ApiKeyPool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, masked in enumerate(pool.masked_keys()):
        rows.append([InlineKeyboardButton(f"🗑 {i + 1}: {masked}", callback_data=f"api:rm:{i}")])
    rows.append([InlineKeyboardButton("« Back", callback_data="api:menu")])
    return InlineKeyboardMarkup(rows)


async def show_api_menu(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool = get_api_pool(context)
    masked = pool.masked_keys()
    lines = [
        "<b>🔑 API key management</b>",
        f"Keys in pool: <b>{len(masked)}</b>",
    ]
    if masked:
        lines.append("")
        for i, m in enumerate(masked, start=1):
            lines.append(f"{i}. <code>{html.escape(m)}</code>")
    else:
        lines.append("\nNo keys loaded — add one or set <code>MOCK_API=true</code> for testing.")
    if len(masked) > 1:
        lines.append("\n<i>Rotation: each API call uses the next key in order.</i>")
    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=api_menu_keyboard(),
    )


def format_match_caption(profile: dict[str, Any], source_key: str) -> str:
    score = profile.get("score", "—")
    fn = (profile.get("first_name") or "").strip()
    mn = (profile.get("maiden_name") or "").strip()
    ln = (profile.get("last_name") or "").strip()
    name_parts = [p for p in (fn, mn, ln) if p]
    name_line = " ".join(name_parts) if name_parts else "—"

    profile_url = (profile.get("profile") or "").strip()
    photo_page_url = (profile.get("photo") or "").strip()
    source_url = (profile.get("source") or "").strip()
    country = (profile.get("country") or "").strip() or "—"
    city = (profile.get("city") or "").strip() or "—"
    source_label = SOURCE_LABELS.get(source_key, source_key)

    lines = [
        f"🎯 <b>Score</b>: <code>{html.escape(str(score))}</code>%",
        f"👤 <b>Name</b>: {html.escape(name_line)}",
        f"🌍 <b>Country</b>: {html.escape(country)} · 🏙 <b>City</b>: {html.escape(city)}",
        f"📚 <b>Index</b>: {html.escape(source_label)}",
    ]
    if profile_url:
        lines.append(f'🔗 <b>Profile</b>: <a href="{html.escape(profile_url, quote=True)}">open</a>')
    if photo_page_url and photo_page_url != profile_url:
        lines.append(
            f'📷 <b>Source Photo</b>: '
            f'<a href="{html.escape(photo_page_url, quote=True)}">open</a>'
        )
    if source_url:
        lines.append(f'🖼 <b>Source image</b>: <a href="{html.escape(source_url, quote=True)}">open</a>')
    return "\n".join(lines)


def face_selection_keyboard(face_count: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"👤 Face {i + 1}", callback_data=f"face:{i}")]
        for i in range(face_count)
    ]
    return InlineKeyboardMarkup(rows)


def _selected_sources(pending: dict[str, Any]) -> set[str]:
    raw = pending.get("selected_sources")
    if isinstance(raw, set):
        return raw
    if isinstance(raw, list):
        return set(raw)
    return set()


def source_multiselect_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key in SEARCH_SOURCE_KEYS:
        mark = "✅" if key in selected else "☐"
        rows.append(
            [InlineKeyboardButton(f"{mark} {SOURCE_LABELS[key]}", callback_data=f"toggle:{key}")]
        )
    rows.append([InlineKeyboardButton("✔️ Finished", callback_data="src_done")])
    return InlineKeyboardMarkup(rows)


def confidence_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎯 80 – 100%", callback_data="conf:80")],
            [InlineKeyboardButton("📊 60 – 100%", callback_data="conf:60")],
            [InlineKeyboardButton("🔍 40 – 100%", callback_data="conf:40")],
        ]
    )


def source_picker_prompt(selected: set[str]) -> str:
    if not selected:
        return (
            "<b>Select the sources where to search</b>\n\n"
            "Tap each database to add a ✅ checkmark. Press <b>Finished</b> when ready."
        )
    names = "\n".join(f"• {SOURCE_LABELS[k]}" for k in SEARCH_SOURCE_KEYS if k in selected)
    return (
        "<b>Select the sources where to search</b>\n\n"
        f"<b>Selected ({len(selected)}):</b>\n{names}\n\n"
        "Tap to toggle. Press <b>Finished</b> when ready."
    )


async def show_source_picker(message, pending: dict[str, Any]) -> None:
    pending.setdefault("selected_sources", set())
    await message.reply_text(
        source_picker_prompt(_selected_sources(pending)),
        parse_mode=ParseMode.HTML,
        reply_markup=source_multiselect_keyboard(_selected_sources(pending)),
    )


def clear_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[EXPECT_KEY] = False
    context.user_data.pop(PENDING_KEY, None)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update):
        return
    clear_session(context)
    await update.effective_message.reply_text(
        WELCOME_HTML,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update):
        return
    text = HELP_USER_HTML
    if is_owner(update.effective_user.id if update.effective_user else None):
        text += HELP_OWNER_HTML
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update):
        return
    clear_session(context)
    await update.effective_message.reply_text("Cancelled. Send /face when you want to search again.")


async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, owner_only=True):
        return
    msg = update.effective_message
    if not context.args:
        await msg.reply_text(
            "Usage: <code>/auth &lt;user_id&gt;</code> or <code>/auth @username</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    target = parse_user_target(context.args[0])
    if not target:
        await msg.reply_text("Invalid user. Use a numeric ID or @username.")
        return

    label = context.args[0].strip()
    try:
        user_id, err = await resolve_target_user_id(context.bot, target)
    except ValueError:
        await msg.reply_text("Invalid user ID.")
        return

    if user_id is None:
        await msg.reply_text(f"Could not resolve user: {html.escape(err)}", parse_mode=ParseMode.HTML)
        return

    ok, reply = authorize_user(user_id, label if target.startswith("user:") else "")
    await msg.reply_text(reply, parse_mode=ParseMode.HTML)


async def cmd_unauth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, owner_only=True):
        return
    msg = update.effective_message
    if not context.args:
        await msg.reply_text(
            "Usage: <code>/unauth &lt;user_id&gt;</code> or <code>/unauth @username</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    target = parse_user_target(context.args[0])
    if not target:
        await msg.reply_text("Invalid user. Use a numeric ID or @username.")
        return

    try:
        user_id, err = await resolve_target_user_id(context.bot, target)
    except ValueError:
        await msg.reply_text("Invalid user ID.")
        return

    if user_id is None:
        await msg.reply_text(f"Could not resolve user: {html.escape(err)}", parse_mode=ParseMode.HTML)
        return

    ok, reply = unauthorize_user(user_id)
    await msg.reply_text(reply, parse_mode=ParseMode.HTML)


async def cmd_api(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, owner_only=True):
        return
    context.user_data.pop(AWAITING_API_KEY, None)
    await show_api_menu(update.effective_message, context)


async def cmd_face(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update):
        return
    msg = update.effective_message
    if msg is None:
        return

    replied = msg.reply_to_message
    if replied and message_has_image(replied):
        await process_face_photo(update, context, replied)
        return

    if message_has_image(msg):
        await process_face_photo(update, context, msg)
        return

    await msg.reply_text(
        "Send a *photo*, or *reply* <code>/face</code> to a photo already in the chat.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_quota(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update):
        return

    pool = get_api_pool(context)
    if pool.mock:
        await update.effective_message.reply_text(
            "<b>📊 API quota</b>\n\nMock mode — no live API keys in the pool. "
            "Owner: use /api to add keys.",
            parse_mode=ParseMode.HTML,
        )
        return

    masked = pool.masked_keys()
    blocks: list[str] = ["<b>📊 API quota</b>"]
    for i in range(pool.key_count):
        try:
            info = await pool.client_for_index(i).rate_limit()
        except Search4FacesError as exc:
            blocks.append(f"\n<b>Key {i + 1}/{len(masked)}</b>: ❌ {html.escape(str(exc))}")
            continue
        except Exception as exc:  # noqa: BLE001
            blocks.append(f"\n<b>Key {i + 1}/{len(masked)}</b>: ❌ {html.escape(str(exc))}")
            continue
        blocks.append(
            "\n" + format_quota_slot(i + 1, len(masked), masked[i], info)
        )

    await update.effective_message.reply_text(
        "\n".join(blocks),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def process_face_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, image_msg) -> None:
    """Run detectFaces and show face/source pickers for any image message."""
    if not await ensure_access(update):
        return

    reply_msg = update.effective_message
    if reply_msg is None or image_msg is None:
        return

    file_id = resolve_image_file_id(image_msg)
    if not file_id:
        await reply_msg.reply_text("Please send a photo (JPEG/PNG) or an image file.")
        return

    context.user_data[EXPECT_KEY] = False
    pool = get_api_pool(context)
    client = pool.next_client()

    try:
        image_bytes = await download_telegram_file(context.bot, file_id)
        logger.info("Downloaded %s bytes for user %s", len(image_bytes), update.effective_user.id)

        status = await reply_msg.reply_text("🔍 Scanning for faces…")
        try:
            detected = await client.detect_faces(image_bytes)
        except Search4FacesError as exc:
            await status.edit_text(f"❌ Face detection failed: {exc}")
            return
        except httpx.HTTPStatusError as exc:
            await status.edit_text(f"❌ Face detection failed (HTTP {exc.response.status_code}).")
            return

        image_id = detected.get("image")
        faces = detected.get("faces") or []
        if not image_id or not faces:
            await status.edit_text("❌ No faces detected. Try a clearer, front-facing photo.")
            return

        detect_scale = _parse_detect_scale(detected)
        context.user_data[PENDING_KEY] = {
            "image_id": image_id,
            "faces": faces,
            "image_bytes": image_bytes,
            "detect_scale": detect_scale,
            "selected_sources": set(),
        }

        await status.delete()
        if len(faces) > 1:
            await reply_msg.reply_text(
                f"👥 Found <b>{len(faces)}</b> faces. Tap which face to search:",
                parse_mode=ParseMode.HTML,
                reply_markup=face_selection_keyboard(len(faces)),
            )
        else:
            context.user_data[PENDING_KEY]["face_idx"] = 0
            await show_source_picker(reply_msg, context.user_data[PENDING_KEY])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Image handling failed for user %s", update.effective_user.id)
        await reply_msg.reply_text(f"❌ Could not process image: {exc}")


async def on_image_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or msg.from_user is None or msg.from_user.is_bot:
        return
    if not message_has_image(msg):
        return
    await process_face_photo(update, context, msg)


async def on_owner_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_owner(update.effective_user.id):
        return
    if not context.user_data.get(AWAITING_API_KEY):
        return
    msg = update.effective_message
    if msg is None or not msg.text:
        return

    key = msg.text.strip()
    context.user_data.pop(AWAITING_API_KEY, None)
    ok, reply = add_api_key(key)
    reload_api_pool(context)
    await msg.reply_text(html.escape(reply), parse_mode=ParseMode.HTML)
    await show_api_menu(msg, context)


async def on_api_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    if not await ensure_access(update, owner_only=True):
        return

    data = query.data
    pool = get_api_pool(context)

    if data == "api:close":
        await query.answer("Closed.")
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            await query.edit_message_reply_markup(reply_markup=None)
        return

    if data == "api:menu":
        await query.answer()
        masked = pool.masked_keys()
        text = f"<b>🔑 API keys</b>: {len(masked)} in pool"
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=api_menu_keyboard(),
        )
        return

    if data == "api:list":
        await query.answer()
        masked = pool.masked_keys()
        if not masked:
            body = "No API keys in the pool."
        else:
            body = "\n".join(f"{i}. <code>{html.escape(m)}</code>" for i, m in enumerate(masked, start=1))
        await query.message.reply_text(body, parse_mode=ParseMode.HTML)
        return

    if data == "api:add":
        context.user_data[AWAITING_API_KEY] = True
        await query.answer()
        await query.message.reply_text(
            "Send the new Search4Faces API key in this chat (one message). "
            "It will be stored locally and never shown in full again.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "api:remove_menu":
        await query.answer()
        if not pool.masked_keys():
            await query.message.reply_text("No keys to remove.")
            return
        await query.message.reply_text(
            "Tap a key to remove:",
            reply_markup=api_remove_keyboard(pool),
        )
        return

    if data.startswith("api:rm:"):
        try:
            idx = int(data.split(":", 2)[2])
        except ValueError:
            await query.answer("Invalid slot.", show_alert=True)
            return
        ok, reply = remove_api_key(idx)
        reload_api_pool(context)
        await query.answer("Removed." if ok else "Failed.", show_alert=True)
        await query.message.reply_text(reply)
        await show_api_menu(query.message, context)
        return

    await query.answer()


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    if not await ensure_access(update):
        return

    pending = context.user_data.get(PENDING_KEY)
    if not pending:
        await query.answer("Session expired. Send /face again.", show_alert=True)
        return

    data = query.data

    if data.startswith("face:"):
        await query.answer()
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            await query.message.reply_text("Invalid face selection.")
            return
        faces = pending.get("faces") or []
        if idx < 0 or idx >= len(faces):
            await query.message.reply_text("That face is no longer available. Send a new photo.")
            return
        pending["face_idx"] = idx
        pending["selected_sources"] = set()
        await query.message.reply_text(
            f"✅ Using <b>face {idx + 1}</b>.",
            parse_mode=ParseMode.HTML,
        )
        await show_source_picker(query.message, pending)
        return

    if data.startswith("toggle:"):
        if "face_idx" not in pending:
            if len(pending.get("faces") or []) == 1:
                pending["face_idx"] = 0
            else:
                await query.answer("Pick a face first.", show_alert=True)
                return

        source_key = data.split(":", 1)[1]
        if source_key not in SEARCH_SOURCE_KEYS:
            await query.answer("Unknown source.", show_alert=True)
            return

        selected = _selected_sources(pending)
        if source_key in selected:
            selected.discard(source_key)
            await query.answer(f"Removed {SOURCE_LABELS[source_key]}")
        else:
            selected.add(source_key)
            await query.answer(f"Added {SOURCE_LABELS[source_key]}")
        pending["selected_sources"] = selected

        await query.edit_message_text(
            source_picker_prompt(selected),
            parse_mode=ParseMode.HTML,
            reply_markup=source_multiselect_keyboard(selected),
        )
        return

    if data == "src_done":
        if "face_idx" not in pending:
            if len(pending.get("faces") or []) == 1:
                pending["face_idx"] = 0
            else:
                await query.answer("Pick a face first.", show_alert=True)
                return

        selected = _selected_sources(pending)
        if not selected:
            await query.answer("Select at least one source.", show_alert=True)
            return

        await query.answer()
        await query.edit_message_text(
            f"<b>Sources selected ({len(selected)})</b>\n\n"
            "Choose the <b>confidence level</b> for matches:",
            parse_mode=ParseMode.HTML,
        )
        await query.message.reply_text(
            "🎚 <b>Minimum match score</b> (results up to 100%):",
            parse_mode=ParseMode.HTML,
            reply_markup=confidence_keyboard(),
        )
        return

    if data.startswith("conf:"):
        if "face_idx" not in pending:
            await query.answer("Session incomplete. Send a new photo.", show_alert=True)
            return

        try:
            min_score = float(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Invalid confidence.", show_alert=True)
            return

        if int(min_score) not in CONFIDENCE_OPTIONS:
            await query.answer("Invalid confidence option.", show_alert=True)
            return

        selected = list(_selected_sources(pending))
        if not selected:
            await query.answer("No sources selected.", show_alert=True)
            return

        await query.answer()
        await query.message.reply_text(
            f"🔎 Searching <b>{len(selected)}</b> source(s) at "
            f"<b>{min_score:g}–{MAX_SCORE:g}%</b>…",
            parse_mode=ParseMode.HTML,
        )
        total = await run_search(query.message, context, selected, min_score=min_score)
        if total > 0:
            clear_session(context)
        else:
            await query.message.reply_text(
                "🎚 <b>Try another minimum match score</b> for the same photo:",
                parse_mode=ParseMode.HTML,
                reply_markup=confidence_keyboard(),
            )
        return

    await query.answer()


async def run_search(
    anchor_message,
    context: ContextTypes.DEFAULT_TYPE,
    sources: list[str],
    *,
    min_score: float,
    max_score: float = MAX_SCORE,
) -> int:
    pending = context.user_data.get(PENDING_KEY)
    if not pending:
        await anchor_message.reply_text("Session expired. Send a new photo.")
        return 0

    pool = get_api_pool(context)
    api_face_box = pending["faces"][pending["face_idx"]]
    image_id = pending["image_id"]
    submitted_image = pending["image_bytes"]
    chat_id = anchor_message.chat_id

    seen_keys: set[str] = set()
    total_albums = 0

    for source in sources:
        client = pool.next_client()
        try:
            profiles = await client.search_face(
                image_id,
                api_face_box,
                source=source,
                results=API_RESULTS_FETCH,
                hidden=INCLUDE_HIDDEN,
                lang=SEARCH_LANG,
            )
        except Search4FacesError as exc:
            await anchor_message.reply_text(f"❌ {SOURCE_LABELS.get(source, source)}: {exc}")
            await asyncio.sleep(SOURCE_DELAY_SEC)
            continue
        except httpx.HTTPStatusError as exc:
            await anchor_message.reply_text(
                f"❌ {SOURCE_LABELS.get(source, source)}: HTTP {exc.response.status_code}"
            )
            await asyncio.sleep(SOURCE_DELAY_SEC)
            continue
        except Exception as exc:  # noqa: BLE001
            await anchor_message.reply_text(f"❌ {SOURCE_LABELS.get(source, source)}: {exc}")
            await asyncio.sleep(SOURCE_DELAY_SEC)
            continue

        filtered = [p for p in profiles if score_in_range(p, min_score, max_score)]
        if not filtered and profiles:
            await anchor_message.reply_text(
                f"ℹ️ {SOURCE_LABELS.get(source, source)}: "
                f"no matches in {min_score:g}–{max_score:g}% range."
            )
            await asyncio.sleep(SOURCE_DELAY_SEC)
            continue

        for profile in filtered:
            dedupe = profile.get("profile") or profile.get("face") or str(profile)
            if dedupe in seen_keys:
                continue
            seen_keys.add(dedupe)

            caption = format_match_caption(profile, source)
            face_url = profile.get("face") or ""
            try:
                if face_url:
                    match_bytes = await download_url_bytes(face_url)
                    media = [
                        InputMediaPhoto(
                            media=match_bytes,
                            caption=caption[:1024],
                            parse_mode=ParseMode.HTML,
                        ),
                        InputMediaPhoto(media=submitted_image),
                    ]
                    await context.bot.send_media_group(chat_id=chat_id, media=media)
                else:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=submitted_image,
                        caption=caption[:1024],
                        parse_mode=ParseMode.HTML,
                    )
                total_albums += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Album send failed: %s", exc)
                await anchor_message.reply_text(
                    caption + f"\n\n⚠️ Could not build album: {html.escape(str(exc))}",
                    parse_mode=ParseMode.HTML,
                )

        await asyncio.sleep(SOURCE_DELAY_SEC)

    if total_albums == 0:
        await anchor_message.reply_text(
            f"😶 No matches between <b>{min_score:g}%</b> and <b>{max_score:g}%</b>. "
            "Try another photo, more sources, or a lower confidence level.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await anchor_message.reply_text(
            f"✅ Done — sent <b>{total_albums}</b> comparison album(s) "
            f"({min_score:g}–{max_score:g}% only).",
            parse_mode=ParseMode.HTML,
        )

    return total_albums


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception: %s", context.error)
    logger.error("".join(traceback.format_exception(None, context.error, context.error.__traceback__)))
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Error — try /face again.")
        except Exception:  # noqa: BLE001
            pass


def _token_ok(token: str) -> bool:
    t = token.strip()
    if not t or t.lower() in {x.lower() for x in PLACEHOLDER_BOT_TOKENS}:
        return False
    return bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]+", t))


def main() -> None:
    if not _token_ok(TELEGRAM_TOKEN):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env")

    owner = get_owner_id()
    if owner is None:
        raise SystemExit("Set OWNER_USER_ID in .env (your Telegram numeric user ID).")

    migrate_env_api_key_if_needed()
    pool = build_api_pool(API_URL, force_mock=MOCK_ENV)
    if pool.mock:
        logger.warning("Search4Faces mock mode (no keys or MOCK_API=true).")
    else:
        logger.info("Search4Faces live mode — %s API key(s), rotation enabled.", pool.key_count)

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )
    application.bot_data["api_pool"] = pool
    application.add_error_handler(on_error)

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("face", cmd_face))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler(["quota", "ratelimit"], cmd_quota))
    application.add_handler(CommandHandler("auth", cmd_auth))
    application.add_handler(CommandHandler("unauth", cmd_unauth))
    application.add_handler(CommandHandler("api", cmd_api))
    application.add_handler(CallbackQueryHandler(on_api_callback, pattern=r"^api:"))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_owner_text),
    )
    application.add_handler(MessageHandler(filters.PHOTO, on_image_message))
    application.add_handler(MessageHandler(filters.Document.ALL, on_image_message))

    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    main()
