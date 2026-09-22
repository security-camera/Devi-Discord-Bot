import asyncio
import disnake
from disnake import TextInputStyle
from disnake.ext import commands
from permissions import Permission, validate_permissions
from cogs.voice import Singleton
import i18n
import yt_dlp

from discord_i18n import localized
from logs import send_log, LogColor

# ---------- yt-dlp settings ----------

YDL_OPTIONS = {
    # Explicitly prefer an audio stream in the Opus codec (webm/opus) — this is
    # the same codec Discord's voice chat uses. If the format is already Opus,
    # ffmpeg can copy the stream WITHOUT re-encoding — this is the main
    # CPU saving (see build_audio_source below).
    "format": "bestaudio[acodec=opus]/bestaudio/best",
    "noplaylist": True,
    "default_search": "auto",
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    # YouTube has recently often been giving the "web" client links that it
    # then blocks itself when downloading (metadata is extracted, but not the
    # stream). android/ios clients usually return more stable direct links.
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },
}

ydl = yt_dlp.YoutubeDL(YDL_OPTIONS)

LOOP_EMOJI = {"off": "▶️", "track": "🔂", "queue": "🔁"}


def loop_label(guild_id: int, mode: str) -> str:
    return i18n.t(f"music_cog.loop.{mode}", locale=guild_id)


def loop_choices() -> list[disnake.OptionChoice]:
    return [
        disnake.OptionChoice(name=localized(f"music_cog.loop.{mode}"), value=mode)
        for mode in ("off", "track", "queue")
    ]


class Track:
    """Track's data"""

    def __init__(
        self,
        title: str,
        url: str,
        webpage_url: str,
        requester: disnake.Member,
        http_headers: dict | None = None,
        acodec: str | None = None,
    ):
        self.title = title
        self.url = url  # direct link to audio
        self.webpage_url = webpage_url  # direct link to video
        self.requester = requester
        # Headers yt-dlp obtained the stream link with. Without them the
        # YouTube CDN often drops the connection a second after playback starts.
        self.http_headers = http_headers or {}
        # Codec of the source audio stream (usually "opus" for YouTube) —
        # determines whether the stream can be sent to Discord without re-encoding.
        self.acodec = acodec


def build_audio_source(track: "Track") -> disnake.FFmpegPCMAudio:
    """Builds an audio source to send to the voice channel."""
    before_options = [
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
    ]
    if track.http_headers:
        header_lines = "".join(f"{k}: {v}\r\n" for k, v in track.http_headers.items())
        before_options += ["-headers", header_lines]

    return disnake.FFmpegPCMAudio(
        track.url,
        before_options=before_options,
        options=["-vn"],
    )


def build_interface_embed(guild_id: int, state: "GuildMusicState") -> disnake.Embed:
    """Builds the control panel embed based on the current server state."""
    if state.current:
        now_playing = f"[{state.current.title}]({state.current.webpage_url})"
    else:
        now_playing = i18n.t("music_cog.interface.nothing_playing", locale=guild_id)

    return disnake.Embed(
        title=i18n.t("music_cog.interface.title", locale=guild_id),
        description=(
            f"**{i18n.t('music_cog.interface.now_playing_label', locale=guild_id)}** {now_playing}\n"
            f"**{i18n.t('music_cog.interface.loop_label', locale=guild_id)}** "
            f"{LOOP_EMOJI[state.loop_mode]} {loop_label(guild_id, state.loop_mode)}\n\n"
            + i18n.t("music_cog.interface.help", locale=guild_id)
        ),
        color=disnake.Color.blurple(),
    )


# ---------- Logging helpers ----------

def track_field(guild_id: int, track: "Track") -> tuple[str, str]:
    """Builds a (name, value) log field describing a track."""
    return (
        i18n.t("music_cog.logs.field_track", locale=guild_id),
        f"[{track.title}]({track.webpage_url})",
    )


