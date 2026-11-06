from cogs.captions.captions import CaptionCog
from disnake.ext.commands import InteractionBot

def setup(bot: InteractionBot):
    bot.add_cog(CaptionCog(bot))