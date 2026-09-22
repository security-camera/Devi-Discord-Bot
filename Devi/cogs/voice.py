import os
import tempfile
import asyncio
import contextlib
import edge_tts

import disnake
from disnake.ext import commands

import i18n
from permissions import validate_permissions, Permission
from discord_i18n import localized
from logs import send_log, LogColor
from paths import env_var, env_var_to_int

from other_apis.topgg_utils import is_voted

MAX_TTS_LENGTH = env_var_to_int("MAX_TTS_LENGTH", "400")
MAX_TTS_LENGTH_VOTED = env_var_to_int("MAX_TTS_LENGTH_VOTED", "800")
EMPTY_CHANNEL_TIMEOUT = env_var_to_int("EMPTY_CHANNEL_TIMEOUT", "60")
VOICE = env_var("VOICE", "ru-RU-DmitryNeural")


class VoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        global Singleton
        self.bot = bot
        self.voice = VOICE
        self.tts_queues: dict[int, asyncio.Queue] = {}
        self.tts_workers: dict[int, asyncio.Task] = {}
        self.empty_channel_tasks: dict[int, asyncio.Task] = {}
        Singleton = self

    def _cleanup_guild(self, guild_id: int):
        """Stops a worker and clears a queue"""
        worker = self.tts_workers.pop(guild_id, None)
        if worker and not worker.done():
            worker.cancel()

        queue = self.tts_queues.pop(guild_id, None)
        if queue:
            while not queue.empty():
                queue.get_nowait()

        empty_task = self.empty_channel_tasks.pop(guild_id, None)
        if empty_task and not empty_task.done():
            empty_task.cancel()

    def enqueue_tts(self, guild_id: int, vc: disnake.VoiceClient, text: str) -> int:
        """Queues text for TTS on the specified server and ensures
        that the synthesis/playback worker is running. Returns
        position at queue."""
        queue = self.tts_queues.setdefault(guild_id, asyncio.Queue())
        queue.put_nowait(text)

        worker = self.tts_workers.get(guild_id)
        if worker is None or worker.done():
            self.tts_workers[guild_id] = asyncio.create_task(
                self._tts_worker(guild_id, vc)
            )

        return queue.qsize()

    @staticmethod
    def _is_channel_empty(channel: disnake.VoiceChannel) -> bool:
        return not any(not m.bot for m in channel.members)

    def _handle_voice_occupancy(self, guild_id: int, vc: disnake.VoiceClient):
        """Checks current bot channel and declines timeout"""
        existing_task = self.empty_channel_tasks.get(guild_id)

        if self._is_channel_empty(vc.channel):
            if existing_task is None or existing_task.done():
                self.empty_channel_tasks[guild_id] = asyncio.create_task(
                    self._auto_leave_after_timeout(guild_id, vc)
                )
        else:
            if existing_task is not None and not existing_task.done():
                existing_task.cancel()
            self.empty_channel_tasks.pop(guild_id, None)

    async def _auto_leave_after_timeout(self, guild_id: int, vc: disnake.VoiceClient):
        """Waits EMPTY_CHANNEL_TIMEOUT seconds and disconnect, if channel is empty."""
        try:
            await asyncio.sleep(EMPTY_CHANNEL_TIMEOUT)
        except asyncio.CancelledError:
            return

        if not vc.is_connected() or not self._is_channel_empty(vc.channel):
            return

        channel_mention = vc.channel.mention

        await vc.disconnect()
        self._cleanup_guild(guild_id)

        await send_log(
            guild_id,
            i18n.t("voice_cog.log_auto_left_title", guild_id=guild_id),
            color=LogColor.Voice,
            fields=[
                (i18n.t("logs_cog.fields.channel", guild_id=guild_id), channel_mention),
            ]
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState):
        guild = member.guild
        vc = guild.voice_client

        if vc is None or not vc.is_connected():
            return

        if before.channel == vc.channel or after.channel == vc.channel:
            self._handle_voice_occupancy(guild.id, vc)

    async def handle_join(self, inter: disnake.ApplicationCommandInteraction, channel: disnake.VoiceChannel | None, command: bool) -> disnake.VoiceClient | None:
        """Connects or moves to voice channel"""
        gid = inter.guild_id

        if channel is None:
            if not inter.author.voice:
                if command:
                    await inter.response.send_message(
                        i18n.t("voice_cog.no_channel_specified", locale=gid),
                        ephemeral=True
                    )
                return None

            channel = inter.author.voice.channel

        voice_client = inter.guild.voice_client

        try:
            if voice_client is None:
                voice_client = await channel.connect()
            else:
                await voice_client.move_to(channel)
        except (disnake.ClientException, disnake.HTTPException) as e:
            if command:
                await inter.response.send_message(
                    i18n.t("voice_cog.join_failed", locale=gid, error=e),
                    ephemeral=True
                )
            return None

        self._handle_voice_occupancy(inter.guild.id, voice_client)

        if command:
            await inter.response.send_message(
                i18n.t("voice_cog.joined", locale=gid, channel=channel.mention),
                ephemeral=True
            )

        return voice_client

    @commands.slash_command(
        name="join",
        description=localized("commands.join.description"),
    )
    async def join(
            self,
            inter: disnake.ApplicationCommandInteraction,
            channel: disnake.VoiceChannel = commands.Param(
                default=None,
                name=localized("commands.join.param_channel_name"),
                description=localized("commands.join.param_channel"),
            )
    ):
        await self.handle_join(inter, channel, True)

    @commands.slash_command(
        name="leave",
        description=localized("commands.leave.description"),
    )
    async def leave(self, inter: disnake.ApplicationCommandInteraction):
        gid = inter.guild_id

        voice_client = inter.guild.voice_client

        if voice_client is None:
            return await inter.response.send_message(
                i18n.t("voice_cog.not_in_voice", locale=gid),
                ephemeral=True
            )

        await voice_client.disconnect()
        self._cleanup_guild(inter.guild.id)

        return await inter.response.send_message(
            i18n.t("voice_cog.left", locale=gid),
            ephemeral=True
        )

