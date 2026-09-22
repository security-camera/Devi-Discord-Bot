import disnake
from disnake.ext import commands

import i18n

from discord_i18n import localized, locale_choices
from permissions import validate_permissions, Permission
from logs import set_log_channel_id, remove_log_channel
from localization import set_localization


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(
        name="set_log_channel",
        description=localized("commands.set_log_channel.description"),
    )
    async def set_log_channel(
            self,
            inter: disnake.ApplicationCommandInteraction,
            channel: disnake.TextChannel | None = commands.Param(
                default=None,
                name=localized("commands.set_log_channel.param_channel_name"),
                description=localized("commands.set_log_channel.param_channel"),
            ),
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.Admin: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        if not channel:
            remove_log_channel(gid)
            return await inter.response.send_message(i18n.t("log_channel_cmd.removed", locale=gid), ephemeral=True)

        set_log_channel_id(gid, channel.id)
        return await inter.response.send_message(i18n.t("log_channel_cmd.set", locale=gid, channel=channel.mention), ephemeral=True)

    @commands.slash_command(
        name="language",
        description=localized("commands.language.description"),
    )
    async def language(
            self,
            inter: disnake.ApplicationCommandInteraction,
            language: str = commands.Param(
                name=localized("commands.language.param_language_name"),
                description=localized("commands.language.param_language"),
                choices=locale_choices()
            ),
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.Admin: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        set_localization(gid, language)

        return await inter.response.send_message(
            i18n.t("language_cmd.set", locale=gid, language=i18n.get_locale_display_name(language)),
            ephemeral=True
        )


def setup(bot: commands.Bot):
    bot.add_cog(AdminCog(bot))