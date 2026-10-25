from cogs.ai.ai import AiCog
from cogs.ai.ai_prompts import load_instructions
from cogs.ai.ai_limits import cleanup
from disnake.ext.commands import Bot

def setup(bot: Bot):
    load_instructions()
    cleanup()
    bot.add_cog(AiCog(bot))