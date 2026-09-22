from datetime import datetime, timezone

import disnake
from disnake.ext import commands, tasks

import i18n
from duration_utils import parse_duration
from logs import send_log, LogColor
from discord_i18n import localized
from db import db_cursor
from permissions import validate_permissions, Permission

def load_warns() -> list:
    with db_cursor() as cur:
        cur.execute(
            """SELECT id, guild_id, user_id, moderator_id, reason, duration_raw,
                      created_at, expires_at, status
               FROM warns ORDER BY id"""
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def save_warns(warns: list):
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM warns")
        cur.executemany(
            """INSERT INTO warns
                   (id, guild_id, user_id, moderator_id, reason, duration_raw,
                    created_at, expires_at, status)
               VALUES
                   (:id, :guild_id, :user_id, :moderator_id, :reason, :duration_raw,
                    :created_at, :expires_at, :status)""",
            warns,
        )


def format_warn_line(w: dict, guild_id: int) -> str:
    status_emoji = "🟢" if w["status"] == "active" else "⚪"
    if w["expires_at"] is None:
        duration_text = i18n.t("warns_cog.forever", guild_id=guild_id)
    else:
        expires_dt = datetime.fromisoformat(w["expires_at"])
        duration_text = i18n.t(
            "warns_cog.expires_relative", guild_id=guild_id,
            relative=f"<t:{int(expires_dt.timestamp())}:R>"
        )

    created_dt = datetime.fromisoformat(w["created_at"])
    return i18n.t(
        "warns_cog.line",
        guild_id=guild_id,
        status_emoji=status_emoji,
        id=w["id"],
        status=w["status"],
        reason=w["reason"],
        duration=duration_text,
        moderator=f"<@{w['moderator_id']}>",
        created=f"<t:{int(created_dt.timestamp())}:f>",
    )


class WarnsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.warns_db = load_warns()
        self.expire_warns_loop.start()

    def cog_unload(self):
        self.expire_warns_loop.cancel()

    def next_warn_id(self) -> int:
        if not self.warns_db:
            return 1
        return max(w["id"] for w in self.warns_db) + 1

    async def check_and_expire_warns(self):
        now = datetime.now(timezone.utc)
        changed = False
        for w in self.warns_db:
            if w["status"] == "active" and w["expires_at"] is not None:
                if datetime.fromisoformat(w["expires_at"]) <= now:
                    w["status"] = "old"
                    changed = True
                    gid = w["guild_id"]
                    await send_log(
                        gid,
                        i18n.t("warns_cog.log_expired_title", locale=gid),
                        color=LogColor.Warn,
                        fields=[
                            (i18n.t("logs_cog.fields.user", locale=gid), f"<@{w['user_id']}>"),
                            (i18n.t("warns_cog.field_case_id", locale=gid), str(w["id"])),
                            (i18n.t("warns_cog.field_reason", locale=gid), w["reason"]),
                        ]
                    )
        if changed:
            save_warns(self.warns_db)

    @tasks.loop(minutes=1)
    async def expire_warns_loop(self):
        await self.check_and_expire_warns()

    @expire_warns_loop.before_loop
    async def before_expire_warns_loop(self):
        await self.bot.wait_until_ready()

    @commands.slash_command(name="warn", description=localized("commands.warn.description"))
    async def warn(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
        pass

    @warn.sub_command(
        name="add",
        description=localized("commands.warn_add.description"),
    )
    async def warn_add(
            self,
            inter: disnake.ApplicationCommandInteraction,
            member: disnake.Member = commands.Param(
                name=localized("commands.warn_add.param_member_name"),
                description=localized("commands.warn_add.param_member"),
            ),
            reason: str = commands.Param(
                name=localized("commands.warn_add.param_reason_name"),
                description=localized("commands.warn_add.param_reason"),
            ),
            duration: str = commands.Param(
                name=localized("commands.warn_add.param_duration_name"),
                description=localized("commands.warn_add.param_duration"),
                default=""
            )
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [
            {Permission.Warnings: True},
            {disnake.Permissions(moderate_members=True): True},
        ]):
            return None

        expires_at, error = parse_duration(duration, locale=gid)
        if error:
            return await inter.response.send_message(error, ephemeral=True)

        await self.check_and_expire_warns()

        new_warn = {
            "id": self.next_warn_id(),
            "guild_id": inter.guild.id,
            "user_id": member.id,
            "moderator_id": inter.author.id,
            "reason": reason,
            "duration_raw": duration,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
            "status": "active",
        }
        self.warns_db.append(new_warn)
        save_warns(self.warns_db)

        duration_display = i18n.t("warns_cog.forever", locale=gid) if expires_at is None else duration

        await send_log(
            inter.guild,
            i18n.t("warns_cog.log_warn_title", locale=gid),
            color=LogColor.Warn,
            fields=[
                (i18n.t("logs_cog.fields.user", locale=gid), f"{member.mention} ({member.id})"),
                (i18n.t("logs_cog.fields.moderator", locale=gid), f"{inter.author.mention} ({inter.author.id})"),
                (i18n.t("warns_cog.field_case_id", locale=gid), str(new_warn["id"])),
                (i18n.t("warns_cog.field_reason", locale=gid), reason),
                (i18n.t("warns_cog.field_duration", locale=gid), duration_display),
            ],
            thumbnail_url=member.display_avatar.url
        )

        active_warns = [
            w for w in self.warns_db
            if w["user_id"] == member.id
            and w.get("guild_id") == inter.guild.id
            and w["status"] == "active"
        ]
        active_warns.sort(key=lambda w: w["created_at"])

        await inter.response.send_message(
            i18n.t(
                "warns_cog.warn_success", locale=gid,
                member=member.mention, id=new_warn["id"], reason=reason, duration=duration_display, active_warns=len(active_warns)
            ),
            ephemeral=False
        )

        return None

    @warn.sub_command(
        name="show",
        description=localized("commands.warn_show.description"),
    )
    async def warn_show(
            self,
            inter: disnake.ApplicationCommandInteraction,
            member: disnake.Member = commands.Param(
                name=localized("commands.warn_show.param_member_name"),
                description=localized("commands.warn_show.param_member"),
            ),
    ):
        gid = inter.guild_id

        await self.check_and_expire_warns()

        user_warns = [
            w for w in self.warns_db
            if w["user_id"] == member.id and w.get("guild_id") == inter.guild.id
        ]
        user_warns.sort(key=lambda w: w["created_at"], reverse=True)

        if not user_warns:
            return await inter.response.send_message(
                i18n.t("warns_cog.no_warns", locale=gid, member=member.mention), ephemeral=True
            )

        active_count = sum(1 for w in user_warns if w["status"] == "active")

        embed = disnake.Embed(
            title=i18n.t("warns_cog.show_title", locale=gid, name=member.display_name),
            description=i18n.t(
                "warns_cog.show_description", locale=gid,
                active=active_count, total=len(user_warns)
            ),
            color=disnake.Color.orange()
        )

        for w in user_warns[:25]:
            embed.add_field(name="\u200b", value=format_warn_line(w, gid), inline=False)

        return await inter.response.send_message(embed=embed, ephemeral=True)

    @warn.sub_command(
        name="remove",
        description=localized("commands.warn_remove.description"),
    )
    async def warn_remove(
            self,
            inter: disnake.ApplicationCommandInteraction,
            case_id: int = commands.Param(
                name=localized("commands.warn_remove.param_case_id_name"),
                description=localized("commands.warn_remove.param_case_id"),
            ),
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [
            {Permission.Warnings: True},
            {disnake.Permissions(moderate_members=True): True},
        ]):
            return None

        target = next(
            (w for w in self.warns_db if w["id"] == case_id and w.get("guild_id") == inter.guild.id),
            None
        )

        if target is None:
            return await inter.response.send_message(
                i18n.t("warns_cog.not_found", locale=gid, id=case_id), ephemeral=True
            )

        self.warns_db[:] = [w for w in self.warns_db if w["id"] != case_id]
        save_warns(self.warns_db)

        await inter.response.send_message(
            i18n.t("warns_cog.removed", locale=gid, id=case_id, user=f"<@{target['user_id']}>"),
            ephemeral=True
        )

        return await send_log(
            inter.guild,
            i18n.t("warns_cog.log_removed_title", locale=gid),
            color=LogColor.Warn,
            fields=[
                (i18n.t("logs_cog.fields.user", locale=gid), f"<@{target['user_id']}>"),
                (i18n.t("warns_cog.field_case_id", locale=gid), str(case_id)),
                (i18n.t("warns_cog.field_removed_by", locale=gid), f"{inter.author.mention} ({inter.author.id})"),
                (i18n.t("warns_cog.field_original_reason", locale=gid), target["reason"]),
            ]
        )

    @warn.sub_command(
        name="obsolete",
        description=localized("commands.warn_obsolete.description"),
    )
    async def warn_obsolete(
            self,
            inter: disnake.ApplicationCommandInteraction,
            case_id: int = commands.Param(
                name=localized("commands.warn_obsolete.param_case_id_name"),
                description=localized("commands.warn_obsolete.param_case_id"),
            ),
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [
            {Permission.Warnings: True},
            {disnake.Permissions(moderate_members=True): True},
        ]):
            return None

        target = next(
            (w for w in self.warns_db if w["id"] == case_id and w.get("guild_id") == inter.guild.id),
            None
        )

        if target is None:
            return await inter.response.send_message(
                i18n.t("warns_cog.not_found", locale=gid, id=case_id), ephemeral=True
            )

        if target["status"] == "old":
            return await inter.response.send_message(
                i18n.t("warns_cog.already_old", locale=gid, id=case_id), ephemeral=True
            )

        target["status"] = "old"
        save_warns(self.warns_db)

        await inter.response.send_message(
            i18n.t("warns_cog.marked_old", locale=gid, id=case_id, user=f"<@{target['user_id']}>"),
            ephemeral=True
        )

        return await send_log(
            inter.guild,
            i18n.t("warns_cog.log_marked_manual_title", locale=gid),
            color=LogColor.Warn,
            fields=[
                (i18n.t("logs_cog.fields.user", locale=gid), f"<@{target['user_id']}>"),
                (i18n.t("warns_cog.field_case_id", locale=gid), str(case_id)),
                (i18n.t("warns_cog.field_marked_by", locale=gid), f"{inter.author.mention} ({inter.author.id})"),
            ]
        )

def setup(bot: commands.Bot):
    bot.add_cog(WarnsCog(bot))