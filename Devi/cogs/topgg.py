import i18n
from discord_i18n import localized

import disnake
from disnake.ext import commands
from other_apis.topgg_utils import is_voted

from cogs.ai.ai import (COOLDOWN_SECONDS as AI_COOLDOWN_SECONDS, COOLDOWN_SECONDS_VOTED as AI_COOLDOWN_SECONDS_VOTED)
from cogs.ai.ai_memory import MAX_MESSAGES, MAX_MESSAGES_VOTED
from cogs.clear import COUNT, COUNT_VOTED
from cogs.mentions import (MAX_MESSAGES as MENTION_MAX_MESSAGES, MAX_MESSAGES_VOTED as MENTION_MAX_MESSAGES_VOTED, COOLDOWN_TIME, COOLDOWN_TIME_VOTED)
from cogs.triggers import RESPONSE_COOLDOWN, RESPONSE_COOLDOWN_VOTED
from cogs.voice import MAX_TTS_LENGTH, MAX_TTS_LENGTH_VOTED

class TopGGCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(
        name="vote",
        description=localized("commands.vote.description")
    )
    async def vote(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)

        voted = await is_voted(inter.author.id)

        voting_text = i18n.t("top_gg_cog.voting", locale=inter.guild_id, bot_mention=self.bot.user.mention,
                             AI_COOLDOWN_SECONDS=AI_COOLDOWN_SECONDS,
                             AI_COOLDOWN_SECONDS_VOTED=AI_COOLDOWN_SECONDS_VOTED,
                             AI_MAX_MESSAGES=MAX_MESSAGES,
                             AI_MAX_MESSAGES_VOTED=MAX_MESSAGES_VOTED,
                             CLEAR_MAX=COUNT,
                             CLEAR_MAX_VOTED=COUNT_VOTED,
                             MENTIONS_MAX_MESSAGES=MENTION_MAX_MESSAGES,
                             MENTIONS_MAX_MESSAGES_VOTED=MENTION_MAX_MESSAGES_VOTED,
                             MENTIONS_COOLDOWN_TIME=COOLDOWN_TIME,
                             MENTIONS_COOLDOWN_TIME_VOTED=COOLDOWN_TIME_VOTED,
                             TRIGGERS_RESPONSE_COOLDOWN=RESPONSE_COOLDOWN,
                             TRIGGERS_RESPONSE_COOLDOWN_VOTED=RESPONSE_COOLDOWN_VOTED,
                             MAX_TTS_LENGTH=MAX_TTS_LENGTH,
                             MAX_TTS_LENGTH_VOTED=MAX_TTS_LENGTH_VOTED)

        voted_text = "\n\n" + i18n.t("top_gg_cog." + ("voted" if voted else "not_voted"), locale=inter.guild_id, bot_mention=self.bot.user.mention)
        vote_link = "\n\n" + i18n.t("top_gg_cog.voting_link", locale=inter.guild_id)

        return await inter.followup.send(voting_text + vote_link + voted_text)


def setup(bot):
    bot.add_cog(TopGGCog(bot))