#=========================================================
# IMPORTS & SETUP
# =========================================================
import os
import json
import random
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")

with open("words.json", "r", encoding="utf-8") as f:
    WORDS = json.load(f)

games = {}  # chat_id -> game data


# =========================================================
# UTILITIES
# =========================================================
def generate_game_id():
    return random.randint(1000, 9999)


def pick_word():
    item = random.choice(WORDS)
    return item["word"], random.choice(item["clues"])


def word_count(text: str):
    return len(text.strip().split())


# =========================================================
# GAME CREATION (/game)
# =========================================================
async def game_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.message.chat
    user = update.message.from_user

    if chat.type == "private":
        return

    member = await context.bot.get_chat_member(chat.id, user.id)
    if member.status not in ["administrator", "creator"]:
        return

    if chat.id in games:
        await update.message.reply_text("A game already exists in this group.")
        return

    game_id = generate_game_id()
    join_link = f"https://t.me/{context.bot.username}?start=join_{game_id}"

    msg = await update.message.reply_text(
        "🎮 New game created!\n\nPlayers:\n\nSend /start when everyone joined",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Join", url=join_link)]
        ])
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
    }


# =========================================================
# /start (GROUP + PRIVATE DEEPLINK ROUTER)
# =========================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    # Start game
    game["state"] = "playing"
    game["turn_order"] = list(game["players"].keys())
    random.shuffle(game["turn_order"])

    game["impostor"] = random.choice(game["turn_order"])
    game["word"], game["clue"] = pick_word()

    role_link = f"https://t.me/{context.bot.username}?start=role_{game['game_id']}"

    await update.message.reply_text(
        "🎭 Game started!\nClick below to see your role.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("See your role", url=role_link)]
        ])
    )

    await start_turn(context, chat.id)


# =========================================================
# JOIN HANDLER
# =========================================================
async def handle_join(update, context, game_id):
    user = update.message.from_user

    for chat_id, game in games.items():
        if game["game_id"] == game_id:

            if game["state"] != "waiting":
                await update.message.reply_text("Game already started.")
                return

            if user.id in game["players"]:
                await update.message.reply_text("You already joined.")
                return

            game["players"][user.id] = user.username or user.full_name
            await update.message.reply_text("✅ You joined the game.")

            players_text = "\n".join(
                f"• @{name}" for name in game["players"].values()
            )

            join_link = f"https://t.me/{context.bot.username}?start=join_{game_id}"

            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=game["message_id"],
                text=(
                    "🎮 New game created!\n\nPlayers:\n"
                    f"{players_text}\n\n"
                    "Send /start when everyone joined"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Join", url=join_link)]
                ])
            )
            return

    await update.message.reply_text("This game doesn't exist.")


# =========================================================
# ROLE HANDLER
# =========================================================
async def handle_role(update, context, game_id):
    user = update.message.from_user

    for game in games.values():
        if game["game_id"] == game_id:
            if user.id not in game["players"]:
                return
            if user.id == game["impostor"]:
                await update.message.reply_text(
                    f"😈 You are the IMPOSTOR\n\nClue: {game['clue']}"
                )
            else:
                await update.message.reply_text(
                    f"✅ Your word is:\n\n{game['word']}"
                )
            return

    await update.message.reply_text("This game doesn't exist.")


# =========================================================
# TURN SYSTEM
# =========================================================
async def start_turn(context, chat_id):
    game = games.get(chat_id)
    if not game or game["state"] != "playing":
        return

    if game["current_round"] > 5:
        await start_voting(context, chat_id)
        return

    current_player = game["turn_order"][game["current_turn"]]

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🕒 Round {game['current_round']}/5\n"
            f"Your turn: @{game['players'][current_player]}\n"
            "You have 40 seconds"
        )
    )

    game["turn_task"] = asyncio.create_task(turn_timeout(context, chat_id, current_player))


async def turn_timeout(context, chat_id, user_id):
    await asyncio.sleep(40)
    game = games.get(chat_id)
    if not game or game["state"] != "playing":
        return
    if game["turn_order"][game["current_turn"]] == user_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏭️ @{game['players'][user_id]} was skipped."
        )
        await advance_turn(context, chat_id)


