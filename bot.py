#!/usr/bin/env python3
"""
xRichOtc Auto-Pin Bot
=====================

Features:
- Locks automated group actions to one configured Telegram chat ID.
- Pins the first eligible non-admin member message for a configurable duration.
- Ignores further member messages while an event is active.
- Automatically unpins at the end and posts the next-event announcement.
- Admin panel, admin ID verification, pause/resume, manual unpin, status.
- Smart filtering for text, photos, and videos.
- Countdown reminders at 10 and 5 minutes before event end.
- SQLite persistence, event recovery, pin history, daily statistics and audit logs.
- Paid-message Stars tracking using Telegram's Message.paid_star_count field.
- /mstats, /topstars and /stats commands.
- Maintenance mode and admin error alerts.
- Admin-panel timezone selector for India, USA (New York), and Russia (Moscow).
- /ping command measures Telegram Bot API response time.
- Semantic Telegram custom emoji IDs for admin buttons and default welcome text.

Requirements:
    Python 3.10+
    python-telegram-bot >= 22.1, < 23
    python-dotenv >= 1.0

Install:
    pip install "python-telegram-bot>=22.1,<23" python-dotenv httpx tzdata

Environment variables:
    TELEGRAM_BOT_TOKEN       Required. Bot token from @BotFather.
    XRICHOtc_CHAT_ID or TARGET_CHAT_ID_RAW  Numeric chat ID of @xRichOtc.
                             Add the bot to the group first, then use /whereami
                             in the group to discover the numeric ID.
    ADMIN_USER_IDS           Required. Comma-separated Telegram user IDs.
    DATABASE_PATH            Optional. Defaults to xrichotc_bot.sqlite3.
    DEFAULT_PIN_MINUTES      Optional. Defaults to 120.
    ERROR_ALERT_USER_IDS     Optional. Comma-separated user IDs for private alerts.
    COUNTDOWN_ENABLED        Optional. "true" or "false", defaults to true.

Stars tracking notes:
- Bot API / python-telegram-bot exposes Message.paid_star_count in supported versions.
- The bot records the field when Telegram includes it in an update.
- It does not guess Stars amounts when the field is absent.
- Historical spending from before the bot was running cannot be reconstructed by this bot.
- The group must be configured for Telegram Paid Messages and the bot must receive
  the relevant message updates. Test this in the actual group before relying on reports.
"""

from __future__ import annotations

import logging
import re
import os
import sqlite3
from html import escape
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from time import perf_counter
from typing import Optional

from dotenv import load_dotenv
import httpx

# Load variables from a .env file in the current working directory.
load_dotenv()

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import Defaults, ExtBot
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------- Configuration -----------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("BOT_TOKEN", "")).strip()
TARGET_CHAT_ID_RAW = os.getenv("XRICHOtc_CHAT_ID", os.getenv("TARGET_CHAT_ID_RAW", "")).strip()
ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", os.getenv("ADMIN_USER_IDS_RAW", "")).strip()
ERROR_ALERT_USER_IDS_RAW = os.getenv("ERROR_ALERT_USER_IDS", os.getenv("ERROR_ALERT_USER_IDS_RAW", "")).strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "xrichotc_bot.sqlite3")
DEFAULT_PIN_MINUTES = max(1, int(os.getenv("DEFAULT_PIN_MINUTES", "120")))
COUNTDOWN_ENABLED = os.getenv("COUNTDOWN_ENABLED", "true").lower() in {
    "1", "true", "yes", "on"
}

if not BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN environment variable.")
if not TARGET_CHAT_ID_RAW:
    raise SystemExit("Missing XRICHOtc_CHAT_ID numeric chat ID environment variable.")
if not ADMIN_USER_IDS_RAW:
    raise SystemExit("Missing ADMIN_USER_IDS environment variable.")

try:
    TARGET_CHAT_ID = int(TARGET_CHAT_ID_RAW)
except ValueError as exc:
    raise SystemExit("XRICHOtc_CHAT_ID must be the numeric Telegram chat ID.") from exc


def parse_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                result.add(int(part))
            except ValueError:
                raise SystemExit(f"Invalid numeric Telegram user ID: {part}")
    return result


ADMIN_USER_IDS = parse_ids(ADMIN_USER_IDS_RAW)
ERROR_ALERT_USER_IDS = parse_ids(ERROR_ALERT_USER_IDS_RAW) or set(ADMIN_USER_IDS)

if not ADMIN_USER_IDS:
    raise SystemExit("ADMIN_USER_IDS must contain at least one numeric user ID.")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("xrichotc_autopin")


# ----------------------------- Database layer -----------------------------

def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> None:
    with db_connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                display_name TEXT NOT NULL,
                start_ts REAL NOT NULL,
                end_ts REAL NOT NULL,
                duration_minutes INTEGER NOT NULL,
                paid_star_count INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                end_reason TEXT,
                UNIQUE(chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS paid_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                display_name TEXT NOT NULL,
                paid_star_count INTEGER NOT NULL,
                created_ts REAL NOT NULL,
                PRIMARY KEY(chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER,
                actor_name TEXT,
                action TEXT NOT NULL,
                details TEXT,
                created_ts REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pin_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                display_name TEXT NOT NULL,
                start_ts REAL NOT NULL,
                end_ts REAL,
                duration_minutes INTEGER NOT NULL,
                paid_star_count INTEGER,
                result TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES events(id)
            );

            CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
            CREATE INDEX IF NOT EXISTS idx_paid_user_time
                ON paid_messages(user_id, created_ts);
            CREATE INDEX IF NOT EXISTS idx_history_start
                ON pin_history(start_ts);
            """
        )
        defaults = {
            "pin_minutes": str(DEFAULT_PIN_MINUTES),
            "paused": "0",
            "maintenance": "0",
            "allowed_types": "text,photo,video",
            "announcement_template": "📌 <b>Next message will pin for {duration}!</b>\n✨ Send your message now to claim the next pin.",
            "confirmation_template": "✅ <b>Your message is now pinned!</b>\n⏳ Pin duration: <b>{duration}</b>.\n💫 Thanks for using @xRichOtc.",
            "countdown_enabled": "1" if COUNTDOWN_ENABLED else "0",
            "announcement_enabled": "1",
        }
        for key, value in defaults.items():
            db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with db_connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db_connect() as db:
        db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def audit(actor_id: Optional[int], actor_name: str, action: str, details: str = "") -> None:
    with db_connect() as db:
        db.execute(
            "INSERT INTO audit_logs(actor_id, actor_name, action, details, created_ts) "
            "VALUES(?, ?, ?, ?, ?)",
            (actor_id, actor_name, action, details, now_ts()),
        )


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def format_duration(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minutes"


def get_configured_timezone():
    name = get_setting("timezone", "UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def format_time(ts: Optional[float]) -> str:
    if ts is None:
        return "N/A"
    tz = get_configured_timezone()
    label = get_setting("timezone", "UTC")
    return datetime.fromtimestamp(ts, tz).strftime(f"%Y-%m-%d %H:%M {label}")


def user_display(user) -> str:
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip()
    return full_name or user.username or str(user.id)


def is_admin(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in ADMIN_USER_IDS


def is_target_chat(chat_id: Optional[int]) -> bool:
    return chat_id == TARGET_CHAT_ID


async def send_error_alert(context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    logger.error(message)
    for user_id in ERROR_ALERT_USER_IDS:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🚨 <b>xRichOtc Bot Alert</b>\n<code>{escape(message[:3000])}</code>",
            )
        except TelegramError:
            logger.exception("Could not send error alert to user ID %s", user_id)


async def check_target_identity(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Resolve @xRichOtc and verify that the configured numeric ID matches."""
    try:
        chat = await context.bot.get_chat("@xRichOtc")
        if chat.id != TARGET_CHAT_ID:
            logger.critical(
                "Configured chat ID %s does not match @xRichOtc ID %s",
                TARGET_CHAT_ID,
                chat.id,
            )
            return False
        return True
    except TelegramError:
        logger.exception("Could not resolve @xRichOtc. Check bot access and username.")
        return False


# ----------------------------- Event handling -----------------------------

def active_event():
    with db_connect() as db:
        return db.execute(
            "SELECT * FROM events WHERE chat_id=? AND status IN ('active','pinning') "
            "ORDER BY id DESC LIMIT 1",
            (TARGET_CHAT_ID,),
        ).fetchone()


def allowed_message_type(message) -> Optional[str]:
    if message.text and not message.text.startswith("/"):
        return "text"
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    return None


def allowed_types() -> set[str]:
    return {
        item.strip().lower()
        for item in (get_setting("allowed_types", "text,photo,video") or "").split(",")
        if item.strip()
    }


def announcement_text() -> str:
    minutes = int(get_setting("pin_minutes", str(DEFAULT_PIN_MINUTES)) or DEFAULT_PIN_MINUTES)
    template = get_setting(
        "announcement_template",
        "📌 <b>Next message will pin for {duration}!</b>\n✨ Send your message now to claim the next pin.",
    ) or "📌 Next message will pin for {duration}."
    return template.format(duration=format_duration(minutes))


async def post_announcement(context: ContextTypes.DEFAULT_TYPE) -> None:
    if get_setting("announcement_enabled", "1") != "1":
        return
    if get_setting("paused", "0") == "1" or get_setting("maintenance", "0") == "1":
        return
    if active_event():
        return
    try:
        await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=announcement_text())
    except TelegramError as exc:
        await send_error_alert(context, f"Announcement failed: {exc}")


