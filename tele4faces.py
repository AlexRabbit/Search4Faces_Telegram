"""
Tele4Faces — Telegram bot that routes a selfie through search4faces.com
(JSON-RPC: detectFaces → searchFace) and returns match thumbnails with profile links.

On a fresh machine, running this file will install packages from requirements.txt
(upgrade pip, then pip install -r) when imports are missing. Set TELE4FACES_NO_AUTO_PIP=1
to skip that step.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_REQUIREMENTS = _ROOT / "requirements.txt"


def _ensure_runtime_dependencies() -> None:
    """Install requirements.txt into the current interpreter if deps are missing."""
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
        print(
            "Missing Python packages and requirements.txt was not found next to tele4faces.py.\n"
            "Install manually: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Tele4Faces: installing dependencies (first run / new Python environment)…", flush=True)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
            cwd=str(_ROOT),
        )
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(_REQUIREMENTS)],
            cwd=str(_ROOT),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            "Automatic pip install failed. Install build tools / Python pip, then run:\n"
            f"  {sys.executable} -m pip install -r {_REQUIREMENTS}\n"
            f"Error: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


_ensure_runtime_dependencies()

import asyncio
import html
import logging
import os
import re
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from search4faces_client import (
    DEFAULT_SOURCES,
    SEARCH4FACES_DEFAULT_URL,
    Search4FacesClient,
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
PLACEHOLDER_API_KEYS = frozenset(
    {
        "",
        "your_search4faces_api_key",
        "your_search4faces_api_key".upper(),
    }
)
PLACEHOLDER_BOT_TOKENS = frozenset({"your_telegram_bot_token", "your_telegram_bot_token".upper()})

API_KEY = (os.environ.get("SEARCH4FACES_API_KEY") or "").strip()
API_URL = (os.environ.get("SEARCH4FACES_API_URL") or "").strip() or None
MOCK_ENV = (os.environ.get("MOCK_API") or "").lower() in ("1", "true", "yes")


def _is_placeholder_api_key(key: str) -> bool:
    k = key.strip()
    if not k:
        return True
    lowered = {x.lower() for x in PLACEHOLDER_API_KEYS if x}
    return k.lower() in lowered or k in PLACEHOLDER_API_KEYS


MOCK = MOCK_ENV or _is_placeholder_api_key(API_KEY)

SOURCES = [
    s.strip()
    for s in (os.environ.get("SEARCH_SOURCES") or ",".join(DEFAULT_SOURCES)).split(",")
    if s.strip()
]
RESULTS_PER_SOURCE = max(1, min(50, int(os.environ.get("RESULTS_PER_SOURCE", "10"))))
SOURCE_DELAY_SEC = max(0.0, float(os.environ.get("SOURCE_DELAY_SEC", "0.69")))
SEARCH_LANG = (os.environ.get("SEARCH_LANG") or "en").strip() or "en"
INCLUDE_HIDDEN = (os.environ.get("INCLUDE_HIDDEN_PROFILES") or "true").lower() in (
    "1",
    "true",
    "yes",
)

EXPECT_KEY = "expecting_face_photo"

WELCOME_HTML = (
    "<b>Tele4Faces</b> finds <i>public</i> social profiles that resemble a face in your photo.\n\n"
    'It uses the <a href="https://search4faces.com/">search4faces</a> JSON-RPC API '
    "(VK / TikTok / Clubhouse / Snap-style indexes — see their docs for current "
    "<code>source</code> values).\n\n"
    "<b>Commands</b>\n"
    "/start — this intro\n"
    "/face — I ask for a photo; you send a clear face picture\n"
    "/cancel — stop waiting for a photo\n"
    "/quota — show API limits (mock mode shows demo numbers)\n\n"
    "<b>Privacy</b>: only send photos you are allowed to process. Matches are probabilistic "
    "and can be wrong."
)


def _largest_face(faces: list[dict[str, Any]]) -> dict[str, Any]:
    return max(faces, key=lambda f: int(f.get("width", 0)) * int(f.get("height", 0)))


def _caption_for_profile(profile: dict[str, Any], source: str) -> str:
    profile_url = profile.get("profile") or ""
    score = profile.get("score", "")
    fn = (profile.get("first_name") or "").strip()
    ln = (profile.get("last_name") or "").strip()
    name = " ".join(x for x in (fn, ln) if x).strip() or "Unknown name"
    city = (profile.get("city") or "").strip()
    country = (profile.get("country") or "").strip()
    loc = ", ".join(x for x in (city, country) if x)

    lines = [
        f"<b>{html.escape(name)}</b> · match <code>{html.escape(str(score))}</code>%",
        f"index <code>{html.escape(source)}</code>",
    ]
    if loc:
        lines.append(html.escape(loc))
    if profile_url:
        lines.append(f'<a href="{html.escape(profile_url, quote=True)}">Open profile</a>')
    photo_page = profile.get("photo")
    if photo_page and photo_page != profile_url:
        lines.append(f'<a href="{html.escape(photo_page, quote=True)}">Source photo page</a>')
    return "\n".join(lines)


async def _send_match_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    profile: dict[str, Any],
    source: str,
) -> None:
    face_url = profile.get("face") or ""
    caption = _caption_for_profile(profile, source)
    if not face_url:
        await update.effective_message.reply_text(caption, parse_mode=ParseMode.HTML)
        return
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            r = await client.get(face_url)
            r.raise_for_status()
            data = r.content
        await update.effective_message.reply_photo(
            photo=InputFile(data, filename="match.jpg"),
            caption=caption[:1024],
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:  # noqa: BLE001 — user-facing fallback
        logger.warning("Could not send face image: %s", exc)
        await update.effective_message.reply_text(
            caption + f"\n\n<i>Thumbnail URL:</i> <code>{html.escape(face_url)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[EXPECT_KEY] = False
    await update.effective_message.reply_text(
        WELCOME_HTML,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[EXPECT_KEY] = False
    await update.effective_message.reply_text("Okay — not waiting for a photo anymore.")


async def cmd_face(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[EXPECT_KEY] = True
    await update.effective_message.reply_text(
        "Send a *photo* with a visible face (JPEG/PNG). Document files work if they are images.\n"
        "Tip: good lighting and a frontal face improve matches.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_quota(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client: Search4FacesClient = context.bot_data["s4f"]
    try:
        info = await client.rate_limit()
    except Search4FacesError as exc:
        await update.effective_message.reply_text(f"API error: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"Request failed: {exc}")
        return

    lines = [
        f"apikey: `{info.get('apikey')}`",
        f"remaining: `{info.get('remaining')}` / `{info.get('limit')}`",
        f"enddate: `{info.get('enddate')}`",
        f"speed (req/min): `{info.get('speed')}`",
        f"disabled: `{info.get('disabled')}`",
        f"allowed: `{info.get('allowed')}`",
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get(EXPECT_KEY):
        await update.effective_message.reply_text("Tap /face first, then send your picture.")
        return

    client: Search4FacesClient = context.bot_data["s4f"]
    msg = update.effective_message
    photo = msg.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    buf = bytearray()
    await tg_file.download_to_memory(buf)
    image_bytes = bytes(buf)
    await _run_pipeline(update, context, client, image_bytes)


async def on_document_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get(EXPECT_KEY):
        await update.effective_message.reply_text("Tap /face first, then send your picture.")
        return
    doc = update.effective_message.document
    mime = (doc.mime_type or "").lower()
    if mime not in ("image/jpeg", "image/jpg", "image/png", "image/webp"):
        await update.effective_message.reply_text("Please send a JPEG or PNG (or a normal photo, not this file type).")
        return
    client: Search4FacesClient = context.bot_data["s4f"]
    tg_file = await context.bot.get_file(doc.file_id)
    buf = bytearray()
    await tg_file.download_to_memory(buf)
    await _run_pipeline(update, context, client, bytes(buf))


async def _run_pipeline(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    client: Search4FacesClient,
    image_bytes: bytes,
) -> None:
    msg = update.effective_message
    context.user_data[EXPECT_KEY] = False

    status = await msg.reply_text("Scanning for faces…")
    try:
        detected = await client.detect_faces(image_bytes)
    except Search4FacesError as exc:
        await status.edit_text(f"Face detection failed: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await status.edit_text(f"Face detection failed: {exc}")
        return

    image_id = detected.get("image")
    faces = detected.get("faces") or []
    if not image_id or not faces:
        await status.edit_text("No faces detected. Try another angle or a sharper photo.")
        return

    face = _largest_face(faces)
    if len(faces) > 1:
        await msg.reply_text(
            f"Found {len(faces)} faces — using the largest bounding box. "
            "Crop to one face and resend if you need a different person."
        )

    await status.edit_text(
        f"Searching {len(SOURCES)} source(s) for up to {RESULTS_PER_SOURCE} hits each…"
        + (" _(mock mode — no real API key)_" if client.mock else "")
    )

    seen_profiles: set[str] = set()
    total_sent = 0

    for source in SOURCES:
        try:
            profiles = await client.search_face(
                image_id,
                face,
                source=source,
                results=RESULTS_PER_SOURCE,
                hidden=INCLUDE_HIDDEN,
                lang=SEARCH_LANG,
            )
        except Search4FacesError as exc:
            await msg.reply_text(f"{source}: search failed ({exc})")
            await asyncio.sleep(SOURCE_DELAY_SEC)
            continue
        except Exception as exc:  # noqa: BLE001
            await msg.reply_text(f"{source}: search failed ({exc})")
            await asyncio.sleep(SOURCE_DELAY_SEC)
            continue

        for profile in profiles:
            key = profile.get("profile") or profile.get("face") or str(profile)
            if key in seen_profiles:
                continue
            seen_profiles.add(key)
            await _send_match_photo(update, context, profile, source)
            total_sent += 1

        await asyncio.sleep(SOURCE_DELAY_SEC)

    if total_sent == 0:
        await msg.reply_text("No matches returned (or every hit was a duplicate). Try another photo.")
    else:
        await msg.reply_text(f"Done — sent {total_sent} unique match(es).")

    try:
        await status.delete()
    except Exception:  # noqa: BLE001
        pass


def _token_ok(token: str) -> bool:
    t = token.strip()
    if not t or t in PLACEHOLDER_BOT_TOKENS or t.lower() in {x.lower() for x in PLACEHOLDER_BOT_TOKENS if x}:
        return False
    return bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]+", t))


def main() -> None:
    if not _token_ok(TELEGRAM_TOKEN):
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN in your environment or .env to a real BotFather token."
        )

    api_url = API_URL or SEARCH4FACES_DEFAULT_URL

    client = Search4FacesClient(
        api_key=API_KEY or "mock",
        api_url=api_url,
        mock=MOCK,
    )
    if MOCK:
        if MOCK_ENV:
            logger.warning("MOCK_API=true: Search4Faces responses are simulated.")
        else:
            logger.warning(
                "No valid SEARCH4FACES_API_KEY in .env — using simulated Search4Faces responses. "
                "Add your API key from search4faces.com to search live, or set MOCK_API=true if mock-only is intentional."
            )
    else:
        logger.info("Search4Faces live mode enabled.")

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.bot_data["s4f"] = client

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("face", cmd_face))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("quota", cmd_quota))
    application.add_handler(MessageHandler(filters.PHOTO, on_photo))
    application.add_handler(
        MessageHandler(filters.Document.IMAGE, on_document_image),
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
