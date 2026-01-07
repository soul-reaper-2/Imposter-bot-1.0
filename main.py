#=========================================================
# IMPORTS & SETUP
# =========================================================
import os
import random
import asyncio
import sqlite3
from typing import Optional, List, Dict

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------- Config ----------
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

# Bot owners (comma-separated user ids) can bypass admin checks and access owner DM menu
BOT_OWNERS = set()
owners_env = os.getenv("BOT_OWNERS", "")
if owners_env:
    for part in owners_env.split(","):
        part = part.strip()
        if part.isdigit():
            BOT_OWNERS.add(int(part))

# Single DB file as required
DB_PATH = "words.db"

# Default categories that must exist
DEFAULT_CATEGORIES = ["Famous People", "Animals", "Objects"]

# Defaults
DEFAULTS = {
    "turn_length": 40,
    "vote_length": 60,
    "rounds": 5,
    "max_players": 10,
}

# In-memory games and owner menu states
games: Dict[int, dict] = {}  # chat_id -> game state
owner_states: Dict[int, dict] = {}  # owner_id -> state dict, used for owner DM flows

# --- SQLite connection ---
_conn: Optional[sqlite3.Connection] = None


def get_db_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # Create DB file if it doesn't exist: sqlite3.connect will create the file
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


# default sample words (used to seed a newly created DB and for reset)
_DEFAULT_SAMPLES = {
    "Famous People": [
        ("Albert Einstein", "relativity"),
        ("Cleopatra", "egyptian queen"),
        ("Shakespeare", "playwright"),
    ],
    "Animals": [
        ("Cat", "meows"),
        ("Elephant", "big ears"),
        ("Dolphin", "intelligent swimmer"),
    ],
    "Objects": [
        ("Chair", "sit"),
        ("Phone", "call"),
        ("Key", "opens"),
    ],
}


def init_db_and_defaults():
    """
    Ensure words.db exists and has the required schema and default categories.
    If the DB didn't exist before, create it and insert the three default categories and sample words for testing.
    If it exists, verify that the three default categories exist and insert any missing.
    """
    newly_created = not os.path.exists(DB_PATH)
    conn = get_db_conn()
    cur = conn.cursor()

    # Create tables
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            clue TEXT NOT NULL,
            FOREIGN KEY(category_id) REFERENCES categories(id)
        )
        """
    )
    # per-chat settings
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            turn_length INTEGER,
            vote_length INTEGER,
            rounds INTEGER,
            max_players INTEGER
        )
        """
    )
    # which categories are enabled for a chat
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_categories (
            chat_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, category_id),
            FOREIGN KEY(category_id) REFERENCES categories(id)
        )
        """
    )
    # users table for broadcasts (tracks users who interacted with the bot)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT
        )
        """
    )
    # groups table for group broadcasts (stores group ids where bot was used)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            username TEXT
        )
        """
    )
    # channels (force subscription list)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier TEXT UNIQUE NOT NULL
        )
        """
    )
    conn.commit()

    # Ensure default categories exist
    for name in DEFAULT_CATEGORIES:
        cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    conn.commit()

    # If newly created, insert sample words for testing
    if newly_created:
        cur.execute("SELECT id, name FROM categories")
        rows = {r["name"]: r["id"] for r in cur.fetchall()}
        for cat_name, entries in _DEFAULT_SAMPLES.items():
            cat_id = rows.get(cat_name)
            if not cat_id:
                continue
            for w, clue in entries:
                cur.execute("INSERT INTO words (category_id, word, clue) VALUES (?, ?, ?)", (cat_id, w, clue))
        conn.commit()


# =========================================================
# Settings & DB helpers
# =========================================================
def get_settings(chat_id: int):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT turn_length, vote_length, rounds, max_players FROM settings WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    if not row:
        return dict(DEFAULTS)
    return {
        "turn_length": row["turn_length"] if row["turn_length"] is not None else DEFAULTS["turn_length"],
        "vote_length": row["vote_length"] if row["vote_length"] is not None else DEFAULTS["vote_length"],
        "rounds": row["rounds"] if row["rounds"] is not None else DEFAULTS["rounds"],
        "max_players": row["max_players"] if row["max_players"] is not None else DEFAULTS["max_players"],
    }


