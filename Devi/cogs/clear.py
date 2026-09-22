from datetime import datetime, timezone

import disnake
from disnake.ext import commands

import i18n
from duration_utils import parse_timedelta
from logs import send_log, LogColor
from discord_i18n import localized
from permissions import validate_permissions
from other_apis.topgg_utils import is_voted
from paths import env_var_to_int

purging_channels: set[int] = set() # Channel IDs where /clear is currently being executed

COUNT = env_var_to_int("CLEAR_COUNT", "500")
COUNT_VOTED = env_var_to_int("CLEAR_COUNT_VOTED", "1500")

class ClearCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(
        name="clear",
        description=localized("commands.clear.description"),
    )
    async def clear_messages(
            self,
            inter: disnake.ApplicationCommandInteraction,
            count: int = commands.Param(
                name=localized("commands.clear.param_count_name"),
                description=localized("commands.clear.param_count"),
            ),
            duration: str = commands.Param(
                default=None,
                name=localized("commands.clear.param_duration_name"),
                description=localized("commands.clear.param_duration"),
            ),
            member: disnake.Member = commands.Param(
                default=None,
                name=localized("commands.clear.param_member_name"),
                description=localized("commands.clear.param_member"),
            )
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{disnake.Permissions(manage_messages=True): True}]):
            return None

        if count <= 0:
            return await inter.response.send_message(i18n.t("clear_cmd.invalid_count", locale=gid), ephemeral=True)

        voted = await is_voted(inter.author.id)
        max_count = COUNT_VOTED if voted else COUNT

        if count > max_count:
            vote_ad = "" if voted else "\n\n" + i18n.t("top_gg_cog.voting_ad", locale=gid)
            return await inter.response.send_message(i18n.t("clear_cmd.count_too_high", locale=gid, max=str(max_count)) + vote_ad, ephemeral=True)

        after_time = None
        if duration is not None:
            delta, error = parse_timedelta(duration, locale=gid)
            if error:
                return await inter.response.send_message(error, ephemeral=True)
            after_time = datetime.now(timezone.utc) - delta

        if not isinstance(inter.channel, (disnake.TextChannel, disnake.Thread)):
            return await inter.response.send_message(i18n.t("clear_cmd.wrong_channel_type", locale=gid), ephemeral=True)

        def check(msg: disnake.Message) -> bool:
            if member is not None and msg.author.id != member.id:
                return False
            return True

        await inter.response.defer(ephemeral=True)

        purging_channels.add(inter.channel.id)
        try:
            deleted = await inter.channel.purge(limit=count, check=check, after=after_time, bulk=True)
        except disnake.Forbidden:
            return await inter.followup.send(
                i18n.t("clear_cmd.forbidden", locale=gid), ephemeral=True
            )
        except disnake.HTTPException as e:
            return await inter.followup.send(
                i18n.t("clear_cmd.http_error", locale=gid, error=e), ephemeral=True
            )
        finally:
            purging_channels.discard(inter.channel.id)

        await inter.followup.send(
            i18n.t("clear_cmd.done", locale=gid, count=len(deleted)), ephemeral=True
        )

        no_value = i18n.t("clear_cmd.log_none", locale=gid)

        return await send_log(
            inter.guild,
            i18n.t("clear_cmd.log_title", locale=gid),
            color=LogColor.Message,
            fields=[
                (i18n.t("clear_cmd.log_channel", locale=gid), inter.channel.mention),
                (i18n.t("clear_cmd.log_moderator", locale=gid), f"{inter.author.mention} ({inter.author.id})"),
                (i18n.t("clear_cmd.log_requested", locale=gid), str(count)),
                (i18n.t("clear_cmd.log_deleted", locale=gid), str(len(deleted))),
                (i18n.t("clear_cmd.log_member_filter", locale=gid), member.mention if member else no_value),
                (i18n.t("clear_cmd.log_duration_filter", locale=gid), duration if duration else no_value),
            ]
        )


def setup(bot: commands.Bot):
    bot.add_cog(ClearCog(bot))