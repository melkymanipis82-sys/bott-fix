import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import threading
import traceback
from datetime import datetime, timezone
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ===================== LOGGING =====================
LOGGER = logging.getLogger("FBWatchBot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# ===================== CONFIG =====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8657858646:AAGW5uCUbi1jvPgmpYr2b0dP3gbAesQnnuk").strip()
HARDCODED_OWNER_ID = 6656858850
DB_PATH = os.getenv("DB_PATH", "fbwatch.db")
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "300"))


def parse_ids(value: str | None) -> list[int]:
    if not value:
        return []
    result = []
    for token in re.split(r"[,\s]+", value.strip()):
        if token.isdigit():
            result.append(int(token))
    return result


OWNER_IDS_SEED = parse_ids(os.getenv("OWNER_IDS", str(HARDCODED_OWNER_ID)))
USER_IDS_SEED = parse_ids(os.getenv("USER_IDS", ""))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.8",
}

# Only these phrases are treated as a confirmed dead/unavailable page.
# A login wall or private page is not automatically treated as DIE.
DEAD_PHRASES = [
    "this content isn't available right now",
    "this page isn't available",
    "the link may be broken",
    "content isn't available",
    "page not found",
    "the page you requested cannot be displayed right now",
]

ADD_UID, ADD_TYPE, ADD_NOTE, ADD_CUSTOMER = range(1, 5)
UID_RE = re.compile(r"^\d{5,}$")

