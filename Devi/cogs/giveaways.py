import random
from datetime import datetime, timedelta, timezone

import disnake
from disnake.ext import commands, tasks

import i18n
from discord_i18n import localized
from duration_utils import parse_duration_seconds
from permissions import validate_permissions, Permission
from db import db_cursor

CHECK_INTERVAL = 15
JOIN_PREFIX = "giveaway_join:"


def load_giveaways() -> dict:

    with db_cursor() as cur:
        cur.execute(
            """SELECT message_id, guild_id, channel_id, host_id, name, prize,
                      winners_count, end_time, ended
               FROM giveaways"""
        )
        giveaway_rows = cur.fetchall()
        cur.execute(
            "SELECT giveaway_message_id, user_id FROM giveaway_participants ORDER BY giveaway_message_id, rowid"
        )
        participant_rows = cur.fetchall()

    participants_by_giveaway: dict[int, list[int]] = {}
    for row in participant_rows:
        participants_by_giveaway.setdefault(row["giveaway_message_id"], []).append(row["user_id"])

    result: dict = {}
    for row in giveaway_rows:
        result[str(row["message_id"])] = {
            "guild_id": row["guild_id"],
            "channel_id": row["channel_id"],
            "host_id": row["host_id"],
            "name": row["name"],
            "prize": row["prize"],
            "winners_count": row["winners_count"],
            "end_time": row["end_time"],
            "participants": participants_by_giveaway.get(row["message_id"], []),
            "ended": bool(row["ended"]),
        }

    return result


