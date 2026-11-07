import asyncio
import dataclasses
import io
import re
from pathlib import Path

import disnake
from disnake import MessageCommandInteraction
from disnake.ext import commands

from discord_i18n import localized
import i18n
from cogs.quotes.renderer import AUTO_COLOR, TEXT_COLORS, QuoteOptions, font_label, list_fonts, render_quote
from cogs.quotes.md_cleaner import strip_markdown

CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
QUOTABLE_TYPES = (disnake.MessageType.default, disnake.MessageType.reply)
VIEW_TIMEOUT = 240

DEFAULT_FONT_KEY = "__default__"

NEXT_MODE = {"gray": "color", "color": "tint", "tint": "gray"}

DEFAULT_COLOR_KEY = "white"

COLOR_EMOJIS = {
    "white": "⚪", "red": "🔴", "orange": "🟠", "yellow": "🟡", "green": "🟢",
    "cyan": "🩵", "blue": "🔵", "purple": "🟣", "pink": "🩷", AUTO_COLOR: "🎨",
}


class RenderError(Exception):
    """Raised by QuoteView.render_file; the message is shown to the user."""


@dataclasses.dataclass(frozen=True)
class QuoteData:
    avatar_bytes: bytes | None
    text: str
    display_name: str
    username: str
    bot_username: str