# ===================== DATABASE =====================
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS allowed(
            user_id INTEGER PRIMARY KEY,
            role TEXT CHECK(role IN ('admin','user')) NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles(
            uid TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            name TEXT,
            last_status TEXT CHECK(last_status IN ('LIVE','DIE'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions(
            chat_id INTEGER NOT NULL,
            uid TEXT NOT NULL,
            note TEXT,
            customer TEXT,
            kind TEXT,
            PRIMARY KEY(chat_id, uid),
            FOREIGN KEY(uid) REFERENCES profiles(uid) ON DELETE CASCADE
        )
        """
    )
    # Safe migrations for databases created by older versions.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(subscriptions)").fetchall()]
    for column in ("note", "customer", "kind"):
        if column not in cols:
            conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {column} TEXT")
    return conn


def seed_allowed_from_env():
    conn = db()
    for user_id in OWNER_IDS_SEED:
        conn.execute(
            "INSERT OR REPLACE INTO allowed(user_id, role) VALUES(?, 'admin')",
            (user_id,),
        )
    for user_id in USER_IDS_SEED:
        existing = conn.execute(
            "SELECT role FROM allowed WHERE user_id=?", (user_id,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT OR REPLACE INTO allowed(user_id, role) VALUES(?, 'user')",
                (user_id,),
            )
    conn.commit()
    conn.close()


def get_role(user_id: int | None) -> str | None:
    if user_id is None:
        return None
    conn = db()
    row = conn.execute(
        "SELECT role FROM allowed WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def grant_role(user_id: int, role: str):
    role = "admin" if role == "admin" else "user"
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO allowed(user_id, role) VALUES(?, ?)",
        (user_id, role),
    )
    conn.commit()
    conn.close()


def revoke_user(user_id: int):
    conn = db()
    conn.execute("DELETE FROM allowed WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# ===================== TRACKING HELPERS =====================
def add_subscription(
    chat_id: int,
    uid: str,
    url: str,
    note: str | None = None,
    customer: str | None = None,
    kind: str | None = "profile",
):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO profiles(uid,url) VALUES(?,?)", (uid, url))
    conn.execute(
        """
        INSERT OR IGNORE INTO subscriptions(chat_id,uid,note,customer,kind)
        VALUES(?,?,?,?,?)
        """,
        (chat_id, uid, note, customer, kind),
    )
    if note is not None:
        conn.execute(
            "UPDATE subscriptions SET note=? WHERE chat_id=? AND uid=?",
            (note, chat_id, uid),
        )
    if customer is not None:
        conn.execute(
            "UPDATE subscriptions SET customer=? WHERE chat_id=? AND uid=?",
            (customer, chat_id, uid),
        )
    if kind is not None:
        conn.execute(
            "UPDATE subscriptions SET kind=? WHERE chat_id=? AND uid=?",
            (kind, chat_id, uid),
        )
    conn.commit()
    conn.close()


def set_profile_status(uid: str, name: str | None, status: str):
    conn = db()
    conn.execute(
        "UPDATE profiles SET name=COALESCE(?,name), last_status=? WHERE uid=?",
        (name, status, uid),
    )
    conn.commit()
    conn.close()


def list_subs(chat_id: int):
    conn = db()
    rows = conn.execute(
        """
        SELECT p.uid, COALESCE(p.name,''), COALESCE(p.last_status,''), p.url,
               COALESCE(s.note,''), COALESCE(s.customer,''), COALESCE(s.kind,'profile')
        FROM subscriptions s JOIN profiles p ON s.uid=p.uid
        WHERE s.chat_id=? ORDER BY p.uid
        """,
        (chat_id,),
    ).fetchall()
    conn.close()
    return rows


def remove_subscription(chat_id: int, uid: str):
    conn = db()
    conn.execute(
        "DELETE FROM subscriptions WHERE chat_id=? AND uid=?", (chat_id, uid)
    )
    conn.commit()
    conn.close()


def get_all_uids():
    conn = db()
    rows = conn.execute(
        "SELECT uid, url, COALESCE(name,''), COALESCE(last_status,'') FROM profiles"
    ).fetchall()
    conn.close()
    return rows


def subscribers_of(uid: str):
    conn = db()
    rows = [
        row[0]
        for row in conn.execute(
            "SELECT chat_id FROM subscriptions WHERE uid=?", (uid,)
        ).fetchall()
    ]
    conn.close()
    return rows


def get_subscription_details(chat_id: int, uid: str):
    conn = db()
    row = conn.execute(
        "SELECT COALESCE(note,''), COALESCE(customer,'') FROM subscriptions WHERE chat_id=? AND uid=?",
        (chat_id, uid),
    ).fetchone()
    conn.close()
    return row or ("", "")


# ===================== FACEBOOK STATUS CHECK =====================
def normalize_target(value: str):
    value = value.strip()
    if value.startswith("http"):
        parsed = urlparse(value)
        if "facebook.com" not in parsed.netloc.lower():
            raise ValueError("This is not a valid Facebook link.")
        query = parse_qs(parsed.query)
        if "id" in query and query["id"][0].isdigit():
            uid = query["id"][0]
            return uid, f"https://mbasic.facebook.com/profile.php?id={uid}"
        slug = parsed.path.strip("/").split("/")[0]
        if not slug:
            raise ValueError("Could not extract a UID or username from the link.")
        return slug, f"https://mbasic.facebook.com/{slug}"

    uid = value
    if not re.match(r"^[A-Za-z0-9.\-_]+$", uid):
        raise ValueError("Invalid UID or username.")
    if UID_RE.match(uid):
        return uid, f"https://mbasic.facebook.com/profile.php?id={uid}"
    return uid, f"https://mbasic.facebook.com/{uid}"


def _try_fetch(url: str, headers: dict, timeout: int):
    try:
        response = requests.get(
            url, headers=headers, timeout=timeout, allow_redirects=True
        )
        final_url = response.url.lower()
        if response.status_code in (404, 410):
            return "DIE", None, final_url

        text_lower = response.text.lower()
        if any(phrase in text_lower for phrase in DEAD_PHRASES):
            return "DIE", None, final_url

        soup = BeautifulSoup(response.text, "html.parser")
        name = None
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            name = og["content"].strip()
        if not name and soup.title and soup.title.text:
            title = soup.title.text.strip()
            low = title.lower()
            if all(word not in low for word in ("facebook", "log in")):
                name = title
        return "LIVE", name, final_url
    except Exception:
        return None, None, url


def fetch_status_and_name(url: str, timeout: int = 20):
    status, name, _ = _try_fetch(url, HEADERS, timeout)
    if status is not None:
        return status, name

    alt = (
        url.replace("mbasic.facebook", "m.facebook")
        if "mbasic.facebook" in url
        else url.replace("m.facebook", "mbasic.facebook")
    )
    status, name, _ = _try_fetch(alt, HEADERS, timeout)
    if status is not None:
        return status, name

    crawler_headers = {
        **HEADERS,
        "User-Agent": "facebookexternalhit/1.1 (+https://www.facebook.com/externalhit_uatext.php)",
    }
    alt2 = alt.replace("m.facebook", "www.facebook").replace(
        "mbasic.facebook", "www.facebook"
    )
    status, name, _ = _try_fetch(alt2, crawler_headers, timeout)
    if status is not None:
        return status, name

    return None, None


# ===================== TELEGRAM UI =====================
HELP = (
    "✨ *FB Watch Bot*\n"
    "/add – Add step by step (UID → Type → Note → Customer)\n"
    "/add <uid/url> | <note> | <customer> | <profile|group> – Quick add\n"
    "/list – View tracked UIDs and check their current status\n"
    "/remove <uid> – Stop tracking a UID\n"
    "/myid – View your Telegram user ID and permission\n"
    "\n*Admin only*: /grant <user_id> [user|admin], /revoke <user_id>, /who\n"
)


def line_box():
    return "____________________________"


def card_added(uid, note, customer, kind, added_when, status, url):
    status_icon = "🟢 LIVE" if status == "LIVE" else "🔴 DIE"
    kind_display = "Profile/Page" if (kind or "profile") == "profile" else "Group"
    return (
        "🆕 *New UID added!*\n"
        f"{line_box()}\n"
        f"🪪 *UID*: [{uid}]({url})\n"
        f"📂 *Type*: {kind_display}\n"
        f"📝 *Note*: {html.escape(note or '—')}\n"
        f"🙍 *Customer*: {html.escape(customer or '—')}\n"
        f"📌 *Date added*: {added_when}\n"
        f"📟 *Current status*: {status_icon}\n"
        f"{line_box()}"
    )


def card_alert(uid, note, customer, url, old, new):
    arrow = "🔴 DIE → 🟢 LIVE" if new == "LIVE" else "🟢 LIVE → 🔴 DIE"
    headline = "🚀 *UID is LIVE again!*" if new == "LIVE" else "☠️ *UID has DIED!*"
    return (
        f"{headline}\n"
        f"{line_box()}\n"
        f"🪪 *UID*: [{uid}]({url})\n"
        f"📝 *Note*: {html.escape(note or '—')}\n"
        f"🙍 *Customer*: {html.escape(customer or '—')}\n"
        f"📟 *Status*: {arrow}\n"
        f"⏰ *Time*: {now_iso()}\n"
        f"{line_box()}"
    )


# ===================== ERROR HANDLER =====================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        LOGGER.warning("Ignoring polling conflict: %s", context.error)
        return

    error = context.error
    if error is None:
        return
    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    snapshot = repr(update)[:2000] if update is not None else "None"
    LOGGER.error("Unhandled exception in handler\n%s\nUpdate snapshot: %s", tb, snapshot)

    if OWNER_IDS_SEED:
        try:
            message = (
                f"⚠️ Unhandled error:\n`{type(error).__name__}` – {error}\n"
                "```\n" + tb[-3500:] + "\n```"
            )
            await context.bot.send_message(
                OWNER_IDS_SEED[0], message, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as notify_error:
            LOGGER.info("Failed to notify admin: %s", notify_error)


# ===================== ACCESS GUARD =====================
def guard(require_admin: bool = False):
    """Return a normal decorator that wraps an async Telegram callback.

    Important: the decorator itself must be synchronous. Making the decorator
    `async def` returns a coroutine at import time, which causes Python-Telegram-
    Bot to fail later with: TypeError: 'coroutine' object is not callable.
    """

    def decorator(func):
        @wraps(func)
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            user_id = user.id if user else None
            if user_id is None:
                return

            role = get_role(user_id)
            if require_admin:
                if role != "admin":
                    await update.effective_message.reply_text(
                        "⛔ This command is for *admins* only.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
            elif role not in ("admin", "user"):
                await update.effective_message.reply_text(
                    "❌ You have not been granted permission to use this bot.\n"
                    "👉 Type */myid* to get your ID, then send it to the admin to grant access.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            return await func(update, context, *args, **kwargs)

        return wrapped

    return decorator


# ===================== COMMANDS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    role = get_role(user_id)
    if role in ("admin", "user"):
        await update.effective_message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.effective_message.reply_text(
            "❌ You have not been granted permission to use this bot.\n"
            "👉 Type */myid* to get your ID, then send it to the admin to grant access.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    role = get_role(user_id)
    await update.effective_message.reply_text(
        f"🪪 *Your ID:* `{user_id}`\n"
        f"🔑 *Current permission:* {role if role else 'No permission granted'}",
        parse_mode=ParseMode.MARKDOWN,
    )


@guard(require_admin=True)
async def grant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Usage: /grant <user_id> [user|admin]")
        return
    try:
        target = int(context.args[0])
        role = context.args[1].lower() if len(context.args) > 1 else "user"
        grant_role(target, role)
        await update.effective_message.reply_text(
            f"✅ Granted *{role}* to `{target}`", parse_mode=ParseMode.MARKDOWN
        )
    except Exception as error:
        await update.effective_message.reply_text(f"❌ {error}")


@guard(require_admin=True)
async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Usage: /revoke <user_id>")
        return
    try:
        target = int(context.args[0])
        revoke_user(target)
        await update.effective_message.reply_text(
            f"🗑️ Revoked permission from `{target}`", parse_mode=ParseMode.MARKDOWN
        )
    except Exception as error:
        await update.effective_message.reply_text(f"❌ {error}")


@guard(require_admin=True)
async def who_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute(
        "SELECT user_id, role FROM allowed ORDER BY role DESC, user_id"
    ).fetchall()
    conn.close()
    if not rows:
        await update.effective_message.reply_text("No one has been granted permission yet.")
        return
    lines = [f"- `{row[0]}` → *{row[1]}*" for row in rows]
    await update.effective_message.reply_text(
        "👥 *Permission list:*\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


def parse_inline_add(text: str):
    parts = [part.strip() for part in text.split("|")]
    target = parts[0]
    note = parts[1] if len(parts) > 1 and parts[1] else None
    customer = parts[2] if len(parts) > 2 and parts[2] else None
    kind = parts[3].lower() if len(parts) > 3 and parts[3] else "profile"
    if kind not in ("profile", "group"):
        kind = "profile"
    return target, note, customer, kind


@guard()
async def add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        raw = " ".join(context.args)
        try:
            target, note, customer, kind = parse_inline_add(raw)
            uid, url = normalize_target(target)
            status, name = fetch_status_and_name(url)
            if status is None:
                status = "DIE"
            add_subscription(update.effective_chat.id, uid, url, note, customer, kind)
            set_profile_status(uid, name, status)
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔗 Open Facebook", url=url)],
                    [InlineKeyboardButton("🛑 Stop tracking this UID", callback_data=f"stop:{uid}")],
                ]
            )
            await update.effective_message.reply_text(
                card_added(uid, note, customer, kind, now_iso(), status, url),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception as error:
            await update.effective_message.reply_text(f"❌ {error}")
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "➕ *Please enter the UID or URL you want to track:*",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["add"] = {}
    return ADD_UID


@guard()
async def add_got_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    try:
        uid, url = normalize_target(text)
    except Exception as error:
        await update.effective_message.reply_text(
            f"❌ {error}\nPlease enter the UID or URL again."
        )
        return ADD_UID

    context.user_data["add"]["uid"] = uid
    context.user_data["add"]["url"] = url
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("👤 Profile/Page", callback_data="type:profile"),
            InlineKeyboardButton("👥 Group", callback_data="type:group"),
        ]]
    )
    await update.effective_message.reply_text(
        f"📌 *Choose the UID type for* `{uid}`:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )
    return ADD_TYPE


@guard()
async def add_pick_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind = "profile" if (query.data or "") != "type:group" else "group"
    context.user_data["add"]["kind"] = kind
    await query.message.reply_text(
        f"✍️ *Enter a note for UID* `{context.user_data['add'].get('uid')}`\n"
        "_Example: unlock 282_",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADD_NOTE


@guard()
async def add_got_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add"]["note"] = (update.effective_message.text or "").strip()
    uid = context.user_data["add"].get("uid")
    await update.effective_message.reply_text(
        f"📝 *Enter a customer name for UID* `{uid}`\n_Example: Customer A_",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADD_CUSTOMER


@guard()
async def add_got_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add"]["customer"] = (
        update.effective_message.text or ""
    ).strip()
    info = context.user_data.get("add", {})
    uid, url = info.get("uid"), info.get("url")
    note, customer = info.get("note"), info.get("customer")
    kind = info.get("kind", "profile")

    status, name = fetch_status_and_name(url)
    if status is None:
        status = "DIE"

    add_subscription(update.effective_chat.id, uid, url, note, customer, kind)
    set_profile_status(uid, name, status)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Open Facebook", url=url)],
            [InlineKeyboardButton("🛑 Stop tracking this UID", callback_data=f"stop:{uid}")],
        ]
    )
    await update.effective_message.reply_text(
        card_added(uid, note, customer, kind, now_iso(), status, url),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    context.user_data.pop("add", None)
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("add", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


@guard()
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_subs(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("No UIDs yet. Use /add to get started.")
        return

    for uid, _, previous_status, url, note, customer, kind in rows:
        status, name = fetch_status_and_name(url)
        if status is None:
            status = previous_status if previous_status else "DIE"
        set_profile_status(uid, name, status)

        status_icon = "🟢 LIVE" if status == "LIVE" else "🔴 DIE"
        kind_display = "Profile/Page" if (kind or "profile") == "profile" else "Group"
        text = (
            f"{line_box()}\n"
            f"🪪 *UID*: [{uid}]({url})\n"
            f"📂 *Type*: {kind_display}\n"
            f"📝 *Note*: {html.escape(note or '—')}\n"
            f"🙍 *Customer*: {html.escape(customer or '—')}\n"
            f"📟 *Status*: {status_icon}\n"
            f"{line_box()}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔗 Open Facebook", url=url)],
                [InlineKeyboardButton("🗑️ Delete this UID", callback_data=f"del:{uid}")],
            ]
        )
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        await asyncio.sleep(0.35)


@guard()
async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Usage: /remove <uid>")
        return
    uid = context.args[0].strip()
    remove_subscription(update.effective_chat.id, uid)
    await update.effective_message.reply_text(f"🗑️ Stopped tracking {uid}")


@guard()
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = query.message.chat.id if query.message else None

    if data.startswith("stop:") or data.startswith("del:"):
        uid = data.split(":", 1)[1]
        if chat_id is not None:
            remove_subscription(chat_id, uid)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"🛑 Stopped tracking UID {uid}")


# ===================== BACKGROUND POLLER =====================
async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    application = context.application
    for uid, url, previous_name, previous_status in get_all_uids():
        try:
            status, name = fetch_status_and_name(url)
            if status is None:
                continue

            if previous_status != status:
                set_profile_status(uid, name, status)
                for chat_id in subscribers_of(uid):
                    note, customer = get_subscription_details(chat_id, uid)
                    text = card_alert(
                        uid,
                        note,
                        customer,
                        url,
                        previous_status or "Unknown",
                        status,
                    )
                    keyboard = InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("🔗 Open Facebook", url=url)],
                            [InlineKeyboardButton("🛑 Stop tracking this UID", callback_data=f"stop:{uid}")],
                        ]
                    )
                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True,
                        reply_markup=keyboard,
                    )
            elif name and name != previous_name:
                set_profile_status(uid, name, status)
        except Exception:
            LOGGER.exception("Failed to check UID %s", uid)
        await asyncio.sleep(0.6)


# ===================== WEB SERVICE =====================
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        LOGGER.info("Web: " + fmt, *args)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body, content_type="text/plain; charset=utf-8", status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as file:
                    body = file.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self._send_text("Web dashboard is not installed.", status=500)
            return

        if path == "/healthz":
            self._send_json({"ok": True, "service": "FBWatchBot"})
            return

        if path == "/api/uids":
            rows = get_all_uids()
            self._send_json(
                {
                    "count": len(rows),
                    "uids": [
                        {
                            "uid": uid,
                            "url": url,
                            "name": name,
                            "status": status or "UNKNOWN",
                        }
                        for uid, url, name, status in rows
                    ],
                }
            )
            return

        if path == "/api/check":
            query = parse_qs(parsed.query)
            value = (query.get("uid") or query.get("url") or [""])[0].strip()
            if not value:
                self._send_json({"error": "Missing uid or url parameter."}, status=400)
                return
            try:
                uid, url = normalize_target(value)
                status, name = fetch_status_and_name(url)
                self._send_json(
                    {
                        "uid": uid,
                        "url": url,
                        "name": name,
                        "status": status or "UNKNOWN",
                        "checked_at": now_iso(),
                    }
                )
            except ValueError as error:
                self._send_json({"error": str(error)}, status=400)
            except Exception as error:
                LOGGER.exception("Web UID check failed")
                self._send_json({"error": str(error)}, status=500)
            return

        self._send_json({"error": "Not found."}, status=404)


def run_web_server():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), WebHandler)
    LOGGER.info("Web server listening on port %s", port)
    server.serve_forever()


# ===================== MAIN =====================
def main():
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing or invalid. Set it in Render environment variables.")

    seed_allowed_from_env()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_error_handler(error_handler)

    application.add_handler(CommandHandler(["start"], start))
    application.add_handler(CommandHandler(["help"], help_cmd))
    application.add_handler(CommandHandler(["myid"], myid))
    application.add_handler(CommandHandler(["grant"], grant_cmd))
    application.add_handler(CommandHandler(["revoke"], revoke_cmd))
    application.add_handler(CommandHandler(["who"], who_cmd))

    add_conversation = ConversationHandler(
        entry_points=[CommandHandler(["add"], add_entry)],
        states={
            ADD_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_uid)],
            ADD_TYPE: [CallbackQueryHandler(add_pick_type, pattern=r"^type:")],
            ADD_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_note)],
            ADD_CUSTOMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_customer)],
        },
        fallbacks=[CommandHandler(["cancel"], add_cancel)],
        allow_reentry=True,
    )
    application.add_handler(add_conversation)
    application.add_handler(CommandHandler(["list"], list_cmd))
    application.add_handler(CommandHandler(["remove"], remove_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Use python-telegram-bot's own JobQueue so the poller runs on the same
    # asyncio event loop as the bot instead of creating tasks from another thread.
    if application.job_queue is None:
        raise RuntimeError("JobQueue is unavailable. Make sure APScheduler is installed.")
    application.job_queue.run_repeating(
        poll_job,
        interval=CHECK_INTERVAL_SEC,
        first=10,
        name="uid-status-poller",
    )

    threading.Thread(target=run_web_server, daemon=True).start()
    LOGGER.info("FBWatchBot is starting...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    db().close()
    main()
