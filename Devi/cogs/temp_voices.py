from datetime import datetime, timezone

import disnake
from disnake import TextInputStyle
from disnake.ext import commands

import i18n
from db import db_cursor
from discord_i18n import localized
from logs import send_log, LogColor
from permissions import validate_permissions, has_permissions, Permission, PermissionCheckType
from other_apis.topgg_utils import validate_vote
from paths import env_var_to_bool, env_var

DEFAULT_NAME_TEMPLATE = env_var("DEFAULT_NAME_TEMPLATE", "[user]`s channel")
ADMINS_CAN_MANAGE_TEMP_CHANNELS = env_var_to_bool("ADMINS_CAN_MANAGE_TEMP_CHANNELS", False)

REGION_CHOICES = [
    disnake.OptionChoice(name="Automatic", value="automatic"),
    disnake.OptionChoice(name="Brazil", value="brazil"),
    disnake.OptionChoice(name="Hong Kong", value="hongkong"),
    disnake.OptionChoice(name="India", value="india"),
    disnake.OptionChoice(name="Japan", value="japan"),
    disnake.OptionChoice(name="Rotterdam", value="rotterdam"),
    disnake.OptionChoice(name="Russia", value="russia"),
    disnake.OptionChoice(name="Singapore", value="singapore"),
    disnake.OptionChoice(name="South Africa", value="southafrica"),
    disnake.OptionChoice(name="Sydney", value="sydney"),
    disnake.OptionChoice(name="US Central", value="us-central"),
    disnake.OptionChoice(name="US East", value="us-east"),
    disnake.OptionChoice(name="US South", value="us-south"),
    disnake.OptionChoice(name="US West", value="us-west"),
]


# ------------------------------------------------------------------ logging


def log_field(inter: disnake.Interaction, key: str, value: str) -> tuple[str, str]:
    """Builds a localized (name, value) log field. `key` is the suffix of the
    locale key temp_voices_cog.logs.field_<key>."""
    return i18n.t(f"temp_voices_cog.logs.field_{key}", locale=inter.guild_id), value


async def log_voice(
    inter: disnake.Interaction,
    name: str,
    channel: disnake.abc.GuildChannel,
    fields: list[tuple[str, str]] | None = None,
):
    """
    Sends a temp voice action log to the guild's log channel.

    `name` is the suffix of the locale key temp_voices_cog.logs.<name> used as the
    embed title. The acting user and the channel are always the first two fields.
    The channel is logged by name + id (not by mention), because temp channels
    are deleted when empty and a mention would turn into "#deleted-channel".
    """
    await send_log(
        inter.guild,
        i18n.t(f"temp_voices_cog.logs.{name}", locale=inter.guild_id),
        color=LogColor.TempVoice,
        fields=[
            log_field(inter, "user", f"{inter.author.mention} ({inter.author.id})"),
            log_field(inter, "channel", f"{channel.mention} ({channel.id})"),
            *(fields or []),
        ],
    )


# ------------------------------------------------------------------ storage


def get_guild_config(guild_id: int) -> dict | None:
    with db_cursor() as cur:
        cur.execute(
            "SELECT lobby_channel_id, category_id, name_template FROM temp_voice_config WHERE guild_id = ?",
            (guild_id,),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return {
        "lobby_channel_id": row["lobby_channel_id"],
        "category_id": row["category_id"],
        "name_template": row["name_template"] or DEFAULT_NAME_TEMPLATE,
    }


def set_guild_config(guild_id: int, lobby_channel_id: int, category_id: int | None, name_template: str | None):
    with db_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO temp_voice_config (guild_id, lobby_channel_id, category_id, name_template)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   lobby_channel_id = excluded.lobby_channel_id,
                   category_id = excluded.category_id,
                   name_template = excluded.name_template""",
            (guild_id, lobby_channel_id, category_id, name_template),
        )


def get_temp_channel(channel_id: int) -> dict | None:
    with db_cursor() as cur:
        cur.execute(
            "SELECT guild_id, owner_id, created_at FROM temp_voice_channels WHERE channel_id = ?",
            (channel_id,),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return {"guild_id": row["guild_id"], "owner_id": row["owner_id"], "created_at": row["created_at"]}


def all_temp_channels() -> list[dict]:
    with db_cursor() as cur:
        cur.execute("SELECT channel_id, guild_id, owner_id FROM temp_voice_channels")
        rows = cur.fetchall()
    return [{"channel_id": r["channel_id"], "guild_id": r["guild_id"], "owner_id": r["owner_id"]} for r in rows]


def save_temp_channel(channel_id: int, guild_id: int, owner_id: int):
    with db_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO temp_voice_channels (channel_id, guild_id, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (channel_id, guild_id, owner_id, datetime.now(timezone.utc).isoformat()),
        )


def set_temp_channel_owner(channel_id: int, owner_id: int):
    with db_cursor(commit=True) as cur:
        cur.execute("UPDATE temp_voice_channels SET owner_id = ? WHERE channel_id = ?", (owner_id, channel_id))


def remove_temp_channel(channel_id: int):
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM temp_voice_channels WHERE channel_id = ?", (channel_id,))


def get_user_settings(user_id: int) -> dict:
    with db_cursor() as cur:
        cur.execute(
            "SELECT name, user_limit, locked, bitrate, rtc_region FROM temp_voice_user_settings WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()

    if row is None:
        return {"name": None, "user_limit": None, "locked": False, "bitrate": None, "rtc_region": None}

    return {
        "name": row["name"],
        "user_limit": row["user_limit"],
        "locked": bool(row["locked"]),
        "bitrate": row["bitrate"],
        "rtc_region": row["rtc_region"],
    }


def save_user_setting(user_id: int, **fields):
    """Partial update — only the changed fields can be provided;
     the remaining fields will stay as they were (read-modify-write)."""
    current = get_user_settings(user_id)
    current.update(fields)

    with db_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO temp_voice_user_settings (user_id, name, user_limit, locked, bitrate, rtc_region)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   name = excluded.name,
                   user_limit = excluded.user_limit,
                   locked = excluded.locked,
                   bitrate = excluded.bitrate,
                   rtc_region = excluded.rtc_region""",
            (
                user_id,
                current["name"],
                current["user_limit"],
                int(current["locked"]),
                current["bitrate"],
                current["rtc_region"],
            ),
        )