# ---------------------- TTS ----------------------
    @commands.slash_command(
        name="tts",
        description=localized("commands.tts.description"),
    )
    async def tts(
            self,
            inter: disnake.ApplicationCommandInteraction,
            text: str = commands.Param(
                name=localized("commands.tts.param_text_name"),
                description=localized("commands.tts.param_text"),
            )
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.TTS: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        vc = inter.guild.voice_client

        if vc is None:
            return await inter.response.send_message(
                i18n.t("voice_cog.not_in_voice", locale=gid),
                ephemeral=True
            )

        voted = await is_voted(inter.author.id)

        max_length = MAX_TTS_LENGTH_VOTED if voted else MAX_TTS_LENGTH

        if len(text) > max_length:
            vote_ad = "" if voted else "\n\n" + i18n.t("top_gg_cog.voting_ad", locale=gid, format_command=True)
            return await inter.response.send_message(
                i18n.t("voice_cog.too_long", locale=gid, max=max_length) + vote_ad,
                ephemeral=True
            )

        guild_id = inter.guild.id

        position = self.enqueue_tts(guild_id, vc, text)

        return await inter.response.send_message(
            i18n.t("voice_cog.queued", locale=gid, position=position),
            ephemeral=True
        )

    async def _synthesize(self, text: str) -> str:
        """Synthesizes text and returns temp file path"""
        fd, filename = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

        try:
            await edge_tts.Communicate(text=text, voice=self.voice).save(filename)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(filename)
            raise

        return filename

    @staticmethod
    async def _play(vc: disnake.VoiceClient, filename: str):
        """Plays temp file and deletes it.
        If something else is already playing on this voice client (primarily
        a track from MusicCog), it is paused for the duration of the TTS clip
        and automatically resumed immediately afterward, so /tts and /ai ask voice
        do not conflict with music on the same VoiceClient.

        disnake/discord.py does not provide a public API like "pause the current
        source, play another source on top of it, then restore it afterward" —
        calling vc.play() again simply replaces the internal vc._player, and a
        regular vc.resume() after that would apply to the TTS player rather than
        the music player. Therefore, the exact AudioPlayer that was paused is
        stored below, and control is explicitly returned to it after the TTS
        playback finishes."""

        loop = asyncio.get_running_loop()
        finished = asyncio.Event()

        ducked_player = None
        if vc.is_playing():
            vc.pause()
            ducked_player = getattr(vc, "_player", None)

        def after(error):
            with contextlib.suppress(OSError):
                os.remove(filename)
            if error:
                print(error)
            loop.call_soon_threadsafe(finished.set)

        vc.play(disnake.FFmpegPCMAudio(filename), after=after)
        await finished.wait()

        if ducked_player is not None:
            try:
                # If what we paused has not finished on its own yet
                # (for example, if the music was not stopped with /music stop while TTS was playing)
                # — resume exactly that and return its VoiceClient
                # reference to it so that /music pause|resume|skip can continue working
                # with the actual source rather than with TTS that has already finished.
                if not ducked_player._end.is_set():
                    ducked_player.resume()
                    vc._player = ducked_player
            except AttributeError as e:
                print(f"[voice] Failed to restore audio after TTS: {e}")

    async def _tts_worker(self, guild_id: int, vc: disnake.VoiceClient):
        queue = self.tts_queues[guild_id]

        # Synthesize the first chunk in advance so that while it is playing,
        # the next one is already being synthesized (pipeline instead of synthesize → pause → synthesize)..
        next_text = await queue.get()
        next_audio_task = asyncio.create_task(self._synthesize(next_text))

        try:
            while True:
                if not vc.is_connected():
                    break

                try:
                    filename = await next_audio_task
                except Exception as e:
                    print(f"TTS synth error: {e}")
                    filename = None

                # Synthesize other files in queue
                if not queue.empty():
                    upcoming_text = queue.get_nowait()
                    next_audio_task = asyncio.create_task(self._synthesize(upcoming_text))
                else:
                    next_audio_task = None

                if filename:
                    try:
                        await self._play(vc, filename)
                    except Exception as e:
                        print(f"TTS playback error: {e}")
                        with contextlib.suppress(OSError):
                            os.remove(filename)

                if next_audio_task is None:
                    if queue.empty():
                        break
                    upcoming_text = await queue.get()
                    next_audio_task = asyncio.create_task(self._synthesize(upcoming_text))
        finally:
            self.tts_workers.pop(guild_id, None)


Singleton: VoiceCog = None

def setup(bot: commands.Bot):
    bot.add_cog(VoiceCog(bot))