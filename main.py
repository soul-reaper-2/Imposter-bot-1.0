import os
import json
import random
import asyncio
from collections import defaultdict

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

TOKEN = os.getenv("BOT_TOKEN")  # Replit env variable

# ===== LOAD WORDS =====
with open("words.json", "r", encoding="utf-8") as f:
    WORDS = json.load(f)

# ===== GAME STORAGE (IN-MEMORY) =====
games = {}  # game_id -> game data


# ===== UTIL =====
def generate_game_id():
    return random.randint(1000, 9999)


def pick_word():
    data = random.choice(WORDS)
    word = data["word"]
    clues = data["clues"]
    impostor_clue = random.choice(clues)
    return word, impostor_clue


# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # PRIVATE CHAT → role checking
    if update.message.chat.type == "private":
        if context.args and context.args[0].startswith("Game_"):
            game_id = int(context.args[0].split("_")[1])
            await handle_role_check(update, context, game_id)
        else:
            await update.message.reply_text("This bot works inside group games.")
        return

    # GROUP CHAT → create game
    chat = update.message.chat
    user = update.message.from_user

    # Ensure admin
    member = await context.bot.get_chat_member(chat.id, user.id)
    if member.status not in ["administrator", "creator"]:
        await update.message.reply_text("Only admins can start a game.")
        return

    game_id = generate_game_id()
    while game_id in games:
        game_id = generate_game_id()

    games[game_id] = {
        "chat_id": chat.id,
        "players": [],
        "started": False,
        "impostor": None,
        "word": None,
        "clue": None,
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Join", callback_data=f"join_{game_id}"),
            InlineKeyboardButton("Start", callback_data=f"start_{game_id}")
        ]
    ])

    await update.message.reply_text(
        f"🎮 New game created!\nGame ID: {game_id}\n\nClick **Join** to play.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ===== CALLBACKS =====
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("join_"):
        game_id = int(data.split("_")[1])
        await join_game(query, game_id)

    elif data.startswith("start_"):
        game_id = int(data.split("_")[1])
        await start_game(query, context, game_id)


async def join_game(query, game_id):
    user = query.from_user

    if game_id not in games:
        return

    game = games[game_id]

    if game["started"]:
        return

    if user.id not in game["players"]:
        game["players"].append(user.id)

    players_text = "\n".join(
        f"• {user.full_name}"
        for user_id in game["players"]
        for user in [query.message.chat.get_member(user_id).user]
    )

    await query.edit_message_text(
        f"🎮 Game ID: {game_id}\n\nPlayers:\n{players_text}",
        reply_markup=query.message.reply_markup
    )


async def start_game(query, context, game_id):
    if game_id not in games:
        return

    game = games[game_id]

    if game["started"]:
        return

    if len(game["players"]) < 4:
        await query.answer("Minimum 4 players required.", show_alert=True)
        return

    game["started"] = True

    # Assign roles
    game["impostor"] = random.choice(game["players"])
    game["word"], game["clue"] = pick_word()

    role_link = f"https://t.me/{context.bot.username}?start=Game_{game_id}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("See your role", url=role_link)]
    ])

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text="🎭 Game started!\nClick below to see your role.",
        reply_markup=keyboard
    )


# ===== ROLE CHECK =====
async def handle_role_check(update, context, game_id):
    user = update.message.from_user

    if game_id not in games:
        await update.message.reply_text("This game doesn't exist.")
        return

    game = games[game_id]

    if user.id not in game["players"]:
        await update.message.reply_text("You are not part of this game.")
        return

    if user.id == game["impostor"]:
        await update.message.reply_text(
            f"😈 You are the IMPOSTOR\n\nClue: {game['clue']}"
        )
    else:
        await update.message.reply_text(
            f"✅ Your word is:\n\n**{game['word']}**",
            parse_mode="Markdown"
        )


# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_buttons))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()