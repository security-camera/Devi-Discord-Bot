from datetime import datetime, timedelta, timezone

import disnake
from disnake.ext import commands, tasks

import i18n
from discord_i18n import localized

from logs import send_log, LogColor
from db import db_cursor
from duration_utils import parse_duration_seconds
from permissions import validate_permissions

def insert_temp_ban(record: dict) -> int:
    with db_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO temp_bans (guild_id, user_id, moderator_id, created_at, expires_at)
               VALUES (%(guild_id)s, %(user_id)s, %(moderator_id)s, %(created_at)s, %(expires_at)s)
               RETURNING id""",
            record,
        )
        return cur.fetchone()["id"]


def fetch_expired_temp_bans(now: datetime) -> list:
    with db_cursor() as cur:
        cur.execute(
            """SELECT id, guild_id, user_id FROM temp_bans WHERE expires_at <= %s""",
            (now.isoformat(),),
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def delete_temp_bans(ids: list[int]):
    """Remove a batch of expired temp ban records in a single query."""
    if not ids:
        return
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM temp_bans WHERE id = ANY(%s)", (ids,))


class TempBansCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.expire_temp_bans_loop.start()

    def cog_unload(self):
        self.expire_temp_bans_loop.cancel()

    @staticmethod
    async def grant_temp_ban(guild: disnake.Guild, member: disnake.Member, duration_seconds: int, moderator_id: int, reason: str | None = None) -> dict:
        """Ban a member temporarily and schedule their automatic unban.

        Raises disnake.Forbidden / disnake.HTTPException if the ban could
        not be issued; the record is only persisted on success."""
        await guild.ban(
            member,
            reason=f"Temporary ban by moderator {moderator_id} for {duration_seconds}s: {reason}",
        )

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)

        record = {
            "guild_id": guild.id,
            "user_id": member.id,
            "moderator_id": moderator_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        record["id"] = insert_temp_ban(record)

        return record

    @tasks.loop(minutes=1)
    async def expire_temp_bans_loop(self):
        now = datetime.now(timezone.utc)
        expired = fetch_expired_temp_bans(now)

        if not expired:
            return

        processed_ids = []

        for record in expired:
            gid = record["guild_id"]
            guild = self.bot.get_guild(gid)
            if guild is not None:
                try:
                    await guild.unban(
                        disnake.Object(id=record["user_id"]),
                        reason="Temp ban expired",
                    )
                except (disnake.Forbidden, disnake.HTTPException, disnake.NotFound):
                    pass

                await send_log(
                    gid,
                    i18n.t("temp_ban_cog.log_expired_title", locale=gid),
                    color=LogColor.Member,
                    fields=[
                        (i18n.t("logs_cog.fields.member", locale=gid), f"<@{record['user_id']}>"),
                    ]
                )

            processed_ids.append(record["id"])

        delete_temp_bans(processed_ids)

    @expire_temp_bans_loop.before_loop
    async def before_expire_temp_bans_loop(self):
        await self.bot.wait_until_ready()

    @commands.slash_command(
        name="temp_ban",
        description=localized("commands.temp_ban.description"),
    )
    async def temp_ban(
            self,
            inter: disnake.ApplicationCommandInteraction,
            member: disnake.Member = commands.Param(
                name=localized("commands.temp_ban.param_member_name"),
                description=localized("commands.temp_ban.param_member"),
            ),
            duration: str = commands.Param(
                name=localized("commands.temp_ban.param_duration_name"),
                description=localized("commands.temp_ban.param_duration"),
            ),
            reason: str = commands.Param(
                name=localized("commands.temp_ban.param_reason_name"),
                description=localized("commands.temp_ban.param_reason"),
                default=None,
            )
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{disnake.Permissions(ban_members=True): True}]):
            return None

        if member.top_role >= inter.guild.me.top_role:
            return await inter.response.send_message(
                i18n.t("temp_ban_cmd.member_too_high_for_bot", locale=gid),
                ephemeral=True
            )

        if member.top_role >= inter.author.top_role and not inter.permissions.administrator:
            return await inter.response.send_message(
                i18n.t("temp_ban_cmd.member_too_high_for_user", locale=gid),
                ephemeral=True
            )

        seconds, error = parse_duration_seconds(duration, locale=gid)
        if error:
            return await inter.response.send_message(error, ephemeral=True)

        try:
            record = await self.grant_temp_ban(inter.guild, member, seconds, inter.author.id, reason)
        except disnake.Forbidden:
            return await inter.response.send_message(
                i18n.t("temp_ban_cmd.forbidden", locale=gid), ephemeral=True
            )
        except disnake.HTTPException as e:
            return await inter.response.send_message(
                i18n.t("temp_ban_cmd.http_error", locale=gid, error=e), ephemeral=True
            )

        expires_at = datetime.fromisoformat(record["expires_at"])

        embed = disnake.Embed(
            description=i18n.t(
                "temp_ban_cmd.success",
                locale=gid,
                member=f"{member} ({member.id})",
                expires_full=f"<t:{int(expires_at.timestamp())}:f>",
                expires_relative=f"<t:{int(expires_at.timestamp())}:R>",
            ),
            color=LogColor.Moderation
        )

        await inter.response.send_message(embed=embed, ephemeral=False)

        return await send_log(
            inter.guild,
            i18n.t("temp_ban_cmd.log_banned_title", locale=gid),
            color=LogColor.Moderation,
            fields=[
                (i18n.t("logs_cog.fields.member", locale=gid), f"{member.mention} ({member.id})"),
                (i18n.t("logs_cog.fields.moderator", locale=gid), f"{inter.author.mention} ({inter.author.id})"),
                (i18n.t("temp_ban_cmd.field_expires", locale=gid), f"<t:{int(expires_at.timestamp())}:f>"),
                (i18n.t("logs_cog.fields.reason", locale=gid), reason),
            ]
        )


def setup(bot: commands.Bot):
    bot.add_cog(TempBansCog(bot))