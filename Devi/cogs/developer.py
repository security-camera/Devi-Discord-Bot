import disnake
from disnake.ext import commands

import i18n

from cogs.giveaways import load_giveaways
from cogs.birthdays import load_birthdays
from cogs.ai.ai_prompts import load_instructions
from logs import _load_log_channels
from discord_i18n import localized, bool_to_yes_no_str
from permissions import Permission, validate_permissions, load_permissions
from storage import TECHNICAL_SUPPORT_SERVER, TEST_SERVER

NONE_PLACEHOLDER = "—"

def _format_features(features: list[str]) -> str:
    if not features:
        return NONE_PLACEHOLDER
    text = ", ".join(sorted(features))
    return text if len(text) <= 1024 else text[:1021] + "…"


class DeveloperCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(
        name="reload",
        description=localized("commands.reload.description"),
    )
    async def reload(self, inter: disnake.ApplicationCommandInteraction):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.Developer: True}]):
            return None

        triggers_cog = self.bot.get_cog("TriggersCog")
        if triggers_cog is not None:
            triggers_cog.reload_triggers_from_disk()

        load_permissions()
        i18n.load_locales()
        load_giveaways()
        load_birthdays()
        load_instructions()
        _load_log_channels()

        return  await inter.response.send_message(
            i18n.t("reload_cmd.success", locale=gid),
            ephemeral=True
        )

    @commands.slash_command(
        name="guild_info",
        description=localized("commands.guild_info.description")
    )
    async def guild_info(
            self,
            inter: disnake.ApplicationCommandInteraction,
            guild_id: str = commands.Param(
                name=localized("commands.guild_info.param_guild_id_name"),
                description=localized("commands.guild_info.param_guild_id"),
            ),
            create_invite: bool = commands.Param(
                default=False,
                name=localized("commands.guild_info.param_create_invite_name"),
                description=localized("commands.guild_info.param_create_invite"),
            ),
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.Developer: True}]):
            return None

        try:
            target_id = int(guild_id.strip())
        except ValueError:
            return await inter.response.send_message(
                i18n.t("developer_cog.invalid_id", locale=gid),
                ephemeral=True,
            )

        await inter.response.defer(ephemeral=True)

        guild = self.bot.get_guild(target_id)
        approx_members = None
        approx_presences = None

        if not guild:
            # The bot can retrieve the guild through REST only if it is still a member of it
            # (Discord does not provide unrelated guilds using the bot's token) — this is just a fallback
            # in case the gateway cache has not been populated yet.
            try:
                guild = await self.bot.fetch_guild(target_id, with_counts=True)
                approx_members = getattr(guild, "approximate_member_count", None)
                approx_presences = getattr(guild, "approximate_presence_count", None)
            except disnake.NotFound:
                return await inter.followup.send(
                    i18n.t("developer_cog.not_found", locale=gid, id=target_id),
                    ephemeral=True,
                )
            except disnake.Forbidden:
                return await inter.followup.send(
                    i18n.t("developer_cog.forbidden", locale=gid, id=target_id),
                    ephemeral=True,
                )
            except disnake.HTTPException as e:
                return await inter.followup.send(
                    i18n.t("developer_cog.http_error", locale=gid, error=e),
                    ephemeral=True,
                )

        try:
            channels_list = await guild.fetch_channels()
        except (disnake.Forbidden, disnake.HTTPException):
            channels_list = list(guild.channels)

        invite = None
        invite_error_key = None
        new_invite = False
        if create_invite:
            invite, invite_error_key, new_invite = await self._create_temp_invite(guild, channels_list)

        embeds = self._build_embeds(
            gid, guild, channels_list, approx_members, approx_presences,
            invite=invite, new_invite=new_invite, invite_error_key=invite_error_key,
        )

        return await inter.followup.send(embeds=embeds, ephemeral=True)

    async def _create_temp_invite(self, guild: disnake.Guild, channels_list: list) -> tuple["disnake.Invite | None", "str | None", "bool | None"]:
        """Attempting of create new invite or use existing invite. Returns (invite, error_i18n_key)."""

        me = guild.me
        if not me:
            try:
                me = await guild.fetch_member(self.bot.user.id)
            except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
                me = None

        if not me:
            return None, "developer_cog.invite_no_member", False

        candidates = []
        if guild.system_channel:
            candidates.append(guild.system_channel)
        candidates += [c for c in channels_list if isinstance(c, disnake.TextChannel)]

        seen_ids = set()
        for channel in candidates:
            if not channel or channel.id in seen_ids:
                continue
            seen_ids.add(channel.id)

            perms = channel.permissions_for(me)
            if not perms.create_instant_invite:
                continue

            try:
                invite = await channel.create_invite(unique=False)
                if invite:
                    new_invite = False
                else:
                    new_invite = True
                    await channel.create_invite(max_age=1800, max_uses=1, unique=True)

                return invite, None, new_invite
            except (disnake.Forbidden, disnake.HTTPException):
                continue

        return None, "developer_cog.invite_no_channel", False

    @staticmethod
    def _build_embeds(
            ctx_guild_id: int,
            guild: disnake.Guild,
            channels_list: list,
            approx_members: int | None,
            approx_presences: int | None,
            invite: "disnake.Invite | None" = None,
            new_invite: bool | None = False,
            invite_error_key: str | None = None,
    ) -> list[disnake.Embed]:

        owner_text = f"<@{guild.owner_id}> (`{guild.owner_id}`)" if guild.owner_id else NONE_PLACEHOLDER
        created_ts = int(guild.created_at.timestamp())

        # ---------- General ----------
        general = disnake.Embed(
            title=guild.name,
            description=guild.description or NONE_PLACEHOLDER,
            color=disnake.Color.blurple(),
        )

        if guild.icon:
            general.set_thumbnail(url=guild.icon.url)

        general.add_field(name="ID", value=str(guild.id), inline=True)
        general.add_field(name=i18n.t("developer_cog.field_owner", locale=ctx_guild_id), value=owner_text, inline=True)
        general.add_field(
            name=i18n.t("developer_cog.field_created", locale=ctx_guild_id),
            value=f"<t:{created_ts}:F> (<t:{created_ts}:R>)",
            inline=False,
        )
        general.add_field(name=i18n.t("developer_cog.field_verification", locale=ctx_guild_id), value=str(guild.verification_level), inline=True)
        general.add_field(name=i18n.t("developer_cog.field_nsfw", locale=ctx_guild_id), value=str(guild.nsfw_level), inline=True)
        general.add_field(name=i18n.t("developer_cog.field_content_filter", locale=ctx_guild_id), value=str(guild.explicit_content_filter), inline=True)
        general.add_field(name=i18n.t("developer_cog.field_mfa", locale=ctx_guild_id), value=str(guild.mfa_level), inline=True)
        general.add_field(name=i18n.t("developer_cog.field_locale", locale=ctx_guild_id), value=str(guild.preferred_locale), inline=True)
        general.add_field(name=i18n.t("developer_cog.field_vanity", locale=ctx_guild_id), value=guild.vanity_url_code or NONE_PLACEHOLDER, inline=True)
        general.add_field(name=i18n.t("developer_cog.field_features", locale=ctx_guild_id), value=_format_features(guild.features), inline=False)

        if invite:
            general.add_field(
                name=i18n.t("developer_cog.field_invite", locale=ctx_guild_id),
                value=f"{invite.url}\n{i18n.t('developer_cog.invite_expiry_note', locale=ctx_guild_id) if new_invite else ''}",
                inline=False,
            )
        elif invite_error_key:
            general.add_field(
                name=i18n.t("developer_cog.field_invite", locale=ctx_guild_id),
                value=i18n.t(invite_error_key, locale=ctx_guild_id),
                inline=False,
            )

        # ---------- Stats ----------
        stats = disnake.Embed(title=i18n.t("developer_cog.title_stats", locale=ctx_guild_id), color=disnake.Color.blurple())

        member_count = guild.member_count if guild.member_count else approx_members
        stats.add_field(
            name=i18n.t("developer_cog.field_members", locale=ctx_guild_id),
            value=str(member_count) if member_count is not None else NONE_PLACEHOLDER,
            inline=True,
        )
        if approx_presences:
            stats.add_field(name=i18n.t("developer_cog.field_approx_online", locale=ctx_guild_id), value=str(approx_presences), inline=True)

        stats.add_field(name=i18n.t("developer_cog.field_max_members", locale=ctx_guild_id), value=str(guild.max_members or NONE_PLACEHOLDER), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_max_presences", locale=ctx_guild_id), value=str(guild.max_presences or NONE_PLACEHOLDER), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_large", locale=ctx_guild_id), value=bool_to_yes_no_str(guild.large, locale=ctx_guild_id), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_boost_tier", locale=ctx_guild_id), value=str(guild.premium_tier), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_boost_count", locale=ctx_guild_id), value=str(guild.premium_subscription_count or 0), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_roles", locale=ctx_guild_id), value=str(len(guild.roles)), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_emojis", locale=ctx_guild_id), value=str(len(guild.emojis)), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_stickers", locale=ctx_guild_id), value=str(len(guild.stickers)), inline=True)

        text_count = sum(1 for c in channels_list if isinstance(c, disnake.TextChannel))
        voice_count = sum(1 for c in channels_list if isinstance(c, disnake.VoiceChannel))
        category_count = sum(1 for c in channels_list if isinstance(c, disnake.CategoryChannel))
        stage_count = sum(1 for c in channels_list if isinstance(c, disnake.StageChannel))
        forum_type = getattr(disnake, "ForumChannel", ())
        forum_count = sum(1 for c in channels_list if isinstance(c, forum_type))

        stats.add_field(name=i18n.t("developer_cog.field_text_channels", locale=ctx_guild_id), value=str(text_count), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_voice_channels", locale=ctx_guild_id), value=str(voice_count), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_categories", locale=ctx_guild_id), value=str(category_count), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_stage_channels", locale=ctx_guild_id), value=str(stage_count), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_forum_channels", locale=ctx_guild_id), value=str(forum_count), inline=True)
        stats.add_field(name=i18n.t("developer_cog.field_total_channels", locale=ctx_guild_id), value=str(len(channels_list)), inline=True)

        # ---------- Extra ----------
        extra = disnake.Embed(title=i18n.t("developer_cog.title_channels", locale=ctx_guild_id), color=disnake.Color.blurple())

        extra.add_field(
            name=i18n.t("developer_cog.field_system_channel", locale=ctx_guild_id),
            value=guild.system_channel.mention if guild.system_channel else NONE_PLACEHOLDER,
            inline=True,
        )
        extra.add_field(
            name=i18n.t("developer_cog.field_rules_channel", locale=ctx_guild_id),
            value=guild.rules_channel.mention if guild.rules_channel else NONE_PLACEHOLDER,
            inline=True,
        )
        extra.add_field(
            name=i18n.t("developer_cog.field_updates_channel", locale=ctx_guild_id),
            value=guild.public_updates_channel.mention if guild.public_updates_channel else NONE_PLACEHOLDER,
            inline=True,
        )
        extra.add_field(
            name=i18n.t("developer_cog.field_afk_channel", locale=ctx_guild_id),
            value=guild.afk_channel.mention if guild.afk_channel else NONE_PLACEHOLDER,
            inline=True,
        )
        extra.add_field(name=i18n.t("developer_cog.field_afk_timeout", locale=ctx_guild_id), value=f"{guild.afk_timeout} сек.", inline=True)
        extra.add_field(name=i18n.t("developer_cog.field_widget", locale=ctx_guild_id), value=bool_to_yes_no_str(guild.widget_enabled, ctx_guild_id), inline=True)

        media_links = []
        if guild.icon:
            media_links.append(f"[{i18n.t('developer_cog.media_icon', locale=ctx_guild_id)}]({guild.icon.url})")
        if guild.banner:
            media_links.append(f"[{i18n.t('developer_cog.media_banner', locale=ctx_guild_id)}]({guild.banner.url})")
        if guild.splash:
            media_links.append(f"[{i18n.t('developer_cog.media_splash', locale=ctx_guild_id)}]({guild.splash.url})")
        if guild.discovery_splash:
            media_links.append(f"[{i18n.t('developer_cog.media_discovery_splash', locale=ctx_guild_id)}]({guild.discovery_splash.url})")
        extra.add_field(
            name=i18n.t("developer_cog.field_media", locale=ctx_guild_id),
            value=" • ".join(media_links) if media_links else NONE_PLACEHOLDER,
            inline=False,
        )

        return [general, stats, extra]


def setup(bot: commands.Bot):
    bot.add_cog(DeveloperCog(bot))