async def schedule_event_jobs(
    context: ContextTypes.DEFAULT_TYPE,
    event_id: int,
    end_ts: float,
    start_ts: float,
) -> None:
    job_queue = context.application.job_queue
    for name in ("end", "countdown10", "countdown5"):
        job_name = f"event_{event_id}_{name}"
        for job in job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()

    now = now_ts()
    end_delay = max(0.0, end_ts - now)
    job_queue.run_once(
        finish_event_job,
        when=end_delay,
        data={"event_id": event_id},
        name=f"event_{event_id}_end",
    )

    if get_setting("countdown_enabled", "1") != "1":
        return

    for minutes in (10, 5):
        reminder_ts = end_ts - minutes * 60
        if reminder_ts > now:
            job_queue.run_once(
                countdown_job,
                when=reminder_ts - now,
                data={"event_id": event_id, "minutes": minutes},
                name=f"event_{event_id}_countdown{minutes}",
            )


async def handle_group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user or not is_target_chat(chat.id):
        return
    if user.is_bot or is_admin(user.id):
        return

    # Record Stars independently of whether the message wins the pin event.
    paid_star_count = getattr(message, "paid_star_count", None)
    if isinstance(paid_star_count, int) and paid_star_count > 0:
        with db_connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO paid_messages
                (chat_id, message_id, user_id, username, display_name,
                 paid_star_count, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat.id,
                    message.message_id,
                    user.id,
                    user.username,
                    user_display(user),
                    paid_star_count,
                    message.date.timestamp() if message.date else now_ts(),
                ),
            )

    if get_setting("paused", "0") == "1" or get_setting("maintenance", "0") == "1":
        return

    if active_event():
        return

    kind = allowed_message_type(message)
    if kind is None or kind not in allowed_types():
        return

    duration = int(get_setting("pin_minutes", str(DEFAULT_PIN_MINUTES)) or DEFAULT_PIN_MINUTES)
    start_ts = now_ts()
    end_ts = start_ts + duration * 60
    star_count_for_event = paid_star_count if isinstance(paid_star_count, int) else None

    # Insert a provisional active event first to make simultaneous updates compete
    # for the unique event lock. A single process plus SQLite transaction keeps this
    # atomic for the normal deployment model.
    try:
        with db_connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM events WHERE chat_id=? AND status IN ('active','pinning')",
                (chat.id,),
            ).fetchone()
            if existing:
                db.rollback()
                return
            cursor = db.execute(
                """
                INSERT INTO events
                (chat_id, message_id, user_id, username, display_name, start_ts,
                 end_ts, duration_minutes, paid_star_count, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pinning')
                """,
                (
                    chat.id, message.message_id, user.id, user.username,
                    user_display(user), start_ts, end_ts, duration, star_count_for_event,
                ),
            )
            event_id = cursor.lastrowid
            db.commit()
    except sqlite3.IntegrityError:
        return

    try:
        await context.bot.pin_chat_message(
            chat_id=chat.id,
            message_id=message.message_id,
            disable_notification=True,
        )
        with db_connect() as db:
            db.execute("UPDATE events SET status='active' WHERE id=?", (event_id,))
            db.execute(
                """
                INSERT INTO pin_history
                (event_id, chat_id, message_id, user_id, username, display_name,
                 start_ts, duration_minutes, paid_star_count, result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pinned')
                """,
                (
                    event_id, chat.id, message.message_id, user.id, user.username,
                    user_display(user), start_ts, duration, star_count_for_event,
                ),
            )
        template = get_setting(
            "confirmation_template",
            "✅ <b>Your message is now pinned!</b>\n⏳ Pin duration: <b>{duration}</b>.\n💫 Thanks for using @xRichOtc.",
        ) or "✅ Your message has been pinned for the next {duration}!"
        try:
            await message.reply_text(template.format(duration=format_duration(duration)))
        except TelegramError:
            logger.warning("Could not send pin confirmation.")
        await schedule_event_jobs(context, event_id, end_ts, start_ts)
    except TelegramError as exc:
        with db_connect() as db:
            db.execute(
                "UPDATE events SET status='failed', end_reason='pin_failed' WHERE id=?",
                (event_id,),
            )
            db.execute(
                """
                INSERT INTO pin_history
                (event_id, chat_id, message_id, user_id, username, display_name,
                 start_ts, end_ts, duration_minutes, paid_star_count, result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pin_failed')
                """,
                (
                    event_id, chat.id, message.message_id, user.id, user.username,
                    user_display(user), start_ts, now_ts(), duration, star_count_for_event,
                ),
            )
        await send_error_alert(context, f"Pin failed for message {message.message_id}: {exc}")
        await post_announcement(context)


async def countdown_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    event_id = data["event_id"]
    minutes = data["minutes"]
    with db_connect() as db:
        row = db.execute(
            "SELECT status, end_ts FROM events WHERE id=?",
            (event_id,),
        ).fetchone()
    if not row or row["status"] != "active" or row["end_ts"] <= now_ts():
        return
    try:
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=f"⏳ <b>Pin ending soon!</b>\n📌 This message will be unpinned in <b>{minutes} minutes</b>.",
        )
    except TelegramError as exc:
        await send_error_alert(context, f"{minutes}-minute countdown failed: {exc}")