def get_user_overwrites(user_id: int) -> dict[int, bool]:
    with db_cursor() as cur:
        cur.execute("SELECT target_id, allowed FROM temp_voice_user_overwrites WHERE user_id = ?", (user_id,))
        rows = cur.fetchall()
    return {row["target_id"]: bool(row["allowed"]) for row in rows}


def save_user_overwrite(user_id: int, target_id: int, allowed: bool):
    with db_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO temp_voice_user_overwrites (user_id, target_id, allowed) VALUES (?, ?, ?)
               ON CONFLICT(user_id, target_id) DO UPDATE SET allowed = excluded.allowed""",
            (user_id, target_id, int(allowed)),
        )


# --------------------------------------------------------------------- UI


class RenameModal(disnake.ui.Modal):
    def __init__(self, cog: "TempVoicesCog", channel_id: int, guild_id: int):
        self.cog = cog
        self.channel_id = channel_id
        components = [
            disnake.ui.TextInput(
                label=i18n.t("temp_voices_cog.modal_name_label", locale=guild_id),
                custom_id="name",
                style=TextInputStyle.short,
                max_length=100,
            )
        ]
        super().__init__(
            title=i18n.t("temp_voices_cog.modal_name_title", locale=guild_id),
            custom_id="temp_voice_rename_modal",
            components=components,
        )

    async def callback(self, inter: disnake.ModalInteraction):
        await self.cog.action_rename(inter, self.channel_id, inter.text_values["name"])


class LimitModal(disnake.ui.Modal):
    def __init__(self, cog: "TempVoicesCog", channel_id: int, guild_id: int):
        self.cog = cog
        self.channel_id = channel_id
        components = [
            disnake.ui.TextInput(
                label=i18n.t("temp_voices_cog.modal_limit_label", locale=guild_id),
                custom_id="limit",
                style=TextInputStyle.short,
                max_length=2,
                placeholder="0-99, 0 = " + i18n.t("temp_voices_cog.unlimited", locale=guild_id),
            )
        ]
        super().__init__(
            title=i18n.t("temp_voices_cog.modal_limit_title", locale=guild_id),
            custom_id="temp_voice_limit_modal",
            components=components,
        )

    async def callback(self, inter: disnake.ModalInteraction):
        raw = inter.text_values["limit"].strip()
        if not raw.isdigit() or not (0 <= int(raw) <= 99):
            return await inter.response.send_message(
                i18n.t("temp_voices_cog.invalid_limit", locale=inter.guild_id), ephemeral=True
            )
        return await self.cog.action_limit(inter, self.channel_id, int(raw))


class BitrateModal(disnake.ui.Modal):
    def __init__(self, cog: "TempVoicesCog", channel_id: int, guild_id: int):
        self.cog = cog
        self.channel_id = channel_id
        components = [
            disnake.ui.TextInput(
                label=i18n.t("temp_voices_cog.modal_bitrate_label", locale=guild_id),
                custom_id="bitrate",
                style=TextInputStyle.short,
                max_length=3,
                placeholder="8-384 (кбит/с)",
            )
        ]
        super().__init__(
            title=i18n.t("temp_voices_cog.modal_bitrate_title", locale=guild_id),
            custom_id="temp_voice_bitrate_modal",
            components=components,
        )

    async def callback(self, inter: disnake.ModalInteraction):
        raw = inter.text_values["bitrate"].strip()
        if not raw.isdigit():
            return await inter.response.send_message(
                i18n.t("temp_voices_cog.invalid_bitrate", locale=inter.guild_id), ephemeral=True
            )
        return await self.cog.action_bitrate(inter, self.channel_id, int(raw))


class MemberTargetSelectView(disnake.ui.View):
    """The second step for kick/allow/ban/transfer — selecting a specific member;
    it opens as an ephemeral message on top of the main panel without affecting it."""

    def __init__(self, cog: "TempVoicesCog", channel_id: int, action: str, guild_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.channel_id = channel_id
        self.action = action

        select = disnake.ui.UserSelect(
            placeholder=i18n.t("temp_voices_cog.select_member_placeholder", locale=guild_id),
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, inter: disnake.MessageInteraction):
        target_id = int(inter.values[0])

        if self.action == "kick":
            await self.cog.action_kick(inter, self.channel_id, target_id)
        elif self.action == "allow":
            await self.cog.action_permission(inter, self.channel_id, target_id, True)
        elif self.action == "ban":
            await self.cog.action_permission(inter, self.channel_id, target_id, False)
        elif self.action == "transfer":
            await self.cog.action_transfer(inter, self.channel_id, target_id)


class VoiceControlView(disnake.ui.View):
    """The /voice interface panel. IMPORTANT: the channel is "baked into" a specific instance of this View when
    the panel is sent — after the bot restarts, old panels stop responding to buttons (not a persistent view)
    until the user invokes /voice interface again."""

    def __init__(self, cog: "TempVoicesCog", channel_id: int, guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.channel_id = channel_id

        lock_button = disnake.ui.Button(
            label=i18n.t("temp_voices_cog.button_lock_unlock", locale=guild_id),
            emoji="🔒", style=disnake.ButtonStyle.secondary, row=0,
        )
        lock_button.callback = self.on_lock_toggle
        self.add_item(lock_button)

        claim_button = disnake.ui.Button(
            label=i18n.t("temp_voices_cog.button_claim", locale=guild_id),
            emoji="👑", style=disnake.ButtonStyle.secondary, row=0,
        )
        claim_button.callback = self.on_claim
        self.add_item(claim_button)

        rename_button = disnake.ui.Button(
            label=i18n.t("temp_voices_cog.button_rename", locale=guild_id),
            emoji="✏️", style=disnake.ButtonStyle.secondary, row=0,
        )
        rename_button.callback = self.on_rename_button
        self.add_item(rename_button)

        limit_button = disnake.ui.Button(
            label=i18n.t("temp_voices_cog.button_limit", locale=guild_id),
            emoji="👤", style=disnake.ButtonStyle.secondary, row=1,
        )
        limit_button.callback = self.on_limit_button
        self.add_item(limit_button)

        bitrate_button = disnake.ui.Button(
            label=i18n.t("temp_voices_cog.button_bitrate", locale=guild_id),
            emoji="🎚️", style=disnake.ButtonStyle.secondary, row=1,
        )
        bitrate_button.callback = self.on_bitrate_button
        self.add_item(bitrate_button)

        region_select = disnake.ui.StringSelect(
            placeholder=i18n.t("temp_voices_cog.select_region_placeholder", locale=guild_id),
            options=[disnake.SelectOption(label=c.name, value=c.value) for c in REGION_CHOICES],
            row=2,
        )
        region_select.callback = self.on_region_select
        self.add_item(region_select)

        manage_select = disnake.ui.StringSelect(
            placeholder=i18n.t("temp_voices_cog.select_manage_placeholder", locale=guild_id),
            options=[
                disnake.SelectOption(label=i18n.t("temp_voices_cog.action_kick", locale=guild_id), value="kick", emoji="🚪"),
                disnake.SelectOption(label=i18n.t("temp_voices_cog.action_allow", locale=guild_id), value="allow", emoji="✅"),
                disnake.SelectOption(label=i18n.t("temp_voices_cog.action_ban", locale=guild_id), value="ban", emoji="⛔"),
                disnake.SelectOption(label=i18n.t("temp_voices_cog.action_transfer", locale=guild_id), value="transfer", emoji="👑"),
            ],
            row=3,
        )
        manage_select.callback = self.on_manage_select
        self.add_item(manage_select)

    async def on_lock_toggle(self, inter: disnake.MessageInteraction):
        await self.cog.action_toggle_lock(inter, self.channel_id)

    async def on_claim(self, inter: disnake.MessageInteraction):
        await self.cog.action_claim(inter, self.channel_id)

    async def on_rename_button(self, inter: disnake.MessageInteraction):
        ctx = await self.cog.resolve_by_id(inter, self.channel_id)
        if ctx is None:
            return None
        _, record = ctx
        if not self.cog.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)
        return await inter.response.send_modal(RenameModal(self.cog, self.channel_id, inter.guild_id))

    async def on_limit_button(self, inter: disnake.MessageInteraction):
        ctx = await self.cog.resolve_by_id(inter, self.channel_id)
        if ctx is None:
            return None
        _, record = ctx
        if not self.cog.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)
        return await inter.response.send_modal(LimitModal(self.cog, self.channel_id, inter.guild_id))

    async def on_bitrate_button(self, inter: disnake.MessageInteraction):
        ctx = await self.cog.resolve_by_id(inter, self.channel_id)
        if ctx is None:
            return None
        _, record = ctx
        if not self.cog.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)
        return await inter.response.send_modal(BitrateModal(self.cog, self.channel_id, inter.guild_id))

    async def on_region_select(self, inter: disnake.MessageInteraction):
        region = inter.values[0]
        await self.cog.action_region(inter, self.channel_id, None if region == "automatic" else region)

    async def on_manage_select(self, inter: disnake.MessageInteraction):
        action = inter.values[0]
        ctx = await self.cog.resolve_by_id(inter, self.channel_id)
        if ctx is None:
            return None
        _, record = ctx
        if not self.cog.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)

        view = MemberTargetSelectView(self.cog, self.channel_id, action, inter.guild_id)
        return await inter.response.send_message(
            i18n.t("temp_voices_cog.select_member_prompt", locale=inter.guild_id),
            view=view,
            ephemeral=True,
        )


# --------------------------------------------------------------------- cog


class TempVoicesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        """Cleans up the database/stale empty channels if the bot was offline
        when a temporary channel was supposed to be deleted."""
        for record in all_temp_channels():
            guild = self.bot.get_guild(record["guild_id"])
            channel = guild.get_channel(record["channel_id"]) if guild else None

            if channel is None:
                remove_temp_channel(record["channel_id"])
                continue

            if len(channel.members) == 0:
                try:
                    await channel.delete(reason="Empty temp voice after bot restart")
                except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
                    pass
                remove_temp_channel(record["channel_id"])

    # ------------------------------------------------------- voice events

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState):
        config = get_guild_config(member.guild.id)

        if config and after.channel is not None and after.channel.id == config["lobby_channel_id"]:
            await self._create_temp_channel(member, after.channel, config)

        if before.channel is not None and before.channel != after.channel:
            await self._maybe_delete_empty_channel(before.channel)

    async def _create_temp_channel(self, member: disnake.Member, lobby_channel: disnake.VoiceChannel, config: dict):
        prefs = get_user_settings(member.id)
        overwrites_map = get_user_overwrites(member.id)

        name_template = config["name_template"] or DEFAULT_NAME_TEMPLATE
        _name = prefs["name"] or name_template
        name = self.format_name(_name, member)

        category = member.guild.get_channel(config["category_id"]) if config["category_id"] else lobby_channel.category

        everyone = member.guild.default_role
        overwrites = {
            everyone: disnake.PermissionOverwrite(connect=not prefs["locked"]),
            member: disnake.PermissionOverwrite(connect=True),
        }
        for target_id, allowed in overwrites_map.items():
            target = member.guild.get_member(target_id)
            if target is not None:
                overwrites[target] = disnake.PermissionOverwrite(connect=allowed)

        try:
            channel = await member.guild.create_voice_channel(
                name=name,
                category=category,
                user_limit=prefs["user_limit"] or 0,
                bitrate=prefs["bitrate"] or lobby_channel.bitrate,
                rtc_region=prefs["rtc_region"],
                overwrites=overwrites,
                reason=f"Temp voice for {member}",
            )
        except (disnake.Forbidden, disnake.HTTPException) as e:
            print(f"[temp_voices] Не удалось создать временный канал для {member}: {e}")
            return

        save_temp_channel(channel.id, member.guild.id, member.id)

        try:
            await member.move_to(channel, reason="Moving to temp voice")
            await channel.send(member.mention)
        except (disnake.Forbidden, disnake.HTTPException):
            pass

        await self.send_interface(channel)

    @staticmethod
    async def _maybe_delete_empty_channel(channel: disnake.abc.GuildChannel):
        if not isinstance(channel, disnake.VoiceChannel):
            return
        if get_temp_channel(channel.id) is None:
            return
        if len(channel.members) > 0:
            return

        try:
            await channel.delete(reason="Temp voice is empty")
        except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
            pass

        remove_temp_channel(channel.id)

    # ------------------------------------------------------------ helpers

    @staticmethod
    def format_name(name: str, member: disnake.Member) -> str:
        return name.replace("[user]", member.display_name).replace("{user}", member.display_name)[:100]

    @staticmethod
    def is_owner(inter: disnake.Interaction, record: dict) -> bool:
        return (inter.author.id == record["owner_id"] or
                (ADMINS_CAN_MANAGE_TEMP_CHANNELS and (
                        inter.author.guild_permissions.administrator or has_permissions(inter.author, inter.channel, Permission.Admin, PermissionCheckType.NONE))))

    @staticmethod
    async def resolve_from_voice_state(inter: disnake.ApplicationCommandInteraction) -> tuple[disnake.VoiceChannel, dict] | None:
        """For slash commands: the channel is determined by where the author is CURRENTLY located."""
        voice_state = inter.author.voice
        if voice_state is None or voice_state.channel is None:
            await inter.response.send_message(i18n.t("temp_voices_cog.not_in_voice", locale=inter.guild_id), ephemeral=True)
            return None

        record = get_temp_channel(voice_state.channel.id)
        if record is None:
            await inter.response.send_message(i18n.t("temp_voices_cog.not_a_temp_channel", locale=inter.guild_id), ephemeral=True)
            return None

        return voice_state.channel, record

    @staticmethod
    async def resolve_by_id(inter: disnake.Interaction, channel_id: int) -> tuple[disnake.VoiceChannel, dict] | None:
        """For panel buttons: the channel is already known (baked into the View)."""
        channel = inter.guild.get_channel(channel_id)
        record = get_temp_channel(channel_id)
        if channel is None or record is None:
            await inter.response.send_message(i18n.t("temp_voices_cog.channel_gone", locale=inter.guild_id), ephemeral=True)
            return None
        return channel, record

    async def send_interface(self, channel: disnake.VoiceChannel):
        """Sends the control panel for a temporary voice channel."""

        record = get_temp_channel(channel.id)
        if record is None:
            return None

        everyone_connect = channel.overwrites_for(channel.guild.default_role).connect
        locked = everyone_connect is False

        embed = disnake.Embed(
            title=i18n.t(
                "temp_voices_cog.interface_title",
                locale=channel.guild.id,
                name=channel.name,
            ),
            color=disnake.Color.blurple(),
        )

        embed.add_field(
            name=i18n.t("temp_voices_cog.field_owner", locale=channel.guild.id),
            value=f"<@{record['owner_id']}>",
            inline=True,
        )

        embed.add_field(
            name=i18n.t("temp_voices_cog.field_limit", locale=channel.guild.id),
            value=(
                str(channel.user_limit)
                if channel.user_limit
                else i18n.t("temp_voices_cog.unlimited", locale=channel.guild.id)
            ),
            inline=True,
        )

        embed.add_field(
            name=i18n.t("temp_voices_cog.field_status", locale=channel.guild.id),
            value=i18n.t(
                "temp_voices_cog.locked" if locked else "temp_voices_cog.unlocked",
                locale=channel.guild.id,
            ),
            inline=True,
        )

        embed.add_field(
            name=i18n.t("temp_voices_cog.field_bitrate", locale=channel.guild.id),
            value=f"{channel.bitrate // 1000} kbps",
            inline=True,
        )

        embed.add_field(
            name=i18n.t("temp_voices_cog.field_region", locale=channel.guild.id),
            value=(
                channel.rtc_region
                or i18n.t(
                    "temp_voices_cog.region_automatic_label",
                    locale=channel.guild.id,
                )
            ),
            inline=True,
        )

        return await channel.send(
            embed=embed,
            view=VoiceControlView(
                self,
                channel.id,
                channel.guild.id,
            ),
        )

    # -------------------------------------------------- shared actions
    # Every action logs to the guild's log channel only after it succeeded and the
    # user has been answered, so a slow log channel can't break the 3-second limit.

    async def action_rename(self, inter: disnake.Interaction, channel_id: int, name: str):
        ctx = await self.resolve_by_id(inter, channel_id)
        if ctx is None:
            return None
        channel, record = ctx
        if not self.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)

        # Name after "[user]" / "{user}" substitution — this is what the channel actually gets
        new_name = self.format_name(name, inter.author)

        try:
            await channel.edit(name=new_name, reason=f"Renamed ({inter.author})")
        except disnake.HTTPException as e:
            return await inter.response.send_message(i18n.t("temp_voices_cog.edit_failed", locale=inter.guild_id, error=e), ephemeral=True)

        save_user_setting(record["owner_id"], name=name[:100])
        await inter.response.send_message(i18n.t("temp_voices_cog.name_set", locale=inter.guild_id, name=name), ephemeral=True)

        # `channel` still holds the OLD name here, so the log shows old (channel field) -> new (new_name field)
        return await log_voice(inter, "rename", channel, [log_field(inter, "new_name", new_name)])

    async def action_limit(self, inter: disnake.Interaction, channel_id: int, limit: int):
        ctx = await self.resolve_by_id(inter, channel_id)
        if ctx is None:
            return None
        channel, record = ctx
        if not self.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)

        try:
            await channel.edit(user_limit=limit, reason=f"Limit changed ({inter.author})")
        except disnake.HTTPException as e:
            return await inter.response.send_message(i18n.t("temp_voices_cog.edit_failed", locale=inter.guild_id, error=e), ephemeral=True)

        save_user_setting(record["owner_id"], user_limit=limit or None)
        key = "temp_voices_cog.limit_set" if limit else "temp_voices_cog.limit_unlimited"
        await inter.response.send_message(i18n.t(key, locale=inter.guild_id, limit=limit), ephemeral=True)

        limit_text = str(limit) if limit else i18n.t("temp_voices_cog.unlimited", locale=inter.guild_id)
        return await log_voice(inter, "limit", channel, [log_field(inter, "limit", limit_text)])

    async def action_toggle_lock(self, inter: disnake.Interaction, channel_id: int):
        await self._set_lock(inter, channel_id, target_locked=None)

    async def _set_lock(self, inter: disnake.Interaction, channel_id: int, target_locked: bool | None):
        ctx = await self.resolve_by_id(inter, channel_id)
        if ctx is None:
            return None
        channel, record = ctx
        if not self.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)

        everyone = inter.guild.default_role
        current_connect = channel.overwrites_for(everyone).connect
        currently_locked = current_connect is False

        new_locked = (not currently_locked) if target_locked is None else target_locked

        try:
            await channel.set_permissions(everyone, connect=not new_locked, reason=f"Access changed ({inter.author})")
        except disnake.HTTPException as e:
            return await inter.response.send_message(i18n.t("temp_voices_cog.edit_failed", locale=inter.guild_id, error=e), ephemeral=True)

        save_user_setting(record["owner_id"], locked=new_locked)
        key = "temp_voices_cog.locked" if new_locked else "temp_voices_cog.unlocked"
        await inter.response.send_message(i18n.t(key, locale=inter.guild_id), ephemeral=True)

        # Covers the toggle button as well as /voice open and /voice close
        return await log_voice(inter, "lock" if new_locked else "unlock", channel)

    async def action_permission(self, inter: disnake.Interaction, channel_id: int, target_id: int, allowed: bool):
        ctx = await self.resolve_by_id(inter, channel_id)
        if ctx is None:
            return None
        channel, record = ctx
        if not self.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)

        target = inter.guild.get_member(target_id)
        if target is None:
            return await inter.response.send_message(i18n.t("temp_voices_cog.member_not_found", locale=inter.guild_id), ephemeral=True)

        try:
            await channel.set_permissions(target, connect=allowed, reason=f"{'Allowed' if allowed else 'Banned'} ({inter.author})")
        except disnake.HTTPException as e:
            return await inter.response.send_message(i18n.t("temp_voices_cog.edit_failed", locale=inter.guild_id, error=e), ephemeral=True)

        save_user_overwrite(record["owner_id"], target_id, allowed)

        if not allowed and target in channel.members:
            try:
                await target.move_to(None, reason="Banned at temp voice")
            except (disnake.Forbidden, disnake.HTTPException):
                pass

        key = "temp_voices_cog.allowed" if allowed else "temp_voices_cog.banned"
        await inter.response.send_message(i18n.t(key, locale=inter.guild_id, target=target.mention), ephemeral=True)
        return await log_voice(
            inter, "allow" if allowed else "ban", channel,
            [log_field(inter, "target", f"{target} ({target.id})")],
        )

    async def action_kick(self, inter: disnake.Interaction, channel_id: int, target_id: int):
        ctx = await self.resolve_by_id(inter, channel_id)
        if ctx is None:
            return None
        channel, record = ctx
        if not self.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)

        target = inter.guild.get_member(target_id)
        if target is None or target not in channel.members:
            return await inter.response.send_message(i18n.t("temp_voices_cog.member_not_in_channel", locale=inter.guild_id), ephemeral=True)

        try:
            await target.move_to(None, reason=f"Kicked from temp voice ({inter.author})")
        except (disnake.Forbidden, disnake.HTTPException) as e:
            return await inter.response.send_message(i18n.t("temp_voices_cog.edit_failed", locale=inter.guild_id, error=e), ephemeral=True)

        await inter.response.send_message(i18n.t("temp_voices_cog.kicked", locale=inter.guild_id, target=target.mention), ephemeral=True)
        return await log_voice(inter, "kick", channel, [log_field(inter, "target", f"{target} ({target.id})")])

    async def action_transfer(self, inter: disnake.Interaction, channel_id: int, target_id: int):
        ctx = await self.resolve_by_id(inter, channel_id)
        if ctx is None:
            return None
        channel, record = ctx
        if not self.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)

        target = inter.guild.get_member(target_id)
        if target is None or target not in channel.members:
            return await inter.response.send_message(i18n.t("temp_voices_cog.member_not_in_channel", locale=inter.guild_id), ephemeral=True)

        set_temp_channel_owner(channel_id, target.id)
        await inter.response.send_message(i18n.t("temp_voices_cog.transferred", locale=inter.guild_id, target=target.mention), ephemeral=True)
        return await log_voice(inter, "transfer", channel, [log_field(inter, "target", f"{target} ({target.id})")])

    async def action_claim(self, inter: disnake.Interaction, channel_id: int):
        if await validate_vote(inter, inter.guild_id):
            return None

        ctx = await self.resolve_by_id(inter, channel_id)
        if ctx is None:
            return None
        channel, record = ctx

        if inter.author not in channel.members:
            return await inter.response.send_message(i18n.t("temp_voices_cog.must_be_in_channel", locale=inter.guild_id), ephemeral=True)

        owner_present = any(m.id == record["owner_id"] for m in channel.members)
        if owner_present:
            return await inter.response.send_message(i18n.t("temp_voices_cog.owner_still_present", locale=inter.guild_id), ephemeral=True)

        previous_owner_id = record["owner_id"]
        set_temp_channel_owner(channel_id, inter.author.id)
        await inter.response.send_message(i18n.t("temp_voices_cog.claimed", locale=inter.guild_id, channel=channel.mention), ephemeral=True)
        return await log_voice(inter, "claim", channel, [log_field(inter, "previous_owner", f"<@{previous_owner_id}> ({previous_owner_id})")])

    async def action_bitrate(self, inter: disnake.Interaction, channel_id: int, kbps: int):
        ctx = await self.resolve_by_id(inter, channel_id)
        if ctx is None:
            return None
        channel, record = ctx
        if not self.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)

        max_kbps = inter.guild.bitrate_limit // 1000
        if not (8 <= kbps <= max_kbps):
            return await inter.response.send_message(
                i18n.t("temp_voices_cog.invalid_bitrate_range", locale=inter.guild_id, max=max_kbps), ephemeral=True
            )

        try:
            await channel.edit(bitrate=kbps * 1000, reason=f"Bitrate changed ({inter.author})")
        except disnake.HTTPException as e:
            return await inter.response.send_message(i18n.t("temp_voices_cog.edit_failed", locale=inter.guild_id, error=e), ephemeral=True)

        save_user_setting(record["owner_id"], bitrate=kbps * 1000)
        await inter.response.send_message(i18n.t("temp_voices_cog.bitrate_set", locale=inter.guild_id, kbps=kbps), ephemeral=True)
        return await log_voice(inter, "bitrate", channel, [log_field(inter, "bitrate", f"{kbps} kbps")])

    async def action_region(self, inter: disnake.Interaction, channel_id: int, region: str | None):
        ctx = await self.resolve_by_id(inter, channel_id)
        if ctx is None:
            return None
        channel, record = ctx
        if not self.is_owner(inter, record):
            return await inter.response.send_message(i18n.t("temp_voices_cog.not_owner", locale=inter.guild_id), ephemeral=True)

        try:
            await channel.edit(rtc_region=region, reason=f"Region changed ({inter.author})")
        except disnake.HTTPException as e:
            return await inter.response.send_message(i18n.t("temp_voices_cog.edit_failed", locale=inter.guild_id, error=e), ephemeral=True)

        save_user_setting(record["owner_id"], rtc_region=region)
        key = "temp_voices_cog.region_set" if region else "temp_voices_cog.region_automatic"
        await inter.response.send_message(i18n.t(key, locale=inter.guild_id, region=region), ephemeral=True)

        region_text = region or i18n.t("temp_voices_cog.region_automatic_label", locale=inter.guild_id)
        return await log_voice(inter, "region", channel, [log_field(inter, "region", region_text)])

    # ---------------------------------------------------------- commands

    @commands.slash_command(name="voice", description=localized("commands.voice.description"))
    async def voice_command(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
        pass

    @voice_command.sub_command(name="setup", description=localized("commands.voice_setup.description"))
    async def voice_setup(
            self,
            inter: disnake.ApplicationCommandInteraction,
            lobby_channel: disnake.VoiceChannel = commands.Param(
                name=localized("commands.voice_setup.param_lobby_channel_name"),
                description=localized("commands.voice_setup.param_lobby_channel"),
            ),
            category: disnake.CategoryChannel | None = commands.Param(
                default=None,
                name=localized("commands.voice_setup.param_category_name"),
                description=localized("commands.voice_setup.param_category"),
            ),
            name_template: str = commands.Param(
                default=DEFAULT_NAME_TEMPLATE,
                name=localized("commands.voice_setup.param_name_template_name"),
                description=localized("commands.voice_setup.param_name_template"),
            ),
    ):
        if await validate_permissions(inter, [{Permission.Admin: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        set_guild_config(inter.guild_id, lobby_channel.id, category.id if category else None, name_template)

        return await inter.response.send_message(
            i18n.t("temp_voices_cog.setup_done", locale=inter.guild_id, channel=lobby_channel.mention),
            ephemeral=True,
        )

    @voice_command.sub_command(name="interface", description=localized("commands.voice_interface.description"))
    async def voice_interface(self, inter: disnake.ApplicationCommandInteraction):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return None
        channel, record = ctx

        return await self.send_interface(channel)

    @voice_command.sub_command(name="name", description=localized("commands.voice_name.description"))
    async def voice_name(self, inter: disnake.ApplicationCommandInteraction, name: str = commands.Param(
        name=localized("commands.voice_name.param_name_name"), description=localized("commands.voice_name.param_name"),
    )):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return
        channel, _ = ctx
        await self.action_rename(inter, channel.id, name)

    @voice_command.sub_command(name="limit", description=localized("commands.voice_limit.description"))
    async def voice_limit(self, inter: disnake.ApplicationCommandInteraction, limit: int = commands.Param(
        min_value=0, max_value=99,
        name=localized("commands.voice_limit.param_limit_name"), description=localized("commands.voice_limit.param_limit"),
    )):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return
        channel, _ = ctx
        await self.action_limit(inter, channel.id, limit)

    @voice_command.sub_command(name="open", description=localized("commands.voice_open.description"))
    async def voice_open(self, inter: disnake.ApplicationCommandInteraction):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return
        channel, _ = ctx
        await self._set_lock(inter, channel.id, target_locked=False)

    @voice_command.sub_command(name="close", description=localized("commands.voice_close.description"))
    async def voice_close(self, inter: disnake.ApplicationCommandInteraction):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return
        channel, _ = ctx
        await self._set_lock(inter, channel.id, target_locked=True)

    @voice_command.sub_command(name="allow", description=localized("commands.voice_allow.description"))
    async def voice_allow(self, inter: disnake.ApplicationCommandInteraction, user: disnake.Member = commands.Param(
        name=localized("commands.voice_allow.param_user_name"), description=localized("commands.voice_allow.param_user"),
    )):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return
        channel, _ = ctx
        await self.action_permission(inter, channel.id, user.id, True)

    @voice_command.sub_command(name="ban", description=localized("commands.voice_ban.description"))
    async def voice_ban(self, inter: disnake.ApplicationCommandInteraction, user: disnake.Member = commands.Param(
        name=localized("commands.voice_ban.param_user_name"), description=localized("commands.voice_ban.param_user"),
    )):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return
        channel, _ = ctx
        await self.action_permission(inter, channel.id, user.id, False)

    @voice_command.sub_command(name="kick", description=localized("commands.voice_kick.description"))
    async def voice_kick(self, inter: disnake.ApplicationCommandInteraction, user: disnake.Member = commands.Param(
        name=localized("commands.voice_kick.param_user_name"), description=localized("commands.voice_kick.param_user"),
    )):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return
        channel, _ = ctx
        await self.action_kick(inter, channel.id, user.id)

    @voice_command.sub_command(name="transfer", description=localized("commands.voice_transfer.description"))
    async def voice_transfer(self, inter: disnake.ApplicationCommandInteraction, user: disnake.Member = commands.Param(
        name=localized("commands.voice_transfer.param_user_name"), description=localized("commands.voice_transfer.param_user"),
    )):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return
        channel, _ = ctx
        await self.action_transfer(inter, channel.id, user.id)

    @voice_command.sub_command(name="claim", description=localized("commands.voice_claim.description"))
    async def voice_claim(self, inter: disnake.ApplicationCommandInteraction):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return
        channel, _ = ctx
        await self.action_claim(inter, channel.id)

    @voice_command.sub_command(name="bitrate", description=localized("commands.voice_bitrate.description"))
    async def voice_bitrate(self, inter: disnake.ApplicationCommandInteraction, kbps: int = commands.Param(
        name=localized("commands.voice_bitrate.param_kbps_name"), description=localized("commands.voice_bitrate.param_kbps"),
    )):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return
        channel, _ = ctx
        await self.action_bitrate(inter, channel.id, kbps)

    @voice_command.sub_command(name="region", description=localized("commands.voice_region.description"))
    async def voice_region(self, inter: disnake.ApplicationCommandInteraction, region: str = commands.Param(
        choices=REGION_CHOICES,
        name=localized("commands.voice_region.param_region_name"), description=localized("commands.voice_region.param_region"),
    )):
        ctx = await self.resolve_from_voice_state(inter)
        if ctx is None:
            return
        channel, _ = ctx
        await self.action_region(inter, channel.id, None if region == "automatic" else region)


def setup(bot: commands.Bot):
    bot.add_cog(TempVoicesCog(bot))