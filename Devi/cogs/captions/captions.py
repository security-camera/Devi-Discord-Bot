import asyncio
import dataclasses
import io
import logging
from pathlib import Path

import aiohttp
import disnake
from disnake.ext import commands

import i18n
from discord_i18n import localized

from paths import env_int
from cogs.gif import DEFAULT_FILESIZE_LIMIT, TooLarge, _download, _find_media
from cogs.captions.renderer import CaptionOptions, ResultTooLarge, is_animated, render_animated, render_static, render_video
from cogs.quotes.renderer import font_label, list_fonts
from cogs.quotes.quotes import RenderError

log = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 200
VIEW_TIMEOUT = 240
SEMAPHORE = env_int("CAPTION_SEMAPHORE", 5) # Do not run more than N heavy renders at the same time

DEFAULT_FONT_KEY = "__default__"


@dataclasses.dataclass(frozen=True)
class Source:
    kind: str # image | video
    filename: str
    data: bytes


def _locale_source(inter: disnake.Interaction):
    return inter.guild_id if inter.guild and isinstance(inter.guild, disnake.Guild) else inter.locale


async def render_caption(source: Source, text: str, options: CaptionOptions, limit: int, semaphore: asyncio.Semaphore) -> tuple[bytes, str]:
    stem = Path(source.filename).stem or "media"
    async with semaphore:
        if source.kind == "video":
            result = await render_video(source.data, Path(source.filename).suffix or ".mp4", text, options, limit)
            return result, f"{stem}_caption.mp4"

        if await asyncio.to_thread(is_animated, source.data):
            result = await asyncio.to_thread(render_animated, source.data, text, options, limit)
            return result, f"{stem}_caption.gif"

        result, extension = await asyncio.to_thread(render_static, source.data, text, options, limit)
        return result, f"{stem}_caption.{extension}"


