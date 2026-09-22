from datetime import datetime, timedelta, timezone

import disnake
from disnake.ext import commands, tasks

import i18n
from duration_utils import parse_duration_seconds
from logs import send_log, LogColor
from discord_i18n import localized
from db import db_cursor
from permissions import validate_permissions


def load_temp_roles() -> list:
    with db_cursor() as cur:
        cur.execute(
            """SELECT id, guild_id, user_id, role_id, moderator_id, created_at, expires_at
               FROM temp_roles ORDER BY id"""
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def save_temp_roles(records: list):
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM temp_roles")
        cur.executemany(
            """INSERT INTO temp_roles
                   (id, guild_id, user_id, role_id, moderator_id, created_at, expires_at)
               VALUES
                   (:id, :guild_id, :user_id, :role_id, :moderator_id, :created_at, :expires_at)""",
            records,
        )


class TempRolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.temp_roles_db = load_temp_roles()
        self.expire_temp_roles_loop.start()

    def cog_unload(self):
        self.expire_temp_roles_loop.cancel()

    def next_temp_role_id(self) -> int:
        if not self.temp_roles_db:
            return 1
        return max(r["id"] for r in self.temp_roles_db) + 1

    @tasks.loop(minutes=1)
    async def expire_temp_roles_loop(self):
        now = datetime.now(timezone.utc)
        still_active = []
        changed = False

        for record in self.temp_roles_db:
            if datetime.fromisoformat(record["expires_at"]) > now:
                still_active.append(record)
                continue

            changed = True
            gid = record["guild_id"]
            guild = self.bot.get_guild(gid)
            if guild is not None:
                member = guild.get_member(record["user_id"])
                role = guild.get_role(record["role_id"])
                if member is not None and role is not None and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Истек срок временной роли")
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

        if changed:
            self.temp_roles_db[:] = still_active
            save_temp_roles(self.temp_roles_db)

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
            await member.add_roles(role, reason=f"Временная роль от {inter.author} на {duration}")
        except disnake.Forbidden:
            return await inter.response.send_message(
                i18n.t("temp_role_cmd.forbidden", locale=gid), ephemeral=True
            )
        except disnake.HTTPException as e:
            return await inter.response.send_message(
                i18n.t("temp_role_cmd.http_error", locale=gid, error=e), ephemeral=True
            )

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)

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
        record = {
            "id": self.next_temp_role_id(),
            "guild_id": inter.guild.id,
            "user_id": member.id,
            "role_id": role.id,
            "moderator_id": inter.author.id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        self.temp_roles_db.append(record)
        save_temp_roles(self.temp_roles_db)

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