async def finish_event_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    event_id = context.job.data["event_id"]
    await finish_event(context, event_id, "timer_expired")


async def finish_event(
    context: ContextTypes.DEFAULT_TYPE,
    event_id: int,
    reason: str,
    actor_id: Optional[int] = None,
    actor_name: str = "System",
) -> bool:
    with db_connect() as db:
        row = db.execute(
            "SELECT * FROM events WHERE id=? AND chat_id=? AND status IN ('active','pinning')",
            (event_id, TARGET_CHAT_ID),
        ).fetchone()
    if not row:
        return False

    try:
        await context.bot.unpin_chat_message(
            chat_id=TARGET_CHAT_ID,
            message_id=row["message_id"],
        )
    except TelegramError as exc:
        # Telegram may already have unpinned the message. Treat "not pinned" as
        # a failure requiring an alert rather than silently claiming success.
        await send_error_alert(
            context,
            f"Unpin failed for event {event_id}, message {row['message_id']}: {exc}",
        )
        return False

    end_ts = now_ts()
    with db_connect() as db:
        db.execute(
            "UPDATE events SET status='ended', end_reason=? WHERE id=?",
            (reason, event_id),
        )
        db.execute(
            "UPDATE pin_history SET end_ts=?, result=? WHERE event_id=?",
            (end_ts, reason, event_id),
        )
    audit(actor_id, actor_name, "event_ended", f"event_id={event_id}; reason={reason}")

    # Cancel leftover jobs for this event.
    for suffix in ("end", "countdown10", "countdown5"):
        for job in context.application.job_queue.get_jobs_by_name(f"event_{event_id}_{suffix}"):
            job.schedule_removal()

    if get_setting("paused", "0") != "1" and get_setting("maintenance", "0") != "1":
        await post_announcement(context)
    return True


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message and user:
        if is_admin(user.id):
            await message.reply_text("👋 Hello, admin! Use /admin ⚙️ to manage the bot.")
        else:
            await message.reply_text("🤖 This bot is configured for group management. 🛠️")


async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user and message and context.application.bot_data.get("awaiting_custom_duration") == user.id:
        context.application.bot_data.pop("awaiting_custom_duration", None)
        await message.reply_text("✖️ Custom duration setup cancelled.")
    elif message:
        await message.reply_text("ℹ️ There is no pending setup to cancel.")


# ----------------------------- Setup handlers -----------------------------

async def save_custom_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not is_admin(user.id):
        return
    if context.application.bot_data.get("awaiting_custom_duration") != user.id:
        return
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        context.application.bot_data.pop("awaiting_custom_duration", None)
        await message.reply_text("✖️ Custom duration cancelled. Use /admin ⚙️ to reopen the panel.")
        return
    try:
        minutes = int(raw)
    except ValueError:
        await message.reply_text("⏱️ Please send a whole number of minutes (1–10080), or /cancel.")
        return
    if not 1 <= minutes <= 10080:
        await message.reply_text("⚠️ Duration must be between 1 and 10080 minutes. Try again or send /cancel.")
        return
    set_setting("pin_minutes", str(minutes))
    context.application.bot_data.pop("awaiting_custom_duration", None)
    audit(user.id, user.full_name, "duration_changed", f"minutes={minutes}")
    await message.reply_text(f"✅ Pin duration set to {format_duration(minutes)}. This applies to the next event.")


async def private_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and context.application.bot_data.get("awaiting_custom_duration") == user.id:
        await save_custom_duration(update, context)


# ---------------------- Random text custom emoji support ----------------------
# IDs are verified against Telegram at startup. Only IDs whose actual sticker
# alt exactly matches a regular emoji are used, because Telegram requires that
# the custom-emoji entity wrap its matching fallback emoji.
_VERIFIED_CUSTOM_EMOJI_BY_ALT: dict[str, list[str]] = {}
_CUSTOM_EMOJI_PATTERN = None


def _convert_text_emojis_to_custom_html(value):
    """Wrap matching text emoji in verified tg-emoji tags, handling VS16 variants."""
    import random
    import re as _re
    if not isinstance(value, str) or not _VERIFIED_CUSTOM_EMOJI_BY_ALT:
        return value

    # Preserve already-converted entities while transforming only ordinary text.
    chunks = _re.split(r'(<tg-emoji\b[^>]*>.*?</tg-emoji>)', value,
                       flags=_re.IGNORECASE | _re.DOTALL)
    # Telegram sticker alt text can include U+FE0F (emoji presentation selector),
    # while the same visible emoji in a message may omit it. Match both forms but
    # always emit the exact sticker fallback string returned by Telegram.
    choices = {}
    for alt, ids in _VERIFIED_CUSTOM_EMOJI_BY_ALT.items():
        normalized = alt.replace('\ufe0f', '')
        if normalized:
            choices.setdefault(normalized, []).append((alt, ids))
    alternatives = sorted(choices, key=len, reverse=True)
    # Optional variation selectors between code points make common emoji forms match.
    parts = []
    for key in alternatives:
        chars = []
        for ch in key:
            chars.append(_re.escape(ch))
            if ord(ch) >= 0x2600:
                chars.append('\ufe0f?')
        parts.append(''.join(chars))
    pattern = _re.compile('|'.join(parts)) if parts else None
    if pattern is None:
        return value

    for i, chunk in enumerate(chunks):
        if i % 2:
            continue
        def replace(match):
            normalized = match.group(0).replace('\ufe0f', '')
            matches = choices.get(normalized)
            if not matches:
                return match.group(0)
            alt, ids = random.choice(matches)
            return f'<tg-emoji emoji-id="{random.choice(ids)}">{alt}</tg-emoji>'
        chunks[i] = pattern.sub(replace, chunk)
    return ''.join(chunks)


def _install_custom_emoji_outgoing_hooks():
    """Apply custom-emoji conversion to Bot and ExtBot outgoing text/captions.

    PTB Application uses ExtBot; wrapping Bot alone can miss calls when ExtBot
    overrides a method. Both classes are wrapped so reply_text/send_message and
    edit_message_text are covered. Existing HTML parse mode is preserved.
    """
    def wrap_method(cls, method_name, text_arg_index=None, caption=False):
        original = getattr(cls, method_name, None)
        if original is None or getattr(original, '_xrichotc_custom_emoji_hook', False):
            return

        async def wrapped(self, *args, **kwargs):
            changed = False
            if caption:
                if isinstance(kwargs.get('caption'), str):
                    kwargs['caption'] = _convert_text_emojis_to_custom_html(kwargs['caption'])
                    changed = True
                elif text_arg_index is not None and len(args) > text_arg_index and isinstance(args[text_arg_index], str):
                    args = list(args)
                    args[text_arg_index] = _convert_text_emojis_to_custom_html(args[text_arg_index])
                    args = tuple(args)
                    changed = True
            elif 'text' in kwargs and isinstance(kwargs['text'], str):
                kwargs['text'] = _convert_text_emojis_to_custom_html(kwargs['text'])
                changed = True
            elif text_arg_index is not None and len(args) > text_arg_index and isinstance(args[text_arg_index], str):
                args = list(args)
                args[text_arg_index] = _convert_text_emojis_to_custom_html(args[text_arg_index])
                args = tuple(args)
                changed = True

            # PTB Defaults(parse_mode=HTML) is used by this bot. If a caller
            # explicitly supplies a non-HTML parse mode, don't silently override it.
            if changed and kwargs.get('parse_mode') is None:
                kwargs['parse_mode'] = ParseMode.HTML
            return await original(self, *args, **kwargs)

        wrapped._xrichotc_custom_emoji_hook = True
        setattr(cls, method_name, wrapped)

    for cls in (Bot, ExtBot):
        wrap_method(cls, 'send_message', text_arg_index=1)
        wrap_method(cls, 'edit_message_text', text_arg_index=0)
        for name in ('send_photo', 'send_video', 'send_animation', 'send_document', 'send_audio'):
            wrap_method(cls, name, caption=True)


