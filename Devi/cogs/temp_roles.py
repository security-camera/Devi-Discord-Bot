from datetime import datetime, timedelta, timezone

import disnake
from disnake.ext import commands, tasks

import i18n
from duration_utils import parse_duration_seconds
from logs import send_log, LogColor
from discord_i18n import localized
from db import db_cursor
from permissions import validate_permissions


def insert_temp_role(record: dict) -> int:
    with db_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO temp_roles (guild_id, user_id, role_id, moderator_id, created_at, expires_at)
               VALUES (%(guild_id)s, %(user_id)s, %(role_id)s, %(moderator_id)s, %(created_at)s, %(expires_at)s)
               RETURNING id""",
            record,
        )
        return cur.fetchone()["id"]


def fetch_expired_temp_roles(now: datetime) -> list:
    with db_cursor() as cur:
        cur.execute(
            """SELECT id, guild_id, user_id, role_id FROM temp_roles WHERE expires_at <= %s""",
            (now.isoformat(),),
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def delete_temp_roles(ids: list[int]):
    """Remove a batch of expired temp role records in a single query."""
    if not ids:
        return
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM temp_roles WHERE id = ANY(%s)", (ids,))


class TempRolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.expire_temp_roles_loop.start()

    def cog_unload(self):
        self.expire_temp_roles_loop.cancel()

    @staticmethod
    async def grant_temp_role(guild: disnake.Guild, member: disnake.Member, role: disnake.Role, duration_seconds: int, moderator_id: int, reason: str | None = None) -> dict:
        """Grant a temporary role to a member and schedule its automatic removal.

        Raises disnake.Forbidden / disnake.HTTPException if the role could
        not be added; the record is only persisted on success."""
        await member.add_roles(
            role,
            reason=reason or f"Temporary role granted by moderator {moderator_id} for {duration_seconds}s",
        )

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)

        record = {
            "guild_id": guild.id,
            "user_id": member.id,
            "role_id": role.id,
            "moderator_id": moderator_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        record["id"] = insert_temp_role(record)

        return record

    @tasks.loop(minutes=1)
    async def expire_temp_roles_loop(self):
        now = datetime.now(timezone.utc)
        expired = fetch_expired_temp_roles(now)

        if not expired:
            return

        processed_ids = []

        for record in expired:
            gid = record["guild_id"]
            guild = self.bot.get_guild(gid)
            if guild is not None:
                member = guild.get_member(record["user_id"])
                role = guild.get_role(record["role_id"])
                if member is not None and role is not None and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Temp role expired")
                    except (disnake.Forbidden, disnake.HTTPException):
                        pass

                await send_log(
                    gid,
                    i18n.t("temp_role_cmd.log_expired_title", locale=gid),
                    color=LogColor.Member,
                    fields=[
                        (i18n.t("logs_cog.fields.member", locale=gid), f"<@{record['user_id']}>"),
                        (i18n.t("logs_cog.fields.role", locale=gid), role.mention if role else str(record["role_id"])),
                    ]
                )

            processed_ids.append(record["id"])

        delete_temp_roles(processed_ids)

    @expire_temp_roles_loop.before_loop
    async def before_expire_temp_roles_loop(self):
        await self.bot.wait_until_ready()

    @commands.slash_command(
        name="temp_role",
        description=localized("commands.temp_role.description"),
    )
    async def temp_role(
            self,
            inter: disnake.ApplicationCommandInteraction,
            member: disnake.Member = commands.Param(
                name=localized("commands.temp_role.param_member_name"),
                description=localized("commands.temp_role.param_member"),
            ),
            role: disnake.Role = commands.Param(
                name=localized("commands.temp_role.param_role_name"),
                description=localized("commands.temp_role.param_role"),
            ),
            duration: str = commands.Param(
                name=localized("commands.temp_role.param_duration_name"),
                description=localized("commands.temp_role.param_duration"),
            )
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{disnake.Permissions(manage_roles=True): True}]):
            return None

        if role >= inter.guild.me.top_role:
            return await inter.response.send_message(
                i18n.t("temp_role_cmd.role_too_high_for_bot", locale=gid),
                ephemeral=True
            )

        if role >= inter.author.top_role and not inter.permissions.administrator:
            return await inter.response.send_message(
                i18n.t("temp_role_cmd.role_too_high_for_user", locale=gid),
                ephemeral=True
            )

        seconds, error = parse_duration_seconds(duration, locale=gid)
        if error:
            return await inter.response.send_message(error, ephemeral=True)

        try:
            record = await self.grant_temp_role(
                guild=inter.guild,
                member=member,
                role=role,
                duration_seconds=seconds,
                moderator_id=inter.author.id,
                reason=f"Временная роль от {inter.author} на {duration}",
            )
        except disnake.Forbidden:
            return await inter.response.send_message(
                i18n.t("temp_role_cmd.forbidden", locale=gid), ephemeral=True
            )
        except disnake.HTTPException as e:
            return await inter.response.send_message(
                i18n.t("temp_role_cmd.http_error", locale=gid, error=e), ephemeral=True
            )

        expires_at = datetime.fromisoformat(record["expires_at"])

        embed = disnake.Embed(
            description=i18n.t(
                "temp_role_cmd.success",
                locale=gid,
                member=member.mention,
                role=role.mention,
                expires_full=f"<t:{int(expires_at.timestamp())}:f>",
                expires_relative=f"<t:{int(expires_at.timestamp())}:R>",
            ),
            color=LogColor.Member
        )

        await inter.response.send_message(embed=embed, ephemeral=False)

        return await send_log(
            inter.guild,
            i18n.t("temp_role_cmd.log_granted_title", locale=gid),
            color=LogColor.Member,
            fields=[
                (i18n.t("logs_cog.fields.member", locale=gid), f"{member.mention} ({member.id})"),
                (i18n.t("logs_cog.fields.role", locale=gid), role.mention),
                (i18n.t("logs_cog.fields.moderator", locale=gid), f"{inter.author.mention} ({inter.author.id})"),
                (i18n.t("temp_role_cmd.field_expires", locale=gid), f"<t:{int(expires_at.timestamp())}:f>"),
            ]
        )


def setup(bot: commands.Bot):
    bot.add_cog(TempRolesCog(bot))