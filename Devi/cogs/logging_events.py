import asyncio
from datetime import datetime, timezone

import disnake
from disnake.ext import commands

import i18n
from logs import send_log, get_audit_executor, LogColor
from cogs.clear import purging_channels
from storage import TECHNICAL_SUPPORT_SERVER


def _guild_id_of(guild_or_id) -> int | None:
    if isinstance(guild_or_id, disnake.Guild):
        return guild_or_id.id
    return guild_or_id


class LoggingEventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ----------------- VOICE CHANNELS -----------------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState):
        gid = member.guild.id
        before_channel = before.channel
        after_channel = after.channel

        before_is_stage = isinstance(before_channel, disnake.StageChannel)
        after_is_stage = isinstance(after_channel, disnake.StageChannel)

        if before_channel is not None and after_channel is not None and before_channel.id == after_channel.id:
            return

        member_field = i18n.t("logs_cog.fields.member", locale=gid)
        channel_field = i18n.t("logs_cog.fields.channel", locale=gid)

        if before_channel is None and after_channel is not None:
            if after_is_stage:
                await send_log(
                    member.guild,
                    i18n.t("logs_cog.titles.stage_joined", locale=gid),
                    color=LogColor.Stage,
                    fields=[(member_field, f"{member.mention} ({member.id})"), (channel_field, after_channel.mention)]
                )
            else:
                await send_log(
                    member.guild,
                    i18n.t("logs_cog.titles.voice_joined", locale=gid),
                    color=LogColor.Voice,
                    fields=[(member_field, f"{member.mention} ({member.id})"), (channel_field, after_channel.mention)]
                )
            return

        if before_channel is not None and after_channel is None:
            if before_is_stage:
                await send_log(
                    member.guild,
                    i18n.t("logs_cog.titles.stage_left", locale=gid),
                    color=LogColor.Stage,
                    fields=[(member_field, f"{member.mention} ({member.id})"), (channel_field, before_channel.mention)]
                )
            else:
                await send_log(
                    member.guild,
                    i18n.t("logs_cog.titles.voice_left", locale=gid),
                    color=LogColor.Voice,
                    fields=[(member_field, f"{member.mention} ({member.id})"), (channel_field, before_channel.mention)]
                )
            return

        if before_channel is not None and after_channel is not None and before_channel.id != after_channel.id:
            if before_is_stage and not after_is_stage:
                await send_log(
                    member.guild,
                    i18n.t("logs_cog.titles.stage_left", locale=gid),
                    color=LogColor.Stage,
                    fields=[(member_field, f"{member.mention} ({member.id})"), (channel_field, before_channel.mention)]
                )
                await send_log(
                    member.guild,
                    i18n.t("logs_cog.titles.voice_joined", locale=gid),
                    color=LogColor.Voice,
                    fields=[(member_field, f"{member.mention} ({member.id})"), (channel_field, after_channel.mention)]
                )
            elif after_is_stage and not before_is_stage:
                await send_log(
                    member.guild,
                    i18n.t("logs_cog.titles.voice_left", locale=gid),
                    color=LogColor.Voice,
                    fields=[(member_field, f"{member.mention} ({member.id})"), (channel_field, before_channel.mention)]
                )
                await send_log(
                    member.guild,
                    i18n.t("logs_cog.titles.stage_joined", locale=gid),
                    color=LogColor.Stage,
                    fields=[(member_field, f"{member.mention} ({member.id})"), (channel_field, after_channel.mention)]
                )
            else:
                await send_log(
                    member.guild,
                    i18n.t("logs_cog.titles.voice_moved", locale=gid),
                    color=LogColor.Voice,
                    fields=[
                        (member_field, f"{member.mention} ({member.id})"),
                        (i18n.t("logs_cog.fields.from_channel", locale=gid), before_channel.mention),
                        (i18n.t("logs_cog.fields.to_channel", locale=gid), after_channel.mention),
                    ]
                )

    @commands.Cog.listener()
    async def on_stage_instance_create(self, stage_instance: disnake.StageInstance):
        gid = stage_instance.guild.id
        executor = await get_audit_executor(
            stage_instance.guild, disnake.AuditLogAction.stage_instance_create, stage_instance.channel_id
        )
        unknown = i18n.t("logs_cog.common.unknown", locale=gid)
        await send_log(
            stage_instance.guild,
            i18n.t("logs_cog.titles.stage_opened", locale=gid),
            color=LogColor.Stage,
            fields=[
                (i18n.t("logs_cog.fields.channel", locale=gid),
                 stage_instance.channel.mention if stage_instance.channel else str(stage_instance.channel_id)),
                (i18n.t("logs_cog.fields.topic", locale=gid),
                 stage_instance.topic or i18n.t("logs_cog.common.dash", locale=gid)),
                (i18n.t("logs_cog.fields.opened_by", locale=gid), executor.mention if executor else unknown),
            ]
        )

    @commands.Cog.listener()
    async def on_stage_instance_delete(self, stage_instance: disnake.StageInstance):
        gid = stage_instance.guild.id
        executor = await get_audit_executor(
            stage_instance.guild, disnake.AuditLogAction.stage_instance_delete, stage_instance.channel_id
        )
        unknown = i18n.t("logs_cog.common.unknown", locale=gid)
        await send_log(
            stage_instance.guild,
            i18n.t("logs_cog.titles.stage_closed", locale=gid),
            color=LogColor.Stage,
            fields=[
                (i18n.t("logs_cog.fields.channel", locale=gid),
                 stage_instance.channel.mention if stage_instance.channel else str(stage_instance.channel_id)),
                (i18n.t("logs_cog.fields.closed_by", locale=gid), executor.mention if executor else unknown),
            ]
        )

    @commands.Cog.listener()
    async def on_stage_instance_update(self, before: disnake.StageInstance, after: disnake.StageInstance):
        if before.topic == after.topic:
            return
        gid = before.guild.id
        executor = await get_audit_executor(
            after.guild, disnake.AuditLogAction.stage_instance_update, after.channel_id
        )
        dash = i18n.t("logs_cog.common.dash", locale=gid)
        unknown = i18n.t("logs_cog.common.unknown", locale=gid)
        await send_log(
            before.guild,
            i18n.t("logs_cog.titles.stage_updated", locale=gid),
            color=LogColor.Stage,
            fields=[
                (i18n.t("logs_cog.fields.channel", locale=gid),
                 after.channel.mention if after.channel else str(after.channel_id)),
                (i18n.t("logs_cog.fields.old_topic", locale=gid), before.topic or dash),
                (i18n.t("logs_cog.fields.new_topic", locale=gid), after.topic or dash),
                (i18n.t("logs_cog.fields.updated_by", locale=gid), executor.mention if executor else unknown),
            ]
        )

    # ----------------- MESSAGES -----------------
    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if message.author.bot:
            return

        if not isinstance(message.channel, disnake.DMChannel):
            return

        print(f"{message.author} ({message.author.id}): {message.content}")

        gid = TECHNICAL_SUPPORT_SERVER
        await send_log(
            gid,
            i18n.t("logs_cog.titles.dm_received", locale=gid),
            color=LogColor.Bot,
            fields=[
                (i18n.t("logs_cog.fields.member", locale=gid), str(message.author.mention)),
                (i18n.t("logs_cog.fields.text", locale=gid), str(message.content)),
                (i18n.t("logs_cog.fields.attachments", locale=gid),
                 str(message.attachments) or i18n.t("logs_cog.common.dash", locale=gid)),
            ]
        )

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: disnake.RawMessageUpdateEvent):
        gid = payload.guild_id
        data = payload.data or {}

        if "content" not in data:
            return  # text not changed

        new_content = data.get("content", "")
        cached = payload.cached_message

        if cached is not None:
            if cached.author.bot:
                return
            if cached.content == new_content:
                return
            before_content = cached.content
            author_display = f"{cached.author.mention} ({cached.author.id})"
        else:
            author_data = data.get("author") or {}
            if author_data.get("bot"):
                return
            author_id = author_data.get("id")
            author_display = f"<@{author_id}>" if author_id else i18n.t("logs_cog.common.unknown_not_cached", locale=gid)
            before_content = i18n.t("logs_cog.common.content_not_cached", locale=gid)

        def trunc(text: str) -> str:
            text = text or i18n.t("logs_cog.common.dash", locale=gid)
            return text if len(text) <= 1000 else text[:1000] + "…"

        channel = self.bot.get_channel(payload.channel_id)
        channel_display = channel.mention if channel is not None and hasattr(channel, "mention") else str(payload.channel_id)
        jump_url = f"https://discord.com/channels/{payload.guild_id or '@me'}/{payload.channel_id}/{payload.message_id}"

        await send_log(
            payload.guild_id,
            i18n.t("logs_cog.titles.message_edited", locale=gid),
            color=LogColor.Message,
            fields=[
                (i18n.t("logs_cog.fields.author", locale=gid), author_display),
                (i18n.t("logs_cog.fields.channel", locale=gid), channel_display),
                (i18n.t("logs_cog.fields.before", locale=gid), trunc(before_content)),
                (i18n.t("logs_cog.fields.after", locale=gid), trunc(new_content)),
                (i18n.t("logs_cog.fields.link", locale=gid), i18n.t("logs_cog.common.jump_to_message", locale=gid, jump_url=jump_url)),
            ]
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: disnake.RawBulkMessageDeleteEvent):
        # /clear already logs this
        pass

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: disnake.RawMessageDeleteEvent):
        if payload.channel_id in purging_channels:
            return  # part of /clear — already logged

        gid = payload.guild_id
        message = payload.cached_message

        if message is not None and message.author.bot:
            return

        def trunc(text: str) -> str:
            text = text or i18n.t("logs_cog.common.dash", locale=gid)
            return text if len(text) <= 1000 else text[:1000] + "…"

        channel = self.bot.get_channel(payload.channel_id)
        channel_display = channel.mention if channel is not None and hasattr(channel, "mention") else str(payload.channel_id)

        if message is None:
            await send_log(
                payload.guild_id,
                i18n.t("logs_cog.titles.message_deleted", locale=gid),
                color=LogColor.Message,
                fields=[
                    (i18n.t("logs_cog.fields.author", locale=gid), i18n.t("logs_cog.common.unknown_not_cached", locale=gid)),
                    (i18n.t("logs_cog.fields.channel", locale=gid), channel_display),
                    (i18n.t("logs_cog.fields.content", locale=gid), i18n.t("logs_cog.common.content_not_cached", locale=gid)),
                    (i18n.t("logs_cog.fields.deleted_by", locale=gid), i18n.t("logs_cog.common.unknown", locale=gid)),
                ]
            )
            return

        await asyncio.sleep(1)

        executor = None
        if payload.guild_id is not None:
            guild = self.bot.get_guild(payload.guild_id)
            executor = await get_audit_executor(
                guild, disnake.AuditLogAction.message_delete, message.author.id, max_age_seconds=10
            )

        deleted_by_text = (
            executor.mention if executor and executor.id != message.author.id
            else i18n.t("logs_cog.common.message_author", locale=gid)
        )

        await send_log(
            payload.guild_id,
            i18n.t("logs_cog.titles.message_deleted", locale=gid),
            color=LogColor.Message,
            fields=[
                (i18n.t("logs_cog.fields.author", locale=gid), f"{message.author.mention} ({message.author.id})"),
                (i18n.t("logs_cog.fields.channel", locale=gid), channel_display),
                (i18n.t("logs_cog.fields.content", locale=gid), trunc(message.content)),
                (i18n.t("logs_cog.fields.deleted_by", locale=gid), deleted_by_text),
            ]
        )

    # ----------------- GUILD MEMBERS -----------------
    @commands.Cog.listener()
    async def on_member_join(self, member: disnake.Member):
        gid = member.guild.id
        await send_log(
            member.guild,
            i18n.t("logs_cog.titles.member_joined", locale=gid),
            color=LogColor.Member,
            fields=[
                (i18n.t("logs_cog.fields.member", locale=gid), f"{member.mention} ({member.id})"),
                (i18n.t("logs_cog.fields.account_created", locale=gid), f"<t:{int(member.created_at.timestamp())}:R>"),
            ],
            thumbnail_url=member.display_avatar.url
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: disnake.Member):
        guild = member.guild
        gid = guild.id
        await asyncio.sleep(1.5)

        kicker = await get_audit_executor(guild, disnake.AuditLogAction.kick, member.id)
        if kicker is not None:
            await send_log(
                guild,
                i18n.t("logs_cog.titles.member_kicked", locale=gid),
                color=LogColor.Moderation,
                fields=[
                    (i18n.t("logs_cog.fields.member", locale=gid), f"{member} ({member.id})"),
                    (i18n.t("logs_cog.fields.kicked_by", locale=gid), f"{kicker.mention} ({kicker.id})"),
                ],
                thumbnail_url=member.display_avatar.url
            )
            return

        banner = await get_audit_executor(guild, disnake.AuditLogAction.ban, member.id)
        if banner is not None:
            return  # on_member_ban log this

        await send_log(
            guild,
            i18n.t("logs_cog.titles.member_left", locale=gid),
            color=LogColor.Member,
            fields=[(i18n.t("logs_cog.fields.member", locale=gid), f"{member} ({member.id})")],
            thumbnail_url=member.display_avatar.url
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: disnake.Guild, user: disnake.User):
        gid = guild.id
        executor = await get_audit_executor(guild, disnake.AuditLogAction.ban, user.id)
        await send_log(
            guild,
            i18n.t("logs_cog.titles.member_banned", locale=gid),
            color=LogColor.Moderation,
            fields=[
                (i18n.t("logs_cog.fields.member", locale=gid), f"{user} ({user.id})"),
                (i18n.t("logs_cog.fields.banned_by", locale=gid),
                 executor.mention if executor else i18n.t("logs_cog.common.unknown", locale=gid)),
            ],
            thumbnail_url=user.display_avatar.url
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: disnake.Guild, user: disnake.User):
        gid = guild.id
        executor = await get_audit_executor(guild, disnake.AuditLogAction.unban, user.id)
        await send_log(
            guild,
            i18n.t("logs_cog.titles.member_unbanned", locale=gid),
            color=LogColor.Moderation,
            fields=[
                (i18n.t("logs_cog.fields.member", locale=gid), f"{user} ({user.id})"),
                (i18n.t("logs_cog.fields.unbanned_by", locale=gid),
                 executor.mention if executor else i18n.t("logs_cog.common.unknown", locale=gid)),
            ],
            thumbnail_url=user.display_avatar.url
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: disnake.Member, after: disnake.Member):
        guild = after.guild
        gid = guild.id

        if before.nick != after.nick:
            executor = await get_audit_executor(guild, disnake.AuditLogAction.member_update, after.id)
            await send_log(
                guild,
                i18n.t("logs_cog.titles.nickname_changed", locale=gid),
                color=LogColor.Member,
                fields=[
                    (i18n.t("logs_cog.fields.member", locale=gid), f"{after.mention} ({after.id})"),
                    (i18n.t("logs_cog.fields.old_nickname", locale=gid), before.nick or before.name),
                    (i18n.t("logs_cog.fields.new_nickname", locale=gid), after.nick or after.name),
                    (i18n.t("logs_cog.fields.changed_by_nickname", locale=gid),
                     executor.mention if executor else i18n.t("logs_cog.common.unknown_or_self", locale=gid)),
                ]
            )

        if set(before.roles) != set(after.roles):
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            executor = await get_audit_executor(guild, disnake.AuditLogAction.member_role_update, after.id)
            dash = i18n.t("logs_cog.common.dash", locale=gid)
            await send_log(
                guild,
                i18n.t("logs_cog.titles.roles_updated", locale=gid),
                color=LogColor.Member,
                fields=[
                    (i18n.t("logs_cog.fields.member", locale=gid), f"{after.mention} ({after.id})"),
                    (i18n.t("logs_cog.fields.added", locale=gid), ", ".join(r.mention for r in added) if added else dash),
                    (i18n.t("logs_cog.fields.removed", locale=gid), ", ".join(r.mention for r in removed) if removed else dash),
                    (i18n.t("logs_cog.fields.changed_by_roles", locale=gid),
                     executor.mention if executor else i18n.t("logs_cog.common.unknown", locale=gid)),
                ]
            )

        before_timeout = before.current_timeout
        after_timeout = after.current_timeout
        now = datetime.now(timezone.utc)

        before_muted = before_timeout is not None and before_timeout > now
        after_muted = after_timeout is not None and after_timeout > now

        if not before_muted and after_muted:
            executor = await get_audit_executor(guild, disnake.AuditLogAction.member_update, after.id)
            await send_log(
                guild,
                i18n.t("logs_cog.titles.member_muted", locale=gid),
                color=LogColor.Moderation,
                fields=[
                    (i18n.t("logs_cog.fields.member", locale=gid), f"{after.mention} ({after.id})"),
                    (i18n.t("logs_cog.fields.before", locale=gid), f"<t:{int(after_timeout.timestamp())}:f>"),
                    (i18n.t("logs_cog.fields.muted_by", locale=gid),
                     executor.mention if executor else i18n.t("logs_cog.common.unknown", locale=gid)),
                ]
            )
        elif before_muted and not after_muted:
            executor = await get_audit_executor(guild, disnake.AuditLogAction.member_update, after.id)
            await send_log(
                guild,
                i18n.t("logs_cog.titles.member_unmuted", locale=gid),
                color=LogColor.Moderation,
                fields=[
                    (i18n.t("logs_cog.fields.member", locale=gid), f"{after.mention} ({after.id})"),
                    (i18n.t("logs_cog.fields.unmuted_by", locale=gid),
                     executor.mention if executor else i18n.t("logs_cog.common.unknown_or_expired", locale=gid)),
                ]
            )

    # ----------------- COMMAND USAGE -----------------
    @commands.Cog.listener()
    async def on_slash_command_completion(self, inter: disnake.ApplicationCommandInteraction):
        gid = inter.guild_id

        if "key" in inter.application_command.qualified_name: #Do not log secret information
            options_text = i18n.t("logs_cog.common.dash", locale=gid)
        else:
            options_text = ", ".join(
                f"{k}: {v}" for k, v in inter.filled_options.items()
            ) if inter.filled_options else i18n.t("logs_cog.common.dash", locale=gid)

        await send_log(
            inter.guild,
            i18n.t("logs_cog.titles.command_used", locale=gid),
            color=LogColor.Command,
            fields=[
                (i18n.t("logs_cog.fields.command", locale=gid), f"/{inter.application_command.qualified_name}"),
                (i18n.t("logs_cog.fields.user", locale=gid), f"{inter.author.mention} ({inter.author.id})"),
                (i18n.t("logs_cog.fields.channel", locale=gid),
                 inter.channel.mention if hasattr(inter.channel, "mention") else str(inter.channel.id)),
                (i18n.t("logs_cog.fields.params", locale=gid), options_text),
            ]
        )

    # ----------------- BOT ADDED / REMOVED -----------------
    @commands.Cog.listener()
    async def on_guild_join(self, guild: disnake.Guild):
        gid = guild.id
        await send_log(
            guild,
            i18n.t("logs_cog.titles.bot_added", locale=gid),
            color=LogColor.Bot,
            fields=[
                (i18n.t("logs_cog.fields.server", locale=gid), f"{guild.name} ({guild.id})"),
                (i18n.t("logs_cog.fields.members_count", locale=gid), str(guild.member_count)),
            ]
        )

        await send_log(
            TECHNICAL_SUPPORT_SERVER,
            i18n.t("logs_cog.titles.bot_added", guild_id=TECHNICAL_SUPPORT_SERVER),
            color=LogColor.Bot,
            fields=[
                (i18n.t("logs_cog.fields.server", guild_id=TECHNICAL_SUPPORT_SERVER), f"{guild.name} ({guild.id})"),
                (i18n.t("logs_cog.fields.members_count", guild_id=TECHNICAL_SUPPORT_SERVER), str(guild.member_count)),
            ]
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: disnake.Guild):
        gid = guild.id
        await send_log(
            guild,
            i18n.t("logs_cog.titles.bot_removed", locale=gid),
            color=LogColor.Bot,
            fields=[(i18n.t("logs_cog.fields.server", locale=gid), f"{guild.name} ({guild.id})")]
        )


def setup(bot: commands.Bot):
    bot.add_cog(LoggingEventsCog(bot))