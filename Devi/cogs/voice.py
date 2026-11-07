import os
import wave
import tempfile
import asyncio
import threading
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
from paths import env_var, env_int, PIPER_VOICES_DIR

from apis_integration.topgg import vote_value

MAX_TTS_LENGTH = env_int("MAX_TTS_LENGTH", 400)
MAX_TTS_LENGTH_VOTED = env_int("MAX_TTS_LENGTH_VOTED", 800)
EMPTY_CHANNEL_TIMEOUT = env_int("EMPTY_CHANNEL_TIMEOUT", 60)
TTS_PLAYBACK_TIMEOUT = env_int("TTS_PLAYBACK_TIMEOUT", 60)
SYNTH_TIMEOUT = env_int("SYNTH_TIMEOUT", 30)
VOICE_LOAD_TIMEOUT = env_int("VOICE_LOAD_TIMEOUT", 60)
VOTE_LOOKUP_TIMEOUT = env_int("VOTE_LOOKUP_TIMEOUT", 2)
FALLBACK_VOICE = env_var("FALLBACK_VOICE", "")

_synth_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="piper")

_voice_cache: dict[str, PiperVoice] = {}
_voice_cache_lock = asyncio.Lock()


def _reset_executor(stuck: ThreadPoolExecutor):
    """A hung thread can't be killed, so abandon the stuck executor and create a fresh one.
    Does nothing if the executor was already replaced by another task."""
    global _synth_executor
    if _synth_executor is not stuck:
        return

    _synth_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="piper")
    stuck.shutdown(wait=False, cancel_futures=True)
    # Models loaded by the hung thread may be in a bad state, so reload them
    _voice_cache.clear()
    print("[voice] Piper executor was reset")


