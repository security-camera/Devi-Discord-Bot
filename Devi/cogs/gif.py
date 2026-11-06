import asyncio
import logging
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import disnake
from disnake.ext import commands
from PIL import Image, ImageSequence

import i18n
from discord_i18n import localized

from paths import env_int

from cogs.quotes.image_generator import list_fonts
from cogs.quotes.quotes import QuoteData, QuotesCog, QuoteView, RenderError

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".apng"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
ALLOWED_PROXY_HOSTS = ("discordapp.net", "discordapp.com", "discord.com")

MAX_DOWNLOAD = 50 * 1024 * 1024
MAX_SECONDS = 15 # only the first N seconds of a video are used
MAX_FRAMES = 200 # frame limit for animated images
FFMPEG_TIMEOUT = 60
DEFAULT_FILESIZE_LIMIT = 10 * 1024 * 1024

VIDEO_PRESETS = [(480, 15), (360, 12), (240, 10)] # (max width, fps)
IMAGE_PRESETS = [800, 480, 320] # max side in pixels

SEMAPHORE = env_int("GIF_SEMAPHORE", 10) # Do not run more than N heavy conversions at the same time

# Used when the message has no media and a quote picture is rendered instead
QUOTE_FILENAME = "quote.png"


class ConversionError(Exception):
    pass

class TooLarge(Exception):
    pass