def set_setting(chat_id: int, key: str, value: int):
    if key not in ("turn_length", "vote_length", "rounds", "max_players"):
        raise ValueError("Invalid setting")
    conn = get_db_conn()
    cur = conn.cursor()
    # Upsert: ensure row exists and update the column
    cur.execute("INSERT OR IGNORE INTO settings (chat_id) VALUES (?)", (chat_id,))
    cur.execute(f"UPDATE settings SET {key} = ? WHERE chat_id = ?", (value, chat_id))
    conn.commit()


def get_all_categories() -> List[sqlite3.Row]:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM categories ORDER BY name COLLATE NOCASE")
    return cur.fetchall()


def get_enabled_category_ids(chat_id: int) -> List[int]:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT category_id FROM chat_categories WHERE chat_id = ?", (chat_id,))
    return [r[0] for r in cur.fetchall()]


def enable_category_for_chat(chat_id: int, category_id: int):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO chat_categories (chat_id, category_id) VALUES (?, ?)", (chat_id, category_id))
    conn.commit()


def disable_category_for_chat(chat_id: int, category_id: int):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM chat_categories WHERE chat_id = ? AND category_id = ?", (chat_id, category_id))
    conn.commit()


def get_users() -> List[sqlite3.Row]:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username FROM users")
    return cur.fetchall()


def add_user(user_id: int, username: Optional[str]):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()


def get_groups() -> List[sqlite3.Row]:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT chat_id, title, username FROM groups")
    return cur.fetchall()


def add_group(chat_id: int, title: Optional[str], username: Optional[str]):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO groups (chat_id, title, username) VALUES (?, ?, ?)", (chat_id, title, username))
    conn.commit()


def get_channels() -> List[sqlite3.Row]:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, identifier FROM channels")
    return cur.fetchall()


def add_channel(identifier: str):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO channels (identifier) VALUES (?)", (identifier,))
    conn.commit()


def remove_channel_row(row_id: int):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM channels WHERE id = ?", (row_id,))
    conn.commit()


def pick_word_for_chat(chat_id: int):
    conn = get_db_conn()
    cur = conn.cursor()
    enabled = get_enabled_category_ids(chat_id)
    if enabled:
        placeholders = ",".join("?" for _ in enabled)
        cur.execute(f"SELECT id, word, clue FROM words WHERE category_id IN ({placeholders}) ORDER BY RANDOM() LIMIT 1", tuple(enabled))
        row = cur.fetchone()
        if row:
            return row["word"], row["clue"]
    # fallback to any word
    cur.execute("SELECT word, clue FROM words ORDER BY RANDOM() LIMIT 1")
    row = cur.fetchone()
    if row:
        return row["word"], row["clue"]
    raise RuntimeError("No words available in the database.")


# =========================================================
# Utilities
# =========================================================
def generate_game_id():
    return random.randint(1000, 9999)


def word_count(text: str):
    return len(text.strip().split())


def get_player_display(game: dict, user_id: int) -> str:
    p = game["players"].get(user_id)
    if not p:
        return "unknown"
    return p.get("username") or p.get("name")


def get_player_mention(game: dict, user_id: int) -> str:
    p = game["players"].get(user_id)
    if not p:
        return "unknown"
    if p.get("username"):
        return f"@{p['username']}"
    return p.get("name")


