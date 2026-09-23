import disnake
from disnake.ext import commands

import i18n
from i18n import LocaleObject

from permissions import Permission, validate_permissions
from discord_i18n import localized, yes_no_choices, bool_to_yes_no_str

from db import db_cursor

from enum import IntEnum

BUTTON_LABEL_KEYS = {
    "send_dm_deny" : "send_dm_cmd.view.deny",
    "send_dm_allow" : "send_dm_cmd.view.allow",
}

BAN_WORDS = ["@everyone", "@here"]

class DmOptOutStatus(IntEnum):
    Waiting = 0,
    Denied = 1,
    Allowed = 2,

# user_id -> 0/1/2
dm_opt_out: dict[int, DmOptOutStatus] = {}

# channel_id -> {"guild_id", "content", "message_id", "created_by"}
sticky_messages: dict[int, dict] = {}


def load_dm_opt_out() -> dict[int, DmOptOutStatus]:
    with db_cursor() as cur:
        cur.execute("SELECT user_id, allow_dm FROM dm_opt_out")
        rows = cur.fetchall()
    return {row["user_id"]: row["allow_dm"] for row in rows}


def save_dm_opt_out(records: dict[int, DmOptOutStatus]):
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM dm_opt_out")
        cur.executemany(
            "INSERT INTO dm_opt_out (user_id, allow_dm) VALUES (?, ?)",
            [(int(user_id), int(allow_dm)) for user_id, allow_dm in records.items()],
        )

def set_dm_opt_out(user_id: int, status: DmOptOutStatus) -> None:
    dm_opt_out[user_id] = status
    save_dm_opt_out(dm_opt_out)


def get_dm_opt_out(user_id: int) -> DmOptOutStatus:
    return dm_opt_out.get(user_id, DmOptOutStatus.Waiting)


# ------------------------------------------------------------ sticky storage


def load_sticky_messages() -> dict[int, dict]:
    with db_cursor() as cur:
        cur.execute("SELECT channel_id, guild_id, content, message_id, created_by FROM sticky_messages")
        rows = cur.fetchall()
    return {
        row["channel_id"]: {
            "guild_id": row["guild_id"],
            "content": row["content"],
            "message_id": row["message_id"],
            "created_by": row["created_by"],
        }
        for row in rows
    }


def save_sticky_messages(records: dict[int, dict]):
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM sticky_messages")
        cur.executemany(
            "INSERT INTO sticky_messages (channel_id, guild_id, content, message_id, created_by) VALUES (?, ?, ?, ?, ?)",
            [
                (channel_id, r["guild_id"], r["content"], r["message_id"], r["created_by"])
                for channel_id, r in records.items()
            ],
        )


def set_sticky_message(channel_id: int, guild_id: int, content: str, message_id: int | None, created_by: int) -> None:
    sticky_messages[channel_id] = {
        "guild_id": guild_id,
        "content": content,
        "message_id": message_id,
        "created_by": created_by,
    }
    save_sticky_messages(sticky_messages)


def update_sticky_message_id(channel_id: int, message_id: int | None) -> None:
    if channel_id not in sticky_messages:
        return
    sticky_messages[channel_id]["message_id"] = message_id
    save_sticky_messages(sticky_messages)


def remove_sticky_message(channel_id: int) -> bool:
    if channel_id not in sticky_messages:
        return False
    del sticky_messages[channel_id]
    save_sticky_messages(sticky_messages)
    return True


def get_sticky_message(channel_id: int) -> dict | None:
    return sticky_messages.get(channel_id, None)


# noinspection unused-parameter
class DenySendDmView(disnake.ui.View):

    def __init__(self, cog: "SendCog", guild_id: int | None = None):
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
        label="Запретить несистемные ЛС", emoji="❌",
        style=disnake.ButtonStyle.danger, custom_id="send_dm_deny",
    )
    async def deny_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        set_dm_opt_out(inter.user.id, DmOptOutStatus.Denied)
        await inter.response.send_message(i18n.t("send_dm_cmd.denied", locale=inter), ephemeral=True)

    @disnake.ui.button(
        label="Разрешить несистемные ЛС", emoji="✅",
        style=disnake.ButtonStyle.success, custom_id="send_dm_allow",
    )
    async def allow_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        set_dm_opt_out(inter.user.id, DmOptOutStatus.Allowed)
        await inter.response.send_message(i18n.t("send_dm_cmd.allowed", locale=inter), ephemeral=True)


class SendCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._view_registered = False

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._view_registered:
            self.bot.add_view(DenySendDmView(self))
            self._view_registered = True

    @staticmethod
    def format_sticky(message: str, locale: LocaleObject):
        title = "***__" + i18n.t("send_sticky_cmd.sticky_message", locale=locale) + "__***"
        footer = i18n.t('send_message_cmd.warning', locale=locale)
        return f"{title}\n\n{message}\n\n{footer}"


    # ------------------------------------------------------- sticky repost

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if message.author.bot or not message.guild:
            return

        record = get_sticky_message(message.channel.id)
        if record is None:
            return

        if record["message_id"]:
            try:
                old = await message.channel.fetch_message(record["message_id"])
                await old.delete()
            except (disnake.NotFound, disnake.Forbidden):
                pass
            except disnake.HTTPException as e:
                raise RuntimeError(f"An HTTP error detected while reposting (deleting old) sticky message: {e}")

        try:
            new_message = await message.channel.send(self.format_sticky(record["content"], locale=message.guild.id))
            update_sticky_message_id(message.channel.id, new_message.id)
        except (disnake.NotFound, disnake.Forbidden):
            pass
        except disnake.HTTPException as e:
            raise RuntimeError(f"An HTTP error detected while reposting (sending new) sticky message: {e}")

    @commands.slash_command(
        name="send",
        description=localized("commands.send.description"),
    )
    async def send(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
        pass

    @send.sub_command(
        name="message",
        description=localized("commands.send_message.description"),
    )
    async def send_message_command(
            self,
            inter: disnake.ApplicationCommandInteraction,
            message: str = commands.Param(
                name=localized("commands.send_message.param_message_name"),
                description=localized("commands.send_message.param_message"),
            ),
            message_id_to_reply: str | None = commands.Param(
                default=None,
                name=localized("commands.send_message.param_reply_to_name"),
                description=localized("commands.send_message.param_reply_to"),
            ),
            mention_author: bool = commands.Param(
                default=False,
                name=localized("commands.send_message.param_mention_author_name"),
                description=localized("commands.send_message.param_mention_author"),
                choices=yes_no_choices()
            ),
            channel: disnake.TextChannel | None = commands.Param(
                default=None,
                name=localized("commands.send_message.param_channel_name"),
                description=localized("commands.send_message.param_channel"),
            ),
    ):
        gid = inter.guild_id
        _channel = channel or inter.channel

        if await validate_permissions(inter, [{Permission.Send: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        _message = f"{message}\n{i18n.t('send_message_cmd.warning', locale=gid)}"
        if message_id_to_reply is not None:
            try:
                int_id = int(message_id_to_reply)
                msg = await _channel.fetch_message(int_id)
                await _channel.send(
                    _message,
                    reference=msg,
                    mention_author=mention_author
                )
            except disnake.NotFound:
                return await inter.response.send_message(
                    i18n.t("send_message_cmd.message_not_found", locale=gid), ephemeral=True
                )
            except disnake.Forbidden:
                return await inter.response.send_message(
                    i18n.t("send_message_cmd.forbidden", locale=gid), ephemeral=True
                )
            except ValueError:
                return await inter.response.send_message(
                    i18n.t("send_message_cmd.invalid_id", locale=gid, value=message_id_to_reply), ephemeral=True
                )
        else:
            try:
                await _channel.send(_message)
            except disnake.Forbidden:
                return await inter.response.send_message(
                    i18n.t("send_message_cmd.forbidden", locale=gid), ephemeral=True
                )

        return await inter.response.send_message(i18n.t("send_message_cmd.sent", locale=gid), ephemeral=True)

    @send.sub_command(
        name="sticky",
        description=localized("commands.send_sticky.description"),
    )
    async def send_sticky_command(
            self,
            inter: disnake.ApplicationCommandInteraction,
            message: str | None = commands.Param(
                default=None,
                name=localized("commands.send_sticky.param_message_name"),
                description=localized("commands.send_sticky.param_message"),
            ),
            channel: disnake.TextChannel | None = commands.Param(
                default=None,
                name=localized("commands.send_sticky.param_channel_name"),
                description=localized("commands.send_sticky.param_channel"),
            )
    ):
        global BAN_WORDS

        gid = inter.guild_id
        target_channel = channel or inter.channel

        if await validate_permissions(inter, [{Permission.Send: True, disnake.Permissions(pin_messages=True): True}, {disnake.Permissions(administrator=True): True}]):
            return None

        if not message:
            existing = get_sticky_message(target_channel.id)
            if existing and existing["message_id"]:
                try:
                    old = await target_channel.fetch_message(existing["message_id"])
                    await old.delete()
                except disnake.NotFound:
                    pass
                except disnake.Forbidden:
                    return await inter.response.send_message(i18n.t("send_sticky_cmd.forbidden", locale=gid), ephemeral=True)
                except disnake.HTTPException as e:
                    raise RuntimeError(f"An HTTP error detected while removing sticky message: {e}")

            removed = remove_sticky_message(target_channel.id)
            key = "send_sticky_cmd.removed" if removed else "send_sticky_cmd.nothing_to_remove"
            return await inter.response.send_message(
                i18n.t(key, locale=gid, channel=target_channel.mention), ephemeral=True
            )

        message_lower = message.lower()
        for word in BAN_WORDS:
            if word in message_lower:
                return await inter.response.send_message(":x:: `@everyone`, `@here`", ephemeral=True)

        existing = get_sticky_message(target_channel.id)
        if existing and existing["message_id"]:
            try:
                old = await target_channel.fetch_message(existing["message_id"])
                await old.delete()
            except disnake.NotFound:
                pass
            except disnake.Forbidden:
                return await inter.response.send_message(i18n.t("send_sticky_cmd.forbidden", locale=gid), ephemeral=True)
            except disnake.HTTPException as e:
                raise RuntimeError(f"An HTTP error detected while sending sticky message: {e}")
        try:
            sent = await target_channel.send(self.format_sticky(message, locale=gid))
        except disnake.Forbidden:
            return await inter.response.send_message(i18n.t("send_sticky_cmd.forbidden", locale=gid), ephemeral=True)

        set_sticky_message(target_channel.id, gid, message, sent.id, inter.author.id)

        return await inter.response.send_message(i18n.t("send_sticky_cmd.set", locale=gid, channel=target_channel.mention), ephemeral=True)

    @send.sub_command(
        name="allow",
        description=localized("commands.send_allow.description")
    )
    async def send_allow(
            self,
            inter: disnake.ApplicationCommandInteraction,
            allow: bool = commands.Param(
                name=localized("commands.send_allow.param_allow_name"),
                description=localized("commands.send_allow.param_allow"),
                choices=yes_no_choices()
            )
    ):
        set_dm_opt_out(inter.author.id, DmOptOutStatus.Allowed if allow else DmOptOutStatus.Denied)
        return await inter.response.send_message(i18n.t(f"send_dm_cmd." + ("allowed" if allow else "denied"), locale=inter), ephemeral=True)


    @send.sub_command(
        name="dm",
        description=localized("commands.send_dm.description"),
    )
    async def send_dm_command(
            self,
            inter: disnake.ApplicationCommandInteraction,
            user: disnake.User = commands.Param(
                name=localized("commands.send_dm.param_user_name"),
                description=localized("commands.send_dm.param_user"),
            ),
            message: str = commands.Param(
                name=localized("commands.send_dm.param_message_name"),
                description=localized("commands.send_dm.param_message"),
            ),
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.Send: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        status = get_dm_opt_out(user.id)

        if status == DmOptOutStatus.Denied:
            return await inter.response.send_message(i18n.t("send_dm_cmd.forbidden", locale=gid), ephemeral=True)

        try:
            text = i18n.t("send_dm_cmd.message_title", locale=gid) + "\n\n" + i18n.t("send_dm_cmd.received_message", locale=gid, user=inter.author.mention, message=message)

            await user.send(text, view=DenySendDmView(self, gid) if status == DmOptOutStatus.Waiting else None)

            await inter.response.send_message(
                i18n.t("send_dm_cmd.sent", locale=gid, user=user.mention),
                ephemeral=True
            )

        except disnake.Forbidden:
            await inter.response.send_message(
                i18n.t("send_dm_cmd.forbidden", locale=gid),
                ephemeral=True
            )

        except Exception as e:
            await inter.response.send_message(
                i18n.t("send_dm_cmd.error", locale=gid, error=e),
                ephemeral=True
            )

def setup(bot):
    global dm_opt_out, sticky_messages
    dm_opt_out = load_dm_opt_out()
    sticky_messages = load_sticky_messages()
    bot.add_cog(SendCog(bot))