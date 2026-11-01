import asyncio
import io
import re

import disnake
from disnake import MessageCommandInteraction
from disnake.ext import commands

from discord_i18n import localized
import i18n
from cogs.quotes.image_generator import render_quote

CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
QUOTABLE_TYPES = (disnake.MessageType.default, disnake.MessageType.reply)
VIEW_TIMEOUT = 180

class QuoteView(disnake.ui.View):
    def __init__(self, inter: disnake.MessageCommandInteraction, avatar_bytes: bytes | None, text: str, display_name: str, username: str, bot_username: str):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.inter = inter
        self.owner_id = inter.author.id
        self.avatar_bytes = avatar_bytes
        self.text = text
        self.display_name = display_name
        self.username = username
        self.bot_username = bot_username
        self.grayscale = True
        self.guild_id = inter.guild_id
        self._update_label(inter)

    def _update_label(self, inter: MessageCommandInteraction):
        key = "quote_cog.button_" + ("color" if self.grayscale else "gray")
        locale_source = inter.guild_id if inter.guild and isinstance(inter.guild, disnake.Guild) else inter.locale
        self.toggle.label = i18n.t(key, locale=locale_source)

    #noinspection unused-parameter
    @disnake.ui.button(emoji="🎨", style=disnake.ButtonStyle.secondary)
    async def toggle(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if inter.author.id != self.owner_id:
            return await inter.response.send_message(i18n.t("quote_cog.not_owner", inter), ephemeral=True)

        await inter.response.defer()
        self.grayscale = not self.grayscale

        image = await asyncio.to_thread(render_quote, self.avatar_bytes, self.text, self.display_name, self.username, self.bot_username, self.grayscale)

        self._update_label(inter)
        return await inter.edit_original_response(file=disnake.File(image, filename="quote.png"), attachments=[], view=self)

    async def on_timeout(self):
        self.toggle.disabled = True
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

        text = message.content
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
            return await author.display_avatar.with_size(512).with_format("png").read()
        except disnake.HTTPException:
            return None # the renderer falls back to a dark placeholder

    def can_quote(self, message: disnake.Message) -> bool:
        return bool(self._prepare_text(message))

    async def render_message(self, message: disnake.Message, grayscale: bool = True) -> io.BytesIO | None:
        text = self._prepare_text(message)
        if not text:
            return None

        author = message.author
        avatar_bytes = await self._fetch_avatar(author)
        return await asyncio.to_thread(render_quote, avatar_bytes, text, author.display_name, author.name, self.bot.user.name, grayscale)

    @commands.message_command(
        name=localized("commands.make_quote.name"),
        install_types=disnake.ApplicationInstallTypes.all(),
        contexts=disnake.InteractionContextTypes.all()
    )
    async def make_quote(self, inter: disnake.MessageCommandInteraction):
        message = inter.target
        text = self._prepare_text(message)

        if not text:
            return await inter.response.send_message(i18n.t("quote_cog.no_text", inter), ephemeral=True)
        await inter.response.defer()

        author = message.author
        avatar_bytes = await self._fetch_avatar(author)

        view = QuoteView(inter, avatar_bytes, text, author.display_name, author.name, self.bot.user.name)
        image = await asyncio.to_thread(render_quote, avatar_bytes, text, author.display_name, author.name, self.bot.user.name)

        return await inter.edit_original_response(file=disnake.File(image, filename="quote.png"), view=view)