def save_giveaways(data: dict) -> None:
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM giveaways")  # cascades to giveaway_participants
        for message_id_str, g in data.items():
            cur.execute(
                """INSERT INTO giveaways
                       (message_id, guild_id, channel_id, host_id, name, prize,
                        winners_count, end_time, ended)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(message_id_str),
                    g["guild_id"],
                    g["channel_id"],
                    g["host_id"],
                    g["name"],
                    g["prize"],
                    g["winners_count"],
                    g["end_time"],
                    int(g.get("ended", False)),
                ),
            )
            participants = g.get("participants", [])
            if participants:
                cur.executemany(
                    "INSERT INTO giveaway_participants (giveaway_message_id, user_id) VALUES (?, ?)",
                    [(int(message_id_str), uid) for uid in participants],
                )


class GiveawayView(disnake.ui.View):
    def __init__(self, cog: "GiveawayCog", message_id: int, guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.message_id = message_id

        button = disnake.ui.Button(
            label=i18n.t("giveaway_cog.button_name", guild_id=guild_id),
            emoji="🎉",
            style=disnake.ButtonStyle.green,
            custom_id=f"{JOIN_PREFIX}{message_id}",
        )
        button.callback = self.button_callback
        self.add_item(button)

    async def button_callback(self, inter: disnake.MessageInteraction):
        await self.cog.handle_join(inter, self.message_id)


class GiveawayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.giveaways: dict = load_giveaways()
        self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        """Restore view after bot restart"""
        for message_id_str, data in self.giveaways.items():
            if data.get("ended"):
                continue
            view = GiveawayView(self, int(message_id_str), data["guild_id"])
            self.bot.add_view(view, message_id=int(message_id_str))

    def _save(self):
        save_giveaways(self.giveaways)

    @staticmethod
    def _build_embed(guild_id: int, message_id: int, data: dict) -> disnake.Embed:
        end_dt = datetime.fromisoformat(data["end_time"])
        end_ts = int(end_dt.timestamp())

        embed = disnake.Embed(
            title=i18n.t("giveaway_cog.giveaway_title", guild_id=guild_id, name=data["name"]),
            description=i18n.t(
                "giveaway_cog.giveaway_description",
                guild_id=guild_id,
                members=str(len(data["participants"])),
                prize=data["prize"],
                winners=str(data["winners_count"]),
                end=f"<t:{end_ts}:R>",
                host=f"<@{data['host_id']}>",
            ),
            color=disnake.Color.purple(),
        )
        embed.set_footer(
            text=i18n.t("giveaway_cog.giveaway_id_footer", guild_id=guild_id, id=str(message_id))
        )
        return embed

    @staticmethod
    def _pick_winners(data: dict) -> list[int]:
        pool = list(data["participants"])
        count = min(data["winners_count"], len(pool))
        if count <= 0:
            return []
        return random.sample(pool, count)

    async def _finish_giveaway(self, message_id_str: str, data: dict, *, rerolled: bool = False):
        guild = self.bot.get_guild(data["guild_id"])
        channel = guild.get_channel(data["channel_id"]) if guild else None

        winners = self._pick_winners(data)

        data["ended"] = True
        self._save()

        if channel is None:
            return

        try:
            message = await channel.fetch_message(int(message_id_str))
        except (disnake.NotFound, disnake.HTTPException):
            message = None

        if message is not None:
            winners_text = (
                ", ".join(f"<@{w}>" for w in winners)
                if winners
                else i18n.t("giveaway_cog.no_winners", guild_id=data["guild_id"])
            )
            finished_embed = disnake.Embed(
                title=i18n.t(
                    "giveaway_cog.giveaway_ended_title", guild_id=data["guild_id"], name=data["name"]
                ),
                description=i18n.t(
                    "giveaway_cog.giveaway_ended_description",
                    guild_id=data["guild_id"],
                    members=str(len(data["participants"])),
                    winners=winners_text,
                ),
                color=disnake.Color.dark_grey(),
            )
            finished_embed.set_footer(
                text=i18n.t(
                    "giveaway_cog.giveaway_id_footer", guild_id=data["guild_id"], id=message_id_str
                )
            )
            try:
                await message.edit(embed=finished_embed, view=None)
            except disnake.HTTPException:
                pass

        key = "giveaway_cog.reroll_announcement" if rerolled else "giveaway_cog.winners_announcement"

        if winners:
            text = i18n.t(
                key,
                guild_id=data["guild_id"],
                winners=", ".join(f"<@{w}>" for w in winners),
                prize=data["prize"],
            )
        else:
            text = i18n.t(
                "giveaway_cog.no_participants", guild_id=data["guild_id"], prize=data["prize"]
            )

        try:
            await channel.send(text)
        except disnake.HTTPException:
            pass

    async def handle_join(self, inter: disnake.MessageInteraction, message_id: int):
        """Button handler"""
        data = self.giveaways.get(str(message_id))
        if data is None or data.get("ended"):
            await inter.response.send_message(
                i18n.t("giveaway_cog.giveaway_ended", locale=inter.guild_id),
                ephemeral=True,
            )
            return

        user_id = inter.author.id
        participants = data["participants"]

        if user_id in participants:
            participants.remove(user_id)
            text_key = "giveaway_cog.button_leave_success"
        else:
            participants.append(user_id)
            text_key = "giveaway_cog.button_success"

        self._save()

        embed = self._build_embed(inter.guild.id, message_id, data)
        await inter.response.edit_message(embed=embed)
        await inter.followup.send(
            i18n.t(text_key, locale=inter.guild_id),
            ephemeral=True,
        )

    @tasks.loop(seconds=CHECK_INTERVAL)
    async def check_giveaways(self):
        now = datetime.now(timezone.utc)
        to_finish = []

        for message_id_str, data in self.giveaways.items():
            if data.get("ended"):
                continue
            end_time = datetime.fromisoformat(data["end_time"])
            if end_time <= now:
                to_finish.append((message_id_str, data))

        for message_id_str, data in to_finish:
            await self._finish_giveaway(message_id_str, data)

    @check_giveaways.before_loop
    async def before_check_giveaways(self):
        await self.bot.wait_until_ready()


    @commands.slash_command(
        name="giveaway",
        description=localized("commands.giveaway.description"),
    )
    async def giveaway(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
        pass

    @giveaway.sub_command(
        name="start",
        description=localized("commands.giveaway_start.description"),
    )
    async def giveaway_start(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(
            name=localized("commands.giveaway_start.param_name_name"),
            description=localized("commands.giveaway_start.param_name"),
        ),
        duration: str = commands.Param(
            name=localized("commands.giveaway_start.param_duration_name"),
            description=localized("commands.giveaway_start.param_duration"),
        ),
        winners: int = commands.Param(
            default=1,
            ge=1,
            name=localized("commands.giveaway_start.param_winners_name"),
            description=localized("commands.giveaway_start.param_winners"),
        ),
        prize: str = commands.Param(
            default="—",
            name=localized("commands.giveaway_start.param_prize_name"),
            description=localized("commands.giveaway_start.param_prize"),
        ),
        channel: disnake.TextChannel = commands.Param(
            default=None,
            name=localized("commands.giveaway_start.param_channel_name"),
            description=localized("commands.giveaway_start.param_channel"),
        ),
    ):
        target_channel = channel or inter.channel

        if await validate_permissions(inter, [{Permission.Giveaways: True}, {disnake.Permissions(administrator=True): True}], channel=target_channel):
            return None

        if not duration.strip().lower():
            return await inter.response.send_message(
                i18n.t("giveaway_cog.duration_must_be_finite", locale=inter.guild_id),
                ephemeral=True,
            )

        seconds, error = parse_duration_seconds(duration, locale=inter.guild_id)
        if error is not None or not seconds or seconds <= 0:
            return await inter.response.send_message(
                i18n.t(
                    "giveaway_cog.invalid_duration",
                    locale=inter.guild_id,
                    error=error or "",
                ),
                ephemeral=True,
            )

        delta = timedelta(seconds=seconds)

        end_time = datetime.now(timezone.utc) + delta

        data = {
            "guild_id": inter.guild.id,
            "channel_id": target_channel.id,
            "host_id": inter.author.id,
            "name": name,
            "prize": prize,
            "winners_count": winners,
            "end_time": end_time.isoformat(),
            "participants": [],
            "ended": False,
        }

        await inter.response.send_message(
            i18n.t(
                "giveaway_cog.giveaway_created",
                locale=inter.guild_id,
                channel=target_channel.mention,
            ),
            ephemeral=True,
        )

        placeholder_embed = self._build_embed(inter.guild.id, 0, data)
        message = await target_channel.send(embed=placeholder_embed)

        view = GiveawayView(self, message.id, inter.guild.id)
        await message.edit(embed=self._build_embed(inter.guild.id, message.id, data), view=view)
        self.bot.add_view(view, message_id=message.id)

        self.giveaways[str(message.id)] = data
        self._save()

        return None

    @giveaway.sub_command(
        name="end",
        description=localized("commands.giveaway_end.description"),
    )
    async def giveaway_end(
        self,
        inter: disnake.ApplicationCommandInteraction,
        message_id: str = commands.Param(
            name=localized("commands.giveaway_end.param_message_id_name"),
            description=localized("commands.giveaway_end.param_message_id"),
        ),
    ):
        if await validate_permissions(inter, [{Permission.Giveaways: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        data = self.giveaways.get(message_id)
        if data is None or data["guild_id"] != inter.guild.id:
            return await inter.response.send_message(
                i18n.t("giveaway_cog.not_found", locale=inter.guild_id),
                ephemeral=True,
            )
        if data.get("ended"):
            return await inter.response.send_message(
                i18n.t("giveaway_cog.already_ended", locale=inter.guild_id),
                ephemeral=True,
            )

        await inter.response.send_message(
            i18n.t("giveaway_cog.ending", locale=inter.guild_id),
            ephemeral=True,
        )
        return await self._finish_giveaway(message_id, data)

    @giveaway.sub_command(
        name="reroll",
        description=localized("commands.giveaway_reroll.description"),
    )
    async def giveaway_reroll(
        self,
        inter: disnake.ApplicationCommandInteraction,
        message_id: str = commands.Param(
            name=localized("commands.giveaway_reroll.param_message_id_name"),
            description=localized("commands.giveaway_reroll.param_message_id"),
        ),
    ):
        if await validate_permissions(inter, [{Permission.Giveaways: True}, {disnake.Permissions(administrator=True): True}], channel=target_channel):
            return None

        data = self.giveaways.get(message_id)
        if data is None or data["guild_id"] != inter.guild.id:
            return await inter.response.send_message(
                i18n.t("giveaway_cog.not_found", locale=inter.guild_id),
                ephemeral=True,
            )
        if not data.get("ended"):
            return await inter.response.send_message(
                i18n.t("giveaway_cog.not_ended_yet", locale=inter.guild_id),
                ephemeral=True,
            )

        await inter.response.send_message(
            i18n.t("giveaway_cog.rerolling", locale=inter.guild_id),
            ephemeral=True,
        )
        return await self._finish_giveaway(message_id, data, rerolled=True)

    @giveaway.sub_command(
        name="list",
        description=localized("commands.giveaway_list.description"),
    )
    async def giveaway_list(self, inter: disnake.ApplicationCommandInteraction):
        if await validate_permissions(inter, [{Permission.Giveaways: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        active = [
            (mid, data)
            for mid, data in self.giveaways.items()
            if data["guild_id"] == inter.guild.id and not data.get("ended")
        ]

        if not active:
            return await inter.response.send_message(
                i18n.t("giveaway_cog.no_active", locale=inter.guild_id),
                ephemeral=True,
            )

        lines = []
        for message_id_str, data in active:
            end_ts = int(datetime.fromisoformat(data["end_time"]).timestamp())
            lines.append(
                i18n.t(
                    "giveaway_cog.list_entry",
                    locale=inter.guild_id,
                    name=data["name"],
                    id=message_id_str,
                    end=f"<t:{end_ts}:R>",
                )
            )

        embed = disnake.Embed(
            title=i18n.t("giveaway_cog.list_title", locale=inter.guild_id),
            description="\n".join(lines),
            color=disnake.Color.purple(),
        )
        return await inter.response.send_message(embed=embed, ephemeral=True)


def setup(bot: commands.Bot):
    load_giveaways()
    bot.add_cog(GiveawayCog(bot))