_install_custom_emoji_outgoing_hooks()


async def _load_verified_custom_emojis(bot):
    """Load usable IDs from the public catalog and index them by the exact fallback emoji."""
    global _VERIFIED_CUSTOM_EMOJI_BY_ALT
    ids = list(dict.fromkeys(str(x) for x in CUSTOM_EMOJI_CATALOG_IDS))
    verified = {}
    # Telegram accepts batches; invalid or inaccessible IDs are skipped safely.
    for start in range(0, len(ids), 20):
        batch = ids[start:start + 20]
        try:
            stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=batch)
        except Exception as exc:
            logger.warning('Could not verify custom emoji ID batch %s: %s', start // 20 + 1, exc)
            continue
        for sticker in stickers:
            alt = getattr(sticker, 'emoji', None)
            emoji_id = getattr(sticker, 'custom_emoji_id', None)
            if alt and emoji_id:
                verified.setdefault(alt, []).append(str(emoji_id))
    _VERIFIED_CUSTOM_EMOJI_BY_ALT = verified
    verified_count = sum(len(v) for v in verified.values())
    logger.info('CUSTOM EMOJI CHECK: %s usable IDs, %s matching fallback emoji characters.',
                verified_count, len(verified))
    if verified:
        logger.info('CUSTOM EMOJI FALLBACKS: %s', ' '.join(list(verified.keys())[:30]))
    else:
        logger.warning('CUSTOM EMOJI CHECK FOUND ZERO USABLE IDS; text emojis will remain standard Unicode.')


# ----------------------------- Admin commands -----------------------------

# Telegram custom-emoji catalog researched from public GitHub emoji registries.
# The broad pool below contains 100+ real IDs; use semantic IDs in CUSTOM_EMOJI_BY_ROLE
# for UI so unrelated pack art is not randomly shown in admin controls.
# Source catalogs: https://github.com/abbosahmad/telegram-premium-emojis
# and https://github.com/thatonearyan-sh/TG_P_EMOJIS
CUSTOM_EMOJI_CATALOG_IDS = (
    5415823884739960215, 5416062311259461906, 5413760586745928144, 5415664129136416110,
    5413523401471972496, 5415979207937248631, 5413420691624055757, 5415896993673267994,
    5415974204300350892, 5415694808087813882, 5413344906926120020, 5415840222795549567,
    5415794008947445177, 5415878014212787903, 5415738771373052760, 5413399418651042229,
    5413422684488883008, 5416083996549338445, 5415913301664092003, 5415870463660282489,
    5415679913141232384, 5415943151686799587, 5415982789939980015, 5413505955314819648,
    5413841194692138820, 5415921208698884023, 5413482526268218557, 5415824872582437248,
    5415854610935993730, 5416119898180965969, 5413871392607198329, 5415769866936276365,
    5413760225968676362, 5415646850482983451, 5415587296466459379, 5415963608616034310,
    5415601817750884185, 5415619452886605630, 5413564414114682477, 5415687201700728745,
    5415772091729334504, 5416055508031267308, 5413791149733207948, 5415930202360404488,
    5415787210014220664, 5415605215070013638, 5413852249937961652, 5413803081152355761,
    5415601001707098760, 5413706624776823299, 5415801623924462610, 5415814723574718692,
    5416055873103483532, 5415620264635422595, 5415915139910094637, 5416123729291792939,
    5415845424000948757, 5415663922977986321, 5413629946725687079, 5416010243370931203,
    5413424222087174564, 5415648379491341225, 5415629245412041823, 5415733630297201370,
    5415662243645772897, 5413577406390752988, 5415696753707998391, 5413811709741654130,
    5416017622124747439, 5415631994191105503, 5415734751283664013, 5415667706844173293,
    5415657390332728607, 5413728060958595822, 5415867139355597875, 5415914164952519402,
    5415982210119393402, 5415588421747889974, 5415726427637044837, 5415692463035667321,
    5413549476218429323, 5415635657798212177, 5413552147688086386, 5413739524226310411,
    5416000051413540086, 5416118416417248385, 5415694017813829810, 5416106424868557022,
    5416102155671064634, 5415926968250029726, 5413479867683463241, 5413594015029289364,
    5416021809717861708, 5416097203573776660, 5413875734819135433,
    5264877396561013397, 5265077198439623133, 5265001525410833904, 5265077533447068814,
    5264829752488797637, 5264738828031142390, 5264959847048193756, 5262722302230963507,
    5262541260769501660, 5264847559423208012, 5264716184963558689, 5265181656339227756,
    5262578614100076444, 5262930535130373840, 5264950385235239749, 5264996805241778408,
    5265093308861950736, 5265030920167008506, 5265146970183344211, 5262944291910628036,
)

# Semantic IDs selected for this bot's interface (fallback Unicode is always retained).
CUSTOM_EMOJI_BY_ROLE = {
    "back": ("⬅️", "5877536313623711363"),
    "home": ("🏠", "5807868868886009920"),
    "success": ("✅", "5920052658743283381"),
    "cancel": ("❌", "5258226313285607065"),
    "premium": ("💎", "5807465992363710697"),
    "star": ("⭐️", "5874948844935974490"),
    "speed": ("⚡️", "5843553939672274145"),
    "document": ("📄", "5258477770735885832"),
    "folder": ("📁", "5875206779196935950"),
    "download": ("⬇️", "5258336354642697821"),
    "upload": ("⬆️", "5260652420052032852"),
    "warning": ("⚠️", "5258474669769497337"),
    "info": ("ℹ️", "5258503720928288433"),
    "settings": ("⚙️", "5258420634785947640"),
}

def _custom_emoji_for_button(text: str) -> Optional[str]:
    t = text.lower()
    if any(k in t for k in ("back", "cancel", "close", "reset")):
        role = "back" if "back" in t else "cancel"
    elif any(k in t for k in ("setting", "timing", "maintenance", "config")):
        role = "settings"
    elif any(k in t for k in ("resume", "saved", "success", "enable", "on")):
        role = "success"
    elif any(k in t for k in ("warning", "pause", "unpin", "disable", "off", "delete")):
        role = "warning"
    elif any(k in t for k in ("stats", "status", "info", "help")):
        role = "info"
    elif any(k in t for k in ("download", "export")):
        role = "download"
    elif any(k in t for k in ("upload", "import")):
        role = "upload"
    elif any(k in t for k in ("star", "premium", "paid")):
        role = "star"
    else:
        role = "home"
    return CUSTOM_EMOJI_BY_ROLE[role][1]


def styled_button(text: str, callback_data: str, style: str = "primary") -> InlineKeyboardButton:
    """Use Telegram's custom emoji button icon, not a Unicode emoji in the label."""
    style = style if style in {"primary", "success", "danger"} else "primary"
    emoji_id = _custom_emoji_for_button(text)
    # Remove the leading decorative Unicode emoji so the custom icon is clearly used
    # as the button icon. Preserve the rest of the label and callback behavior.
    clean_text = re.sub(r"^[^\w\s]+\s*", "", text, count=1, flags=re.UNICODE).strip() or text
    try:
        return InlineKeyboardButton(
            clean_text, callback_data=callback_data,
            api_kwargs={"style": style, "icon_custom_emoji_id": emoji_id},
        )
    except (TypeError, ValueError):
        # Older python-telegram-bot versions may not support these Bot API fields.
        return InlineKeyboardButton(text, callback_data=callback_data)


def admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [styled_button("⏱️ Set Timing", "menu:timing", "primary"), styled_button("📌 Event Controls", "menu:event", "primary")],
        [styled_button("🛠️ Maintenance", "menu:maintenance", "danger")],
        [styled_button("🌍 Timezone Settings", "menu:timezone", "primary")],
        [styled_button("📊 Stats & Status", "menu:stats", "primary")],
    ])


