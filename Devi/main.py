import os
from dotenv import load_dotenv

import disnake
import platform
from disnake.ext import commands, tasks

import db
import permissions
import logs
import storage

from emojis import load_custom_emojis

import paths
from paths import ENV_FILE, env_bool, env_var

load_dotenv(ENV_FILE)

# Discord Developer Portal -> App -> Bot.
intents = disnake.Intents.all()
bot = commands.Bot(command_prefix="+", intents=intents)
_TEST_MODE = env_bool("TEST_ENABLED")

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
    "cogs.ai",
    "cogs.giveaways",
    "cogs.help",
    "cogs.send",
    "cogs.permissions_commands",
    "cogs.music",
    "cogs.playlists",
    "cogs.developer",
    "cogs.birthdays",
    "cogs.topgg",
    "cogs.temp_voices",
    "cogs.honeypots",
    "cogs.temp_bans",
    "cogs.dashboard_api",
    "cogs.quotes",
    "cogs.gif"
]

for extension in EXTENSIONS:
    bot.load_extension(extension)

@tasks.loop(minutes=1)
async def update_presence():
    unique_members = {
        member.id for member in bot.get_all_members() if not member.bot
    }

    await bot.change_presence(
        activity=disnake.Game(
            name=f"Serving: {len(bot.guilds):,} servers | {len(unique_members):,} unique members"
        )
    )


@update_presence.before_loop
async def before_update_presence():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"✅ Bot {bot.user} is online!")
    print(platform.system())
    print(platform.machine())
    await load_custom_emojis(bot)

    if not update_presence.is_running():
        update_presence.start()

if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    finally:
        db.close()