class CaptionView(disnake.ui.View):
    def __init__(self, inter: disnake.ModalInteraction, source: Source, text: str, fonts: dict[str, Path], semaphore: asyncio.Semaphore, limit: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.inter = inter
        self.owner_id = inter.author.id
        self.source = source
        self.text = text
        self.fonts = fonts
        self.semaphore = semaphore
        self.limit = limit
        self.options = CaptionOptions()
        self.font_key = DEFAULT_FONT_KEY
        self.locale_source = _locale_source(inter)
        self._lock = asyncio.Lock()

        self.font_select: disnake.ui.StringSelect | None = None
        if fonts:
            options = [disnake.SelectOption(label=self._t("quote_cog.font_default"), value=DEFAULT_FONT_KEY, default=True)]
            options += [disnake.SelectOption(label=font_label(path)[:100], value=name) for name, path in fonts.items()]
            self.font_select = disnake.ui.StringSelect(placeholder=self._t("quote_cog.font_placeholder"), options=options, row=0)
            self.font_select.callback = self._on_font

        self._update_components()

    def _t(self, key: str) -> str:
        return i18n.t(key, locale=self.locale_source)

    def _update_components(self):
        to_on = self.options.style == "outside"
        self.toggle_style.label = self._t("caption_cog.buttons." + ("on" if to_on else "outside"))

        to_bottom = self.options.position == "top"
        self.toggle_position.label = self._t("caption_cog.buttons." + ("bottom" if to_bottom else "top"))
        self.toggle_position.emoji = "⬇️" if to_bottom else "⬆️"

        if self.font_select:
            for option in self.font_select.options:
                option.default = option.value == self.font_key

            show_fonts = self.options.style == "outside"
            if show_fonts and self.font_select not in self.children:
                self.add_item(self.font_select)
            elif not show_fonts and self.font_select in self.children:
                self.remove_item(self.font_select)

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id == self.owner_id:
            return True
        await inter.response.send_message(i18n.t("caption_cog.not_owner", inter), ephemeral=True)
        return False

    async def render_file(self, options: CaptionOptions) -> disnake.File:
        try:
            result, filename = await render_caption(self.source, self.text, options, self.limit, self.semaphore)
        except ResultTooLarge:
            raise RenderError(self._t("caption_cog.result_too_large"))
        except Exception:
            log.exception("Caption rendering failed")
            raise RenderError(self._t("caption_cog.convert_failed"))
        return disnake.File(io.BytesIO(result), filename=filename)

    async def _refresh(self, inter: disnake.MessageInteraction, options: CaptionOptions, font_key: str | None = None):
        await inter.response.defer()
        async with self._lock:
            try:
                file = await self.render_file(options)
            except RenderError as error:
                return await inter.followup.send(str(error), ephemeral=True)

            self.options = options
            if font_key is not None:
                self.font_key = font_key
            self._update_components()
            await inter.edit_original_response(file=file, attachments=[], view=self)

    #noinspection unused-parameter
    @disnake.ui.button(emoji="🖼️", style=disnake.ButtonStyle.secondary, row=1)
    async def toggle_style(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        style = "on" if self.options.style == "outside" else "outside"
        await self._refresh(inter, dataclasses.replace(self.options, style=style))

    #noinspection unused-parameter
    @disnake.ui.button(emoji="⬇️", style=disnake.ButtonStyle.secondary, row=1)
    async def toggle_position(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        position = "bottom" if self.options.position == "top" else "top"
        await self._refresh(inter, dataclasses.replace(self.options, position=position))

    async def _on_font(self, inter: disnake.MessageInteraction):
        key = inter.values[0]
        font = self.fonts.get(key)
        await self._refresh(inter, dataclasses.replace(self.options, font=font), font_key=key if font else DEFAULT_FONT_KEY)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.inter.edit_original_response(view=self)
        except disnake.HTTPException:
            pass


class CaptionModal(disnake.ui.Modal):
    def __init__(self, cog: "CaptionCog", found: tuple, locale_source):
        self.cog = cog
        self.found = found
        super().__init__(
            title=i18n.t("caption_cog.modal.title", locale=locale_source),
            components=[
                disnake.ui.TextInput(
                    label=i18n.t("caption_cog.modal.label", locale=locale_source),
                    placeholder=i18n.t("caption_cog.modal.placeholder", locale=locale_source),
                    custom_id="text",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=MAX_TEXT_LENGTH,
                    required=True,
                ),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        await self.cog.handle_submit(inter, self.found, inter.text_values["text"])


class CaptionCog(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self._semaphore = asyncio.Semaphore(SEMAPHORE)

    @commands.message_command(
        name=localized("commands.caption.name"),
        install_types=disnake.ApplicationInstallTypes.all(),
        contexts=disnake.InteractionContextTypes.all(),
    )
    async def caption(self, inter: disnake.MessageCommandInteraction):
        found = _find_media(inter.target)
        if not found:
            return await inter.response.send_message(i18n.t("caption_cog.no_media", inter), ephemeral=True)

        return await inter.response.send_modal(CaptionModal(self, found, _locale_source(inter)))

    async def handle_submit(self, inter: disnake.ModalInteraction, found: tuple, raw_text: str):
        text = raw_text.strip()
        if not text:
            return await inter.response.send_message(i18n.t("caption_cog.empty_text", inter), ephemeral=True)
        await inter.response.defer()

        kind, filename, source = found
        limit = getattr(inter, "filesize_limit", DEFAULT_FILESIZE_LIMIT)

        try:
            data = await _download(source)
        except TooLarge:
            return await inter.edit_original_response(content=i18n.t("caption_cog.source_too_large", inter))
        except (aiohttp.ClientError, asyncio.TimeoutError, disnake.HTTPException):
            return await inter.edit_original_response(content=i18n.t("caption_cog.download_failed", inter))

        view = CaptionView(inter, Source(kind, filename, data), text, list_fonts(), self._semaphore, limit)
        try:
            file = await view.render_file(view.options)
        except RenderError as error:
            return await inter.edit_original_response(content=str(error))

        return await inter.edit_original_response(file=file, view=view)