async def advance_turn(context, chat_id):
    game = games[chat_id]
    game["current_turn"] += 1
    if game["current_turn"] >= len(game["turn_order"]):
        game["current_turn"] = 0
        game["current_round"] += 1
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

    if word_count(update.message.text) > 4:
        await update.message.delete()
        await context.bot.send_message(
            chat_id=chat.id,
            text="Send 4 words or less."
        )
        return

    if game["turn_task"]:
        game["turn_task"].cancel()

    await update.message.reply_text("Word received ✅")
    await advance_turn(context, chat.id)


# =========================================================
# VOTING SYSTEM (FULL FINAL)
# =========================================================
async def start_voting(context, chat_id):
    game = games[chat_id]
    game["state"] = "voting"

    vote_link = f"https://t.me/{context.bot.username}?start=vote_{game['game_id']}"

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🗳️ Voting time!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Vote", url=vote_link)]
        ])
    )
    game["vote_message_id"] = msg.message_id

    # Start 60s timer
    asyncio.create_task(vote_timer(context, chat_id))


async def handle_vote(update, context, game_id):
    user = update.message.from_user
    for game in games.values():
        if game["game_id"] == game_id:
            if game["state"] != "voting" or user.id not in game["players"]:
                await update.message.reply_text("This game doesn't exist or you're not a player.")
                return
            await send_vote_ui(update, context, game, user.id)
            return
    await update.message.reply_text("This game doesn't exist.")


async def send_vote_ui(update, context, game, user_id):
    # Initialize selection if not exists
    if user_id not in game["vote_selections"]:
        game["vote_selections"][user_id] = None

    keyboard = []
    row = []
    for idx, (uid, name) in enumerate(game["players"].items(), 1):
        row.append(InlineKeyboardButton(name, callback_data=f"vote_select_{uid}_{user_id}"))
        if idx % 2 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("Done", callback_data=f"vote_done_{user_id}")])

    # Send or edit vote message
    await update.message.reply_text(
        f"🗳️ Vote for imposter\n\n"
        f"You voted for: {game['vote_selections'][user_id]}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def vote_timer(context, chat_id):
    await asyncio.sleep(60)
    await finalize_votes(context, chat_id)


async def finalize_votes(context, chat_id):
    game = games.get(chat_id)
    if not game or game["state"] != "voting":
        return

    # Count votes
    vote_count = {}
    for voter, target in game["votes"].items():
        if target is None:
            continue
        vote_count[target] = vote_count.get(target, 0) + 1

    # Format vote result for group message
    text = "🗳️ Voting time!\n\n"
    for voted_id in vote_count:
        voters = [f"@{game['players'][v]}" for v, t in game["votes"].items() if t == voted_id]
        text += f"@{game['players'][voted_id]} - {' '.join(voters)}\n"

    # Edit original vote message in group
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=game["vote_message_id"],
        text=text
    )

    # Determine impostor result
    impostor_id = game["impostor"]
    if impostor_id in vote_count and max(vote_count.values()) == vote_count[impostor_id]:
        result_text = f"🎉 Impostor @{game['players'][impostor_id]} was voted out! Word was: {game['word']}"
    else:
        result_text = f"😈 Impostor @{game['players'][impostor_id]} survives! Word was: {game['word']}"

    await context.bot.send_message(chat_id=chat_id, text=result_text)
    del games[chat_id]


# =========================================================
# /end & /kill
# =========================================================
async def end_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.message.chat
    user = update.message.from_user

    if chat.id not in games:
        return

    if games[chat.id]["creator"] != user.id:
        return

    del games[chat.id]
    await update.message.reply_text("Game ended.")


async def kill_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.message.chat
    user = update.message.from_user

    member = await context.bot.get_chat_member(chat.id, user.id)
    if member.status not in ["administrator", "creator"]:
        return

    if chat.id in games:
        del games[chat.id]
        await update.message.reply_text("All games killed.")


# =========================================================
# MAIN
# =========================================================
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("game", game_cmd))
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("end", end_cmd))
    app.add_handler(CommandHandler("kill", kill_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, turn_message_handler))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