def admin_submenu(name: str) -> tuple[str, InlineKeyboardMarkup]:
    back = styled_button("⬅️ Back to Admin Panel", "menu:home", "primary")
    if name == "timing":
        return "⏱️ <b>Set Pin Timing</b>\n\n<i>Choose a preset or set a custom duration.</i>", InlineKeyboardMarkup([
            [styled_button("🟢 30 Minutes", "duration:30", "success"), styled_button("🟢 60 Minutes", "duration:60", "success")],
            [styled_button("🟢 120 Minutes", "duration:120", "success")],
            [styled_button("✍️ Custom Duration", "duration:custom", "primary")], [back],
        ])
    if name == "event":
        return "📌 <b>Event Controls</b>\n\n<i>Manage the auto-pin event below.</i>", InlineKeyboardMarkup([
            [styled_button("⏸️ Pause Auto-Pin", "pause", "danger"), styled_button("▶️ Resume Auto-Pin", "resume", "success")],
            [styled_button("📍 Manual Unpin", "unpin", "danger"), styled_button("🧹 Reset Event", "reset_event", "danger")],
            [styled_button("📋 Event Status", "status", "primary")], [back],
        ])
    if name == "maintenance":
        enabled = get_setting("maintenance", "0") == "1"
        current = "<b>ON</b> 🔴" if enabled else "<b>OFF</b> 🟢"
        return f"🛠️ <b>Maintenance Mode</b>\n\nCurrent status: {current}\n\n<i>Maintenance mode prevents new events.</i>", InlineKeyboardMarkup([
            [styled_button("🟢 Turn OFF", "maintenance:off", "success"), styled_button("🔴 Turn ON", "maintenance:on", "danger")],
            [back],
        ])
    if name == "timezone":
        current = get_setting("timezone", "Asia/Kolkata") or "Asia/Kolkata"
        try:
            now = datetime.now(ZoneInfo(current)).strftime("%Y-%m-%d %H:%M:%S")
        except (ZoneInfoNotFoundError, ValueError):
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        zone_labels = {
            "Asia/Kolkata": "🇮🇳 India (Kolkata)",
            "America/New_York": "🇺🇸 USA (New York)",
            "Europe/Moscow": "🇷🇺 Russia (Moscow)",
        }
        return (f"🌍 <b>Timezone Settings</b>\n\nCurrent timezone: <code>{escape(current)}</code>\nCurrent local time: <b>{escape(now)}</b>\n\nChoose a region. The active timezone is shown in green."), InlineKeyboardMarkup([
            [styled_button(("✅ " if current == "Asia/Kolkata" else "🇮🇳 ") + "India (Kolkata)", "timezone:set:Asia/Kolkata", "success" if current == "Asia/Kolkata" else "primary")],
            [styled_button(("✅ " if current == "America/New_York" else "🇺🇸 ") + "USA (New York)", "timezone:set:America/New_York", "success" if current == "America/New_York" else "primary")],
            [styled_button(("✅ " if current == "Europe/Moscow" else "🇷🇺 ") + "Russia (Moscow)", "timezone:set:Europe/Moscow", "success" if current == "Europe/Moscow" else "primary")],
            [back],
        ])
    if name == "stats":
        return "📊 <b>Stats & Status</b>\n\n<i>Choose the information you want to view.</i>", InlineKeyboardMarkup([
            [styled_button("📋 Event Status", "status", "primary")],
            [styled_button("📈 Daily Stats", "stats", "success"), styled_button("⭐ Top Stars", "topstars", "success")],
            [back],
        ])
    return "⚙️ <b>xRichOtc Auto-Pin Admin Panel</b>\n\n<i>Select a section below.</i>", admin_home_keyboard()


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ <b>Access Denied</b>\n\n<i>This command is available to authorized admins only.</i>")
        return
    minutes = int(get_setting("pin_minutes", str(DEFAULT_PIN_MINUTES)) or DEFAULT_PIN_MINUTES)
    paused = get_setting("paused", "0") == "1"
    maintenance = get_setting("maintenance", "0") == "1"
    event = active_event()
    status = (
        f"⚙️ <b>xRichOtc Auto-Pin Admin Panel</b>\n\n"
        f"⏱️ <b>Pin Duration:</b> <i>{format_duration(minutes)}</i>\n"
        f"⏸️ <b>Auto-Pin:</b> {'<b>PAUSED</b> 🟡' if paused else '<b>ACTIVE</b> 🟢'}\n"
        f"🛠️ <b>Maintenance:</b> {'<b>ON</b> 🔴' if maintenance else '<b>OFF</b> 🟢'}\n"
        f"📌 <b>Active Event:</b> {'<b>YES</b> 🔴' if event else '<b>NO</b> ⚪'}\n"
        "<i>Select a section below to manage your bot.</i>"
    )
    await message.reply_text(status, reply_markup=admin_home_keyboard())


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    user = query.from_user
    if not is_admin(user.id):
        await query.answer("⛔ Access denied: admins only.", show_alert=True)
        return
    await query.answer()
    data = query.data or ""

    if data.startswith("menu:"):
        name = data.split(":", 1)[1]
        text, keyboard = admin_submenu(name)
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    if data.startswith("timezone:set:"):
        zone_name = data.split(":", 2)[2]
        allowed_zones = {
            "Asia/Kolkata": "🇮🇳 India (Kolkata)",
            "America/New_York": "🇺🇸 USA (New York)",
            "Europe/Moscow": "🇷🇺 Russia (Moscow)",
        }
        if zone_name not in allowed_zones:
            await query.edit_message_text(
                "❌ Unknown timezone selection. Please reopen Admin Panel → 🌍 Timezone Settings and choose a button.",
                reply_markup=admin_home_keyboard(),
            )
            return
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            await query.edit_message_text(
                "⚠️ Timezone database is missing on this device. In Termux run:\n"
                "<code>pip install -U tzdata</code>\n\n"
                "Then restart the bot and select the timezone again.",
                reply_markup=admin_submenu("timezone")[1],
                parse_mode=ParseMode.HTML,
            )
            return
        set_setting("timezone", zone_name)
        audit(user.id, user.full_name, "timezone_changed", zone_name)
        now = datetime.now(zone).strftime("%Y-%m-%d %H:%M:%S")
        text, keyboard = admin_submenu("timezone")
        await query.edit_message_text(
            f"✅ Timezone updated to <code>{escape(zone_name)}</code>.\nLocal time now: <b>{escape(now)}</b>\n\n" + text,
            reply_markup=keyboard,
        )
        return
    if data == "duration:custom":
        context.application.bot_data["awaiting_custom_duration"] = user.id
        await query.edit_message_text(
            "⏱ <b>Custom Pin Duration</b>\nSend the duration in minutes (1–10080) in this private chat with the bot.\nSend /cancel to cancel.",
            reply_markup=InlineKeyboardMarkup([[styled_button("✖️ Cancel", "menu:timing", "danger")]]),
        )
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text="⏱️ Send custom pin duration in minutes (1–10080). Example: <code>90</code>. Send /cancel to cancel. ✖️",
            )
        except TelegramError:
            context.application.bot_data.pop("awaiting_custom_duration", None)
            await query.edit_message_text("📩 Please open the bot's private chat and send /start first, then try Custom again.", reply_markup=admin_home_keyboard())
        return

    if data.startswith("duration:"):
        minutes = int(data.split(":", 1)[1])
        if not 1 <= minutes <= 10080:
            await query.edit_message_text("⚠️ Duration must be between 1 and 10,080 minutes. ⏱️")
            return
        set_setting("pin_minutes", str(minutes))
        audit(user.id, user.full_name, "duration_changed", f"minutes={minutes}")
        text, keyboard = admin_submenu("timing")
        await query.edit_message_text(f"✅ Pin duration updated to {format_duration(minutes)}.\n\n" + text, reply_markup=keyboard)
    elif data == "pause":
        set_setting("paused", "1")
        audit(user.id, user.full_name, "paused")
        await query.edit_message_text("⏸️ Auto-pin is paused. Any active event will finish on its timer. ⏳")
    elif data == "resume":
        set_setting("paused", "0")
        audit(user.id, user.full_name, "resumed")
        await query.edit_message_text("▶️ Auto-pin resumed successfully! ✅")
        await post_announcement(context)
    elif data == "unpin":
        event = active_event()
        if not event:
            await query.edit_message_text("ℹ️ There is no active event.")
        else:
            ok = await finish_event(
                context, event["id"], "manual_unpin", user.id, user.full_name
            )
            await query.edit_message_text(
                "Active message unpinned." if ok else
                "Could not unpin the active message. An error alert was attempted."
            )
    elif data == "reset_event":
        event = active_event()
        if not event:
            await query.edit_message_text("🧹 <b>Reset Event</b>\n\nThere is no running event to reset.")
        else:
            keyboard = InlineKeyboardMarkup([
                [
                    styled_button("✅ Yes, Reset Event", "reset_event:confirm", "danger"),
                    styled_button("❌ Cancel", "reset_event:cancel", "primary"),
                ]
            ])
            await query.edit_message_text(
                "⚠️ <b>Reset the running event?</b>\n\n"
                "This will try to unpin the current message, end the event, cancel its timers, "
                "and start the next event announcement. This cannot be undone.",
                reply_markup=keyboard,
            )
    elif data == "reset_event:confirm":
        event = active_event()
        if not event:
            await query.edit_message_text("🧹 There is no running event to reset.")
        else:
            ok = await finish_event(
                context, event["id"], "admin_reset", user.id, user.full_name
            )
            await query.edit_message_text(
                "✅ <b>Event reset complete.</b> The running event was ended and its pin was removed."
                if ok else
                "❌ <b>Reset failed.</b> The message could not be unpinned; the event remains available for retry."
            )
    elif data == "reset_event:cancel":
        await query.edit_message_text("✖️ Reset cancelled. The running event was not changed.")
    elif data == "status":
        await query.edit_message_text(event_status_text())
    elif data == "maintenance:on":
        set_setting("maintenance", "1")
        audit(user.id, user.full_name, "maintenance_enabled")
        await query.edit_message_text("🛠️ Maintenance mode enabled. New events are disabled. 🔴")
    elif data == "maintenance:off":
        set_setting("maintenance", "0")
        audit(user.id, user.full_name, "maintenance_disabled")
        await query.edit_message_text("✅ Maintenance mode disabled. 🟢")
        await post_announcement(context)
    elif data == "stats":
        await query.edit_message_text(daily_stats_text())
    elif data == "topstars":
        await query.edit_message_text(top_stars_text())


