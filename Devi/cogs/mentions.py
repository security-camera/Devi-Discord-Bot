import asyncio
import time

import disnake
from disnake.ext import commands

import i18n
from permissions import validate_permissions, Permission, has_permissions
from discord_i18n import localized
from other_apis.topgg_utils import is_voted
from paths import env_var_to_int

MAX_MESSAGES = env_var_to_int("MAX_MENTIONS", "10")
COOLDOWN_TIME = env_var_to_int("MENTIONS_COOLDOWN_TIME", "60")
MAX_MESSAGES_VOTED = env_var_to_int("MAX_MENTIONS_VOTED", "30")
COOLDOWN_TIME_VOTED = env_var_to_int("MENTIONS_COOLDOWN_TIME", "30")
BAN_WORDS = ["@everyone", "@here", "@&"]


def can_ping_role(inter: disnake.ApplicationCommandInteraction, role: disnake.Role | None) -> bool:
    if role is None:
        return False
    return role.mentionable or inter.permissions.administrator or inter.permissions.manage_roles


class MentionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cooldowns = {}
        self.need_to_stop = False

    async def validate_mention_request(
            self,
            inter: disnake.ApplicationCommandInteraction,
            count: int,
            target_id: int,
            message: str,
            is_role: bool = False
    ) -> str | None:
        gid = inter.guild_id
        user_id = inter.author.id

        voted = await is_voted(user_id)
        max_voted = MAX_MESSAGES_VOTED if voted else MAX_MESSAGES

        if await validate_permissions(inter, [{Permission.MentionBlackList: False}]):
            return None

        if count > max_voted and not has_permissions(inter.author, inter.channel, Permission.ManageMention):
            vote_ad = "" if voted else "\n\n" + i18n.t("top_gg_cog.voting_ad", locale=gid)
            return i18n.t("mentions_cog.max_count_exceeded", locale=gid, max=MAX_MESSAGES) + vote_ad

        if count <= 0:
            return i18n.t("mentions_cog.invalid_count", locale=gid)

        message_lower = message.lower()
        for word in BAN_WORDS:
            if word in message_lower:
                return i18n.t("mentions_cog.banned_words", locale=gid)

        #if not is_role and has_permissions(inter.author, inter.channel, Permission.ManageMention):
        #    return i18n.t("mentions_cog.mention_forbidden", locale=gid)

        if is_role and not can_ping_role(inter, inter.guild.get_role(target_id)):
            return i18n.t("mentions_cog.role_forbidden", locale=gid)

        now = time.time()
        cooldown_time = self.cooldowns.get(user_id)
        if cooldown_time is not None and not has_permissions(inter.author, inter.channel, Permission.ManageMention):
            if cooldown_time > now:
                remaining = int(cooldown_time - now)
                return i18n.t("mentions_cog.cooldown", locale=gid, seconds=remaining)
            else:
                self.cooldowns.pop(user_id, None)

        return None

    async def mention_cycle(self, inter: disnake.ApplicationCommandInteraction, count: int,
                             full_text: str = "", delay: float = 1.0):
        gid = inter.guild_id
        for _ in range(count):
            if self.need_to_stop:
                await inter.channel.send(i18n.t("mentions_cog.stopped", locale=gid))
                break
            try:
                await inter.channel.send(full_text)
            except disnake.HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(4)
                else:
                    break
            await asyncio.sleep(delay)

    @commands.slash_command(
        name="mention",
        description=localized("commands.mention.description"),
    )
    async def mention(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
        pass

    @mention.sub_command(
        name="user",
        description=localized("commands.mention_user.description"),
    )
    async def mention_user(
            self,
            inter: disnake.ApplicationCommandInteraction,
            count: int = commands.Param(
                name=localized("commands.mention_user.param_count_name"),
                description=localized("commands.mention_user.param_count"),
            ),
            member: disnake.Member = commands.Param(
                name=localized("commands.mention_user.param_member_name"),
                description=localized("commands.mention_user.param_member"),
            ),
            message: str = commands.Param(
                default="",
                name=localized("commands.mention_user.param_message_name"),
                description=localized("commands.mention_user.param_message"),
            ),
            delay: float = commands.Param(
                default=1.0,
                name=localized("commands.mention_user.param_delay_name"),
                description=localized("commands.mention_user.param_delay"),
            ),
    ):
        gid = inter.guild_id

        error_msg = await self.validate_mention_request(inter, count, member.id, message, is_role=False)
        if error_msg:
            return await inter.response.send_message(error_msg, ephemeral=True)

        if not has_permissions(inter.author, inter.channel, Permission.ManageMention):
            self.cooldowns[inter.author.id] = time.time() + (COOLDOWN_TIME_VOTED if await is_voted(inter.author.id) else COOLDOWN_TIME)

        self.need_to_stop = False

        await inter.response.send_message(
            i18n.t("mentions_cog.started_member", locale=gid, count=count, member=member.display_name)
        )

        full_text = f"{member.mention} {message}".strip()
        delay = max(0.1, delay)

        return await self.mention_cycle(inter, count, full_text, delay)

    @mention.sub_command(
        name="role",
        description=localized("commands.mention_role.description"),
    )
    async def mention_role(
            self,
            inter: disnake.ApplicationCommandInteraction,
            count: int = commands.Param(
                name=localized("commands.mention_role.param_count_name"),
                description=localized("commands.mention_role.param_count"),
            ),
            role: disnake.Role = commands.Param(
                name=localized("commands.mention_role.param_role_name"),
                description=localized("commands.mention_role.param_role"),
            ),
            message: str = commands.Param(
                default="",
                name=localized("commands.mention_role.param_message_name"),
                description=localized("commands.mention_role.param_message"),
            ),
            delay: float = commands.Param(
                default=1.0,
                name=localized("commands.mention_role.param_delay_name"),
                description=localized("commands.mention_role.param_delay"),
            ),
    ):
        gid = inter.guild_id

        error_msg = await self.validate_mention_request(inter, count, role.id, message, is_role=True)
        if error_msg:
            return await inter.response.send_message(error_msg, ephemeral=True)

        if not has_permissions(inter.author, inter.channel, Permission.ManageMention):
            self.cooldowns[inter.author.id] = time.time() + COOLDOWN_TIME

        self.need_to_stop = False

        await inter.response.send_message(
            i18n.t("mentions_cog.started_role", locale=gid, count=count, role=role.name)
        )

        full_text = f"{role.mention} {message}".strip()
        delay = max(0.1, delay)

        return await self.mention_cycle(inter, count, full_text, delay)

    @mention.sub_command(
        name="stop",
        description=localized("commands.mention_stop.description"),
    )
    async def mention_stop(self, inter: disnake.ApplicationCommandInteraction):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.ManageMention: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        self.need_to_stop = True
        return await inter.response.send_message(
            i18n.t("mentions_cog.stop_signal_sent", locale=gid), ephemeral=True
        )


def setup(bot: commands.Bot):
    bot.add_cog(MentionsCog(bot))