async def is_admin_or_owner(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    if user_id in BOT_OWNERS:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in ["administrator", "creator"]


# =========================================================
# Force subscription check - applies to all users except BOT_OWNERS
# =========================================================
async def ensure_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Returns True if the user is allowed (subscribed to all required channels or owner bypass).
    If not subscribed, sends a message with inline buttons linking to required channels and returns False.
    """
    # bot owners bypass
    user = None
    if update.message:
        user = update.message.from_user
    elif update.callback_query:
        user = update.callback_query.from_user
    else:
        return True

    if user.id in BOT_OWNERS:
        return True

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT identifier FROM channels")
    rows = [r["identifier"] for r in cur.fetchall()]
    if not rows:
        return True  # no channels required

    missing = []
    for chan in rows:
        try:
            # try to get user membership in channel
            # the identifier can be @channelusername or numeric id
            chat = await context.bot.get_chat(chan)
            mem = await context.bot.get_chat_member(chat.id, user.id)
            if mem.status in ["left", "kicked"]:
                missing.append(chan)
        except Exception:
            # treat as not subscribed if any error
            missing.append(chan)

    if missing:
        # send message with inline buttons linking to channels
        buttons = []
        for chan in missing:
            try:
                # ensure url format for t.me
                label = chan
                url = f"https://t.me/{chan.lstrip('@')}" if chan.startswith("@") else f"https://t.me/{chan}"
                buttons.append([InlineKeyboardButton(str(label), url=url)])
            except Exception:
                pass
        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
        text = "Please subscribe to the update channel(s) to use the bot."
        if update.message:
            await update.message.reply_text(text, reply_markup=reply_markup)
        else:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup)
        return False
    return True


# =========================================================
# Record users/groups when they interact (used for broadcasts)
# =========================================================
def record_user_and_group(update: Update):
    if update.message is None:
        return
    chat = update.message.chat
    user = update.message.from_user
    if chat.type == "private":
        add_user(user.id, user.username)
    else:
        # group or supergroup
        add_group(chat.id, chat.title or "", getattr(chat, "username", None))


# =========================================================
# GAME CREATION (/game)
# =========================================================
async def game_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    if not await ensure_subscribed(update, context):
        return
    record_user_and_group(update)

    chat = update.message.chat
    user = update.message.from_user

    if chat.type == "private":
        return

    member = await context.bot.get_chat_member(chat.id, user.id)
    if member.status not in ["administrator", "creator"] and user.id not in BOT_OWNERS:
        return

    if chat.id in games:
        await update.message.reply_text("A game already exists in this group.")
        return

    init_db_and_defaults()

    game_id = generate_game_id()
    join_link = f"https://t.me/{context.bot.username}?start=join_{game_id}"

    msg = await update.message.reply_text(
        "🎮 New game created!\n\nPlayers:\n\nSend /start when everyone joined",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join", url=join_link)]]),
    )

    games[chat.id] = {
        "game_id": game_id,
        "creator": user.id,
        "message_id": msg.message_id,
        "players": {},
        "state": "waiting",
        "turn_order": [],
        "current_turn": 0,
        "current_round": 1,
        "turn_task": None,
        "impostor": None,
        "word": None,
        "clue": None,
        "vote_message_id": None,
        "vote_selections": {},
        "votes": {},
        "done_votes": set(),
        "settings_at_start": None,
    }


# =========================================================
# /start (GROUP + PRIVATE DEEPLINK ROUTER)
# =========================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    # --- NEW: show owner menu when owner sends /start in private with no args ---
    chat = update.message.chat
    user = update.message.from_user
    if chat.type == "private" and not context.args and user.id in BOT_OWNERS:
        # initialize DB and owner tracking like owner_dm_handler does
        init_db_and_defaults()
        add_user(user.id, user.username)
        owner_states[user.id] = {"state": OWNER_MAIN}
        keyboard = [["Broadcast", "Settings"]]
        await update.message.reply_text("Owner Menu", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True))
        return
    # --- END NEW ---

    if not await ensure_subscribed(update, context):
        return
    record_user_and_group(update)

    chat = update.message.chat

    # ---------- PRIVATE (DEEPLINKS) ----------
    if chat.type == "private":
        if not context.args:
            return

        payload = context.args[0]

        if payload.startswith("join_"):
            await handle_join(update, context, int(payload.split("_")[1]))
        elif payload.startswith("role_"):
            await handle_role(update, context, int(payload.split("_")[1]))
        elif payload.startswith("vote_"):
            await handle_vote(update, context, int(payload.split("_")[1]))
        return

    # ---------- GROUP (START GAME) ----------
    if chat.id not in games:
        return

    game = games[chat.id]

    if game["state"] != "waiting":
        return

    if len(game["players"]) < 3:
        await update.message.reply_text("Minimum 3 players required.")
        return

    # Start game - snapshot settings
    game["state"] = "playing"
    game["turn_order"] = list(game["players"].keys())
    random.shuffle(game["turn_order"])

    game["impostor"] = random.choice(game["turn_order"])
    try:
        game["word"], game["clue"] = pick_word_for_chat(chat.id)
    except RuntimeError as e:
        await update.message.reply_text(f"Cannot start game: {e}")
        del games[chat.id]
        return

    for pid in game["players"].keys():
        game["vote_selections"].setdefault(pid, None)
        game["votes"].setdefault(pid, None)
    game["settings_at_start"] = get_settings(chat.id)

    role_link = f"https://t.me/{context.bot.username}?start=role_{game['game_id']}"

    await update.message.reply_text(
        "🎭 Game started!\nClick below to see your role.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("See your role", url=role_link)]]),
    )

    await start_turn(context, chat.id)
