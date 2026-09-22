from datetime import datetime, timezone, time

import disnake
from disnake.ext import commands, tasks

import i18n
from i18n import LocaleObject
from db import db_cursor
from logs import send_log, LogColor
from discord_i18n import localized, bool_to_yes_no_str
from permissions import validate_permissions, Permission
from paths import env_var_to_int

# Hour (UTC) to check birthdays
BIRTHDAY_CHECK_HOUR_UTC = env_var_to_int("BIRTHDAY_CHECK_HOUR_UTC", "9")

DAYS_IN_MONTH = {
    1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}


def is_valid_day(day: int, month: int) -> bool:
    return 1 <= day <= DAYS_IN_MONTH.get(month, 31)


# ------------------------------------------------------------ storage: birthdays


def load_birthdays() -> dict[int, dict]:
    """Returns all birthdays: user_id -> {"day", "month", "ping_on_servers"}"""
    with db_cursor() as cur:
        cur.execute("SELECT user_id, day, month, ping_on_servers FROM birthdays")
        rows = cur.fetchall()

    return {
        row["user_id"]: {
            "day": row["day"],
            "month": row["month"],
            "ping_on_servers": bool(row["ping_on_servers"]),
        }
        for row in rows
    }


def save_birthday(user_id: int, day: int, month: int, ping_on_servers: bool):
    with db_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO birthdays (user_id, day, month, ping_on_servers)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   day = excluded.day,
                   month = excluded.month,
                   ping_on_servers = excluded.ping_on_servers""",
            (user_id, day, month, int(ping_on_servers)),
        )


def get_birthday(user_id: int) -> tuple[int, int, bool] | None:
    with db_cursor() as cur:
        cur.execute(
            "SELECT day, month, ping_on_servers FROM birthdays WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()

    if not row:
        return None

    return row["day"], row["month"], bool(row["ping_on_servers"])


def remove_birthday(user_id: int) -> bool:
    """Returns True if record was deleted"""
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM birthdays WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0


# -------------------------------------------------------- storage: birthday_channels


def get_birthday_channel(guild_id: int) -> int | None:
    with db_cursor() as cur:
        cur.execute("SELECT channel_id FROM birthday_channels WHERE guild_id = ?", (guild_id,))
        row = cur.fetchone()
    return row["channel_id"] if row else None


def set_birthday_channel(guild_id: int, channel_id: int):
    with db_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO birthday_channels (guild_id, channel_id) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id""",
            (guild_id, channel_id),
        )


def remove_birthday_channel(guild_id: int) -> bool:
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM birthday_channels WHERE guild_id = ?", (guild_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------- helpers


def month_choices() -> list[disnake.OptionChoice]:
    return [
        disnake.OptionChoice(name=localized(f"birthday_cog.months.{i:02d}"), value=i)
        for i in range(1, 13)
    ]


def format_birthday(day: int, month: int, locale: LocaleObject = None) -> str:
    month_name = i18n.t(f"birthday_cog.months.{month:02d}", locale=locale)[4:]
    return i18n.t("birthday_cog.date_format", locale=locale, day=day, month=month_name)


class BirthdayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.birthday_check_loop.start()

    def cog_unload(self):
        self.birthday_check_loop.cancel()

    # ------------------------------------------------------------- congratulation

    async def congratulate(self, user_id: int, ping_on_servers: bool) -> tuple[bool, int]:
        """Pings user on servers with congratulation text (if ping_on_servers=True),
        One time writes DM to a user if it's on ANY guild with the bot.

        Returns (dm_delivered, servers_pinged) — used by test command"""
        dm_attempted = False
        dm_delivered = False
        servers_pinged = 0

        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member is None:
                continue

            if not dm_attempted:
                dm_attempted = True  # DM only once
                try:
                    await member.send(
                        i18n.t("birthday_cog.dm_congratulation", locale=guild.id, name=member.display_name)
                    )
                    dm_delivered = True
                except (disnake.Forbidden, disnake.HTTPException):
                    pass  # Closed DMs

            if not ping_on_servers:
                continue

            channel_id = get_birthday_channel(guild.id)
            if channel_id is None:
                continue

            channel = guild.get_channel(channel_id)
            if channel is None:
                continue

            try:
                await channel.send(
                    i18n.t("birthday_cog.server_congratulation", locale=guild.id, mention=member.mention)
                )
                servers_pinged += 1
            except (disnake.Forbidden, disnake.HTTPException):
                pass

        return dm_delivered, servers_pinged

    @tasks.loop(time=time(hour=BIRTHDAY_CHECK_HOUR_UTC, tzinfo=timezone.utc))
    async def birthday_check_loop(self):
        today = datetime.now(timezone.utc)

        for user_id, data in load_birthdays().items():
            if data["day"] == today.day and data["month"] == today.month:
                await self.congratulate(user_id, data["ping_on_servers"])

    @birthday_check_loop.before_loop
    async def before_birthday_check_loop(self):
        await self.bot.wait_until_ready()

    # --------------------------------------------------------------- commands

    @commands.slash_command(
        name="birthday",
        description=localized("commands.birthday.description"),
    )
    async def birthday_command(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
        pass

    @birthday_command.sub_command(
        name="set",
        description=localized("commands.birthday_set.description"),
    )
    async def birthday_set_command(
            self,
            inter: disnake.ApplicationCommandInteraction,
            day: int = commands.Param(
                min_value=1,
                max_value=31,
                name=localized("commands.birthday_set.param_day_name"),
                description=localized("commands.birthday_set.param_day"),
            ),
            month: int = commands.Param(
                min_value=1,
                max_value=12,
                name=localized("commands.birthday_set.param_month_name"),
                description=localized("commands.birthday_set.param_month"),
                choices=month_choices(),
            ),
            ping_on_servers: bool = commands.Param(
                default=True,
                name=localized("commands.birthday_set.param_ping_on_servers_name"),
                description=localized("commands.birthday_set.param_ping_on_servers"),
            ),
            user: disnake.Member = commands.Param(
                default=None,
                name=localized("commands.birthday_set.param_user_name"),
                description=localized("commands.birthday_set.param_user"),
            )
    ):
        user = user or inter.author

        gid = inter.guild_id

        if user != inter.author and await validate_permissions(inter, {Permission.Developer: True}):
            return None

        if not is_valid_day(day, month):
            return await inter.response.send_message(
                i18n.t("birthday_cog.invalid_date", locale=gid), ephemeral=True
            )

        save_birthday(user.id, day, month, ping_on_servers)

        return await inter.response.send_message(
            i18n.t(
                "birthday_cog.set_success",
                locale=gid,
                date=format_birthday(day, month, gid),
            ),
            ephemeral=True,
        )

    @birthday_command.sub_command(
        name="check",
        description=localized("commands.birthday_check.description"),
    )
    async def birthday_check(
            self,
            inter: disnake.ApplicationCommandInteraction,
            user: disnake.Member | None = commands.Param(
                default=None,
                name=localized("commands.birthday_check.param_user_name"),
                description=localized("commands.birthday_check.param_user"),
            ),
    ):
        gid = inter.guild_id
        target = user or inter.author

        result = get_birthday(target.id)

        if result is None:
            return await inter.response.send_message(
                i18n.t("birthday_cog.not_set", locale=gid, target=target.mention),
                ephemeral=True,
            )

        day, month, _ = result

        return await inter.response.send_message(
            i18n.t(
                "birthday_cog.check_result",
                locale=gid,
                target=target.mention,
                date=format_birthday(day, month, gid),
            ),
            ephemeral=True,
        )

    @birthday_command.sub_command(
        name="remove",
        description=localized("commands.birthday_remove.description"),
    )
    async def birthday_remove(
            self,
            inter: disnake.ApplicationCommandInteraction,
            user: disnake.Member | None = commands.Param(
                default=None,
                name=localized("commands.birthday_remove.param_user_name"),
                description=localized("commands.birthday_remove.param_user"),
            ),
    ):
        gid = inter.guild_id
        target = user or inter.author

        # Only developer can change birthdays of other users
        if target.id != inter.author.id and await validate_permissions(inter, {Permission.Developer: True}):
            return None

        removed = remove_birthday(target.id)

        key = "birthday_cog.removed" if removed else "birthday_cog.not_set"
        return await inter.response.send_message(i18n.t(key, locale=gid, target=target.mention), ephemeral=True)

    @birthday_command.sub_command(
        name="channel",
        description=localized("commands.birthday_channel.description"),
    )
    async def birthday_channel(
            self,
            inter: disnake.ApplicationCommandInteraction,
            channel: disnake.TextChannel | None = commands.Param(
                default=None,
                name=localized("commands.birthday_channel.param_channel_name"),
                description=localized("commands.birthday_channel.param_channel"),
            ),
    ):
        if await validate_permissions(inter, [{Permission.Admin: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        gid = inter.guild_id

        if not channel:
            remove_birthday_channel(gid)

            await inter.response.send_message(i18n.t("birthday_cog.channel_removed", locale=gid), ephemeral=True)

            return await send_log(
                inter.guild,
                i18n.t("birthday_cog.log_channel_removed_title", locale=gid),
                color=LogColor.Bot,
                fields=[
                    (i18n.t("logs_cog.fields.moderator", locale=gid), f"{inter.author.mention} ({inter.author.id})"),
                ],
            )

        set_birthday_channel(gid, channel.id)

        await inter.response.send_message(i18n.t("birthday_cog.channel_set", locale=gid, channel=channel.mention), ephemeral=True)

        return await send_log(
            inter.guild,
            i18n.t("birthday_cog.log_channel_set_title", locale=gid),
            color=LogColor.Bot,
            fields=[
                (i18n.t("logs_cog.fields.channel", locale=gid), channel.mention),
                (i18n.t("logs_cog.fields.moderator", locale=gid), f"{inter.author.mention} ({inter.author.id})"),
            ],
        )

    @birthday_command.sub_command(
        name="congratulate",
        description=localized("commands.birthday_congratulate.description"),
    )
    async def birthday_congratulate(
            self,
            inter: disnake.ApplicationCommandInteraction,
            user: disnake.Member | None = commands.Param(
                default=None,
                name=localized("commands.birthday_congratulate.param_user_name"),
                description=localized("commands.birthday_congratulate.param_user"),
            ),
    ):
        if await validate_permissions(inter, {Permission.Developer: True}):
            return None

        gid = inter.guild_id
        target = user or inter.author

        await inter.response.defer(ephemeral=True)

        data = load_birthdays().get(target.id, None)

        if not data:
            return await inter.edit_original_response(
                i18n.t("birthday_cog.test_failed", locale=gid),
            )

        dm_delivered, servers_pinged = await self.congratulate(target.id, ping_on_servers=data["ping_on_servers"])

        return await inter.edit_original_response(
            i18n.t(
                "birthday_cog.test_triggered",
                locale=gid,
                target=target.mention,
                dm=bool_to_yes_no_str(dm_delivered, locale=gid),
                servers=servers_pinged,
            ),
        )


def setup(bot: commands.Bot):
    load_birthdays()
    bot.add_cog(BirthdayCog(bot))