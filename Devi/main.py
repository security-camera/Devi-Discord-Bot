import os
from dotenv import load_dotenv

import disnake
import platform
from disnake.ext import commands

import db
import permissions
import logs
import storage

import paths
from paths import ENV_FILE, env_var_to_bool, env_var

load_dotenv(ENV_FILE)

# Discord Developer Portal -> App -> Bot.
intents = disnake.Intents.all()
bot = commands.Bot(command_prefix="+", intents=intents)
_TEST_MODE = env_var_to_bool("TEST_ENABLED")

#Do not change queue of initialization
paths.init()
storage.init(_TEST_MODE)
db.init()
logs.init(bot)
permissions.init(bot)

TOKEN = env_var("BOT_TOKEN" if not _TEST_MODE else "BOT_TOKEN_TEST", "PUT_BOT_TOKEN_AND_BOT_TOKEN_TEST_TO_ENV")

EXTENSIONS = [
    "cogs.admin",
    "cogs.mentions",
    "cogs.triggers",
    "cogs.utils",
    "cogs.warns",
    "cogs.temp_roles",
    "cogs.clear",
    "cogs.logging_events",
    "cogs.voice",
    "cogs.ai.ai",
    "cogs.giveaways",
    "cogs.help",
    "cogs.send",
    "cogs.permissions_commands",
    "cogs.music",
    "cogs.developer",
    "cogs.birthdays",
    "cogs.topgg",
    "cogs.temp_voices",
    "cogs.traps",
    "cogs.temp_bans"
]

for extension in EXTENSIONS:
    bot.load_extension(extension)

@bot.event
async def on_ready():
    print(f"✅ Bot {bot.user} is online!")

    from cogs.help import COMMAND_MENTIONS, register_commands

    cmds = await bot.fetch_global_commands()

    register_commands(cmds)

    print(len(COMMAND_MENTIONS))
    print(platform.system())
    print(platform.machine())

if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    finally:
        db.close()