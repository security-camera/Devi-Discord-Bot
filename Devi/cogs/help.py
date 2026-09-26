import disnake
from disnake.ext import commands

import re
import i18n

from discord_i18n import localized, yes_no_choices
from i18n import get_locale_display_name
from datetime import datetime, timezone
from disnake import OptionType

from permissions import has_permissions, Permission
from localization import get_localization

UPTIME = datetime.now(timezone.utc)

COMMAND_PATTERN = re.compile(r"(?<![\w./-])/([a-zA-Z0-9_]+(?: [a-zA-Z0-9_]+)*)")
COMMAND_MENTIONS = {}

HELP_SECTIONS = [
    "mention", "triggers", "utils", "messages", "voice", "ai", "admin",
    "warns", "giveaway", "music", "birthdays", "temp_voices", "security",
    "permissions",
]


class CommandMentionFormatException(Exception):
    PREFIX = "An error detected while formatting command mention: "

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.PREFIX + message)


def register_commands(cmds):
    for command in cmds:
        COMMAND_MENTIONS[command.name] = (
            f"</{command.name}:{command.id}>",
            command.id
        )

        register_subcommands(
            command.options or [],
            command.name,
            command.id
        )


def register_subcommands(options, path, command_id):
    for option in options:
        if option.type not in (OptionType.sub_command, OptionType.sub_command_group):
            continue  # real parameter, not a subcommand/group — stop here

        full_name = f"{path} {option.name}"
        key = full_name.replace(" ", "_")

        if option.type == OptionType.sub_command:
            COMMAND_MENTIONS[key] = (f"</{full_name}:{command_id}>", command_id)

        if option.options:
            register_subcommands(option.options, full_name, command_id)


def format_commands(text: str, guild_id: int, show_descriptions: bool) -> str:
    def replace(match: re.Match[str]) -> str:
        words = match.group(1).split(" ")

        for length in range(min(len(words), 3), 0, -1):
            key = "_".join(words[:length])
            data = COMMAND_MENTIONS.get(key)

            if data is not None:
                mention = data[0]
                leftover = " ".join(words[length:])

                result = (
                    f"{mention} — {i18n.t(f'commands.{key}.description', locale=guild_id)}"
                    if show_descriptions else mention
                )
                return f"{result} {leftover}" if leftover else result

        raise CommandMentionFormatException(f"/{match.group(1)} not found in COMMAND_MENTIONS")

    return COMMAND_PATTERN.sub(replace, text)


def find_command(cmds, query: str):
    query = query.strip("/").lower()

    for cmd in cmds:
        if cmd.name.lower() == query:
            return cmd, f"</{cmd.name}:{cmd.id}>"

        result = _find_subcommand(
            cmd,
            query,
            cmd.name,
            cmd.id
        )

        if result:
            return result

    return None


def _find_subcommand(parent, query: str, path: str, command_id: int):
    for option in parent.options or []:
        full = f"{path} {option.name}"

        if full.lower() == query and option.type != OptionType.sub_command_group:
            return option, f"</{full}:{command_id}>"

        result = _find_subcommand(option, query, full, command_id)
        if result:
            return result

    return None


def add_chunked_field(embed: disnake.Embed, name: str, value: str, inline: bool) -> None:
    """Adds a field to the embed, splitting the value into multiple fields if it exceeds Discord's limit (1024 characters)."""
    max_field_length = 1024

    lines = value.split("\n")
    chunks: list[str] = []
    current = ""

    for line in lines:
        candidate = f"{current}\n{line}" if current else line

        if len(candidate) > max_field_length:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        embed.add_field(
            name=name if i == 0 else "\u200b",
            value=chunk,
            inline=inline
        )

def get_help_sections(author) -> list[str]:
    sections = list(HELP_SECTIONS)

    if has_permissions(author, None, Permission.Developer):
        sections.append("developer")

    return sections


def build_section_embed(bot: commands.Bot, section: str, gid: int, show_descriptions: bool) -> disnake.Embed:
    """Build the embed for a single help category (its commands + optional extra info)."""
    embed_value = i18n.t(f"help.sections.{section}.value", locale=gid, bot_id=bot.user.id)

    embed = disnake.Embed(
        title=i18n.t(f"help.sections.{section}.name", locale=gid),
        description=format_commands(embed_value, gid, show_descriptions),
        color=disnake.Color.blue(),
        timestamp=UPTIME,
    )

    additional = i18n.try_t(f"help.sections.{section}.additional", locale=gid)
    if additional:
        add_chunked_field(
            embed,
            name=i18n.t("help.additional_field", locale=gid),
            value=format_commands(additional, gid, False),
            inline=False,
        )

    embed.set_footer(text=i18n.t("help.footer", locale=gid))

    return embed