async def set_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    if len(context.args) != 1:
        await message.reply_text("ℹ️ Usage: /setduration MINUTES ⏱️")
        return
    try:
        minutes = int(context.args[0])
    except ValueError:
        await message.reply_text("⚠️ Minutes must be a whole number. 🔢")
        return
    if not 1 <= minutes <= 10080:
        await message.reply_text("⚠️ Duration must be between 1 and 10,080 minutes. ⏱️")
        return
    set_setting("pin_minutes", str(minutes))
    audit(user.id, user.full_name, "duration_changed", f"minutes={minutes}")
    await message.reply_text(
        f"Pin duration set to {format_duration(minutes)}. This applies to the next event."
    )


async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    requested = ",".join(context.args).replace(" ", "").lower()
    valid = {"text", "photo", "video"}
    selected = {item for item in requested.split(",") if item}
    if not selected or not selected.issubset(valid):
        await message.reply_text("ℹ️ Usage: /setfilter text,photo,video (choose any of these types) 📝📷🎥")
        return
    value = ",".join(sorted(selected))
    set_setting("allowed_types", value)
    audit(user.id, user.full_name, "filter_changed", value)
    await message.reply_text(f"Allowed message types updated: {value}")


async def set_custom_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    setting_key: str,
    usage: str,
    audit_action: str,
) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.reply_text(f"Usage: {usage}")
        return
    if len(text) > 500:
        await message.reply_text("⚠️ Message template must be 500 characters or fewer. ✍️")
        return
    if "{duration}" not in text and setting_key in {
        "announcement_template", "confirmation_template"
    }:
        await message.reply_text("⚠️ Include {duration} where the duration should appear. ⏱️")
        return
    set_setting(setting_key, text)
    audit(user.id, user.full_name, audit_action, text)
    await message.reply_text("✅ Message template updated successfully! ✍️")


async def set_announcement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_custom_message(
        update, context, "announcement_template",
        "/setannouncement 📌 Next message will pin for {duration}.",
        "announcement_template_changed",
    )


