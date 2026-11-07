from cogs.ai.ai import AiCog
from cogs.ai.prompts import load_instructions
from cogs.ai.limits import cleanup
from disnake.ext.commands import Bot

def setup(bot: Bot):
    load_instructions()
    cleanup()
    bot.add_cog(AiCog(bot))