def _kind_by_name(name: str, content_type: str | None = None) -> str | None:
    if content_type:
        if content_type.startswith("image/"):
            return "image"
        if content_type.startswith("video/"):
            return "video"
    suffix = Path(urlparse(name).path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS or suffix == ".gif":
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return None


def _find_media(message: disnake.Message):
    """Return (kind, filename, source) of the first image/video, or None."""
    for att in message.attachments:
        kind = _kind_by_name(att.filename, att.content_type)
        if kind:
            return kind, att.filename, att

    for embed in message.embeds:
        if embed.type == "gifv":
            candidates = [(embed.video, "video")]
        elif embed.type == "image":
            candidates = [(embed.thumbnail, "image")]
        else:
            candidates = [(embed.image, "image")]
        for media, kind in candidates:
            url = getattr(media, "proxy_url", None)
            if url and (urlparse(url).hostname or "").endswith(ALLOWED_PROXY_HOSTS):
                return kind, Path(urlparse(url).path).name or "media", url
    return None


async def _download(source) -> bytes:
    if isinstance(source, disnake.Attachment):
        if source.size > MAX_DOWNLOAD:
            raise TooLarge
        return await source.read()

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(source) as resp:
            resp.raise_for_status()
            if resp.content_length and resp.content_length > MAX_DOWNLOAD:
                raise TooLarge
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                buf.extend(chunk)
                if len(buf) > MAX_DOWNLOAD:
                    raise TooLarge
    return bytes(buf)


def _image_to_gif(data: bytes, max_side: int) -> bytes:
    frames, durations = [], []
    with Image.open(BytesIO(data)) as img:
        for frame in ImageSequence.Iterator(img):
            durations.append(frame.info.get("duration", 100))
            converted = frame.convert("RGBA")
            converted.thumbnail((max_side, max_side), Image.LANCZOS)
            frames.append(converted)
            if len(frames) >= MAX_FRAMES:
                break

    out = BytesIO()
    frames[0].save(
        out, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations, loop=0, disposal=2,
    )
    return out.getvalue()


async def _video_to_gif(data: bytes, suffix: str, width: int, fps: int) -> bytes:
    vf = (
        f"fps={fps},scale='min({width},iw)':-2:flags=lanczos,split[a][b];"
        "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5"
    )
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / f"in{suffix}", Path(tmp) / "out.gif"
        await asyncio.to_thread(src.write_bytes, data)

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "error", "-t", str(MAX_SECONDS), "-i", str(src),
            "-an", "-vf", vf, "-loop", "0", str(dst),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ConversionError("ffmpeg timeout")

        if proc.returncode != 0 or not dst.exists():
            raise ConversionError(err.decode(errors="replace")[-300:])
        return await asyncio.to_thread(dst.read_bytes)


class GifQuoteView(QuoteView):
    """QuoteView whose picture is delivered as a GIF, so every tweak re-renders a GIF."""

    def __init__(self, inter: disnake.MessageCommandInteraction, data: QuoteData, fonts: dict[str, Path], cog: "GifCog", limit: int):
        super().__init__(inter, data, fonts)
        self.cog = cog
        self.limit = limit

    async def render_file(self, options) -> disnake.File:
        png = await self.render_png(options)
        try:
            async with self.cog._semaphore:
                result = await self.cog._convert("image", QUOTE_FILENAME, png.getvalue(), self.limit)
        except Exception:
            log.exception("GIF conversion failed")
            raise RenderError(self._t("gif_cog.convert_failed"))

        if not result:
            raise RenderError(self._t("gif_cog.result_too_large"))
        return disnake.File(BytesIO(result), filename=f"{Path(QUOTE_FILENAME).stem}.gif")


class GifCog(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self._semaphore = asyncio.Semaphore(SEMAPHORE)

    @staticmethod
    async def _convert(kind: str, filename: str, data: bytes, limit: int) -> bytes | None:
        if kind == "image":
            for max_side in IMAGE_PRESETS:
                result = await asyncio.to_thread(_image_to_gif, data, max_side)
                if len(result) <= limit:
                    return result
        else:
            suffix = Path(filename).suffix or ".mp4"
            for width, fps in VIDEO_PRESETS:
                result = await _video_to_gif(data, suffix, width, fps)
                if len(result) <= limit:
                    return result
        return None

    async def _send_quote_gif(self, inter: disnake.MessageCommandInteraction, quotes: QuotesCog, limit: int):
        data = await quotes.prepare_quote(inter.target)
        if not data:
            return await inter.edit_original_response(content=i18n.t("gif_cog.no_media", inter))

        view = GifQuoteView(inter, data, list_fonts(), self, limit)
        try:
            file = await view.render_file(view.options)
        except RenderError as error:
            return await inter.edit_original_response(content=str(error))
        return await inter.edit_original_response(file=file, view=view)

    @commands.message_command(
        name=localized("commands.make_gif.name"),
        install_types=disnake.ApplicationInstallTypes.all(),
        contexts=disnake.InteractionContextTypes.all(),
    )
    async def make_gif(self, inter: disnake.MessageCommandInteraction):
        message = inter.target
        found = _find_media(message)
        quotes: QuotesCog | None = None

        if found:
            kind, filename, source = found
            if Path(filename).suffix.lower() == ".gif":
                return await inter.response.send_message(i18n.t("gif_cog.already_gif", inter), ephemeral=True)
        else:
            # No media: fall back to a quote picture made by QuotesCog
            quotes = self.bot.get_cog("QuotesCog")
            if quotes is None or not quotes.can_quote(message):
                return await inter.response.send_message(i18n.t("gif_cog.no_media", inter), ephemeral=True)

        await inter.response.defer()
        limit = getattr(inter, "filesize_limit", DEFAULT_FILESIZE_LIMIT)

        if quotes:
            return await self._send_quote_gif(inter, quotes, limit)

        try:
            data = await _download(source)
        except TooLarge:
            return await inter.edit_original_response(content=i18n.t("gif_cog.source_too_large", inter))
        except (aiohttp.ClientError, asyncio.TimeoutError, disnake.HTTPException):
            return await inter.edit_original_response(content=i18n.t("gif_cog.download_failed", inter))

        try:
            async with self._semaphore:
                result = await self._convert(kind, filename, data, limit)
        except Exception:
            log.exception("GIF conversion failed")
            return await inter.edit_original_response(content=i18n.t("gif_cog.convert_failed", inter))

        if not result:
            return await inter.edit_original_response(content=i18n.t("gif_cog.result_too_large", inter))

        gif_name = f"{Path(filename).stem}.gif"
        return await inter.edit_original_response(file=disnake.File(BytesIO(result), filename=gif_name))


def setup(bot: commands.InteractionBot):
    bot.add_cog(GifCog(bot))