async def set_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_custom_message(
        update, context, "confirmation_template",
        "/setconfirmation ✅ Your message has been pinned for {duration}!",
        "confirmation_template_changed",
    )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    started = perf_counter()
    try:
        await context.bot.get_me()
        api_ms = (perf_counter() - started) * 1000
        await message.reply_text(
            "🏓 <b>Pong!</b>\n"
            f"⚡ Bot API response: <b>{api_ms:.0f} ms</b>\n"
            "🟢 Bot is responding."
        )
    except TelegramError as exc:
        await message.reply_text(f"🔴 Bot API check failed: <code>{escape(str(exc)[:500])}</code>")


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    set_setting("paused", "1")
    audit(user.id, user.full_name, "paused")
    await message.reply_text("⏸️ Auto-pin paused. The active event, if any, will finish on schedule. ⏳")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    set_setting("paused", "0")
    audit(user.id, user.full_name, "resumed")
    await message.reply_text("▶️ Auto-pin resumed successfully! ✅")
    await post_announcement(context)


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    if len(context.args) != 1 or context.args[0].lower() not in {"on", "off"}:
        await message.reply_text("ℹ️ Usage: /maintenance on OR /maintenance off 🛠️")
        return
    enabled = context.args[0].lower() == "on"
    set_setting("maintenance", "1" if enabled else "0")
    audit(user.id, user.full_name, "maintenance_enabled" if enabled else "maintenance_disabled")
    await message.reply_text(
        "🛠️ Maintenance mode enabled. New events are disabled. 🔴"
        if enabled else "✅ Maintenance mode disabled. 🟢"
    )
    if not enabled:
        await post_announcement(context)


async def unpin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    event = active_event()
    if not event:
        await message.reply_text("ℹ️ There is no active event.")
        return
    ok = await finish_event(context, event["id"], "manual_unpin", user.id, user.full_name)
    await message.reply_text(
        "Active message unpinned." if ok else
        "Could not unpin the active message. An error alert was attempted."
    )


def event_status_text() -> str:
    event = active_event()
    if not event:
        return (
            "📊 <b>Event Status</b>\n\n⚪ <b>No active event</b>\n"
            f"⏸️ Paused: {'Yes' if get_setting('paused', '0') == '1' else 'No'}\n"
            f"🛠️ Maintenance: {'On' if get_setting('maintenance', '0') == '1' else 'Off'}"
        )
    remaining = max(0, int(event["end_ts"] - now_ts()))
    return (
        "📌 <b>Active Event</b>\n\n"
        f"👤 <b>Member:</b> {escape(event['display_name'])} (@{escape(event['username'] or 'no_username')})\n"
        f"🆔 <b>User ID:</b> <code>{event['user_id']}</code>\n"
        f"💬 <b>Message ID:</b> <code>{event['message_id']}</code>\n"
        f"🟢 <b>Started:</b> {format_time(event['start_ts'])}\n"
        f"🔔 <b>Ends:</b> {format_time(event['end_ts'])}\n"
        f"⏳ <b>Remaining:</b> {remaining // 60}m {remaining % 60}s\n"
        f"⭐ <b>Stars on message:</b> {event['paid_star_count'] if event['paid_star_count'] is not None else 'Not provided by Telegram'}"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    await message.reply_text(event_status_text())


async def daily_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    await message.reply_text(daily_stats_text())


def daily_stats_text() -> str:
    # UTC day boundaries are explicit to avoid ambiguous timezone handling.
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()
    with db_connect() as db:
        event_row = db.execute(
            "SELECT COUNT(*) AS n FROM events WHERE chat_id=? AND start_ts>=? "
            "AND status IN ('active','ended','failed')",
            (TARGET_CHAT_ID, start),
        ).fetchone()
        stars_row = db.execute(
            "SELECT COALESCE(SUM(paid_star_count),0) AS stars, COUNT(*) AS n "
            "FROM paid_messages WHERE chat_id=? AND created_ts>=?",
            (TARGET_CHAT_ID, start),
        ).fetchone()
        unique_row = db.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM paid_messages "
            "WHERE chat_id=? AND created_ts>=?",
            (TARGET_CHAT_ID, start),
        ).fetchone()
        duration_row = db.execute(
            "SELECT COALESCE(SUM(MIN(COALESCE(end_ts, ?), ?) - start_ts),0) AS seconds "
            "FROM pin_history WHERE chat_id=? AND start_ts>=? AND result IN "
            "('pinned','timer_expired','manual_unpin')",
            (now_ts(), now_ts(), TARGET_CHAT_ID, start),
        ).fetchone()
    return (
        "📊 <b>Daily Statistics</b> <i>(UTC)</i>\n\n"
        f"🎯 <b>Events started:</b> {event_row['n']}\n"
        f"💬 <b>Paid messages tracked:</b> {stars_row['n']}\n"
        f"⭐ <b>Known Stars spent:</b> {stars_row['stars']}\n"
        f"👥 <b>Unique paying members:</b> {unique_row['n']}\n"
        f"📌 <b>Total pinned time:</b> {max(0, int(duration_row['seconds'])) // 60} minutes\n\n"
        "ℹ️ <i>Totals include only paid messages recorded by this bot.</i>"
    )


def top_stars_text(limit: int = 10) -> str:
    with db_connect() as db:
        rows = db.execute(
            """
            SELECT user_id, username, display_name,
                   SUM(paid_star_count) AS stars,
                   COUNT(*) AS paid_count
            FROM paid_messages
            WHERE chat_id=?
            GROUP BY user_id, username, display_name
            ORDER BY stars DESC, paid_count DESC
            LIMIT ?
            """,
            (TARGET_CHAT_ID, limit),
        ).fetchall()
    if not rows:
        return "⭐ No paid-message Stars records have been collected yet."
    lines = ["🏆 <b>Top Stars Spenders</b> <i>(tracked data)</i>"]
    for index, row in enumerate(rows, start=1):
        username = f"@{row['username']}" if row["username"] else row["display_name"]
        lines.append(
            f"{index}. {username} — {row['stars']} ⭐ across {row['paid_count']} paid messages"
        )
    return "\n".join(lines)


async def top_stars_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    await message.reply_text(top_stars_text())


def parse_user_query(value: str) -> tuple[Optional[int], Optional[str]]:
    value = value.strip()
    if value.startswith("@"):
        return None, value[1:].lower()
    try:
        return int(value), None
    except ValueError:
        return None, value.lower()


