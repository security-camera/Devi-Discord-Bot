from cogs.quotes.quotes import QuotesCog
from disnake.ext.commands import InteractionBot

def setup(bot: InteractionBot):
    bot.add_cog(QuotesCog(bot))