async def log_music(
    inter: disnake.Interaction,
    name: str,
    fields: list[tuple[str, str]] | None = None,
):
    """
    Sends a music action log to the guild's log channel.

    `name` is the suffix of the locale key music_cog.logs.<name> used as the
    embed title. The user who triggered the action is always added as the first field.
    """
    gid = inter.guild_id
    await send_log(inter.guild,
        i18n.t(f"music_cog.logs.{name}", locale=gid),
        color=LogColor.Music,
        fields=[
            (
                i18n.t("music_cog.logs.field_user", locale=gid),
                f"{inter.author.mention} ({inter.author.id})",
            ),
            *(fields or []),
        ],
    )


class GuildMusicState:
    """Contains queue, loop mode and play-state for guild"""

    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.current: Track | None = None
        self.text_channel: disnake.abc.Messageable | None = None
        self.play_next_event = asyncio.Event()
        self.loop_mode = "off"  # "off" | "track" | "queue"
        # The /interface panel message that needs to be updated when the track changes
        self.interface_message: disnake.Message | None = None
        self.player_task = self.bot.loop.create_task(self.player_loop())

    async def update_interface_message(self):
        """Redraws the /interface panel embed, if it was opened on this server."""
        if self.interface_message is None:
            return
        try:
            await self.interface_message.edit(embed=build_interface_embed(self.guild_id, self))
        except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
            # The message was deleted or the bot lost access — just stop tracking it
            self.interface_message = None

    async def player_loop(self):
        while True:
            self.play_next_event.clear()

            if self.loop_mode == "track" and self.current is not None:
                track = self.current
            else:
                track = await self.queue.get()
            self.current = track

            guild = self.bot.get_guild(self.guild_id)
            voice_client = guild.voice_client if guild else None

            if voice_client is None or not voice_client.is_connected():
                self.loop_mode = "off"
                await self.update_interface_message()
                break

            try:
                source = build_audio_source(track)

                def after_playing(error):
                    if error:
                        print(f"[music] Playback error for '{track.title}': {error}")
                    self.bot.loop.call_soon_threadsafe(self.play_next_event.set)

                voice_client.play(source, after=after_playing)

                # The track changed — refresh the /interface panel if it's open
                await self.update_interface_message()

            except Exception as e:
                # The most common cause of silence with no errors in chat:
                # ffmpeg not found (PATH), PyNaCl not installed, or the connection
                # to the source dropped. Print the full traceback to the console
                # and post it to the channel.
                import traceback
                traceback.print_exc()
                if self.text_channel:
                    await self.text_channel.send(
                        i18n.t(
                            "music_cog.playback_error",
                            guild_id=self.guild_id,
                            title=track.title,
                            error=e,
                        )
                    )
                self.play_next_event.set()

            await self.play_next_event.wait()

            # If queue loop is enabled — the track goes back to the end
            if self.loop_mode == "queue" and self.current is not None:
                await self.queue.put(self.current)

    def destroy(self):
        self.player_task.cancel()


# ---------------------- UI ----------------------

class PlayModal(disnake.ui.Modal):
    def __init__(self, cog: "MusicCog", guild_id: int):
        self.cog = cog
        components = [
            disnake.ui.TextInput(
                label=i18n.t("music_cog.modal.label", locale=guild_id),
                custom_id="query",
                style=TextInputStyle.short,
                placeholder=i18n.t("music_cog.modal.placeholder", locale=guild_id),
                max_length=200,
            )
        ]
        super().__init__(
            title=i18n.t("music_cog.modal.title", locale=guild_id),
            custom_id="music_play_modal",
            components=components,
        )

    async def callback(self, inter: disnake.ModalInteraction):
        await self.cog.handle_play(inter, inter.text_values["query"])


BUTTON_LABEL_KEYS = {
    "music_play": "music_cog.interface.button_play",
    "music_pause_resume": "music_cog.interface.button_pause_resume",
    "music_skip": "music_cog.interface.button_skip",
    "music_loop": "music_cog.interface.button_loop",
    "music_queue": "music_cog.interface.button_queue",
    "music_stop": "music_cog.interface.button_stop",
}


