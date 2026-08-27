"""Legacy Telegram bot implementation kept for reference.

The Render deployment uses tele_fb_monitor.py. This file is not started by the
current Procfile and is retained only for compatibility with older setups.
"""

import os
import re
import time
from datetime import datetime

import requests
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

try:
    from check_live_sync import check_live
except ImportError:
    def check_live(uid):
        return "unknown"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("Please set BOT_TOKEN in the BOT_TOKEN environment variable.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
tracking = {}

GREEN = "🟢"
RED = "🔴"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"

MENU_TEXT = (
    "<b>Supported commands</b>\n\n"
    "/start - Start the bot\n"
    "/add - Add a new UID\n"
    "/addbulk - Add multiple UIDs\n"
    "/remove - Stop tracking a UID\n"
    "/list - View tracked UIDs\n"
    "/help - Usage guide\n"
    "/menu - Show command menu\n"
    "/getuid - Get a UID from a Facebook link\n\n"
    "<i>Examples:</i>\n"
    "• /add <code>&lt;uid&gt; [note] [customer]</code>\n"
    "• /addbulk followed by one UID per line: <code>uid,note,customer</code>\n"
    "• /remove <code>&lt;uid&gt;</code>\n"
    "• /getuid <code>&lt;facebook_link&gt;</code>\n"
)


def build_card(uid: str, note: str = "unlock", customer: str = "Customer"):
    status = check_live(uid)
    dot = GREEN if status == "live" else RED
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        "🆕 <b>UID added/updated!</b>\n\n"
        f"🆔 <b>UID:</b> <a href=\"https://facebook.com/{uid}\">{uid}</a>\n"
        "📄 <b>Type:</b> Profile/Page\n"
        f"📝 <b>Note:</b> {note}\n"
        f"👤 <b>Customer:</b> {customer}\n"
        f"📅 <b>Date added:</b> {now}\n"
        f"✅ <b>Current status:</b> {dot} {status.upper()}"
    )
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(InlineKeyboardButton("🌐 Open Facebook", url=f"https://facebook.com/{uid}"))
    following = tracking.get(uid, {}).get("following", True)
    if following:
        keyboard.add(
            InlineKeyboardButton("🟢 Continue tracking", callback_data=f"noop:{uid}"),
            InlineKeyboardButton("🛑 Stop tracking", callback_data=f"stop:{uid}"),
        )
    else:
        keyboard.add(InlineKeyboardButton("✅ Resume tracking", callback_data=f"start:{uid}"))
    return text, keyboard


def extract_uid_from_link(link: str, timeout: float = 10.0):
    match = re.search(r"[?&]id=(\d{5,})", link)
    if match:
        return match.group(1)

    match = re.search(r"facebook\.com/(?:profile\.php\?id=)?(\d{7,})", link)
    if match:
        return match.group(1)

    match = re.search(r"facebook\.com/([A-Za-z0-9.\-_]+)/?", link)
    if match:
        username = match.group(1)
        if username.lower() in {"profile.php", "people", "pages"}:
            return None
        try:
            headers = {"User-Agent": USER_AGENT, "Connection": "keep-alive", "Accept": "*/*"}
            response = requests.get(
                f"https://graph.facebook.com/{username}",
                params={"fields": "id"},
                headers=headers,
                timeout=timeout,
            )
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            uid = str(data.get("id")) if isinstance(data, dict) else None
            if uid and uid.isdigit():
                return uid
        except Exception:
            pass
    return None


def ensure_tracked(uid: str, note="unlock", customer="Customer"):
    if uid not in tracking:
        tracking[uid] = {
            "note": note,
            "customer": customer,
            "added": int(time.time()),
            "following": True,
        }
    else:
        tracking[uid].update({
            "note": note or tracking[uid]["note"],
            "customer": customer or tracking[uid]["customer"],
        })


@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.reply_to(message, "👋 Hello! I am ready.\n\n" + MENU_TEXT)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.reply_to(message, MENU_TEXT)


@bot.message_handler(commands=["menu"])
def cmd_menu(message):
    bot.reply_to(message, MENU_TEXT)


