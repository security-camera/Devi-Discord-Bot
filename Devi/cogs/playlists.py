"""User playlists: /playlist create | add | remove | delete | steal | show | play.

Playlists belong to a Discord user (not to a server) and are public: anybody can
view them with /playlist show or copy them with /playlist steal.

Only track metadata (title, author, link, length) is stored. The audio is resolved
by yt-dlp when the playlist is played, so links never go stale because of an
outdated encoded track.
"""

import asyncio
import logging
from dataclasses import dataclass

import disnake
import psycopg
import yt_dlp
from disnake.ext import commands

import i18n
from cogs.music import (
    MAX_PLAYLIST_IMPORT,
    TrackEntry,
    format_duration,
    log_music,
    md_link,
    resolve_query_playlist,
)
from db import db_cursor
from discord_i18n import discord_localizations, localized
from paths import env_int
from permissions import Permission, require_permissions

logger = logging.getLogger(__name__)

MAX_PLAYLISTS = env_int("PLAYLIST_MAX_PER_USER", 25)
MAX_TRACKS = env_int("PLAYLIST_MAX_TRACKS", 200)
MAX_NAME_LENGTH = 50
PAGE_SIZE = 10
AUTOCOMPLETE_LIMIT = 25  # Discord shows at most 25 choices


# ---------------------------- Data layer ----------------------------
# Plain synchronous functions (psycopg pool); the cog runs them in a worker
# thread with asyncio.to_thread() so a slow query can never freeze the bot.

class PlaylistError(Exception):
    """Base class for the expected failures below."""


class NameTaken(PlaylistError):
    """The user already has a playlist with this name."""


class LimitReached(PlaylistError):
    """The user has reached MAX_PLAYLISTS."""


def clean_name(name: str) -> str:
    """Collapses whitespace and cuts the name to the allowed length."""
    return " ".join(name.split())[:MAX_NAME_LENGTH]


def name_key(name: str) -> str:
    """Case-insensitive comparison key. Done in Python (not with SQL lower()),
    so it doesn't depend on the database locale and also works for Cyrillic."""
    return clean_name(name).casefold()


def db_list_playlists(owner_id: int):
    with db_cursor() as cur:
        cur.execute(
            "SELECT p.id, p.name, COUNT(t.id) AS track_count "
            "FROM playlists p LEFT JOIN playlist_tracks t ON t.playlist_id = p.id "
            "WHERE p.owner_id = %s "
            "GROUP BY p.id "
            "ORDER BY p.updated_at DESC, p.id DESC",
            (owner_id,),
        )
        return cur.fetchall()


def db_find_playlist(owner_id: int, name: str):
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, name FROM playlists WHERE owner_id = %s AND name_key = %s",
            (owner_id, name_key(name)),
        )
        return cur.fetchone()


