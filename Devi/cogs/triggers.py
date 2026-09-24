import random
import re
import time

import disnake
from disnake.ext import commands

import i18n
from permissions import Permission, validate_permissions
from discord_i18n import localized, add_remove_check_choices
from db import db_cursor
from other_apis.topgg_utils import is_voted
from paths import env_var_to_int

RESPONSE_COOLDOWN = env_var_to_int("RESPONSE_COOLDOWN", "3")
RESPONSE_COOLDOWN_VOTED = env_var_to_int("RESPONSE_COOLDOWN_VOTED", "0")


def load_triggers() -> dict[int, dict[str, list[str]]]:
    """Loads triggers from SQLite. Structure: {guild_id: {regex: [responses]}}."""

    with db_cursor() as cur:
        cur.execute("SELECT id, guild_id, pattern FROM triggers ORDER BY guild_id, sort_order, id")
        trigger_rows = cur.fetchall()
        cur.execute(
            "SELECT trigger_id, response FROM trigger_responses ORDER BY trigger_id, sort_order, id"
        )
        response_rows = cur.fetchall()

    responses_by_trigger: dict[int, list[str]] = {}
    for row in response_rows:
        responses_by_trigger.setdefault(row["trigger_id"], []).append(row["response"])

    result: dict[int, dict[str, list[str]]] = {}
    for row in trigger_rows:
        guild_triggers = result.setdefault(row["guild_id"], {})
        guild_triggers[row["pattern"]] = responses_by_trigger.get(row["id"], [])

    if not result:
        print("⚠️ Triggers table is empty!")

    return result


def save_triggers(triggers: dict[int, dict[str, list[str]]]):
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM triggers")  # cascades to trigger_responses
        for guild_id, guild_triggers in triggers.items():
            for order, (pattern, responses) in enumerate(guild_triggers.items()):
                cur.execute(
                    "INSERT INTO triggers (guild_id, pattern, sort_order) VALUES (%s, %s, %s) RETURNING id",
                    (guild_id, pattern, order),
                )
                trigger_id = cur.fetchone()["id"]
                if responses:
                    cur.executemany(
                        "INSERT INTO trigger_responses (trigger_id, response, sort_order) VALUES (%s, %s, %s)",
                        [(trigger_id, response, i) for i, response in enumerate(responses)],
                    )


class TriggersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.triggers: dict[int, dict[str, list[str]]] = load_triggers()
        self.last_response_time: dict[int, float] = {}

    def reload_triggers_from_disk(self) -> int:
        self.triggers = load_triggers()

        return sum(
            len(guild_triggers)
            for guild_triggers in self.triggers.values()
        )

    def get_guild_triggers(self, guild_id: int) -> dict[str, list[str]]:
        return self.triggers.setdefault(guild_id, {})

    @staticmethod
    def _get_trigger_by_id(guild_triggers: dict[str, list[str]], trigger_id: int) -> str | None:
        """Returns a trigger by its numeric ID."""
        if trigger_id < 0 or trigger_id >= len(guild_triggers):
            return None

        return list(guild_triggers.keys())[trigger_id]

    @staticmethod
    def _find_trigger_key(guild_triggers: dict[str, list[str]], trigger: str) -> str | None:
        """Finds an existing trigger case-insensitively. This preserves compatibility with old triggers that were stored in lowercase."""
        trigger_lower = trigger.lower()

        for existing_trigger in guild_triggers:
            if existing_trigger.lower() == trigger_lower:
                return existing_trigger

        return None

    @staticmethod
    def _normalize_message(content: str) -> str:
        """Normalizes Discord mentions without changing regex content."""
        return re.sub(r'<@!(\d+)>', r'<@\1>', content.strip())

    @staticmethod
    def _validate_regex(pattern: str, guild_id: int) -> str | None:
        """Validates a regex pattern. Returns an error message or None if the pattern is valid."""
        if not pattern:
            return i18n.t("triggers_cog.empty_regex", guild_id=guild_id)

        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return str(e)

        return None

    @staticmethod
    def _regex_matches(pattern: str, text: str) -> bool:
        """Checks whether a trigger regex matches the message."""
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            return False # Invalid regex should normally be impossible because validation happens when adding a trigger.

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if message.author.bot:
            return None

        if message.guild is None:
            return await self.bot.process_commands(message)

        guild_id = message.guild.id
        guild_triggers = self.triggers.get(guild_id, {})

        msg_text = self._normalize_message(message.content)

        now = time.time()
        last_time = self.last_response_time.get(guild_id, 0)

        for trigger, responses in guild_triggers.items():

            if not self._regex_matches(trigger, msg_text):
                continue

            response_cooldown = RESPONSE_COOLDOWN_VOTED if await is_voted(message.author.id) else RESPONSE_COOLDOWN

            if now - last_time >= response_cooldown:
                try:
                    response = random.choice(responses)

                    await message.reply(response)

                    self.last_response_time[guild_id] = now

                    print(f"💬 Trigger LOG [{trigger}] | {message.author} on guild {guild_id}")

                except Exception as e:
                    print(f"❌ Trigger error: {e}")

            break

        return await self.bot.process_commands(message)

    @commands.slash_command(
        name="trigger",
        description=localized("commands.trigger.description"),
    )
    async def trigger(
            self,
            inter: disnake.ApplicationCommandInteraction,
            trigger: str = commands.Param(
                name=localized("commands.trigger.param_trigger_name"),
                description=localized("commands.trigger.param_trigger"),
            ),
            action: str = commands.Param(
                name=localized("commands.trigger.param_action_name"),
                description=localized("commands.trigger.param_action"),
                choices=add_remove_check_choices()
            ),
            response: str = commands.Param(
                default="",
                name=localized("commands.trigger.param_response_name"),
                description=localized("commands.trigger.param_response"),
            ),
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.ManageTriggers: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        trigger_clean = trigger.strip()
        response_clean = response.strip() if response else None

        guild_triggers = self.get_guild_triggers(gid)
        existing_trigger = self._find_trigger_key(guild_triggers, trigger_clean)

        match action:
            case "add":
                if not response_clean:
                    return await inter.response.send_message(
                        i18n.t(
                            "triggers_cog.missing_response",
                            locale=gid
                        ),
                        ephemeral=True
                    )

                try:
                    trigger_id = int(trigger_clean)
                except ValueError:
                    trigger_id = None

                if trigger_id is not None:
                    existing_trigger = self._get_trigger_by_id(
                        guild_triggers,
                        trigger_id
                    )

                    if existing_trigger is None:
                        return await inter.response.send_message(
                            i18n.t(
                                "triggers_cog.not_found",
                                locale=gid,
                                trigger=trigger_clean
                            ),
                            ephemeral=True
                        )

                else:
                    existing_trigger = self._find_trigger_key(guild_triggers, trigger_clean)

                if existing_trigger is not None:
                    if response_clean in guild_triggers[existing_trigger]:
                        return await inter.response.send_message(
                            i18n.t("triggers_cog.already_exists_response", locale=gid, trigger=existing_trigger, response=response_clean),
                            ephemeral=True
                        )

                    guild_triggers[existing_trigger].append(response_clean)

                else:
                    regex_error = self._validate_regex(trigger_clean, gid)

                    if regex_error:
                        return await inter.response.send_message(
                            i18n.t("triggers_cog.invalid_regex", locale=gid, error=regex_error),
                            ephemeral=True
                        )

                    guild_triggers[trigger_clean] = [response_clean]

                save_triggers(self.triggers)

                return await inter.response.send_message(
                    i18n.t("triggers_cog.saved", locale=gid, trigger=existing_trigger if existing_trigger is not None else trigger_clean),
                    ephemeral=True
                )

            case "check":
                if existing_trigger is None:
                    return await inter.response.send_message(
                        i18n.t("triggers_cog.not_found", locale=gid, trigger=trigger_clean),
                        ephemeral=True
                    )

                responses = guild_triggers[existing_trigger]

                if response_clean:
                    if response_clean in responses:
                        return await inter.response.send_message(
                            i18n.t("triggers_cog.found_response", locale=gid, trigger=existing_trigger, response=response_clean, id=str(responses.index(response_clean))),
                            ephemeral=True
                        )

                    return await inter.response.send_message(
                        i18n.t("triggers_cog.not_found_response", locale=gid, trigger=existing_trigger, response=response_clean),
                        ephemeral=True
                    )

                all_responses = i18n.t("triggers_cog.all_responses", locale=gid, trigger=existing_trigger)

                for i, _response in enumerate(responses):
                    all_responses += f"\n**{i}:** {_response}"

                return await inter.response.send_message(all_responses, ephemeral=True)

            case "remove":
                trigger_id = None

                try:
                    trigger_id = int(trigger_clean)
                except ValueError:
                    pass

                if trigger_id is not None:
                    existing_trigger = self._get_trigger_by_id(guild_triggers, trigger_id)

                    if existing_trigger is None:
                        return await inter.response.send_message(
                            i18n.t("triggers_cog.not_found", locale=gid, trigger=trigger_clean),
                            ephemeral=True
                        )

                else:
                    existing_trigger = self._find_trigger_key(guild_triggers, trigger_clean)

                    if existing_trigger is None:
                        return await inter.response.send_message(
                            i18n.t(
                                "triggers_cog.not_found",
                                locale=gid,
                                trigger=trigger_clean
                            ),
                            ephemeral=True
                        )

                responses = guild_triggers[existing_trigger]

                if response_clean:
                    try:
                        # Try to interpret response as response ID
                        response_id = int(response_clean)

                    except ValueError:
                        response_id = None

                    if response_id is not None:
                        if not 0 <= response_id < len(responses):
                            return await inter.response.send_message(
                                i18n.t("triggers_cog.not_found_response", locale=gid, trigger=existing_trigger, response=response_clean),
                                ephemeral=True
                            )

                        del responses[response_id]

                    else:
                        if response_clean not in responses:
                            return await inter.response.send_message(
                                i18n.t("triggers_cog.not_found_response", locale=gid, trigger=existing_trigger, response=response_clean),
                                ephemeral=True
                            )

                        responses.remove(response_clean)

                    if not responses:
                        del guild_triggers[existing_trigger]

                else:
                    del guild_triggers[existing_trigger]

                save_triggers(self.triggers)

                return await inter.response.send_message(
                    i18n.t("triggers_cog.removed", locale=gid, trigger=existing_trigger),
                    ephemeral=True
                )

        return None

    @commands.slash_command(
        name="triggers",
        description=localized("commands.triggers.description"),
    )
    async def trigger_list(self, inter: disnake.ApplicationCommandInteraction):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.ManageTriggers: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        guild_triggers = self.triggers.get(gid, {})

        if not guild_triggers:
            return await inter.response.send_message(
                i18n.t("triggers_cog.no_guild_triggers", locale=gid),
                ephemeral=True
            )

        lines = []

        for trigger_id, (trigger, responses) in enumerate(guild_triggers.items()):
            lines.append(f"**{trigger_id}.** `{trigger}`")

            for response_id, response in enumerate(responses):
                lines.append(f"> **{response_id}:** {response}")

            lines.append("")

        # Discord message limit
        chunks = []
        current_chunk = ""

        for line in lines:
            if len(current_chunk) + len(line) + 1 > 2000:
                if current_chunk:
                    chunks.append(current_chunk)

                current_chunk = line
            else:
                current_chunk += ("\n" if current_chunk else "") + line

        if current_chunk:
            chunks.append(current_chunk)

        await inter.response.send_message(chunks[0], ephemeral=True)

        for chunk in chunks[1:]:
            await inter.followup.send(chunk, ephemeral=True)

        return None


def setup(bot: commands.Bot):
    bot.add_cog(TriggersCog(bot))