"""Log system"""

from datetime import datetime, timezone

import disnake

from db import db_cursor

class LogColor:
    Voice = disnake.Color.blue()
    Stage = disnake.Color.purple()
    Message = disnake.Color.greyple()
    Warn = disnake.Color.orange()
    Moderation = disnake.Color.red()
    Member = disnake.Color.green()
    Bot = disnake.Color.gold()
    Command = disnake.Color.teal()
    Music = disnake.Color.dark_magenta()
    TempVoice = disnake.Color.dark_blue()

_log_channels: dict[int, int] = {}  # guild_id -> channel_id
_bot = None  # sets in init(bot) from main.py


def _load_log_channels() -> dict[int, int]:
    with db_cursor() as cur:
        cur.execute("SELECT guild_id, channel_id FROM log_channels")
        rows = cur.fetchall()
    return {row["guild_id"]: row["channel_id"] for row in rows}


def _save_log_channels(log_channels: dict[int, int]):
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM log_channels")
        cur.executemany(
            "INSERT INTO log_channels (guild_id, channel_id) VALUES (%s, %s)",
            list(log_channels.items()),
        )


_log_channels = None


def init(bot):
    global _bot, _log_channels
    _bot, _log_channels = bot, _load_log_channels()


def get_log_channel_id(guild_id: int) -> int | None:
    return _log_channels.get(guild_id)


def set_log_channel_id(guild_id: int, channel_id: int):
    _log_channels[guild_id] = channel_id
    _save_log_channels(_log_channels)


def remove_log_channel(guild_id: int):
    if _log_channels.pop(guild_id, None):
        _save_log_channels(_log_channels)


async def send_log(guild: "disnake.Guild | int", title: str, description: str = "",
                    color: disnake.Color | None = None, fields: "list[tuple[str, str]] | None" = None,
                    thumbnail_url: str | None = None, channel_id: int | None = None):
    guild_id = guild.id if isinstance(guild, disnake.Guild) else guild

    if guild_id is None or _bot is None:
        return

    channel_id = channel_id or _log_channels.get(guild_id)
    if channel_id is None:
        return

    channel = _bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await _bot.fetch_channel(channel_id)
        except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
            return

    embed = disnake.Embed(
        title=title,
        description=description,
        color=color or disnake.Color.greyple(),
        timestamp=datetime.now(timezone.utc)
    )

    if fields:
        for name, value in fields:
            embed.add_field(name=name, value=value or "—", inline=False)

    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    try:
        await channel.send(embed=embed)
    except (disnake.Forbidden, disnake.HTTPException):
        pass


async def get_audit_executor(guild: "disnake.Guild", action: "disnake.AuditLogAction",
                              target_id: int, max_age_seconds: int = 15):
    if not guild:
        return None
    try:
        async for entry in guild.audit_logs(action=action, limit=5):
            target = entry.target
            entry_target_id = getattr(target, "id", None)
            if entry_target_id == target_id:
                age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                if age <= max_age_seconds:
                    return entry.user
        return None
    except (disnake.Forbidden, disnake.HTTPException):
        return None