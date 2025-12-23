#=========================================================
# IMPORTS & SETUP
# =========================================================
import os
import random
import asyncio
import sqlite3
from typing import Optional, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# Bot owners (comma-separated user ids) can bypass admin checks
BOT_OWNERS = set()
owners_env = os.getenv("BOT_OWNERS", "")
if owners_env:
    for part in owners_env.split(","):
        part = part.strip()
        if part.isdigit():
            BOT_OWNERS.add(int(part))

# Single DB file as required
DB_PATH = "words.db"

# Defaults
DEFAULTS = {
    "turn_length": 40,
    "vote_length": 60,
    "rounds": 5,
    "max_players": 10,
}

# In-memory games
games = {}  # chat_id -> game state

# --- SQLite connection ---
_conn: Optional[sqlite3.Connection] = None


def get_db_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # Create DB file if it doesn't exist: sqlite3.connect will create file automatically.
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db_and_defaults():
    """
    Ensure words.db exists and has the required schema and default categories.
    If the DB didn't exist before, create it and insert the three default categories and sample words for testing.
    If it exists, verify that the three default categories are present and insert them if missing.
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
    conn.commit()

    # Ensure default categories exist
    default_cats = ["Famous People", "Animals", "Objects"]
    for name in default_cats:
        cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    conn.commit()

    # If newly created, insert some sample words for testing
    if newly_created:
        # fetch category ids
        cur.execute("SELECT id, name FROM categories")
        rows = {r['name']: r['id'] for r in cur.fetchall()}
        sample = {
            "Famous People": [
                ("Albert Einstein", "relativity"),
                ("Cleopatra", "Egyptian queen"),
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
        inserted = 0
        for cat_name, entries in sample.items():
            cat_id = rows.get(cat_name)
            if not cat_id:
                continue
            for w, clue in entries:
                cur.execute(
                    "INSERT INTO words (category_id, word, clue) VALUES (?, ?, ?)", (cat_id, w, clue)
                )
                inserted += 1
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
    # Use INSERT OR REPLACE to upsert settings
    cur.execute(
        "INSERT INTO settings (chat_id, %s) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET %s=excluded.%s" % (key, key, key),
        (chat_id, value),
    )
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


def pick_word_for_chat(chat_id: int):
    conn = get_db_conn()
    cur = conn.cursor()
    enabled = get_enabled_category_ids(chat_id)
    if enabled:
        placeholders = ",".join("?" for _ in enabled)
        cur.execute(
            f"SELECT id, word, clue FROM words WHERE category_id IN ({placeholders}) ORDER BY RANDOM() LIMIT 1",
            tuple(enabled),
        )
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
# GAME CREATION (/game)
# =========================================================
async def game_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
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
    # ensure DB and defaults
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
        # snapshot settings will be stored at start
        "settings_at_start": None,
    }


# =========================================================
# /start (GROUP + PRIVATE DEEPLINK ROUTER)
# =========================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat = update.message.chat
    # PRIVATE deeplinks
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
    # GROUP start
    if chat.id not in games:
        return
    game = games[chat.id]
    if game["state"] != "waiting":
        return
    if len(game["players"]) < 3:
        await update.message.reply_text("Minimum 3 players required.")
        return
    # Start game: snapshot settings
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


# =========================================================
# JOIN HANDLER
# =========================================================
async def handle_join(update: Update, context: ContextTypes.DEFAULT_TYPE, game_id: int):
    if update.message is None:
        return
    user = update.message.from_user
    for chat_id, game in games.items():
        if game["game_id"] == game_id:
            if game["state"] != "waiting":
                await update.message.reply_text("Game already started.")
                return
            # max players check
            settings = get_settings(chat_id)
            if len(game["players"]) >= settings.get("max_players", DEFAULTS["max_players"]):
                await update.message.reply_text("The game is full (max players reached).")
                return
            if user.id in game["players"]:
                await update.message.reply_text("You already joined.")
                return
            game["players"][user.id] = {"username": user.username, "name": user.full_name}
            await update.message.reply_text("✅ You joined the game.")
            players_text = "\n".join(f"• {get_player_mention(game, uid)}" for uid in game["players"].keys())
            join_link = f"https://t.me/{context.bot.username}?start=join_{game_id}"
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=game["message_id"],
                    text=("🎮 New game created!\n\nPlayers:\n" f"{players_text}\n\n" "Send /start when everyone joined"),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join", url=join_link)]]),
                )
            except Exception:
                pass
            return
    await update.message.reply_text("This game doesn't exist.")


# =========================================================
# ROLE HANDLER
# =========================================================
async def handle_role(update: Update, context: ContextTypes.DEFAULT_TYPE, game_id: int):
    if update.message is None:
        return
    user = update.message.from_user
    for game in games.values():
        if game["game_id"] == game_id:
            if user.id not in game["players"]:
                await update.message.reply_text("You are not in this game.")
                return
            if user.id == game["impostor"]:
                await update.message.reply_text(f"😈 You are the IMPOSTOR\n\nClue: {game['clue']}")
            else:
                await update.message.reply_text(f"✅ Your word is:\n\n{game['word']}")
            return
    await update.message.reply_text("This game doesn't exist.")


# =========================================================
# TURN SYSTEM
# =========================================================
async def start_turn(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game or game["state"] != "playing":
        return
    settings = game.get("settings_at_start", DEFAULTS)
    if game["current_round"] > settings.get("rounds", DEFAULTS["rounds"]):
        await start_voting(context, chat_id)
        return
    if not game["turn_order"]:
        await context.bot.send_message(chat_id=chat_id, text="No players remain. Ending game.")
        if chat_id in games:
            del games[chat_id]
        return
    current_player = game["turn_order"][game["current_turn"]]
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🕒 Round {game['current_round']}/{settings.get('rounds', DEFAULTS['rounds'])}\n"
            f"Your turn: {get_player_mention(game, current_player)}\n"
            f"You have {settings.get('turn_length', DEFAULTS['turn_length'])} seconds"
        ),
    )
    if game.get("turn_task"):
        try:
            game["turn_task"].cancel()
        except Exception:
            pass
        game["turn_task"] = None
    turn_len = settings.get("turn_length", DEFAULTS["turn_length"])
    game["turn_task"] = asyncio.create_task(turn_timeout(context, chat_id, current_player, turn_len))


async def turn_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, timeout_secs: int):
    try:
        await asyncio.sleep(timeout_secs)
    except asyncio.CancelledError:
        return
    game = games.get(chat_id)
    if not game or game["state"] != "playing":
        return
    if game["turn_order"][game["current_turn"]] == user_id:
        await context.bot.send_message(chat_id=chat_id, text=f"⏭️ {get_player_mention(game, user_id)} was skipped.")
        await advance_turn(context, chat_id)


async def advance_turn(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game:
        return
    game["current_turn"] += 1
    if game["current_turn"] >= len(game["turn_order"]):
        game["current_turn"] = 0
        game["current_round"] += 1
    if game.get("turn_task"):
        try:
            game["turn_task"].cancel()
        except Exception:
            pass
        game["turn_task"] = None
    await start_turn(context, chat_id)


# =========================================================
# TURN MESSAGE HANDLER
# =========================================================
async def turn_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat = update.message.chat
    user = update.message.from_user
    if chat.id not in games:
        return
    game = games[chat.id]
    if game["state"] != "playing":
        return
    if game["turn_order"][game["current_turn"]] != user.id:
        return
    if not update.message.text:
        return
    if word_count(update.message.text) > 4:
        try:
            await update.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=chat.id, text="Send 4 words or less.")
        return
    if game.get("turn_task"):
        try:
            game["turn_task"].cancel()
        except Exception:
            pass
        game["turn_task"] = None
    await update.message.reply_text("Word received ✅")
    await advance_turn(context, chat.id)


# =========================================================
# VOTING SYSTEM
# =========================================================
async def start_voting(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game:
        return
    game["state"] = "voting"
    vote_link = f"https://t.me/{context.bot.username}?start=vote_{game['game_id']}"
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🗳️ Voting time!",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Vote", url=vote_link)]]),
    )
    game["vote_message_id"] = msg.message_id
    for pid in game["players"].keys():
        game["vote_selections"].setdefault(pid, None)
        game["votes"].setdefault(pid, None)
    vote_len = game.get("settings_at_start", DEFAULTS).get("vote_length", DEFAULTS["vote_length"])
    asyncio.create_task(vote_timer(context, chat_id, vote_len))


async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE, game_id: int):
    if update.message is None:
        return
    user = update.message.from_user
    for game in games.values():
        if game["game_id"] == game_id:
            if game["state"] != "voting" or user.id not in game["players"]:
                await update.message.reply_text("This game doesn't exist or you're not a player.")
                return
            await send_vote_ui(update, context, game, user.id)
            return
    await update.message.reply_text("This game doesn't exist.")


async def send_vote_ui(update: Update, context: ContextTypes.DEFAULT_TYPE, game: dict, user_id: int):
    game["vote_selections"].setdefault(user_id, None)
    keyboard = []
    row = []
    idx = 0
    for uid in game["players"].keys():
        idx += 1
        label = get_player_display(game, uid)
        row.append(InlineKeyboardButton(label, callback_data=f"vote_select_{uid}_{user_id}"))
        if idx % 2 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("Done", callback_data=f"vote_done_{user_id}")])
    voted_for = game["vote_selections"].get(user_id)
    voted_label = get_player_display(game, voted_for) if voted_for else "None"
    await update.message.reply_text(
        f"🗳️ Vote for impostor\n\nYou voted for: {voted_label}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def vote_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, timeout_secs: int):
    try:
        await asyncio.sleep(timeout_secs)
    except asyncio.CancelledError:
        return
    await finalize_votes(context, chat_id)


async def finalize_votes(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game or game["state"] != "voting":
        return
    vote_count = {}
    for voter, target in game["votes"].items():
        if target is None:
            continue
        vote_count[target] = vote_count.get(target, 0) + 1
    text = "🗳️ Voting results\n\n"
    for voted_id, cnt in vote_count.items():
        voters = [get_player_mention(game, v) for v, t in game["votes"].items() if t == voted_id]
        text += f"{get_player_mention(game, voted_id)} - {cnt} vote(s) by {' '.join(voters)}\n"
    if not vote_count:
        text += "No votes were cast.\n"
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=game["vote_message_id"], text=text)
    except Exception:
        pass
    impostor_id = game.get("impostor")
    if impostor_id is None:
        result_text = f"Game ended. Word was: {game.get('word')}"
    else:
        if vote_count and impostor_id in vote_count and max(vote_count.values()) == vote_count[impostor_id]:
            result_text = f"🎉 Impostor {get_player_mention(game, impostor_id)} was voted out! Word was: {game.get('word')}"
        else:
            result_text = f"😈 Impostor {get_player_mention(game, impostor_id)} survives! Word was: {game.get('word')}"
    await context.bot.send_message(chat_id=chat_id, text=result_text)
    if chat_id in games:
        del games[chat_id]


# =========================================================
# CALLBACK HANDLING
# =========================================================
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or query.data is None:
        return
    data = query.data
    if data.startswith("vote_"):
        await handle_vote_callback(query, context)
        return
    if data.startswith("help_") or data.startswith("customise_"):
        await handle_help_customise_callbacks(query, context)
        return
    # unknown
    await query.answer()


# Vote callback logic
async def handle_vote_callback(query, context: ContextTypes.DEFAULT_TYPE):
    await query.answer()
    data = query.data
    parts = data.split("_")
    if len(parts) < 2 or parts[0] != "vote":
        return
    action = parts[1]
    if action == "select" and len(parts) == 4:
        try:
            target_id = int(parts[2])
            voter_id = int(parts[3])
        except ValueError:
            await query.answer("Invalid vote data.", show_alert=True)
            return
        if query.from_user.id != voter_id:
            await query.answer("This is not your ballot.", show_alert=True)
            return
        for game in games.values():
            if voter_id in game["players"]:
                game["vote_selections"][voter_id] = target_id
                try:
                    voted_label = get_player_display(game, target_id)
                    await query.edit_message_text(
                        f"🗳️ Vote for impostor\n\nYou voted for: {voted_label}",
                        reply_markup=query.message.reply_markup,
                    )
                except Exception:
                    pass
                return
        await query.answer("Game not found.", show_alert=True)
        return
    if action == "done" and len(parts) == 3:
        try:
            voter_id = int(parts[2])
        except ValueError:
            await query.answer("Invalid request.", show_alert=True)
            return
        if query.from_user.id != voter_id:
            await query.answer("This is not your ballot.", show_alert=True)
            return
        for chat_id, game in games.items():
            if voter_id in game["players"]:
                selected = game["vote_selections"].get(voter_id)
                game["votes"][voter_id] = selected
                game["done_votes"].add(voter_id)
                try:
                    await query.edit_message_text("✅ Vote submitted. Waiting for others...")
                except Exception:
                    pass
                players_set = set(game["players"].keys())
                if players_set.issubset(game["done_votes"]):
                    await finalize_votes(context, chat_id)
                return
        await query.answer("Game not found.", show_alert=True)
        return


# Help & customise callbacks
async def handle_help_customise_callbacks(query, context: ContextTypes.DEFAULT_TYPE):
    data = query.data
    if data == "help_menu":
        await show_help_menu(query, context)
        return
    if data == "help_rules":
        await show_rules_screen(query, context)
        return
    if data == "help_back":
        await show_help_menu(query, context)
        return
    if data == "help_customise":
        chat = query.message.chat
        user = query.from_user
        allowed = await is_admin_or_owner(context, chat.id, user.id)
        if not allowed:
            await query.answer("Only group admins and the bot owner can customise settings.", show_alert=True)
            return
        await show_customise_menu(query, context)
        return
    if data == "customise_back":
        await show_customise_menu(query, context)
        return
    if data == "customise_turn":
        await show_turn_menu(query, context)
        return
    if data == "customise_voting":
        await show_voting_menu(query, context)
        return
    if data == "customise_rounds":
        await show_rounds_menu(query, context)
        return
    if data == "customise_players":
        await show_players_menu(query, context)
        return
    if data == "customise_words":
        chat = query.message.chat
        user = query.from_user
        allowed = await is_admin_or_owner(context, chat.id, user.id)
        if not allowed:
            await query.answer("Not allowed.", show_alert=True)
            return
        await show_words_menu(query, context)
        return
    # turning specific options
    if data.startswith("customise_turn_"):
        parts = data.split("_")
        try:
            secs = int(parts[2])
        except Exception:
            await query.answer("Invalid value.", show_alert=True)
            return
        chat = query.message.chat
        user = query.from_user
        allowed = await is_admin_or_owner(context, chat.id, user.id)
        if not allowed:
            await query.answer("Not allowed.", show_alert=True)
            return
        set_setting(chat.id, "turn_length", secs)
        await query.edit_message_text(f"✅ Turn length will be set to {secs}s for future games.")
        await context.bot.send_message(chat_id=chat.id, text=f"✅ Turn length set to {secs}s (applies to future games).")
        return
    if data.startswith("customise_vote_"):
        parts = data.split("_")
        try:
            secs = int(parts[2])
        except Exception:
            await query.answer("Invalid value.", show_alert=True)
            return
        chat = query.message.chat
        user = query.from_user
        allowed = await is_admin_or_owner(context, chat.id, user.id)
        if not allowed:
            await query.answer("Not allowed.", show_alert=True)
            return
        set_setting(chat.id, "vote_length", secs)
        await query.edit_message_text(f"✅ Vote length will be set to {secs}s for future games.")
        await context.bot.send_message(chat_id=chat.id, text=f"✅ Vote length set to {secs}s (applies to future games).")
        return
    if data.startswith("customise_rounds_"):
        parts = data.split("_")
        try:
            n = int(parts[2])
        except Exception:
            await query.answer("Invalid value.", show_alert=True)
            return
        chat = query.message.chat
        user = query.from_user
        allowed = await is_admin_or_owner(context, chat.id, user.id)
        if not allowed:
            await query.answer("Not allowed.", show_alert=True)
            return
        set_setting(chat.id, "rounds", n)
        await query.edit_message_text(f"✅ Number of rounds will be set to {n} for future games.")
        await context.bot.send_message(chat_id=chat.id, text=f"✅ Rounds set to {n} (applies to future games).")
        return
    if data.startswith("customise_players_"):
        parts = data.split("_")
        try:
            n = int(parts[2])
        except Exception:
            await query.answer("Invalid value.", show_alert=True)
            return
        chat = query.message.chat
        user = query.from_user
        allowed = await is_admin_or_owner(context, chat.id, user.id)
        if not allowed:
            await query.answer("Not allowed.", show_alert=True)
            return
        set_setting(chat.id, "max_players", n)
        await query.edit_message_text(f"✅ Max players will be set to {n} for future games.")
        await context.bot.send_message(chat_id=chat.id, text=f"✅ Max players set to {n} (applies to future games).")
        return
    # words toggle handling
    if data.startswith("customise_words_toggle_"):
        parts = data.split("_")
        try:
            cat_id = int(parts[3]) if len(parts) >= 4 else int(parts[2])
        except Exception:
            await query.answer("Invalid category.", show_alert=True)
            return
        chat = query.message.chat
        user = query.from_user
        allowed = await is_admin_or_owner(context, chat.id, user.id)
        if not allowed:
            await query.answer("Not allowed.", show_alert=True)
            return
        enabled = get_enabled_category_ids(chat.id)
        if cat_id in enabled:
            disable_category_for_chat(chat.id, cat_id)
            await query.answer("Category disabled for this chat.")
            await context.bot.send_message(chat_id=chat.id, text="✅ Category disabled for future games.")
        else:
            enable_category_for_chat(chat.id, cat_id)
            await query.answer("Category enabled for this chat.")
            await context.bot.send_message(chat_id=chat.id, text="✅ Category enabled for future games.")
        await show_words_menu(query, context)
        return
    await query.answer()


# =========================================================
# HELP / CUSTOMISE MENU UI
# =========================================================
async def show_help_menu(query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Rules", callback_data="help_rules"), InlineKeyboardButton("Customise", callback_data="help_customise")],
    ]
    try:
        await query.edit_message_text("Rules and Customizations", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.answer()


async def show_rules_screen(query, context: ContextTypes.DEFAULT_TYPE):
    rules_text = (
        "Chameleon-style game rules:\n\n"
        "• All players except one (the impostor) receive the same secret word.\n"
        "• The impostor receives only a clue related to the word and must blend in.\n"
        "• Players take turns saying a single word (or short phrase) to show they know the word without revealing it.\n"
        "• The impostor tries to guess what others will say based on their clue and pick words that won't expose them.\n"
        "• After the rounds finish, players vote for who they think the impostor is.\n"
        "• If the impostor is voted out, the other players win and the secret word is revealed.\n"
        "• If the impostor avoids detection and is not voted out, the impostor wins.\n\n"
        "Good luck and have fun!"
    )
    keyboard = [[InlineKeyboardButton("Back", callback_data="help_back")]]
    try:
        await query.edit_message_text(rules_text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.answer()


async def show_customise_menu(query, context: ContextTypes.DEFAULT_TYPE):
    chat = query.message.chat
    settings = get_settings(chat.id)
    text = (
        "Customise settings (these values are current and apply to future games):\n\n"
        f"• Max players: {settings.get('max_players')}\n"
        f"• Turn length: {settings.get('turn_length')}s\n"
        f"• Vote length: {settings.get('vote_length')}s\n"
        f"• Rounds: {settings.get('rounds')}"
    )
    keyboard = [
        [InlineKeyboardButton("Turn", callback_data="customise_turn")],
        [InlineKeyboardButton("Voting", callback_data="customise_voting")],
        [InlineKeyboardButton("Rounds", callback_data="customise_rounds")],
        [InlineKeyboardButton("Players", callback_data="customise_players")],
        [InlineKeyboardButton("Words", callback_data="customise_words")],
        [InlineKeyboardButton("Back", callback_data="help_back")],
    ]
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.answer()


async def show_turn_menu(query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("20 sec", callback_data="customise_turn_20"), InlineKeyboardButton("40 sec", callback_data="customise_turn_40")],
        [InlineKeyboardButton("60 sec", callback_data="customise_turn_60")],
        [InlineKeyboardButton("Back", callback_data="customise_back")],
    ]
    try:
        await query.edit_message_text("Turn length — choose the time (applies to future games):", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.answer()


async def show_voting_menu(query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("30 sec", callback_data="customise_vote_30"), InlineKeyboardButton("60 sec", callback_data="customise_vote_60")],
        [InlineKeyboardButton("90 sec", callback_data="customise_vote_90"), InlineKeyboardButton("120 sec", callback_data="customise_vote_120")],
        [InlineKeyboardButton("Back", callback_data="customise_back")],
    ]
    try:
        await query.edit_message_text("Voting length — choose the time (applies to future games):", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.answer()


async def show_rounds_menu(query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("3", callback_data="customise_rounds_3"), InlineKeyboardButton("4", callback_data="customise_rounds_4")],
        [InlineKeyboardButton("5", callback_data="customise_rounds_5"), InlineKeyboardButton("6", callback_data="customise_rounds_6")],
        [InlineKeyboardButton("7", callback_data="customise_rounds_7")],
        [InlineKeyboardButton("Back", callback_data="customise_back")],
    ]
    try:
        await query.edit_message_text("Number of rounds (applies to future games):", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.answer()


async def show_players_menu(query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("4", callback_data="customise_players_4"), InlineKeyboardButton("6", callback_data="customise_players_6")],
        [InlineKeyboardButton("8", callback_data="customise_players_8"), InlineKeyboardButton("10", callback_data="customise_players_10")],
        [InlineKeyboardButton("Back", callback_data="customise_back")],
    ]
    try:
        await query.edit_message_text("Max players allowed (applies to future games):", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.answer()


async def show_words_menu(query, context: ContextTypes.DEFAULT_TYPE):
    chat = query.message.chat
    cats = get_all_categories()
    enabled_ids = set(get_enabled_category_ids(chat.id))
    keyboard = []
    row = []
    for cat in cats:
        label = f"{cat['name']} {'✅' if cat['id'] in enabled_ids else ''}"
        row.append(InlineKeyboardButton(label, callback_data=f"customise_words_toggle_{cat['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("Back", callback_data="customise_back")])
    try:
        await query.edit_message_text("Choose categories to include in games (checked = enabled):", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.answer()


# =========================================================
# /end & /kill
# =========================================================
async def end_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat = update.message.chat
    user = update.message.from_user
    if chat.id not in games:
        return
    if games[chat.id]["creator"] != user.id:
        return
    del games[chat.id]
    await update.message.reply_text("Game ended.")


async def kill_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat = update.message.chat
    user = update.message.from_user
    member = await context.bot.get_chat_member(chat.id, user.id)
    if member.status not in ["administrator", "creator"]:
        return
    if chat.id in games:
        del games[chat.id]
        await update.message.reply_text("All games killed.")


# =========================================================
# /help command
# =========================================================
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    keyboard = [[InlineKeyboardButton("Rules", callback_data="help_rules"), InlineKeyboardButton("Customise", callback_data="help_customise")]]
    await update.message.reply_text("Rules and Customizations", reply_markup=InlineKeyboardMarkup(keyboard))


# =========================================================
# MAIN
# =========================================================
def main():
    # initialize DB and defaults (creates words.db if absent)
    init_db_and_defaults()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("game", game_cmd))
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("end", end_cmd))
    app.add_handler(CommandHandler("kill", kill_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, turn_message_handler))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
