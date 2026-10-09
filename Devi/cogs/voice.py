import os
import wave
import tempfile
import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor

import disnake
from disnake.ext import commands
from piper import PiperVoice

import i18n
from i18n import DEFAULT_LOCALE, LocaleObject
from permissions import Permission, require_permissions
from discord_i18n import localized, locale_choices
from logs import send_log, LogColor
from paths import env_var, env_var_to_int, PIPER_VOICES_DIR

from other_apis.topgg_utils import vote_value

MAX_TTS_LENGTH = env_var_to_int("MAX_TTS_LENGTH", "400")
MAX_TTS_LENGTH_VOTED = env_var_to_int("MAX_TTS_LENGTH_VOTED", "800")
EMPTY_CHANNEL_TIMEOUT = env_var_to_int("EMPTY_CHANNEL_TIMEOUT", "60")
TTS_PLAYBACK_TIMEOUT = env_var_to_int("TTS_PLAYBACK_TIMEOUT", "60")
FALLBACK_VOICE = env_var("FALLBACK_VOICE", "")

_synth_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="piper")

_voice_cache: dict[str, PiperVoice] = {}
_voice_cache_lock = asyncio.Lock()


def _voice_exists(name: str) -> bool:
    return name and (PIPER_VOICES_DIR / f"{name}.onnx").exists()


def resolve_voice(locale: LocaleObject) -> str:
    for source in (locale, DEFAULT_LOCALE):
        name = i18n.t("voice_cog.tts-voice", locale=source)
        if _voice_exists(name):
            return name
        print(f"[voice] No usable Piper voice for locale {source!r} (got {name!r}), falling back")

    if not FALLBACK_VOICE:
        raise RuntimeError("[voice] No usable Piper voice for FALLBACK_VOICE. Voice is not defined")
    return FALLBACK_VOICE


async def _get_piper_voice(name: str) -> PiperVoice:
    async with _voice_cache_lock:
        voice = _voice_cache.get(name)
        if voice is None:
            model_path = PIPER_VOICES_DIR / f"{name}.onnx"
            if not model_path.exists():
                raise FileNotFoundError(
                    f"Piper voice model not found: {model_path}. "
                    f"Download it from https://huggingface.co/rhasspy/piper-voices"
                )
            loop = asyncio.get_running_loop()
            # Loading takes a few seconds, so models are cached after the first use
            voice = await loop.run_in_executor(_synth_executor, PiperVoice.load, str(model_path))
            _voice_cache[name] = voice
        return voice


class VoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        global Singleton
        self.bot = bot
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

    def enqueue_tts(self, guild_id: int, vc: disnake.VoiceClient, text: str, voice: str | None = None) -> int:
        """Queues (text, voice) for TTS on the specified server and ensures
        that the synthesis/playback worker is running. Returns
        position at queue."""
        queue = self.tts_queues.setdefault(guild_id, asyncio.Queue())
        queue.put_nowait((text, voice or resolve_voice(guild_id)))

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
    @require_permissions([{Permission.TTS: True}, {disnake.Permissions(administrator=True): True}])
    async def tts(
            self,
            inter: disnake.ApplicationCommandInteraction,
            text: str = commands.Param(
                name=localized("commands.tts.param_text_name"),
                description=localized("commands.tts.param_text"),
            ),
            locale: str = commands.Param(
                default=None,
                name=localized("commands.tts.param_locale_name"),
                description=localized("commands.tts.param_locale"),
                choices=locale_choices()
            )
    ):
        gid = inter.guild_id

        vc = inter.guild.voice_client

        if vc is None:
            return await inter.response.send_message(
                i18n.t("voice_cog.not_in_voice", locale=gid),
                ephemeral=True
            )

        max_length, ad = await vote_value(inter.author.id, MAX_TTS_LENGTH_VOTED, MAX_TTS_LENGTH, locale=gid)

        if len(text) > max_length:
            return await inter.response.send_message(
                i18n.t("voice_cog.too_long", locale=gid, max=max_length) + ad,
                ephemeral=True
            )

        voice = resolve_voice(locale or gid)

        position = self.enqueue_tts(inter.guild.id, vc, text, voice)

        return await inter.response.send_message(
            i18n.t("voice_cog.queued", locale=gid, position=position),
            ephemeral=True
        )

    async def _synthesize(self, text: str, voice: str) -> str:
        """Synthesizes text locally with Piper using the given voice.
        Inference is CPU-bound and synchronous, so it
        runs in the single-thread Piper executor to avoid blocking the event
        loop and to keep espeak-ng calls serialized."""
        fd, filename = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        try:
            piper_voice = await _get_piper_voice(voice)
            loop = asyncio.get_running_loop()

            def _run_synthesis():
                with wave.open(filename, "wb") as wav_file:
                    if hasattr(piper_voice, "synthesize_wav"):  # piper-tts >= 1.3
                        piper_voice.synthesize_wav(text, wav_file)
                    else:  # piper-tts <= 1.2
                        piper_voice.synthesize(text, wav_file)

            await loop.run_in_executor(_synth_executor, _run_synthesis)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(filename)
            raise

        return filename

    @staticmethod
    async def _play(vc: disnake.VoiceClient, filename: str):
        """Plays temp file and deletes it. ..."""  # (docstring unchanged)
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

        try:
            # Piper itself can no longer hang (no network calls), but this stays
            # as a safety net in case ffmpeg/discord never fires `after`.
            await asyncio.wait_for(finished.wait(), timeout=TTS_PLAYBACK_TIMEOUT)
        except asyncio.TimeoutError:
            print(f"[voice] TTS playback stuck for over {TTS_PLAYBACK_TIMEOUT}s, forcing stop")
            vc.stop()
            with contextlib.suppress(OSError):
                os.remove(filename)

        if ducked_player is not None:
            try:
                if not ducked_player._end.is_set():
                    ducked_player.resume()
                    vc._player = ducked_player
            except AttributeError as e:
                print(f"[voice] Failed to restore audio after TTS: {e}")

    async def _tts_worker(self, guild_id: int, vc: disnake.VoiceClient):
        queue = self.tts_queues[guild_id]

        # Synthesize the first item in advance so that while it is playing,
        # the next one is already being synthesized (pipeline).
        next_text, next_voice = await queue.get()
        next_audio_task = asyncio.create_task(self._synthesize(next_text, next_voice))

        try:
            while True:
                if not vc.is_connected():
                    break

                try:
                    filename = await next_audio_task
                except Exception as e:
                    print(f"TTS synth error: {e}")
                    filename = None

                # Start synthesizing the next queued item (it may use a different voice)
                if not queue.empty():
                    upcoming_text, upcoming_voice = queue.get_nowait()
                    next_audio_task = asyncio.create_task(self._synthesize(upcoming_text, upcoming_voice))
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
                    upcoming_text, upcoming_voice = await queue.get()
                    next_audio_task = asyncio.create_task(self._synthesize(upcoming_text, upcoming_voice))
        finally:
            if next_audio_task is not None and not next_audio_task.done():
                next_audio_task.cancel()
            self.tts_workers.pop(guild_id, None)


Singleton: VoiceCog = None

def setup(bot: commands.Bot):
    bot.add_cog(VoiceCog(bot))