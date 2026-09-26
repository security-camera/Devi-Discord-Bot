from enum import IntEnum

import disnake
import i18n
from discord_i18n import localized
from disnake.ext import commands

from db import db_cursor
from permissions import Permission, validate_permissions
from cogs.send import SendCog, get_sticky_message, remove_sticky_message, set_sticky_message
from duration_utils import parse_duration_seconds
from paths import env_var_to_int

DEFAULT_TIMEOUT = env_var_to_int("TRAPS_DEFAULT_TIMEOUT", "300")
MAX_TIMEOUT = 28 * 24 * 60 * 60


class PunishmentType(IntEnum):
    Timeout = 0
    Ban = 1
    Role = 2


def punishment_choices() -> list[disnake.OptionChoice]:
    return [
        disnake.OptionChoice(
            name=localized(f"trap_cog.punishment_types.{i:02d}"),
            value=i
        )
        for i in range(len(PunishmentType))
    ]


class TrapsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _delete_sticky_message(self, channel_id: int) -> None:
        """
        Delete the tracked sticky Discord message for a channel (if any) and
        drop its bookkeeping record.

        Used both when a trap channel is removed and when a trap sticky is
        about to be replaced, so no orphaned sticky message is left behind
        for SendCog's on_message listener to keep reposting.
        """
        existing = get_sticky_message(channel_id)
        if not existing:
            return

        if existing["message_id"]:
            channel = self.bot.get_channel(channel_id)
            if channel is not None:
                try:
                    old = await channel.fetch_message(existing["message_id"])
                    await old.delete()
                except (disnake.NotFound, disnake.Forbidden):
                    pass

        remove_sticky_message(channel_id)

    @staticmethod
    def get_trap_channel(guild_id: int) -> tuple[int, PunishmentType, int, int] | None:
        with db_cursor() as cur:
            cur.execute(
                """SELECT channel_id, punishment_type, punishment_duration, role_id FROM trap_channels WHERE guild_id = %s""",
                (guild_id,)
            )
            row = cur.fetchone()

        if not row:
            return None

        return row["channel_id"], PunishmentType(row["punishment_type"]), row["punishment_duration"], row["role_id"]

    async def set_trap_channel(self, guild_id: int, channel: disnake.TextChannel, punishment_type: PunishmentType, punishment_duration: int = DEFAULT_TIMEOUT, role_id: int = 0) -> bool:
        try:
            # Replace whatever sticky message (trap sticky or an unrelated
            # /send sticky) is currently tracked for this channel, so we
            # never end up with two stickies competing in the same channel.
            await self._delete_sticky_message(channel.id)

            content = i18n.t("trap_cog.sticky", locale=guild_id)
            sent = await channel.send(SendCog.format_sticky(content, locale=guild_id, sent_by_user=False))
            set_sticky_message(channel.id, guild_id, content, sent.id, self.bot.user.id)

            with db_cursor(commit=True) as cur:
                cur.execute(
                    """INSERT INTO trap_channels (guild_id, channel_id, punishment_type, punishment_duration, role_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (guild_id) DO UPDATE SET
                        channel_id = EXCLUDED.channel_id,
                        punishment_type = EXCLUDED.punishment_type,
                        punishment_duration = EXCLUDED.punishment_duration,
                        role_id = EXCLUDED.role_id""",
                    (guild_id, channel.id, int(punishment_type), punishment_duration, role_id,),
                )

            return True

        except disnake.Forbidden:
            return False

    async def remove_trap_channel(self, guild_id: int) -> bool:
        config = self.get_trap_channel(guild_id)

        if not config:
            return False

        channel_id = config[0]

        try:
            await self._delete_sticky_message(channel_id)

            with db_cursor(commit=True) as cur:
                cur.execute(
                    "DELETE FROM trap_channels WHERE guild_id = %s",
                    (guild_id,),
                )

                return cur.rowcount > 0

        except disnake.Forbidden:
            return False

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if not message.guild.id:
            return

        if self.bot.user and message.author.id == self.bot.user.id:
            return

        config = self.get_trap_channel(message.guild.id)

        if not config:
            return

        channel_id, punishment, duration, role_id = config

        if message.channel.id != channel_id:
            return

        reason = i18n.t("trap_cog.punish_reason", locale=message.guild.id)

        try:
            match punishment:
                case PunishmentType.Timeout:
                    if not 1 <= duration <= MAX_TIMEOUT:
                        return

                    await message.author.timeout(duration=duration, reason=reason,)

                case PunishmentType.Ban:
                    if duration > 0:
                        temp_bans_cog = self.bot.get_cog("TempBansCog")
                        if temp_bans_cog:
                            await temp_bans_cog.grant_temp_ban(message.guild, message.author, duration, self.bot.user.id, reason=reason)
                        else:
                            # Fallback if TempBansCog isn't loaded: ban permanently
                            await message.guild.ban(message.author, reason=reason)
                    else:
                        await message.author.kick(reason=reason)

                case PunishmentType.Role:
                    if not role_id:
                        return

                    role = message.guild.get_role(role_id) or await message.guild.fetch_role(role_id)

                    if duration > 0:
                        temp_roles_cog = self.bot.get_cog("TempRolesCog")
                        if temp_roles_cog:
                            await temp_roles_cog.grant_temp_role(message.guild, message.author, role, duration, self.bot.user.id, reason=reason)
                        else:
                            # Fallback if TempRolesCog isn't loaded: grant permanently
                            await message.author.add_roles(role, reason=reason)
                    else:
                        await message.author.add_roles(role, reason=reason)

            await message.channel.send(f":hammer: {message.author.mention}")
            await message.delete()

        except (disnake.Forbidden, disnake.NotFound):
            return
        except disnake.HTTPException as e:
            print(e)

    @commands.slash_command(
        name="trap",
        description=localized("commands.trap.description"),
    )
    async def trap_command(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
        pass

    @trap_command.sub_command(
        name="channel",
        description=localized("commands.trap_channel.description"),
    )
    async def trap_channel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.TextChannel = commands.Param(
            default=None,
            name=localized("commands.trap_channel.param_channel_name"),
            description=localized("commands.trap_channel.param_channel")
        ),
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.Admin: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        current = self.get_trap_channel(gid)

        if not channel:
            if not current:
                return await inter.response.send_message(i18n.t("trap_cog.nothing_to_remove", locale=gid), ephemeral=True)

            if not await self.remove_trap_channel(gid):
                return await inter.response.send_message(i18n.t("trap_cog.permission_error", locale=gid), ephemeral=True)

            return await inter.response.send_message(i18n.t("trap_cog.channel_removed", locale=gid), ephemeral=True)

        if current:
            old_channel_id, punishment_type, punishment_duration, role_id = current
            if old_channel_id != channel.id:
                # The trap moved to a different channel: clean up the sticky
                # that would otherwise be left behind in the old one.
                await self._delete_sticky_message(old_channel_id)
        else:
            punishment_type = PunishmentType.Timeout
            punishment_duration = DEFAULT_TIMEOUT
            role_id = 0

        success = await self.set_trap_channel(gid, channel, punishment_type, punishment_duration, role_id)

        if not success:
            return await inter.response.send_message(i18n.t("trap_cog.permission_error", locale=gid), ephemeral=True)

        return await inter.response.send_message(i18n.t("trap_cog.channel_set", locale=gid, channel=channel.mention), ephemeral=True)

    @trap_command.sub_command(
        name="punishment",
        description=localized("commands.trap_punishment.description"),
    )
    async def trap_punishment(
        self,
        inter: disnake.ApplicationCommandInteraction,
        punishment_type: int = commands.Param(
            name=localized("commands.trap_punishment.param_punishment_type_name"),
            description=localized("commands.trap_punishment.param_punishment_type"),
            choices=punishment_choices(),
        ),
        duration: str = commands.Param(
            default="",
            name=localized("commands.trap_punishment.param_duration_name"),
            description=localized("commands.trap_punishment.param_duration"),
        ),
        role: disnake.Role = commands.Param(
            default=None,
            name=localized("commands.trap_punishment.param_role_name"),
            description=localized("commands.trap_punishment.param_role"),
        ),
    ):
        gid = inter.guild_id
        duration, error = parse_duration_seconds(duration, locale=gid)

        if error:
            return await inter.response.send_message(error, ephemeral=True)

        current = self.get_trap_channel(gid)

        if not current:
            return await inter.response.send_message(i18n.t("trap_cog.channel_not_set", locale=gid), ephemeral=True)

        punishment = PunishmentType(punishment_type)
        role_id = 0

        match punishment:
            case PunishmentType.Timeout:
                if not 1 <= duration <= MAX_TIMEOUT:
                    max_duration = "28" + i18n.t("duration_utils.duration_chars.days", locale=gid)
                    return await inter.response.send_message(i18n.t("trap_cog.invalid_timeout", locale=gid, max_duration=max_duration), ephemeral=True)

            case PunishmentType.Ban:
                # duration == 0 -> kick, duration > 0 -> temporary ban for that long
                duration = max(duration, 0)

            case PunishmentType.Role:
                if not role:
                    return await inter.response.send_message(i18n.t("trap_cog.role_required", locale=gid), ephemeral=True)

                role_id = role.id
                # duration == 0 -> permanent role, duration > 0 -> temporary role
                duration = max(duration, 0)

        with db_cursor(commit=True) as cur:
            cur.execute(
                """UPDATE trap_channels SET punishment_type = %s, punishment_duration = %s, role_id = %s WHERE guild_id = %s""",
                (punishment_type, duration, role_id, gid),
            )

            if cur.rowcount == 0:
                return await inter.response.send_message(i18n.t("trap_cog.channel_not_set", locale=gid), ephemeral=True)

        return await inter.response.send_message(i18n.t("trap_cog.punishment_set", locale=gid, punishment=punishment.name, duration=duration, role=role.mention if role else ":x:"), ephemeral=True)


def setup(bot):
    bot.add_cog(TrapsCog(bot))