# noinspection unused-parameter
class MusicInterfaceView(disnake.ui.View):
    """Player`s menu."""

    def __init__(self, cog: "MusicCog", guild_id: int | None = None):
        super().__init__(timeout=None)
        self.cog = cog

        # guild_id is only passed when the panel is actually sent to a user —
        # in that case button labels are localized for that guild's language.
        # When the persistent view is registered in on_ready (without guild_id),
        # labels stay at their defaults — that's fine, since that instance is
        # never sent to chat, it's only needed for dispatching component
        # interactions on old messages by custom_id after a bot restart.
        if guild_id is not None:
            for child in self.children:
                key = BUTTON_LABEL_KEYS.get(child.custom_id)
                if key:
                    child.label = i18n.t(key, locale=guild_id)

    @disnake.ui.button(
        label="Добавить трек", emoji="➕",
        style=disnake.ButtonStyle.success, custom_id="music_play",
    )
    async def play_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(PlayModal(self.cog, inter.guild_id))

    @disnake.ui.button(
        label="Пауза / Продолжить", emoji="⏯️",
        style=disnake.ButtonStyle.primary, custom_id="music_pause_resume",
    )
    async def pause_resume_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await self.cog.handle_pause_resume(inter)

    @disnake.ui.button(
        label="Пропустить", emoji="⏭️",
        style=disnake.ButtonStyle.secondary, custom_id="music_skip",
    )
    async def skip_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await self.cog.handle_skip(inter)

    @disnake.ui.button(
        label="Повтор", emoji="🔁",
        style=disnake.ButtonStyle.secondary, custom_id="music_loop",
    )
    async def loop_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await self.cog.handle_loop(inter, None)

    @disnake.ui.button(
        label="Очередь", emoji="📜",
        style=disnake.ButtonStyle.secondary, custom_id="music_queue",
    )
    async def queue_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await self.cog.handle_queue(inter)

    @disnake.ui.button(
        label="Стоп", emoji="⏹️",
        style=disnake.ButtonStyle.danger, custom_id="music_stop",
    )
    async def stop_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await self.cog.handle_stop(inter)


# ---------------------------- Cog ----------------------------