def build_main_embed(gid: int) -> disnake.Embed:
    """Build the landing page embed shown when /help is called with no arguments."""
    embed = disnake.Embed(
        title=i18n.t("help.title", locale=gid),
        description=i18n.t(
            "help.intro",
            locale=gid,
            server_locale=f"**{get_locale_display_name(get_localization(gid), True)}**",
        ),
        color=disnake.Color.blue(),
        timestamp=UPTIME,
    )

    embed.add_field(
        name=i18n.t("help.outro.title", locale=gid),
        value=i18n.t("help.outro.value", locale=gid),
        inline=False,
    )

    embed.add_field(
        name=i18n.t("help.monitorings.title", locale=gid),
        value=format_commands(i18n.t("help.monitorings.value", locale=gid), gid, False),
        inline=False,
    )

    embed.set_footer(text=i18n.t("help.footer", locale=gid))

    return embed


class HelpCategorySelect(disnake.ui.StringSelect):
    def __init__(self, bot: commands.Bot, sections: list[str], gid: int, show_descriptions: bool):
        options = [
            disnake.SelectOption(
                label=i18n.t(f"help.sections.{section}.name", locale=gid),
                value=section,
            )
            for section in sections
        ]

        super().__init__(
            placeholder=i18n.t("help.select_placeholder", locale=gid),
            min_values=1,
            max_values=1,
            options=options,
            custom_id="help_category_select",
        )

        self.bot = bot
        self.gid = gid
        self.show_descriptions = show_descriptions

    async def callback(self, interaction: disnake.MessageInteraction):
        section = self.values[0]
        embed = build_section_embed(self.bot, section, self.gid, self.show_descriptions)

        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(disnake.ui.View):
    def __init__(self, bot: commands.Bot, sections: list[str], gid: int, show_descriptions: bool):
        super().__init__(timeout=180)
        self.message: disnake.Message | None = None
        self.add_item(HelpCategorySelect(bot, sections, gid, show_descriptions))

    async def on_timeout(self):
        if self.message is None:
            return

        for item in self.children:
            item.disabled = True

        try:
            await self.message.edit(view=self)
        except disnake.HTTPException:
            pass


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(
        name="command_id",
        description=localized("commands.command_id.description"),
    )
    async def command_id(
            self,
            inter: disnake.ApplicationCommandInteraction,
            command: str = commands.Param(
                name=localized("commands.command_id.param_command_name"),
                description=localized("commands.command_id.param_command")
            )
    ):
        command = command.replace("/", "")

        try:
            id = str(COMMAND_MENTIONS.get(command)[1])
        except TypeError:
            return await inter.response.send_message(
                i18n.t("commands.command_id.unknown_command", locale=inter.guild.id),
                ephemeral=True
            )

        return await inter.response.send_message(id, ephemeral=True)

    # ----------------- HELP -----------------
    @commands.slash_command(
        name="help",
        description=localized("commands.help.description"),
    )
    async def help_command(
        self,
        inter: disnake.ApplicationCommandInteraction,
        show_descriptions: bool = commands.Param(
            name=localized("commands.help.param_show_descriptions_name"),
            description=localized("commands.help.param_show_descriptions"),
            default=False,
            choices=yes_no_choices()
        ),
        command: str | None = commands.Param(
            default=None,
            name=localized("commands.help.param_command_name"),
            description=localized("commands.help.param_command")
        ),
    ):
        gid = inter.guild_id

        if command:
            result = find_command(
                await self.bot.fetch_global_commands(),
                command
            )

            if result is None:
                return await inter.response.send_message(
                    i18n.t("help.unknown_command", locale=gid),
                    ephemeral=True
                )

            target, mention = result
            flag = True

            code = command.replace(" ", "_").replace("/", "")

            embed = disnake.Embed(
                title=mention,
                color=disnake.Color.blue(),
                timestamp=UPTIME,
            )

            if show_descriptions:
                embed.description = i18n.t(f"commands.{code}.description", locale=gid)

            if target.options:
                parameters = []

                for option in target.options:
                    if option.type in (
                            disnake.OptionType.sub_command,
                            disnake.OptionType.sub_command_group
                    ):
                        continue

                    if not option.required and flag:
                        parameters.append(i18n.t("help.optional_parameters", locale=gid))
                        flag = False

                    line = f"`{option.name} (" + i18n.t(f"commands.{code}.param_{option.name}_name", locale=gid) + ")`"

                    if show_descriptions:
                        line += f" — " + i18n.t(f"commands.{code}.param_{option.name}", locale=gid)

                    parameters.append(line)

                if parameters:
                    embed.add_field(
                        name=i18n.t("help.parameters", locale=gid),
                        value="\n".join(parameters),
                        inline=False
                    )

            embed.set_footer(
                text=i18n.t("help.footer", locale=gid)
            )

            return await inter.response.send_message(
                embed=embed,
                ephemeral=True
            )

        embed = build_main_embed(gid)
        sections = get_help_sections(inter.author)
        view = HelpView(self.bot, sections, gid, show_descriptions)

        await inter.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await inter.original_response()

        return None


def setup(bot: commands.Bot):
    bot.add_cog(HelpCog(bot))