async def mstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("Access denied. /mstats is for authorized admins only.")
        return
    if len(context.args) != 1:
        await message.reply_text("Usage: /mstats USER_ID_OR_USERNAME")
        return

    user_id, username = parse_user_query(context.args[0])
    with db_connect() as db:
        if user_id is not None:
            row = db.execute(
                """
                SELECT user_id, MAX(username) AS username,
                       MAX(display_name) AS display_name,
                       COALESCE(SUM(paid_star_count),0) AS stars,
                       COUNT(*) AS paid_count,
                       MIN(created_ts) AS first_seen,
                       MAX(created_ts) AS last_seen
                FROM paid_messages WHERE chat_id=? AND user_id=?
                GROUP BY user_id
                """,
                (TARGET_CHAT_ID, user_id),
            ).fetchone()
        else:
            row = db.execute(
                """
                SELECT user_id, MAX(username) AS username,
                       MAX(display_name) AS display_name,
                       COALESCE(SUM(paid_star_count),0) AS stars,
                       COUNT(*) AS paid_count,
                       MIN(created_ts) AS first_seen,
                       MAX(created_ts) AS last_seen
                FROM paid_messages
                WHERE chat_id=? AND lower(username)=?
                GROUP BY user_id
                """,
                (TARGET_CHAT_ID, username),
            ).fetchone()

        if row:
            daily_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
            month_start = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
            today = db.execute(
                "SELECT COALESCE(SUM(paid_star_count),0) AS n FROM paid_messages "
                "WHERE chat_id=? AND user_id=? AND created_ts>=?",
                (TARGET_CHAT_ID, row["user_id"], daily_start),
            ).fetchone()["n"]
            month = db.execute(
                "SELECT COALESCE(SUM(paid_star_count),0) AS n FROM paid_messages "
                "WHERE chat_id=? AND user_id=? AND created_ts>=?",
                (TARGET_CHAT_ID, row["user_id"], month_start),
            ).fetchone()["n"]
            pinned_count = db.execute(
                "SELECT COUNT(*) AS n FROM pin_history WHERE chat_id=? AND user_id=?",
                (TARGET_CHAT_ID, row["user_id"]),
            ).fetchone()["n"]

    if not row:
        await message.reply_text(
            "No paid-message records found for that member. "
            "Check the user ID/username or wait for a paid message after the bot is installed."
        )
        return

    display = f"@{row['username']}" if row["username"] else row["display_name"]
    await message.reply_text(
        "📊 <b>Member Statistics</b>\n\n"
        f"👤 <b>Member:</b> {escape(display)}\n"
        f"🆔 <b>User ID:</b> <code>{row['user_id']}</code>\n"
        f"⭐ <b>Total tracked Stars:</b> {row['stars']}\n"
        f"💬 <b>Paid messages:</b> {row['paid_count']}\n"
        f"📅 <b>Stars today (UTC):</b> {today} ⭐\n"
        f"🗓️ <b>Stars this month (UTC):</b> {month} ⭐\n"
        f"📌 <b>Messages pinned by bot:</b> {pinned_count}\n"
        f"🟢 <b>First tracked payment:</b> {format_time(row['first_seen'])}\n"
        f"🔔 <b>Last tracked payment:</b> {format_time(row['last_seen'])}\n\n"
        "ℹ️ <i>Figures cover only paid messages recorded by this bot.</i>"
    )


async def pin_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    with db_connect() as db:
        rows = db.execute(
            "SELECT display_name, username, message_id, start_ts, duration_minutes, "
            "paid_star_count, result FROM pin_history WHERE chat_id=? "
            "ORDER BY id DESC LIMIT 10",
            (TARGET_CHAT_ID,),
        ).fetchall()
    if not rows:
        await message.reply_text("📭 No pin history yet.")
        return
    lines = ["📌 <b>Recent Pin History</b>"]
    for row in rows:
        name = f"@{row['username']}" if row["username"] else row["display_name"]
        stars = (
            f", {row['paid_star_count']} ⭐"
            if row["paid_star_count"] is not None else ""
        )
        lines.append(
            f"• {name} | message <code>{row['message_id']}</code> | "
            f"{format_time(row['start_ts'])} | <b>{row['duration_minutes']} min</b> | "
            f"{escape(row['result'])}{stars}"
        )
    await message.reply_text("\n".join(lines))


async def audit_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    with db_connect() as db:
        rows = db.execute(
            "SELECT actor_id, actor_name, action, details, created_ts "
            "FROM audit_logs ORDER BY id DESC LIMIT 15"
        ).fetchall()
    if not rows:
        await message.reply_text("📭 No admin audit entries yet.")
        return
    lines = ["🧾 <b>Recent Admin Audit Log</b>"]
    for row in rows:
        lines.append(
            f"• {format_time(row['created_ts'])} | {escape(row['actor_name'])} "
            f"(<code>{row['actor_id']}</code>) | <b>{escape(row['action'])}</b> | {escape(row['details'] or '-')}"
        )
    await message.reply_text("\n".join(lines))


async def whereami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if not chat or not message:
        return
    if not user or not is_admin(user.id):
        await message.reply_text("⛔ Access denied. Admins only. 🔐")
        return
    await message.reply_text(
        f"Current chat ID: {chat.id}\n"
        f"Configured target chat ID: {TARGET_CHAT_ID}\n"
        f"Target match: {'Yes' if chat.id == TARGET_CHAT_ID else 'No'}"
    )


# ----------------------------- Recovery and startup -----------------------------

async def recover_active_event(application: Application) -> None:
    """Rebuild timer jobs from persisted event end timestamps after a restart."""
    with db_connect() as db:
        rows = db.execute(
            "SELECT * FROM events WHERE chat_id=? AND status IN ('active','pinning')",
            (TARGET_CHAT_ID,),
        ).fetchall()

    for row in rows:
        event_id = row["id"]
        if row["end_ts"] <= now_ts():
            try:
                await application.bot.unpin_chat_message(
                    chat_id=TARGET_CHAT_ID,
                    message_id=row["message_id"],
                )
                with db_connect() as db:
                    db.execute(
                        "UPDATE events SET status='ended', end_reason='recovered_expired' "
                        "WHERE id=?",
                        (event_id,),
                    )
                    db.execute(
                        "UPDATE pin_history SET end_ts=?, result='recovered_expired' "
                        "WHERE event_id=?",
                        (now_ts(), event_id),
                    )
                logger.info("Recovered expired event %s and unpinned it.", event_id)
            except TelegramError as exc:
                await send_error_alert(
                    application,
                    f"Could not recover expired event {event_id}: {exc}",
                )
                # Keep the active state so a later restart or admin can retry.
        else:
            with db_connect() as db:
                db.execute(
                    "UPDATE events SET status='active' WHERE id=? AND status='pinning'",
                    (event_id,),
                )
            await schedule_event_jobs(
                application, event_id, row["end_ts"], row["start_ts"]
            )

    if not active_event():
        await post_announcement(application)


async def post_init(application: Application) -> None:
    await _load_verified_custom_emojis(application.bot)
    init_db()
    matches = await check_target_identity(application)
    if not matches:
        logger.critical(
            "Target identity check failed. The bot will not process group messages."
        )
        return
    await recover_active_event(application)
    logger.info("xRichOtc Auto-Pin Bot started.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception while processing update", exc_info=context.error)
    try:
        await send_error_alert(context, f"Unhandled exception: {context.error}")
    except Exception:
        logger.exception("Failed to dispatch error alert")


def main() -> None:
    init_db()
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cancel", cancel_setup))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_text_router)
    )

    # Admin commands
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("setduration", set_duration))
    application.add_handler(CommandHandler("setfilter", set_filter))
    application.add_handler(CommandHandler("setannouncement", set_announcement))
    application.add_handler(CommandHandler("setconfirmation", set_confirmation))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("maintenance", maintenance_command))
    application.add_handler(CommandHandler("unpin", unpin_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stats", daily_stats_command))
    application.add_handler(CommandHandler("topstars", top_stars_command))
    application.add_handler(CommandHandler("mstats", mstats_command))
    application.add_handler(CommandHandler("pinhistory", pin_history_command))
    application.add_handler(CommandHandler("auditlog", audit_log_command))
    application.add_handler(CommandHandler("whereami", whereami_command))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^(menu:|duration:|timezone:set:|pause$|resume$|unpin$|reset_event(?::confirm|:cancel)?$|status$|maintenance:|stats$|topstars$)"))

    # All non-command group messages reach this handler. The target ID and
    # allowed type checks inside the handler prevent actions in other groups.
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND,
            handle_group_message,
        )
    )
    application.add_error_handler(error_handler)

    logger.info("Starting polling. Target chat ID: %s", TARGET_CHAT_ID)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
