import disnake
from disnake.ext import commands

import permissions, i18n

from permissions import grant_permissions, revoke_permissions, get_permissions, Permission, PermissionCheckType, all_permissions, validate_permissions
from discord_i18n import localized


PERMISSION_LABELS: dict[Permission, str] = {
    Permission.ManageMention: "manage_mention",
    Permission.MentionBlackList: "mention_blacklist",
    Permission.ManageTriggers: "manage_triggers",
    Permission.TTS: "tts",
    Permission.Send: "send_dm",
    Permission.Admin: "admin_all",
    Permission.AiBlackList: "ai_blacklist",
    Permission.Giveaways: "giveaways",
    Permission.Warnings: "warnings",
    Permission.MusicBlackList: "music_blacklist"
}

OBJECT_TYPE_MAP: dict[str, PermissionCheckType] = {
    "user": PermissionCheckType.User,
    "role": PermissionCheckType.Role,
    "channel": PermissionCheckType.Channel,
    "guild": PermissionCheckType.Guild,
}


def describe_permissions(guild_id: int, resolved: Permission) -> list[str]:
    """Returns human-readable permission names"""
    names = []

    for flag, choice_key in PERMISSION_LABELS.items():
        if flag == Permission.Admin:
            continue
        if flag in resolved:
            names.append(i18n.t(f"commands.permissions_manage.choices.{choice_key}", locale=guild_id))

    if (resolved & Permission.Admin) == Permission.Admin:
        names.append(i18n.t("commands.permissions_manage.choices.admin_all", locale=guild_id))

    return names


def format_target_name(inter: disnake.Interaction, object_type: PermissionCheckType, id: int | None = None) -> str:
    """Returns object`s mention by ID"""
    if object_type == PermissionCheckType.User:
        member = inter.guild.get_member(id)
        return member.mention if member else f"`{id}` ({i18n.t('permissions_cmd.unknown_user')})"

    if object_type == PermissionCheckType.Role:
        role = inter.guild.get_role(id)
        return role.mention if role else f"`{id}` ({i18n.t('permissions_cmd.unknown_role')})"

    if object_type == PermissionCheckType.Channel:
        channel = inter.guild.get_channel(id)
        return channel.mention if channel else f"`{id}` ({i18n.t('permissions_cmd.unknown_channel')})"

    if object_type == PermissionCheckType.Guild:
        return f"**{inter.guild.name}**"

    raise ValueError("This PermissionCheckType is unsupported")


def format_permissions_block(inter: disnake.ApplicationCommandInteraction, guild_id: int, object_type: PermissionCheckType, entries: dict[int, Permission]) -> list[str]:
    """Formats all records of one type to category"""
    lines = []

    for target_id, permission in entries.items():
        names = describe_permissions(guild_id, permission)
        if not names:
            continue

        target_name = format_target_name(inter, object_type, target_id)
        lines.append(f"{target_name}: {', '.join(names)}")

    return lines


# ------------------------------------------------------ /permissions manage


def build_object_type_options(guild_id: int) -> list[disnake.SelectOption]:
    return [
        disnake.SelectOption(label=i18n.t("permissions_cmd.manage_type_user", locale=guild_id), value="user", emoji="👤"),
        disnake.SelectOption(label=i18n.t("permissions_cmd.manage_type_role", locale=guild_id), value="role", emoji="🎭"),
        disnake.SelectOption(label=i18n.t("permissions_cmd.manage_type_channel", locale=guild_id), value="channel", emoji="📺"),
        disnake.SelectOption(label=i18n.t("permissions_cmd.manage_type_guild", locale=guild_id), value="guild", emoji="🌐"),
    ]


def build_permission_select_options(guild_id: int, resolved: Permission) -> list[disnake.SelectOption]:
    options = []
    for flag, choice_key in PERMISSION_LABELS.items():
        label_text = i18n.t(f"commands.permissions_manage.choices.{choice_key}", locale=guild_id)

        is_set = (resolved & Permission.Admin) == Permission.Admin if flag == Permission.Admin else flag in resolved
        emoji = "✅" if is_set else "▫️"

        options.append(disnake.SelectOption(label=f"{emoji} {label_text}", value=flag.name))
    return options