def _voice_exists(name: str) -> bool:
    return bool(name) and (PIPER_VOICES_DIR / f"{name}.onnx").exists()


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
            executor = _synth_executor
            try:
                # Loading takes a few seconds, so models are cached after the first use
                voice = await asyncio.wait_for(
                    loop.run_in_executor(executor, PiperVoice.load, str(model_path)),
                    timeout=VOICE_LOAD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                print(f"[voice] Loading voice {name!r} hung for over {VOICE_LOAD_TIMEOUT}s")
                _reset_executor(executor)
                raise
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

    async def _drop_dead_voice_client(self, guild: disnake.Guild) -> disnake.VoiceClient | None:
        """Returns the guild's voice client if it is really connected.
        A client that exists but lost its connection is dropped, so that the next
        /join creates a fresh one instead of calling move_to on a dead object."""
        vc = guild.voice_client
        if vc is None:
            return None

        if vc.is_connected():
            return vc

        print(f"[voice] Dropping dead voice client in guild {guild.id}")
        with contextlib.suppress(Exception):
            await vc.disconnect(force=True)
        self._cleanup_guild(guild.id)
        return None

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

        # The bot itself was kicked or disconnected: drop the worker and the queue
        if self.bot.user and member.id == self.bot.user.id and after.channel is None:
            self._cleanup_guild(guild.id)
            return

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

        voice_client = await self._drop_dead_voice_client(inter.guild)

        try:
            if voice_client is None:
                voice_client = await channel.connect()
            else:
                await voice_client.move_to(channel)
        except (disnake.ClientException, disnake.HTTPException, asyncio.TimeoutError) as e:
            # channel.connect() raises asyncio.TimeoutError when the handshake takes too long
            if command:
                await inter.response.send_message(
                    i18n.t("voice_cog.join_failed", locale=gid, error=e or type(e).__name__),
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

        # force=True so that a dead client is also removed
        with contextlib.suppress(Exception):
            await voice_client.disconnect(force=True)
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

        vc = await self._drop_dead_voice_client(inter.guild)

        if vc is None:
            return await inter.response.send_message(
                i18n.t("voice_cog.not_in_voice", locale=gid),
                ephemeral=True
            )

        # The top.gg lookup may be slow, so acknowledge the interaction first
        await inter.response.defer(ephemeral=True)

        try:
            max_length, ad = await asyncio.wait_for(
                vote_value(inter.author.id, MAX_TTS_LENGTH_VOTED, MAX_TTS_LENGTH, locale=gid),
                timeout=VOTE_LOOKUP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print("[voice] top.gg vote lookup timed out, using the default limit")
            max_length, ad = MAX_TTS_LENGTH, ""

        if len(text) > max_length:
            return await inter.edit_original_response(
                content=i18n.t("voice_cog.too_long", locale=gid, max=max_length) + ad
            )

        voice = resolve_voice(locale or gid)

        position = self.enqueue_tts(inter.guild.id, vc, text, voice)

        return await inter.edit_original_response(
            content=i18n.t("voice_cog.queued", locale=gid, position=position)
        )

    @staticmethod
    async def _synthesize(text: str, voice: str) -> str:
        """Synthesizes text locally with Piper using the given voice.
        Inference is CPU-bound and synchronous, so it
        runs in the single-thread Piper executor to avoid blocking the event
        loop and to keep espeak-ng calls serialized.
        If Piper hangs, the call times out and the executor is replaced."""
        fd, filename = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        # Set when we give up on this job, so a late-starting thread skips it
        abandoned = threading.Event()

        try:
            piper_voice = await _get_piper_voice(voice)
            loop = asyncio.get_running_loop()
            executor = _synth_executor

            def _run_synthesis():
                if abandoned.is_set():
                    return
                with wave.open(filename, "wb") as wav_file:
                    if hasattr(piper_voice, "synthesize_wav"):  # piper-tts >= 1.3
                        piper_voice.synthesize_wav(text, wav_file)
                    else:  # piper-tts <= 1.2
                        piper_voice.synthesize(text, wav_file)

            try:
                await asyncio.wait_for(
                    loop.run_in_executor(executor, _run_synthesis),
                    timeout=SYNTH_TIMEOUT,
                )
            except asyncio.TimeoutError:
                print(f"[voice] Piper synthesis hung for over {SYNTH_TIMEOUT}s (voice={voice!r}, len={len(text)})")
                abandoned.set()
                _reset_executor(executor)
                raise
        except BaseException:
            # BaseException so the temp file is also removed on task cancellation
            abandoned.set()
            with contextlib.suppress(OSError):
                os.remove(filename)
            raise

        return filename

    @staticmethod
    async def _play(vc: disnake.VoiceClient, filename: str):
        """Plays temp file and deletes it. If music is playing, pauses it for the
        duration of the TTS and resumes it afterwards."""
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

        def restore_ducked():
            if ducked_player is None:
                return
            try:
                if not ducked_player._end.is_set():
                    ducked_player.resume()
                    vc._player = ducked_player
            except AttributeError as e:
                print(f"[voice] Failed to restore audio after TTS: {e}")

        try:
            vc.play(disnake.FFmpegPCMAudio(filename), after=after)
        except Exception:
            # Don't leave the paused music stuck forever if play() fails
            with contextlib.suppress(OSError):
                os.remove(filename)
            restore_ducked()
            raise

        try:
            # Piper itself can no longer hang (no network calls), but this stays
            # as a safety net in case ffmpeg/discord never fires `after`.
            await asyncio.wait_for(finished.wait(), timeout=TTS_PLAYBACK_TIMEOUT)
        except asyncio.TimeoutError:
            print(f"[voice] TTS playback stuck for over {TTS_PLAYBACK_TIMEOUT}s, forcing stop")
            vc.stop()
            with contextlib.suppress(OSError):
                os.remove(filename)

        restore_ducked()

    @staticmethod
    def _discard_synth_task(task: asyncio.Task | None):
        """Cancels an unfinished synthesis task or removes the file of a finished one."""
        if task is None:
            return

        if not task.done():
            task.cancel()  # _synthesize removes its own temp file on cancellation
            return

        if not task.cancelled() and task.exception() is None:
            with contextlib.suppress(OSError):
                os.remove(task.result())

    async def _tts_worker(self, guild_id: int, vc: disnake.VoiceClient):
        queue = self.tts_queues[guild_id]
        next_audio_task = None

        try:
            # Synthesize the first item in advance so that while it is playing,
            # the next one is already being synthesized (pipeline).
            next_text, next_voice = await queue.get()
            next_audio_task = asyncio.create_task(self._synthesize(next_text, next_voice))

            while True:
                if not vc.is_connected():
                    break

                try:
                    filename = await next_audio_task
                except Exception as e:
                    print(f"TTS synth error: {type(e).__name__}: {e}")
                    filename = None
                next_audio_task = None

                # Start synthesizing the next queued item (it may use a different voice)
                if not queue.empty():
                    upcoming_text, upcoming_voice = queue.get_nowait()
                    next_audio_task = asyncio.create_task(self._synthesize(upcoming_text, upcoming_voice))

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
            self._discard_synth_task(next_audio_task)
            # Remove only our own entry, a newer worker may already be registered
            if self.tts_workers.get(guild_id) is asyncio.current_task():
                del self.tts_workers[guild_id]


Singleton: VoiceCog = None

def setup(bot: commands.Bot):
    bot.add_cog(VoiceCog(bot))