@bot.message_handler(commands=["add", "them"])
def cmd_add(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Syntax: <code>/add &lt;uid&gt; [note] [customer]</code>")
        return
    uid = parts[1]
    note = parts[2] if len(parts) >= 3 else "unlock"
    customer = parts[3] if len(parts) >= 4 else "Customer"
    ensure_tracked(uid, note, customer)
    text, keyboard = build_card(uid, tracking[uid]["note"], tracking[uid]["customer"])
    bot.send_message(message.chat.id, text, reply_markup=keyboard, disable_web_page_preview=True)


@bot.message_handler(commands=["addbulk", "themnhg"])
def cmd_addbulk(message):
    payload = message.text.split(maxsplit=1)
    tail = payload[1] if len(payload) > 1 else ""
    lines = tail.strip().splitlines()
    if not lines and message.reply_to_message and message.reply_to_message.text:
        lines = message.reply_to_message.text.strip().splitlines()
    if not lines:
        bot.reply_to(
            message,
            "Send the list in this format:\n<code>/addbulk</code>\n<code>uid1,note1,customer1</code>\n<code>uid2</code>",
        )
        return

    results = []
    for raw in lines:
        if not raw.strip():
            continue
        parts = [part.strip() for part in raw.split(",")]
        uid = parts[0]
        note = parts[1] if len(parts) >= 2 and parts[1] else "unlock"
        customer = parts[2] if len(parts) >= 3 and parts[2] else "Customer"
        ensure_tracked(uid, note, customer)
        status = check_live(uid)
        results.append({"uid": uid, "status": status, "note": note, "customer": customer})

    summary = "\n".join(f"{item['uid']}: {item['status']}" for item in results)
    bot.reply_to(message, "<b>Added in bulk:</b>\n<code>" + summary + "</code>")
    for item in results[:20]:
        text, keyboard = build_card(item["uid"], item["note"], item["customer"])
        bot.send_message(message.chat.id, text, reply_markup=keyboard, disable_web_page_preview=True)


@bot.message_handler(commands=["remove", "xoa"])
def cmd_remove(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Syntax: <code>/remove &lt;uid&gt;</code>")
        return
    uid = parts[1]
    if tracking.pop(uid, None) is None:
        bot.reply_to(message, f"UID <code>{uid}</code> does not exist in the list.")
    else:
        bot.reply_to(message, f"Deleted UID <code>{uid}</code> from the tracking list.")


@bot.message_handler(commands=["list", "danhsach"])
def cmd_list(message):
    if not tracking:
        bot.reply_to(message, "The list is empty.")
        return
    lines = []
    for index, (uid, info) in enumerate(list(tracking.items())[:50], start=1):
        status = check_live(uid)
        dot = GREEN if status == "live" else RED
        lines.append(f"{index}. {uid} {dot} {status.upper()} | {info['note']} | {info['customer']}")
    bot.reply_to(message, "<b>Tracked UIDs (maximum 50):</b>\n" + "\n".join(lines))


@bot.message_handler(commands=["getuid"])
def cmd_getuid(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Syntax: <code>/getuid &lt;facebook_link&gt;</code>")
        return
    uid = extract_uid_from_link(parts[1].strip())
    if uid:
        bot.reply_to(message, f"UID found: <code>{uid}</code>")
    else:
        bot.reply_to(
            message,
            "Could not extract the UID from the link. Try a link such as "
            "<code>profile.php?id=...</code> or a public username.",
        )


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    try:
        action, uid = call.data.split(":", 1)
    except ValueError:
        bot.answer_callback_query(call.id)
        return

    if action == "stop":
        if uid in tracking:
            tracking[uid]["following"] = False
        bot.answer_callback_query(call.id, "Tracking stopped.")
    elif action == "start":
        if uid in tracking:
            tracking[uid]["following"] = True
        bot.answer_callback_query(call.id, "Tracking resumed.")
    else:
        bot.answer_callback_query(call.id)

    note = tracking.get(uid, {}).get("note", "unlock")
    customer = tracking.get(uid, {}).get("customer", "Customer")
    text, keyboard = build_card(uid, note, customer)
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=keyboard, disable_web_page_preview=True)


if __name__ == "__main__":
    print("FBWatchBot legacy implementation is running...")
    bot.infinity_polling(skip_pending=True, timeout=60)
        