def db_default_playlist(owner_id: int):
    """The playlist the user worked with last (created or changed most recently)."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, name FROM playlists WHERE owner_id = %s "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (owner_id,),
        )
        return cur.fetchone()


def db_create_playlist(owner_id: int, name: str):
    name = clean_name(name)
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM playlists WHERE owner_id = %s", (owner_id,))
        if cur.fetchone()["n"] >= MAX_PLAYLISTS:
            raise LimitReached

        cur.execute(
            "INSERT INTO playlists (owner_id, name, name_key) VALUES (%s, %s, %s) "
            "ON CONFLICT (owner_id, name_key) DO NOTHING "
            "RETURNING id, name",
            (owner_id, name, name_key(name)),
        )
        row = cur.fetchone()
        if row is None:
            raise NameTaken
        return row


def db_delete_playlist(playlist_id: int) -> None:
    with db_cursor(commit=True) as cur:
        # playlist_tracks rows are removed by ON DELETE CASCADE
        cur.execute("DELETE FROM playlists WHERE id = %s", (playlist_id,))


def db_get_tracks(playlist_id: int):
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, title, author, uri, length_ms FROM playlist_tracks "
            "WHERE playlist_id = %s ORDER BY position, id",
            (playlist_id,),
        )
        return cur.fetchall()


def db_add_tracks(playlist_id: int, entries: list[TrackEntry]) -> tuple[int, int, int]:
    """Appends tracks to the playlist. Returns (added, duplicates, over_limit)."""
    added = duplicates = over_limit = 0

    with db_cursor(commit=True) as cur:
        # Serializes concurrent /playlist add calls on the same playlist
        cur.execute("SELECT id FROM playlists WHERE id = %s FOR UPDATE", (playlist_id,))
        cur.execute(
            "SELECT COALESCE(MAX(position), 0) AS last, COUNT(*) AS n "
            "FROM playlist_tracks WHERE playlist_id = %s",
            (playlist_id,),
        )
        row = cur.fetchone()
        position, free = row["last"], MAX_TRACKS - row["n"]

        for entry in entries:
            if free <= 0:
                over_limit += 1
                continue

            cur.execute(
                "INSERT INTO playlist_tracks (playlist_id, position, title, author, uri, length_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (playlist_id, uri) DO NOTHING",
                (playlist_id, position + 1, entry.title, entry.author, entry.uri, entry.length_ms),
            )
            if cur.rowcount:
                added += 1
                position += 1
                free -= 1
            else:
                duplicates += 1

        if added:
            cur.execute("UPDATE playlists SET updated_at = now() WHERE id = %s", (playlist_id,))

    return added, duplicates, over_limit


def db_remove_track(playlist_id: int, track_id: int) -> bool:
    with db_cursor(commit=True) as cur:
        cur.execute(
            "DELETE FROM playlist_tracks WHERE id = %s AND playlist_id = %s",
            (track_id, playlist_id),
        )
        removed = bool(cur.rowcount)
        if removed:
            cur.execute("UPDATE playlists SET updated_at = now() WHERE id = %s", (playlist_id,))
        return removed


def db_copy_playlist(source_id: int, new_owner_id: int, base_name: str):
    """Copies a playlist to another user. If the name is taken, a suffix is added: "Name (2)".
    Returns (new playlist row, number of copied tracks)."""
    base_name = clean_name(base_name)

    try:
        with db_cursor(commit=True) as cur:
            cur.execute("SELECT name_key FROM playlists WHERE owner_id = %s", (new_owner_id,))
            taken = {row["name_key"] for row in cur.fetchall()}
            if len(taken) >= MAX_PLAYLISTS:
                raise LimitReached

            name, number = base_name, 2
            while name_key(name) in taken:
                suffix = f" ({number})"
                name = base_name[: MAX_NAME_LENGTH - len(suffix)] + suffix
                number += 1

            cur.execute(
                "INSERT INTO playlists (owner_id, name, name_key) VALUES (%s, %s, %s) RETURNING id, name",
                (new_owner_id, name, name_key(name)),
            )
            new_playlist = cur.fetchone()

            cur.execute(
                "INSERT INTO playlist_tracks (playlist_id, position, title, author, uri, length_ms) "
                "SELECT %s, position, title, author, uri, length_ms "
                "FROM playlist_tracks WHERE playlist_id = %s",
                (new_playlist["id"], source_id),
            )
            return new_playlist, cur.rowcount
    except psycopg.errors.UniqueViolation:
        # Somebody created a playlist with this name in the same moment
        raise NameTaken from None


def find_track(rows, song: str):
    """Finds the track to remove. `song` is one of:
      "id:123"        - database id (what the autocomplete sends);
      "3"             - position in the playlist, starting from 1;
      a link          - exact match;
      a title         - exact or partial match, case-insensitive.
    Returns (row or None, number of candidates): more than one candidate means "ambiguous"."""
    song = song.strip()

    if song.startswith("id:") and song[3:].isdigit():
        wanted = int(song[3:])
        row = next((r for r in rows if r["id"] == wanted), None)
        return row, int(row is not None)

    if song.isdigit():
        index = int(song)
        if 1 <= index <= len(rows):
            return rows[index - 1], 1
        return None, 0

    needle = song.casefold()
    exact = [r for r in rows if r["uri"] == song or r["title"].casefold() == needle]
    candidates = exact or [
        r for r in rows if needle in r["title"].casefold() or needle in r["author"].casefold()
    ]
    return (candidates[0] if len(candidates) == 1 else None), len(candidates)


# ---------------------------- Helpers ----------------------------

def run_db(func, *args):
    """Runs a blocking database function in a worker thread."""
    return asyncio.to_thread(func, *args)


def esc(text: str) -> str:
    return disnake.utils.escape_markdown(text)


def option_name(default: str, key: str) -> disnake.Localized:
    """Option name with a FIXED canonical form (autocomplete reads sibling options by it)
    and translations from the locale files."""
    return disnake.Localized(default, data=discord_localizations(key))


def _selected_user_id(inter: disnake.ApplicationCommandInteraction) -> int | None:
    """Id of the user already picked in the `user` option (used by autocomplete)."""
    raw = inter.filled_options.get("user")
    try:
        return int(getattr(raw, "id", raw))
    except (TypeError, ValueError):
        return None


def _matching_names(rows, string: str) -> list[str]:
    needle = string.casefold().strip()
    return [r["name"] for r in rows if needle in r["name"].casefold()][:AUTOCOMPLETE_LIMIT]


async def own_playlists_autocomplete(inter: disnake.ApplicationCommandInteraction, string: str):
    return _matching_names(await run_db(db_list_playlists, inter.author.id), string)


async def user_playlists_autocomplete(inter: disnake.ApplicationCommandInteraction, string: str):
    """Playlists of the user picked in the `user` option (or of the author, if there is none)."""
    owner_id = _selected_user_id(inter) or inter.author.id
    return _matching_names(await run_db(db_list_playlists, owner_id), string)


async def remove_song_autocomplete(inter: disnake.ApplicationCommandInteraction, string: str):
    """Tracks of the selected (or the default) playlist: {label: "id:<row id>"}."""
    name = inter.filled_options.get("playlist")

    def load():
        playlist = db_find_playlist(inter.author.id, name) if name else db_default_playlist(inter.author.id)
        return db_get_tracks(playlist["id"]) if playlist else []

    needle = string.casefold().strip()
    choices: dict[str, str] = {}
    for index, row in enumerate(await run_db(load), start=1):
        if needle and needle not in row["title"].casefold():
            continue
        choices[f"{index}. {row['title']}"[:100]] = f"id:{row['id']}"
        if len(choices) >= AUTOCOMPLETE_LIMIT:
            break
    return choices


# ---------------------------- UI ----------------------------

class PlaylistPagesView(disnake.ui.View):
    """Paginated track list for /playlist show. The message is ephemeral,
    so only the author can press the buttons."""

    def __init__(self, inter: disnake.Interaction, title: str, owner: str, rows):
        super().__init__(timeout=180)
        self.inter = inter
        self.title = title
        self.owner = owner
        self.rows = rows
        self.page = 0
        self.pages = max(1, -(-len(rows) // PAGE_SIZE))
        self._sync_buttons()

    def _sync_buttons(self):
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.pages - 1

    def build_embed(self) -> disnake.Embed:
        gid = self.inter.guild_id
        start = self.page * PAGE_SIZE

        lines = []
        for number, row in enumerate(self.rows[start:start + PAGE_SIZE], start=start + 1):
            line = f"{number}. {md_link(row['title'], row['uri'])}"
            duration = format_duration(row["length_ms"])
            if duration:
                line += f" — `{duration}`"
            lines.append(line)

        embed = disnake.Embed(
            title=self.title,
            description="\n".join(lines) or i18n.t("playlist_cog.show_empty", locale=gid),
            color=disnake.Color.dark_magenta(),
        )
        embed.set_author(name=self.owner)
        # Only numbers, so the footer needs no translation: "page / pages  ·  total length"
        total = format_duration(sum(row["length_ms"] for row in self.rows))
        embed.set_footer(text=f"{self.page + 1} / {self.pages}" + (f"  ·  {total}" if total else ""))
        return embed

    @disnake.ui.button(emoji="◀️", style=disnake.ButtonStyle.secondary)
    async def previous_page(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        self.page -= 1
        self._sync_buttons()
        await inter.response.edit_message(embed=self.build_embed(), view=self)

    @disnake.ui.button(emoji="▶️", style=disnake.ButtonStyle.secondary)
    async def next_page(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        self.page += 1
        self._sync_buttons()
        await inter.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.inter.edit_original_message(view=self)
        except disnake.HTTPException:
            pass


# ---------------------------- Cog ----------------------------

class PlaylistCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _own_playlist(self, inter: disnake.Interaction, name: str | None):
        """The author's playlist `name`, or their most recently used one when `name` is omitted.
        Answers the (already deferred) interaction and returns None if there is no such playlist."""
        gid = inter.guild_id

        if name:
            playlist = await run_db(db_find_playlist, inter.author.id, name)
            if playlist is None:
                await inter.followup.send(i18n.t("playlist_cog.not_found", locale=gid, name=esc(clean_name(name))), ephemeral=True)
            return playlist

        playlist = await run_db(db_default_playlist, inter.author.id)
        if playlist is None:
            await inter.followup.send(i18n.t("playlist_cog.no_playlists_self", locale=gid), ephemeral=True)
        return playlist

    # ---------------------------- Commands ----------------------------

    @commands.slash_command(
        name="playlist",
        description=localized("commands.playlist.description"),
        contexts=disnake.InteractionContextTypes(guild=True),
    )
    async def playlist_command(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
        pass

    @playlist_command.sub_command(
        name="create",
        description=localized("commands.playlist_create.description"),
    )
    @require_permissions({Permission.MusicBlackList: False})
    async def create(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(
            name=option_name("name", "commands.playlist_create.param_name_name"),
            description=localized("commands.playlist_create.param_name"),
            min_length=1,
            max_length=MAX_NAME_LENGTH,
        ),
    ):
        gid = inter.guild_id
        name = clean_name(name)
        if not name:
            return await inter.response.send_message(i18n.t("playlist_cog.invalid_name", locale=gid), ephemeral=True)

        try:
            playlist = await run_db(db_create_playlist, inter.author.id, name)
        except NameTaken:
            return await inter.response.send_message(i18n.t("playlist_cog.exists", locale=gid, name=esc(name)), ephemeral=True)
        except LimitReached:
            return await inter.response.send_message(i18n.t("playlist_cog.limit_playlists", locale=gid, max=MAX_PLAYLISTS), ephemeral=True)

        return await inter.response.send_message(
            i18n.t("playlist_cog.created", locale=gid, name=esc(playlist["name"])), ephemeral=True
        )

    @playlist_command.sub_command(
        name="add",
        description=localized("commands.playlist_add.description"),
    )
    @require_permissions({Permission.MusicBlackList: False})
    async def add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        song: str = commands.Param(
            name=option_name("song", "commands.playlist_add.param_song_name"),
            description=localized("commands.playlist_add.param_song"),
            max_length=200,
        ),
        playlist: str = commands.Param(
            default=None,
            name=option_name("playlist", "commands.playlist_add.param_playlist_name"),
            description=localized("commands.playlist_add.param_playlist"),
            autocomplete=own_playlists_autocomplete,
            max_length=MAX_NAME_LENGTH,
        ),
    ):
        gid = inter.guild_id
        await inter.response.defer(ephemeral=True)

        target = await self._own_playlist(inter, playlist)
        if target is None:
            return None

        # yt-dlp extracts metadata from YouTube, SoundCloud, etc.
        try:
            result = await resolve_query_playlist(song, import_limit=min(MAX_PLAYLIST_IMPORT, MAX_TRACKS))
        except yt_dlp.utils.DownloadError as e:
            return await inter.followup.send(i18n.t("music_cog.extract_failed", locale=gid, error=str(e)), ephemeral=True)
        except Exception as e:
            logger.exception("yt-dlp search failed for %r", song)
            return await inter.followup.send(i18n.t("music_cog.extract_failed", locale=gid, error=e), ephemeral=True)

        if not result:
            return await inter.followup.send(i18n.t("music_cog.no_results", locale=gid), ephemeral=True)

        entries = [
            TrackEntry(track.title or "?", track.author or "", track.uri, track.length_ms)
            for track in result
        ]
        added, duplicates, over_limit = await run_db(db_add_tracks, target["id"], entries)
        playlist_name = esc(target["name"])

        if len(entries) == 1:
            if added:
                key, args = "track_added", {"title": esc(entries[0].title), "name": playlist_name}
            elif duplicates:
                key, args = "track_duplicate", {"title": esc(entries[0].title), "name": playlist_name}
            else:
                key, args = "limit_tracks", {"max": MAX_TRACKS}
            text = i18n.t(f"playlist_cog.{key}", locale=gid, **args)
        elif added:
            text = i18n.t("playlist_cog.tracks_added", locale=gid, count=added, name=playlist_name)
            if duplicates or over_limit:
                text += i18n.t("playlist_cog.skipped_note", locale=gid, count=duplicates + over_limit)
        elif over_limit:
            text = i18n.t("playlist_cog.limit_tracks", locale=gid, max=MAX_TRACKS)
        else:
            text = i18n.t("playlist_cog.tracks_all_duplicates", locale=gid, name=playlist_name)

        return await inter.followup.send(text, ephemeral=True)

    @playlist_command.sub_command(
        name="remove",
        description=localized("commands.playlist_remove.description"),
    )
    @require_permissions({Permission.MusicBlackList: False})
    async def remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        song: str = commands.Param(
            name=option_name("song", "commands.playlist_remove.param_song_name"),
            description=localized("commands.playlist_remove.param_song"),
            autocomplete=remove_song_autocomplete,
            max_length=200,
        ),
        playlist: str = commands.Param(
            default=None,
            name=option_name("playlist", "commands.playlist_remove.param_playlist_name"),
            description=localized("commands.playlist_remove.param_playlist"),
            autocomplete=own_playlists_autocomplete,
            max_length=MAX_NAME_LENGTH,
        ),
    ):
        gid = inter.guild_id
        await inter.response.defer(ephemeral=True)

        target = await self._own_playlist(inter, playlist)
        if target is None:
            return None

        rows = await run_db(db_get_tracks, target["id"])
        row, candidates = find_track(rows, song)

        if row is None:
            key = "track_ambiguous" if candidates > 1 else "track_not_found"
            return await inter.followup.send(
                i18n.t(f"playlist_cog.{key}", locale=gid, song=esc(song[:100]), name=esc(target["name"])),
                ephemeral=True,
            )

        await run_db(db_remove_track, target["id"], row["id"])
        return await inter.followup.send(
            i18n.t("playlist_cog.track_removed", locale=gid, title=esc(row["title"]), name=esc(target["name"])),
            ephemeral=True,
        )

    @playlist_command.sub_command(
        name="delete",
        description=localized("commands.playlist_delete.description"),
    )
    @require_permissions({Permission.MusicBlackList: False})
    async def delete(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(
            name=option_name("name", "commands.playlist_delete.param_name_name"),
            description=localized("commands.playlist_delete.param_name"),
            autocomplete=own_playlists_autocomplete,
            max_length=MAX_NAME_LENGTH,
        ),
    ):
        gid = inter.guild_id

        playlist = await run_db(db_find_playlist, inter.author.id, name)
        if playlist is None:
            return await inter.response.send_message(
                i18n.t("playlist_cog.not_found", locale=gid, name=esc(clean_name(name))), ephemeral=True
            )

        await run_db(db_delete_playlist, playlist["id"])
        return await inter.response.send_message(
            i18n.t("playlist_cog.deleted", locale=gid, name=esc(playlist["name"])), ephemeral=True
        )

    @playlist_command.sub_command(
        name="steal",
        description=localized("commands.playlist_steal.description"),
    )
    @require_permissions({Permission.MusicBlackList: False})
    async def steal(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(
            name=option_name("user", "commands.playlist_steal.param_user_name"),
            description=localized("commands.playlist_steal.param_user"),
        ),
        playlist: str = commands.Param(
            name=option_name("playlist", "commands.playlist_steal.param_playlist_name"),
            description=localized("commands.playlist_steal.param_playlist"),
            autocomplete=user_playlists_autocomplete,
            max_length=MAX_NAME_LENGTH,
        ),
    ):
        gid = inter.guild_id

        if user.id == inter.author.id:
            return await inter.response.send_message(i18n.t("playlist_cog.steal_self", locale=gid), ephemeral=True)

        source = await run_db(db_find_playlist, user.id, playlist)
        if source is None:
            return await inter.response.send_message(
                i18n.t("playlist_cog.not_found_user", locale=gid, name=esc(clean_name(playlist)), user=esc(user.display_name)),
                ephemeral=True,
            )

        try:
            copy, count = await run_db(db_copy_playlist, source["id"], inter.author.id, source["name"])
        except NameTaken:
            return await inter.response.send_message(i18n.t("playlist_cog.exists", locale=gid, name=esc(source["name"])), ephemeral=True)
        except LimitReached:
            return await inter.response.send_message(i18n.t("playlist_cog.limit_playlists", locale=gid, max=MAX_PLAYLISTS), ephemeral=True)

        return await inter.response.send_message(
            i18n.t(
                "playlist_cog.stolen", locale=gid,
                source=esc(source["name"]), user=esc(user.display_name), name=esc(copy["name"]), count=count,
            ),
            ephemeral=True,
        )

    @playlist_command.sub_command(
        name="show",
        description=localized("commands.playlist_show.description"),
    )
    @require_permissions({Permission.MusicBlackList: False})
    async def show(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(
            default=None,
            name=option_name("user", "commands.playlist_show.param_user_name"),
            description=localized("commands.playlist_show.param_user"),
        ),
        name: str = commands.Param(
            default=None,
            name=option_name("name", "commands.playlist_show.param_name_name"),
            description=localized("commands.playlist_show.param_name"),
            autocomplete=user_playlists_autocomplete,
            max_length=MAX_NAME_LENGTH,
        ),
    ):
        gid = inter.guild_id
        target = user or inter.author
        is_self = target.id == inter.author.id

        await inter.response.defer(ephemeral=True)

        if name is None:
            # No name: overview of all playlists of the user
            rows = await run_db(db_list_playlists, target.id)
            if not rows:
                key = "no_playlists_self" if is_self else "no_playlists_user"
                return await inter.followup.send(
                    i18n.t(f"playlist_cog.{key}", locale=gid, user=esc(target.display_name)), ephemeral=True
                )

            embed = disnake.Embed(
                title=i18n.t("playlist_cog.list_title", locale=gid, user=esc(target.display_name)),
                description="\n".join(
                    i18n.t("playlist_cog.list_item", locale=gid, name=esc(row["name"]), count=row["track_count"])
                    for row in rows
                ),
                color=disnake.Color.dark_magenta(),
            )
            return await inter.followup.send(embed=embed, ephemeral=True)

        playlist = await run_db(db_find_playlist, target.id, name)
        if playlist is None:
            key = "not_found" if is_self else "not_found_user"
            return await inter.followup.send(
                i18n.t(f"playlist_cog.{key}", locale=gid, name=esc(clean_name(name)), user=esc(target.display_name)),
                ephemeral=True,
            )

        rows = await run_db(db_get_tracks, playlist["id"])
        view = PlaylistPagesView(
            inter,
            title=i18n.t("playlist_cog.show_title", locale=gid, name=esc(playlist["name"])),
            owner=i18n.t("playlist_cog.show_owner", locale=gid, user=target.display_name),
            rows=rows,
        )
        # Buttons are pointless when everything fits on one page
        if view.pages > 1:
            return await inter.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
        return await inter.followup.send(embed=view.build_embed(), ephemeral=True)

    # NOTE: Discord requires required options to go before optional ones, so in the
    # slash command UI `name` is shown first even though `user` is declared first here.
    @playlist_command.sub_command(
        name="play",
        description=localized("commands.playlist_play.description"),
    )
    @require_permissions({Permission.MusicBlackList: False})
    async def play(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(
            default=None,
            name=option_name("user", "commands.playlist_play.param_user_name"),
            description=localized("commands.playlist_play.param_user"),
        ),
        name: str = commands.Param(
            name=option_name("name", "commands.playlist_play.param_name_name"),
            description=localized("commands.playlist_play.param_name"),
            autocomplete=user_playlists_autocomplete,
            max_length=MAX_NAME_LENGTH,
        ),
    ):
        gid = inter.guild_id
        target = user or inter.author

        # Joining the voice channel can take longer than 3 seconds
        await inter.response.defer(ephemeral=True)

        music = self.bot.get_cog("MusicCog")
        if music is None:
            return await inter.followup.send(i18n.t("music_cog.lavalink_unavailable", locale=gid), ephemeral=True)

        playlist = await run_db(db_find_playlist, target.id, name)
        if playlist is None:
            key = "not_found" if target.id == inter.author.id else "not_found_user"
            return await inter.followup.send(
                i18n.t(f"playlist_cog.{key}", locale=gid, name=esc(clean_name(name)), user=esc(target.display_name)),
                ephemeral=True,
            )

        rows = await run_db(db_get_tracks, playlist["id"])
        if not rows:
            return await inter.followup.send(i18n.t("playlist_cog.play_empty", locale=gid), ephemeral=True)

        # Ensure voice connection
        vc = inter.guild.voice_client
        if vc is None or not vc.is_connected():
            voice_cog = self.bot.get_cog("VoiceCog")
            vc = await voice_cog.handle_join(inter, None, False) if voice_cog else None
            if vc is None:
                return await inter.followup.send(i18n.t("music_cog.not_connected", locale=gid), ephemeral=True)

        # Resolve URIs to Track objects and enqueue them
        state = music.get_state(gid)
        added = 0
        errors = 0

        for row in rows:
            try:
                track = await music.extract_track(row["uri"], inter.author, gid)
                state.text_channel = inter.channel
                await state.queue.put(track)
                added += 1
            except Exception:
                errors += 1

        if added == 0:
            return await inter.followup.send(i18n.t("music_cog.extract_failed", locale=gid, error="Could not resolve any tracks"), ephemeral=True)

        await inter.followup.send(
            i18n.t("playlist_cog.playing", locale=gid, name=esc(playlist["name"]), count=added), ephemeral=True
        )
        return await log_music(
            inter, "play",
            [(i18n.t("music_cog.logs.field_track", locale=gid), f"{playlist['name']} ({added})")],
        )


def setup(bot: commands.Bot):
    bot.add_cog(PlaylistCog(bot))
