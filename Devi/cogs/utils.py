import random
import io
import qrcode
import aiohttp
import yt_dlp

import disnake
from disnake.ext import commands

import i18n
from discord_i18n import localized

from duration_utils import parse_duration_seconds


class UtilsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(
        name="random",
        description=localized("commands.random.description"),
    )
    async def random_number(
            self,
            inter: disnake.ApplicationCommandInteraction,
            min_value: int = commands.Param(
                name=localized("commands.random.param_min_name"),
                description=localized("commands.random.param_min"),
            ),
            max_value: int = commands.Param(
                name=localized("commands.random.param_max_name"),
                description=localized("commands.random.param_max"),
            ),
            count: int = commands.Param(
                default=1,
                name=localized("commands.random.param_count_name"),
                description=localized("commands.random.param_count"),
            )
    ):
        gid = inter.guild_id

        if not (0 < count <= 100):
            return await inter.response.send_message(i18n.t("random_cmd.invalid_count", locale=gid), ephemeral=True)

        results = random.choices(range(min_value, max_value + 1), k=count)

        return await inter.response.send_message(
            ", ".join(map(str, results)),
            ephemeral=True
        )

    @commands.slash_command(
        name="coin",
        description=localized("commands.coin.description"),
    )
    async def coin(
            self,
            inter: disnake.ApplicationCommandInteraction,
            count: int = commands.Param(
                default=1,
                name=localized("commands.coin.param_count_name"),
                description=localized("commands.coin.param_count"),
            )
    ):
        gid = inter.guild_id

        if not (0 < count <= 100):
            return await inter.response.send_message(i18n.t("random_cmd.invalid_count", locale=gid), ephemeral=True)

        sides = i18n.get_raw("random.coin.sides", locale=gid) or ["Heads", "Tails"]
        results = random.choices(sides, k=count)

        return await inter.response.send_message(
            ", ".join(map(str, results)),
            ephemeral=True
        )

    @commands.slash_command(
        name="ball",
        description=localized("commands.ball.description"),
    )
    async def ball(
            self,
            inter: disnake.ApplicationCommandInteraction,
            question: str = commands.Param(
                name=localized("commands.ball.param_question_name"),
                description=localized("commands.ball.param_question"),
            )
    ):
        gid = inter.guild_id

        intro_phrases = i18n.get_raw("random.ball.intros", locale=gid) or ["The orb answers:"]
        answers = i18n.get_raw("random.ball.answers", locale=gid) or ["Yes.", "No.", "Maybe."]

        embed = disnake.Embed(
            title=f"🔮 {question}" + ("" if question.endswith("?") else "?"),
            description=f"{random.choice(intro_phrases)} {random.choice(answers)}",
            color=disnake.Color.purple()
        )

        return await inter.response.send_message(embed=embed)

    @commands.slash_command(
        name="avatar",
        description=localized("commands.avatar.description")
    )
    async def avatar(
            self,
            inter: disnake.ApplicationCommandInteraction,
            user: disnake.Member | None = commands.Param(
                default=None,
                description=localized("commands.avatar.param_user"),
                name=localized("commands.avatar.param_user_name"),
            ),
    ):
        user = inter.author if not user else user

        embeds = []

        # server
        embed = disnake.Embed(
            title=i18n.t("avatar_cmd.title", locale=inter.guild_id, name=user.display_name),
            color=user.color
        )
        embed.set_image(url=user.display_avatar.url)
        embeds.append(embed)

        # global != server
        if user.guild_avatar and user.avatar and user.guild_avatar.url != user.avatar.url:
            global_embed = disnake.Embed(
                title=i18n.t("avatar_cmd.global_title", locale=inter.guild_id, name=user.display_name),
                color=user.color
            )
            global_embed.set_image(url=user.avatar.url)
            embeds.append(global_embed)

        return await inter.response.send_message(embeds=embeds, ephemeral=True)

    @commands.slash_command(
        name="qr",
        description=localized("commands.qr.description")
    )
    async def qr(
            self,
            inter: disnake.ApplicationCommandInteraction,
            link: str = commands.Param(
                description=localized("commands.qr.param_link"),
                name=localized("commands.qr.param_link_name"),
            ),
    ):
        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(link)
        qr.make(fit=True)

        image = qr.make_image()

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)

        tmp_id = random.randint(1, 99999999)
        file = disnake.File(buffer, filename=f"qr_{tmp_id}.png")

        embed = disnake.Embed(
            title="QR",
            description=link,
            color=disnake.Color.blurple(),
        )
        embed.set_image(url=f"attachment://qr_{tmp_id}.png")

        return await inter.response.send_message(embed=embed, file=file, ephemeral=True)

    @commands.slash_command(
        name="preview",
        description=localized("commands.preview.description")
    )
    async def preview(
            self,
            inter: disnake.ApplicationCommandInteraction,
            link: str = commands.Param(
                description=localized("commands.preview.param_link"),
                name=localized("commands.preview.param_link_name"),
            ),
    ):
        await inter.response.defer(ephemeral=True)

        ydl_options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_options) as ydl:
                info = await self.bot.loop.run_in_executor(None, lambda: ydl.extract_info(link, download=False))

            thumbnail_url = info.get("thumbnail")

            if not thumbnail_url:
                return await inter.edit_original_response(content=i18n.t("preview_cmd.no_thumbnail", locale=inter.guild_id))

            async with aiohttp.ClientSession() as session:
                async with session.get(thumbnail_url) as response:
                    if response.status != 200:
                        print(response)
                        return await inter.edit_original_response(content=i18n.t("preview_cmd.download_error", locale=inter.guild_id, e=response))

                    image_data = await response.read()

            tmp_id = random.randint(1, 99999999)
            file = disnake.File(io.BytesIO(image_data), filename=f"preview_{tmp_id}.jpg")

            return await inter.edit_original_response(content=None, file=file)

        except yt_dlp.utils.DownloadError:
            return await inter.edit_original_response(content=i18n.t("preview_cmd.invalid_video", locale=inter.guild_id))

        except Exception as e:
            print(e)
            return await inter.edit_original_response(content=i18n.t("preview_cmd.error", locale=inter.guild_id, e=e))

    @commands.slash_command(
        name="slowmode",
        description=localized("commands.slowmode.description")
    )
    async def slowmode(
            self,
            inter: disnake.ApplicationCommandInteraction,
            time: str = commands.Param(
                default="0s",
                description=localized("commands.slowmode.param_time"),
                name=localized("commands.slowmode.param_time_name"),
            ),
            channel: disnake.TextChannel | None = commands.Param(
                default=None,
                description=localized("commands.slowmode.param_channel"),
                name=localized("commands.slowmode.param_channel_name"),
            ),
    ):
        gid = inter.guild_id

        if await check_permissions_and_return(inter, [{disnake.Permissions(manage_channels=True): True}]):
            return None

        seconds, error = parse_duration_seconds(time, locale=gid)

        if error:
            return await inter.response.send_message(error, ephemeral=True)

        if seconds > 21600: #6 hours
            return await inter.response.send_message(i18n.t("slowmode_cmd.max_duration", locale=gid), ephemeral=True)

        channel = channel or inter.channel

        if not isinstance(channel, disnake.TextChannel):
            return await inter.response.send_message(i18n.t("slowmode_cmd.invalid_channel", locale=gid), ephemeral=True)

        try:
            await channel.edit(slowmode_delay=seconds, reason=f"/slowmode {inter.author.name} ({inter.author.id})")

            return await inter.response.send_message(i18n.t("slowmode_cmd.success", locale=gid, channel=channel.mention, time=time))

        except disnake.Forbidden:
            return await inter.response.send_message(i18n.t("slowmode_cmd.no_permission", locale=gid), ephemeral=True)

        except disnake.HTTPException as e:
            return await inter.response.send_message(i18n.t("slowmode_cmd.error", locale=gid, error=e), ephemeral=True)

def setup(bot: commands.Bot):
    bot.add_cog(UtilsCog(bot))