def build_manage_panel(
        inter: disnake.MessageInteraction,
        object_type: PermissionCheckType,
        target_id: int,
        author_id: int,
) -> tuple[disnake.Embed, "ManagePanelView"]:
    guild_id = inter.guild_id
    resolved = get_permissions(guild_id, object_type, target_id)

    target_name = format_target_name(inter, object_type, target_id)
    names = describe_permissions(guild_id, resolved)

    embed = disnake.Embed(
        title=i18n.t("permissions_cmd.manage_panel_title", locale=guild_id),
        description=i18n.t("permissions_cmd.manage_panel_target", locale=guild_id, target=target_name),
        color=disnake.Color.blurple(),
    )
    embed.add_field(
        name=i18n.t("permissions_cmd.manage_panel_current", locale=guild_id),
        value="\n".join(f"• {n}" for n in names) if names else i18n.t("permissions_cmd.manage_panel_current_empty", locale=guild_id),
        inline=False,
    )
    embed.set_footer(text=i18n.t("permissions_cmd.manage_panel_footer", locale=guild_id))

    view = ManagePanelView(author_id, guild_id, object_type, target_id, resolved)
    return embed, view


class _AuthorGuardedView(disnake.ui.View):
    """General logic: only respond to the session author, and remove the components from the ephemeral message
     after the timeout (the View itself will not disappear;
     it will simply become non-interactive so the user is not confused by dead buttons)."""

    def __init__(self, author_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message: disnake.Message | None = None

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.author_id:
            await inter.response.send_message(
                i18n.t("permissions_cmd.manage_not_your_session", locale=inter.guild_id),
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except disnake.HTTPException:
                pass

    def add_back_button(self, guild_id: int):
        """The "Back" button is on a separate row (row=1) so it does not interfere with the select menu,
         which always occupies its entire row."""
        button = disnake.ui.Button(
            label=i18n.t("permissions_cmd.manage_back_button", locale=guild_id),
            style=disnake.ButtonStyle.secondary,
            emoji="◀️",
            custom_id=f"permissions_manage_back_{id(self)}",
            row=1,
        )
        button.callback = self.on_back
        self.add_item(button)

    async def on_back(self, inter: disnake.MessageInteraction):
        raise NotImplementedError


class ObjectTypeSelectView(_AuthorGuardedView):
    """Step 1: select the object type (user / role / channel / guild)."""

    def __init__(self, author_id: int, guild_id: int):
        super().__init__(author_id)

        select = disnake.ui.StringSelect(
            placeholder=i18n.t("permissions_cmd.manage_select_type_placeholder", locale=guild_id),
            options=build_object_type_options(guild_id),
            custom_id="permissions_manage_type",
        )
        select.callback = self.on_type_selected
        self.add_item(select)

    async def on_type_selected(self, inter: disnake.MessageInteraction):
        object_type = OBJECT_TYPE_MAP[inter.values[0]]

        if object_type == PermissionCheckType.Guild:
            embed, view = build_manage_panel(inter, PermissionCheckType.Guild, inter.guild_id, self.author_id)
            await inter.response.edit_message(embed=embed, view=view)
            view.message = inter.message
            return

        view = ObjectSelectView(self.author_id, inter.guild_id, object_type)
        embed = disnake.Embed(
            description=i18n.t("permissions_cmd.manage_select_object_prompt", locale=inter.guild_id),
            color=disnake.Color.blurple(),
        )
        await inter.response.edit_message(embed=embed, view=view)
        view.message = inter.message


class ObjectSelectView(_AuthorGuardedView):
    """Step 2 (skipped for guild): select the specific object using Discord's native picker (members/roles/channels)"""

    def __init__(self, author_id: int, guild_id: int, object_type: PermissionCheckType):
        super().__init__(author_id)
        self.object_type = object_type

        if object_type == PermissionCheckType.User:
            select = disnake.ui.UserSelect(
                placeholder=i18n.t("permissions_cmd.manage_select_user_placeholder", locale=guild_id),
                custom_id="permissions_manage_object_user",
            )
        elif object_type == PermissionCheckType.Role:
            select = disnake.ui.RoleSelect(
                placeholder=i18n.t("permissions_cmd.manage_select_role_placeholder", locale=guild_id),
                custom_id="permissions_manage_object_role",
            )
        else:  # Channel
            select = disnake.ui.ChannelSelect(
                placeholder=i18n.t("permissions_cmd.manage_select_channel_placeholder", locale=guild_id),
                custom_id="permissions_manage_object_channel",
            )

        select.callback = self.on_object_selected
        self.add_item(select)
        self.add_back_button(guild_id)

    async def on_object_selected(self, inter: disnake.MessageInteraction):
        # For UserSelect/RoleSelect/ChannelSelect, Discord provides RAW IDs (strings) in values,
        # not resolved objects — .id does not exist here.
        target_id = int(inter.values[0])

        embed, view = build_manage_panel(inter, self.object_type, target_id, self.author_id)
        await inter.response.edit_message(embed=embed, view=view)
        view.message = inter.message

    async def on_back(self, inter: disnake.MessageInteraction):
        view = ObjectTypeSelectView(self.author_id, inter.guild_id)
        embed = disnake.Embed(
            description=i18n.t("permissions_cmd.manage_select_type_prompt", locale=inter.guild_id),
            color=disnake.Color.blurple(),
        )
        await inter.response.edit_message(embed=embed, view=view)
        view.message = inter.message


class ManagePanelView(_AuthorGuardedView):
    """Step 3: a panel showing the object's current permissions + a select menu
      for instant toggling (click a permission to grant it if
      it is missing, or revoke it if it is already granted)."""

    def __init__(self, author_id: int, guild_id: int, object_type: PermissionCheckType, target_id: int, resolved: Permission):
        super().__init__(author_id)
        self.object_type = object_type
        self.target_id = target_id

        select = disnake.ui.StringSelect(
            placeholder=i18n.t("permissions_cmd.manage_toggle_placeholder", locale=guild_id),
            options=build_permission_select_options(guild_id, resolved),
            custom_id="permissions_manage_toggle",
        )
        select.callback = self.on_toggle
        self.add_item(select)
        self.add_back_button(guild_id)

    async def on_toggle(self, inter: disnake.MessageInteraction):
        flag = Permission[inter.values[0]]
        guild_id = inter.guild_id

        resolved = get_permissions(guild_id, self.object_type, self.target_id)
        is_set = (resolved & Permission.Admin) == Permission.Admin if flag == Permission.Admin else flag in resolved

        if is_set:
            revoke_permissions(guild_id, self.object_type, self.target_id, flag)
        else:
            grant_permissions(guild_id, self.object_type, self.target_id, flag)

        embed, view = build_manage_panel(inter, self.object_type, self.target_id, self.author_id)
        await inter.response.edit_message(embed=embed, view=view)
        view.message = inter.message

    async def on_back(self, inter: disnake.MessageInteraction):
        guild_id = inter.guild_id

        if self.object_type == PermissionCheckType.Guild:
            # For guild, step 2 (object selection) was not shown — return directly to step 1.
            view = ObjectTypeSelectView(self.author_id, guild_id)
            embed = disnake.Embed(
                description=i18n.t("permissions_cmd.manage_select_type_prompt", locale=guild_id),
                color=disnake.Color.blurple(),
            )
        else:
            view = ObjectSelectView(self.author_id, guild_id, self.object_type)
            embed = disnake.Embed(
                description=i18n.t("permissions_cmd.manage_select_object_prompt", locale=guild_id),
                color=disnake.Color.blurple(),
            )

        await inter.response.edit_message(embed=embed, view=view)
        view.message = inter.message


def _split_long_line(line: str, max_length: int) -> list[str]:
    """Split a single line that's too long to fit on one page.

    Breaks on spaces (so comma/space-separated lists like guild
    permissions don't get cut mid-word). If a single "word" is somehow
    still longer than max_length on its own, hard-splits it by character
    as a last resort so we never produce an oversized chunk.
    """
    if len(line) <= max_length:
        return [line]

    words = line.split(" ")
    chunks: list[str] = []
    current = ""

    for word in words:
        piece = f"{current} {word}" if current else word

        if len(piece) > max_length:
            if current:
                chunks.append(current)
                current = word
            else:
                for i in range(0, len(word), max_length):
                    chunks.append(word[i:i + max_length])
                current = ""
        else:
            current = piece

    if current:
        chunks.append(current)

    return chunks

def paginate_sections(sections: list[str], max_page_length: int = 3800) -> list[str]:
    """Pack section blocks into pages within Discord's embed description limit.

    Sections are kept whole when possible (joined with a blank line). If a
    section is bigger than max_page_length, it's split by line; and if a
    single line is itself bigger than max_page_length (e.g. one huge
    comma-separated list with no newlines), that line is further split by
    word so no page ever exceeds the limit.
    """
    pages: list[str] = []
    current = ""

    def flush():
        nonlocal current
        if current:
            pages.append(current)
            current = ""

    for section in sections:
        candidate = f"{current}\n\n{section}" if current else section

        if len(candidate) <= max_page_length:
            current = candidate
            continue

        flush()

        if len(section) <= max_page_length:
            current = section
            continue

        # Section itself is too big — split by line, expanding any
        # individual line that's too long on its own.
        lines: list[str] = []
        for raw_line in section.split("\n"):
            lines.extend(_split_long_line(raw_line, max_page_length))

        chunk = ""

        for line in lines:
            piece = f"{chunk}\n{line}" if chunk else line

            if len(piece) > max_page_length:
                if chunk:
                    pages.append(chunk)
                chunk = line
            else:
                chunk = piece

        if chunk:
            current = chunk

    flush()

    return pages or [""]


class PermissionsListView(disnake.ui.View):
    def __init__(self, pages: list[str], title: str, gid: int, author_id: int):
        super().__init__(timeout=180)
        self.pages = pages
        self.title = title
        self.gid = gid
        self.author_id = author_id
        self.index = 0
        self.message: disnake.Message | None = None

        self.previous_button = disnake.ui.Button(
            label="◀",
            style=disnake.ButtonStyle.secondary,
            custom_id="permissions_list_previous",
        )
        self.previous_button.callback = self.on_previous

        self.next_button = disnake.ui.Button(
            label="▶",
            style=disnake.ButtonStyle.secondary,
            custom_id="permissions_list_next",
        )
        self.next_button.callback = self.on_next

        self.add_item(self.previous_button)
        self.add_item(self.next_button)
        self.update_buttons()

    def update_buttons(self):
        self.previous_button.disabled = self.index == 0
        self.next_button.disabled = self.index >= len(self.pages) - 1

    def build_embed(self) -> disnake.Embed:
        embed = disnake.Embed(
            title=self.title,
            description=self.pages[self.index],
            color=disnake.Color.blurple(),
        )

        if len(self.pages) > 1:
            embed.set_footer(text=f"{self.index + 1}/{len(self.pages)}")

        return embed

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id != self.author_id:
            await interaction.response.send_message(
                i18n.t("permissions_cmd.page_not_yours", locale=self.gid),
                ephemeral=True,
            )
            return False

        return True

    async def on_previous(self, interaction: disnake.MessageInteraction):
        self.index = max(0, self.index - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_next(self, interaction: disnake.MessageInteraction):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        if self.message is None:
            return

        for item in self.children:
            item.disabled = True

        try:
            await self.message.edit(view=self)
        except disnake.HTTPException:
            pass

class PermissionCog(commands.Cog):
    def __init__(self, bot):
        self.bot: commands.Bot = bot

    @commands.slash_command(name="permissions", description=localized("commands.permissions.description"))
    async def permissions_command(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
        pass

    @permissions_command.sub_command(
        name="manage",
        description=localized("commands.permissions_manage.description"),
    )
    async def permissions_command_manage(self, inter: disnake.ApplicationCommandInteraction):
        if await validate_permissions(inter, [{Permission.Admin: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        view = ObjectTypeSelectView(inter.author.id, inter.guild_id)
        embed = disnake.Embed(
            description=i18n.t("permissions_cmd.manage_select_type_prompt", locale=inter.guild_id),
            color=disnake.Color.blurple(),
        )

        await inter.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await inter.original_message()
        return None

    @permissions_command.sub_command(
        name="list",
        description=localized("commands.permissions_list.description")
    )
    async def permissions_command_list(self, inter: disnake.ApplicationCommandInteraction):
        if await validate_permissions(inter, [{Permission.Admin: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        snapshot = all_permissions(inter.guild_id)

        sections: list[str] = []

        guild_names = describe_permissions(inter.guild_id, snapshot["guild"])
        if guild_names:
            sections.append(
                f"**{i18n.t('permissions_cmd.section_guild')}**\n"
                + ", ".join(guild_names)
            )

        user_lines = format_permissions_block(inter, inter.guild_id, PermissionCheckType.User, snapshot["users"])
        if user_lines:
            sections.append(
                f"**{i18n.t('permissions_cmd.section_users')}**\n"
                + "\n".join(f"• {line}" for line in user_lines)
            )

        role_lines = format_permissions_block(inter, inter.guild_id, PermissionCheckType.Role, snapshot["roles"])
        if role_lines:
            sections.append(
                f"**{i18n.t('permissions_cmd.section_roles')}**\n"
                + "\n".join(f"• {line}" for line in role_lines)
            )

        channel_lines = format_permissions_block(inter, inter.guild_id, PermissionCheckType.Channel, snapshot["channels"])
        if channel_lines:
            sections.append(
                f"**{i18n.t('permissions_cmd.section_channels')}**\n"
                + "\n".join(f"• {line}" for line in channel_lines)
            )

        if not sections:
            return await inter.response.send_message(
                i18n.t("permissions_cmd.list_all_empty", locale=inter.guild_id),
                ephemeral=True
            )

        pages = paginate_sections(sections)

        view = PermissionsListView(
            pages=pages,
            title=i18n.t("permissions_cmd.list_all_title", locale=inter.guild_id),
            gid=inter.guild_id,
            author_id=inter.author.id,
        )

        if len(pages) == 1:
            return await inter.response.send_message(embed=view.build_embed(), ephemeral=True)

        await inter.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
        view.message = await inter.original_response()

        return None


def setup(bot):
    bot.add_cog(PermissionCog(bot))