class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}
        self._view_registered = False

    @commands.Cog.listener()
    async def on_ready(self):
        # Register the persistent view here instead of in __init__: at the time
        # __init__ is called (during load_extension), the event loop is not running yet,
        # so creating a disnake.ui.View would raise:
        # RuntimeError: no running event loop.
        if not self._view_registered:
            self.bot.add_view(MusicInterfaceView(self))
            self._view_registered = True

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.states[guild_id]

    @staticmethod
    async def extract_track(query: str, requester: disnake.Member, guild_id: int) -> Track:
        loop = asyncio.get_event_loop()

        def _extract():
            _info = ydl.extract_info(query, download=False)
            if "entries" in _info:  # result of search / playlist
                _info = _info["entries"][0]
            return _info

        info = await loop.run_in_executor(None, _extract)
        return Track(
            title=info.get("title") or i18n.t("music_cog.untitled_track", locale=guild_id),
            url=info["url"],
            webpage_url=info.get("webpage_url", query),
            requester=requester,
            http_headers=info.get("http_headers"),
            acodec=info.get("acodec"),
        )

    # ---------------- Handlers (Used by commands and buttons) ----------------
    # Every handler logs to the guild's log channel AFTER the user has been
    # answered, so a slow log channel can never break the 3-second interaction limit.

    async def handle_play(self, inter: disnake.Interaction, query: str):
        if await validate_permissions(inter, {Permission.MusicBlackList: False}):
            return None

        gid = inter.guild_id
        voice_client = inter.guild.voice_client

        if voice_client is None or not voice_client.is_connected():
            voice_cog = self.bot.get_cog("VoiceCog")

            voice_client = await voice_cog.handle_join(inter, None, False) if voice_cog else None

            if voice_client is None:
                return await inter.response.send_message(
                    i18n.t("music_cog.not_connected", locale=gid),
                    ephemeral=True,
                )

        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)

        try:
            track = await self.extract_track(query, inter.author, gid)
        except Exception as e:
            return await inter.followup.send(
                i18n.t("music_cog.extract_failed", locale=gid, error=e),
                ephemeral=True,
            )

        state = self.get_state(gid)
        state.text_channel = inter.channel
        await state.queue.put(track)

        # Something is already playing -> the track just goes to the queue
        already_playing = voice_client.is_playing() or voice_client.is_paused()
        reply_key = "track_added_queue" if already_playing else "track_loaded"

        await inter.followup.send(
            i18n.t(f"music_cog.{reply_key}", locale=gid, title=track.title),
            ephemeral=True,
        )
        return await log_music(inter, "play", [track_field(gid, track)])

    async def handle_pause_resume(self, inter: disnake.Interaction):
        if await validate_permissions(inter, {Permission.MusicBlackList: False}):
            return None

        gid = inter.guild_id
        vc = inter.guild.voice_client
        if vc is None:
            return await inter.response.send_message(i18n.t("music_cog.not_connected", locale=gid), ephemeral=True)

        # .get() so that we don't spawn a new player task just for logging
        state = self.states.get(gid)
        log_fields = [track_field(gid, state.current)] if state and state.current else []

        if vc.is_playing():
            vc.pause()
            await inter.response.send_message(i18n.t("music_cog.paused", locale=gid), ephemeral=True)
            return await log_music(inter, "pause", log_fields)
        elif vc.is_paused():
            vc.resume()
            await inter.response.send_message(i18n.t("music_cog.resumed", locale=gid), ephemeral=True)
            return await log_music(inter, "resume", log_fields)
        else:
            return await inter.response.send_message(i18n.t("music_cog.nothing_playing", locale=gid), ephemeral=True)

    async def handle_skip(self, inter: disnake.Interaction):
        if await validate_permissions(inter, {Permission.MusicBlackList: False}):
            return None

        gid = inter.guild_id
        vc = inter.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            # Remember the track BEFORE stopping — the player loop switches
            # state.current as soon as the next track starts
            state = self.states.get(gid)
            skipped = state.current if state else None

            vc.stop()  # will call after_playing -> next track
            await inter.response.send_message(i18n.t("music_cog.skipped", locale=gid), ephemeral=True)
            return await log_music(inter, "skip", [track_field(gid, skipped)] if skipped else None)
        else:
            return await inter.response.send_message(i18n.t("music_cog.nothing_playing", locale=gid), ephemeral=True)

    async def handle_stop(self, inter: disnake.Interaction):
        if await validate_permissions(inter, {Permission.MusicBlackList: False}):
            return None

        gid = inter.guild_id
        state = self.get_state(gid)

        # Collect data for the log before the state is wiped
        stopped = state.current
        cleared = state.queue.qsize()

        while not state.queue.empty():
            state.queue.get_nowait()
        state.current = None

        vc = inter.guild.voice_client
        if vc:
            vc.stop()

        await inter.response.send_message(i18n.t("music_cog.stopped", locale=gid), ephemeral=True)
        await state.update_interface_message()

        log_fields = []
        if stopped:
            log_fields.append(track_field(gid, stopped))
        log_fields.append((i18n.t("music_cog.logs.field_cleared", locale=gid), str(cleared)))
        return await log_music(inter, "stop", log_fields)

    async def handle_queue(self, inter: disnake.Interaction):
        if await validate_permissions(inter, {Permission.MusicBlackList: False}):
            return None

        gid = inter.guild_id
        state = self.get_state(gid)
        items = list(state.queue._queue)  # access to internal asyncio.Queue

        lines = []
        if state.current:
            lines.append(
                i18n.t(
                    "music_cog.queue_now_playing",
                    locale=gid,
                    title=state.current.title,
                    url=state.current.webpage_url,
                )
            )
        lines.append(
            i18n.t(
                "music_cog.queue_loop_status",
                locale=gid,
                emoji=LOOP_EMOJI[state.loop_mode],
                label=loop_label(gid, state.loop_mode),
            )
        )
        if items:
            lines.append(i18n.t("music_cog.queue_upcoming_header", locale=gid))
            for i, track in enumerate(items, start=1):
                lines.append(f"{i}. {track.title}")
        elif not state.current:
            lines = [i18n.t("music_cog.queue_empty", locale=gid)]

        await inter.response.send_message("\n".join(lines), ephemeral=True)
        return await log_music(inter, "queue")

    async def handle_loop(self, inter: disnake.Interaction, mode: str | None):
        if await validate_permissions(inter, [{Permission.MusicBlackList: False}]):
            return None

        gid = inter.guild_id
        state = self.get_state(gid)

        if mode is None:
            # off -> track -> queue -> off
            order = ["off", "track", "queue"]
            state.loop_mode = order[(order.index(state.loop_mode) + 1) % len(order)]
        else:
            state.loop_mode = mode

        await inter.response.send_message(
            i18n.t(
                "music_cog.loop_set",
                locale=gid,
                emoji=LOOP_EMOJI[state.loop_mode],
                label=loop_label(gid, state.loop_mode),
            ),
            ephemeral=True,
        )
        await state.update_interface_message()
        return await log_music(
            inter,
            "loop",
            [(
                i18n.t("music_cog.logs.field_mode", locale=gid),
                f"{LOOP_EMOJI[state.loop_mode]} {loop_label(gid, state.loop_mode)}",
            )],
        )

    # ---------------------------- Commands ----------------------------

    @commands.slash_command(name="music", description=localized("commands.music.description"))
    async def music_command(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
        pass

    @music_command.sub_command(
        name="play",
        description=localized("commands.music_play.description")
    )
    async def play(
        self,
        inter: disnake.ApplicationCommandInteraction,
        query: str = commands.Param(
            name=localized("commands.music_play.param_query_name"),
            description=localized("commands.music_play.param_query")
        ),
    ):
        await self.handle_play(inter, query)

    @music_command.sub_command(
        name="pause",
        description=localized("commands.music_pause.description")
    )
    async def pause(self, inter: disnake.ApplicationCommandInteraction):
        await self.handle_pause_resume(inter)

    @music_command.sub_command(
        name="resume",
        description=localized("commands.music_resume.description")
    )
    async def resume(self, inter: disnake.ApplicationCommandInteraction):
        await self.handle_pause_resume(inter)

    @music_command.sub_command(
        name="skip",
        description=localized("commands.music_skip.description")
    )
    async def skip(self, inter: disnake.ApplicationCommandInteraction):
        await self.handle_skip(inter)

    @music_command.sub_command(
        name="queue",
        description=localized("commands.music_queue.description")
    )
    async def queue_cmd(self, inter: disnake.ApplicationCommandInteraction):
        await self.handle_queue(inter)

    @music_command.sub_command(
        name="stop",
        description=localized("commands.music_stop.description")
    )
    async def stop(self, inter: disnake.ApplicationCommandInteraction):
        await self.handle_stop(inter)

    @music_command.sub_command(
        name="loop",
        description=localized("commands.music_loop.description")
    )
    async def loop(
        self,
        inter: disnake.ApplicationCommandInteraction,
        mode: str = commands.Param(
            default=None,
            choices=loop_choices(),
            name=localized("commands.music_loop.param_mode_name"),
            description=localized("commands.music_loop.param_mode"),
        ),
    ):
        await self.handle_loop(inter, mode)

    @music_command.sub_command(
        name="interface",
        description=localized("commands.music_interface.description")
    )
    async def interface(self, inter: disnake.ApplicationCommandInteraction):
        if await validate_permissions(inter, [{Permission.Admin: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        state = self.get_state(inter.guild_id)

        await inter.response.send_message(
            embed=build_interface_embed(inter.guild_id, state),
            view=MusicInterfaceView(self, inter.guild_id),
        )

        # Remember the message so it can be updated when the track/loop mode changes
        state.interface_message = await inter.original_message()

        return None


def setup(bot: commands.Bot):
    bot.add_cog(MusicCog(bot))