class QuoteView(disnake.ui.View):
    def __init__(self, inter: disnake.MessageCommandInteraction, data: QuoteData, fonts: dict[str, Path]):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.inter = inter
        self.owner_id = inter.author.id
        self.data = data
        self.fonts = fonts
        self.options = QuoteOptions()
        self.font_key = DEFAULT_FONT_KEY
        self.color_key = DEFAULT_COLOR_KEY
        self.locale_source = inter.guild_id if inter.guild and isinstance(inter.guild, disnake.Guild) else inter.locale
        self._lock = asyncio.Lock() # clicks arriving together must not render out of order

        # The font menu is shown only when QUOTES_DIR has extra fonts
        self.font_select: disnake.ui.StringSelect | None = None
        if fonts:
            options = [disnake.SelectOption(label=self._t("quote_cog.font_default"), value=DEFAULT_FONT_KEY, default=True)]
            options += [disnake.SelectOption(label=font_label(path)[:100], value=name) for name, path in fonts.items()]
            self.font_select = disnake.ui.StringSelect(placeholder=self._t("quote_cog.font_placeholder"), options=options, row=0)
            self.font_select.callback = self._on_font
            self.add_item(self.font_select)

        color_options = [
            disnake.SelectOption(label=self._t(f"quote_cog.colors.{key}"), value=key, emoji=emoji, default=key == DEFAULT_COLOR_KEY)
            for key, emoji in COLOR_EMOJIS.items()
        ]
        self.color_select = disnake.ui.StringSelect(placeholder=self._t("quote_cog.colors.placeholder"), options=color_options, row=1)
        self.color_select.callback = self._on_color
        self.add_item(self.color_select)

        self._update_components()

    def _t(self, key: str) -> str:
        return i18n.t(key, locale=self.locale_source)

    def _update_components(self):
        self.cycle_style.label = self._t("quote_cog.buttons." + NEXT_MODE[self.options.mode])

        to_portrait = not self.options.portrait
        self.toggle_orientation.label = self._t("quote_cog.buttons." + ("vertical" if to_portrait else "horizontal"))
        self.toggle_orientation.emoji = "📱" if to_portrait else "🖥️"

        if self.font_select:
            for option in self.font_select.options:
                option.default = option.value == self.font_key
        for option in self.color_select.options:
            option.default = option.value == self.color_key

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id == self.owner_id:
            return True
        await inter.response.send_message(i18n.t("quote_cog.not_owner", inter), ephemeral=True)
        return False

    async def render_png(self, options: QuoteOptions) -> io.BytesIO:
        data = self.data
        return await asyncio.to_thread(render_quote, data.avatar_bytes, data.text, data.display_name, data.username, data.bot_username, options)

    async def render_file(self, options: QuoteOptions) -> disnake.File:
        """Build the attachment for the given options. Subclasses may change the format (see GifQuoteView)."""
        return disnake.File(await self.render_png(options), filename="quote.png")

    async def _refresh(self, inter: disnake.MessageInteraction, options: QuoteOptions, font_key: str | None = None, color_key: str | None = None):
        await inter.response.defer()
        async with self._lock:
            try:
                file = await self.render_file(options)
            except RenderError as error:
                # Keep the previous picture and settings, tell only the person who clicked
                return await inter.followup.send(str(error), ephemeral=True)

            self.options = options
            if font_key is not None:
                self.font_key = font_key
            if color_key is not None:
                self.color_key = color_key
            self._update_components()
            await inter.edit_original_response(file=file, attachments=[], view=self)

    #noinspection unused-parameter
    @disnake.ui.button(emoji="🎨", style=disnake.ButtonStyle.secondary, row=2)
    async def cycle_style(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await self._refresh(inter, dataclasses.replace(self.options, mode=NEXT_MODE[self.options.mode]))

    #noinspection unused-parameter
    @disnake.ui.button(emoji="📱", style=disnake.ButtonStyle.secondary, row=2)
    async def toggle_orientation(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await self._refresh(inter, dataclasses.replace(self.options, portrait=not self.options.portrait))

    async def _on_font(self, inter: disnake.MessageInteraction):
        key = inter.values[0]
        font = self.fonts.get(key)
        await self._refresh(inter, dataclasses.replace(self.options, font=font), font_key=key if font else DEFAULT_FONT_KEY)

    async def _on_color(self, inter: disnake.MessageInteraction):
        key = inter.values[0]

        if key == AUTO_COLOR:
            color = AUTO_COLOR
        elif key in TEXT_COLORS:
            color = TEXT_COLORS[key]
        else:
            key, color = DEFAULT_COLOR_KEY, TEXT_COLORS[DEFAULT_COLOR_KEY]
        await self._refresh(inter, dataclasses.replace(self.options, color=color), color_key=key)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.inter.edit_original_response(view=self)
        except disnake.HTTPException:
            pass


class QuotesCog(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @staticmethod
    def _prepare_text(message: disnake.Message) -> str:
        if message.type not in QUOTABLE_TYPES:
            return ""

        users = { user.id: user for user in message.mentions }

        roles: dict[int, disnake.Role] = { }
        channels: dict[int, disnake.abc.GuildChannel] = { }
        if isinstance(message.guild, disnake.Guild):
            roles = { role.id: role for role in message.role_mentions }
            channels = { channel.id: channel for channel in message.channel_mentions }

        def replace_user(match: re.Match) -> str:
            user = users.get(int(match[1]))
            return f"@{user.name}" if user else ""

        def replace_role(match: re.Match) -> str:
            role = roles.get(int(match[1]))
            return f"@{role.name}" if role else ""

        def replace_channel(match: re.Match) -> str:
            channel = channels.get(int(match[1]))
            return f"#{channel.name}" if channel else ""

        text = strip_markdown(message.content)
        text = USER_MENTION_RE.sub(replace_user, text)
        text = ROLE_MENTION_RE.sub(replace_role, text)
        text = CHANNEL_MENTION_RE.sub(replace_channel, text)
        text = CUSTOM_EMOJI_RE.sub("", text)

        lines = [" ".join(line.split()) for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()

    @staticmethod
    async def _fetch_avatar(author: disnake.User | disnake.Member) -> bytes | None:
        # Guild avatar for members, global avatar otherwise
        try:
            return await author.display_avatar.with_size(1024).with_format("png").read()
        except disnake.HTTPException:
            return None # the renderer falls back to a dark placeholder

    def can_quote(self, message: disnake.Message) -> bool:
        return bool(self._prepare_text(message))

    async def prepare_quote(self, message: disnake.Message) -> QuoteData | None:
        """Everything needed to draw a quote; None if the message has no text. Also used by GifCog."""
        text = self._prepare_text(message)
        if not text:
            return None

        author = message.author
        return QuoteData(await self._fetch_avatar(author), text, author.display_name, author.name, self.bot.user.name)

    @commands.message_command(
        name=localized("commands.make_quote.name"),
        install_types=disnake.ApplicationInstallTypes.all(),
        contexts=disnake.InteractionContextTypes.all()
    )
    async def make_quote(self, inter: disnake.MessageCommandInteraction):
        message = inter.target
        if not self.can_quote(message):
            return await inter.response.send_message(i18n.t("quote_cog.no_text", inter), ephemeral=True)
        await inter.response.defer()

        data = await self.prepare_quote(message)
        view = QuoteView(inter, data, list_fonts())
        file = await view.render_file(view.options)

        return await inter.edit_original_response(file=file, view=view)