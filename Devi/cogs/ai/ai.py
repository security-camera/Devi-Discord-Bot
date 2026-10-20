"""AI cog: @mention chat, /ai ask text|voice, /ai clear.

One shared API key (AI_API_KEY in config/.env; ProxyAPI, DeepSeek or Qwen) serves every server and user.
Because of that every paid request goes through AiCog._gate() first: cooldown, daily limits per user
and per server, and a global daily budget. See ai_client.py (provider), ai_limits.py (quotas)
and ai_memory.py (history) for the details.
"""

import asyncio
import base64
import math
import time
from dataclasses import dataclass
from enum import IntEnum

import aiohttp
import disnake
from disnake.ext import commands

import i18n
from logs import send_log, LogColor
from permissions import Permission, has_permissions, DEVELOPER_LIST, require_permissions
from discord_i18n import localized, yes_no_choices, add_remove_check_choices
from cogs.voice import MAX_TTS_LENGTH, MAX_TTS_LENGTH_VOTED
from cogs.ai import ai_limits
from cogs.ai.ai_client import AiClient, AiRequestError, build_user_message, create_client, money
from cogs.ai.ai_memory import (
    add_exchange, clear_memory, load_history, purge_stored_images, IMAGE_PLACEHOLDER,
    MAX_MESSAGES, MAX_MESSAGES_VOTED, MAX_HISTORY_CHARS, MAX_HISTORY_CHARS_VOTED,
)
from cogs.ai.ai_prompts import (
    SYSTEM_PROMPT, VOICE_SYSTEM_PROMPT, NO_LIMIT_SYSTEM_PROMPT, NO_LIMIT_VOICE_SYSTEM_PROMPT,
    get_user_instruction, set_user_instruction, delete_user_instruction, load_instructions, PRONOUNS
)

from storage import AI_SIGNAL_CHANNEL, TECHNICAL_SUPPORT_SERVER
from other_apis.topgg_utils import is_voted
from paths import env_int

COOLDOWN_SECONDS = env_int("AI_COOLDOWN_SECONDS", 30)
COOLDOWN_SECONDS_VOTED = env_int("AI_COOLDOWN_SECONDS_VOTED", 10)

DISCORD_MESSAGE_LIMIT = 2000
MAX_INPUT_CHARS = env_int("AI_MAX_INPUT_CHARS", 2000)  # longer prompts are rejected before any paid call
MAX_OUTPUT_TOKENS = env_int("AI_MAX_OUTPUT_TOKENS", 600)
VOICE_MAX_TOKENS = env_int("AI_VOICE_MAX_TOKENS", 350)
MAX_ATTACHMENTS = env_int("AI_MAX_IMAGES", 2)
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB per image, sane upper bound for inline base64 upload
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}  # accepted by both DeepSeek and Qwen

# top.gg is only used to find out who voted, so it must never slow down or break the AI.
VOTE_CHECK_TIMEOUT_SECONDS = 1.5
VOTE_CACHE_SECONDS = 120
VOTE_CACHE_FAILURE_SECONDS = 30


class PromptType(IntEnum):
    NONE = 0

    TEXT = 1
    VOICE = 2


PROMPTS_BY_TYPE: dict[PromptType, list[str]] = {
    PromptType.TEXT: [SYSTEM_PROMPT, NO_LIMIT_SYSTEM_PROMPT],
    PromptType.VOICE: [VOICE_SYSTEM_PROMPT, NO_LIMIT_VOICE_SYSTEM_PROMPT],
}

DEBUG_MODE = False

class SignalException(Exception):
    PREFIX = "An error detected while sending a suspicious AI prompt signal: "

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.PREFIX + message)


@dataclass
class Gate:
    """Result of AiCog._gate(): either the request may go to the provider, or `message` explains why not."""
    ok: bool
    message: str = ""
    voted: bool = False
    max_messages: int = MAX_MESSAGES
    max_chars: int = MAX_HISTORY_CHARS


class AiCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client: AiClient = create_client()
        self.cooldowns: dict[int, float] = {}
        self._vote_cache: dict[int, tuple[bool, float]] = {}

        if not self.client.configured:
            print("⚠️ AI_API_KEY is not set - AI answers are disabled until it is added to config/.env")

    def cog_unload(self):
        try:
            asyncio.get_running_loop().create_task(self.client.close())
        except RuntimeError:
            pass  # no running loop (shutdown): the session dies with the process

    async def _is_voted(self, user_id: int) -> bool:
        """top.gg vote status with a short cache (it used to be asked 3-4 times per message)."""
        now = time.monotonic()
        cached = self._vote_cache.get(user_id)
        if cached and cached[1] > now:
            return cached[0]

        try:
            voted = bool(await asyncio.wait_for(is_voted(user_id), VOTE_CHECK_TIMEOUT_SECONDS))
            ttl = VOTE_CACHE_SECONDS
        except Exception as e:
            print(f"❌ top.gg vote check failed: {e!r}")
            voted, ttl = False, VOTE_CACHE_FAILURE_SECONDS

        if len(self._vote_cache) > 5000:
            self._vote_cache = {key: value for key, value in self._vote_cache.items() if value[1] > now}
        self._vote_cache[user_id] = (voted, now + ttl)
        return voted

    @staticmethod
    def _vote_ad(voted: bool, locale) -> str:
        return "" if voted else "\n\n" + i18n.t("top_gg_cog.voting_ad", locale=locale)

    def _remember_request(self, user_id: int, now: float):
        if len(self.cooldowns) > 5000:
            longest = max(COOLDOWN_SECONDS, COOLDOWN_SECONDS_VOTED)
            self.cooldowns = {key: value for key, value in self.cooldowns.items() if now - value < longest}
        self.cooldowns[user_id] = now

    async def _gate(self, user_id: int, guild_id: int) -> Gate:
        """Everything that has to pass before a paid request is made: the provider is configured and not
        paused, the user's cooldown, the daily limits of the user and of the server, and the global daily
        budget. Saying no here costs nothing."""
        locale = guild_id

        if not self.client.configured:
            return Gate(False, i18n.t("ai_cog.not_configured", locale=locale))

        if self.client.blocked_for():
            return Gate(False, i18n.t("ai_cog.generic_error", locale=locale))

        voted = await self._is_voted(user_id)
        ad = self._vote_ad(voted, locale)

        now = time.time()
        cooldown = COOLDOWN_SECONDS_VOTED if voted else COOLDOWN_SECONDS
        remaining = cooldown - (now - self.cooldowns.get(user_id, 0.0))
        if remaining > 0:
            return Gate(False, i18n.t("ai_cog.cooldown", locale=locale, seconds=math.ceil(remaining)) + ad)

        usage = ai_limits.get_usage(user_id, guild_id)

        if ai_limits.budget_exhausted(usage.cost_micro):
            return Gate(False, i18n.t("ai_cog.budget_limit", locale=locale))

        if user_id not in DEVELOPER_LIST:
            hours = ai_limits.hours_until_reset()

            user_limit = ai_limits.USER_DAILY_REQUESTS_VOTED if voted else ai_limits.USER_DAILY_REQUESTS
            if 0 < user_limit <= usage.user_requests:
                return Gate(False, i18n.t("ai_cog.user_limit", locale=locale, limit=user_limit, hours=hours) + ad)

            guild_limit = ai_limits.GUILD_DAILY_REQUESTS
            if 0 < guild_limit <= usage.guild_requests:
                return Gate(False, i18n.t("ai_cog.guild_limit", locale=locale, limit=guild_limit, hours=hours))

        self._remember_request(user_id, now)
        return Gate(
            True,
            voted=voted,
            max_messages=MAX_MESSAGES_VOTED if voted else MAX_MESSAGES,
            max_chars=MAX_HISTORY_CHARS_VOTED if voted else MAX_HISTORY_CHARS,
        )

    def format_system_prompt(self, prompt_type: PromptType, user: int):
        prompt = PROMPTS_BY_TYPE[prompt_type][int(DEBUG_MODE if user in DEVELOPER_LIST else 0)].format(bot_name=self.bot.user.name, pronouns=PRONOUNS)

        custom = get_user_instruction(user) or "This user doesn't have any custom instructions."

        return prompt + custom

    @staticmethod
    async def _download_image(attachment: disnake.Attachment) -> dict | None:
        """Downloads a Discord attachment and turns it into {"mime_type", "data" (base64)} if it's a supported image."""
        mime_type = (attachment.content_type or "").split(";")[0].strip().lower()
        if mime_type not in ALLOWED_IMAGE_TYPES:
            return None
        if attachment.size > MAX_IMAGE_BYTES:
            return None

        raw = await attachment.read()
        return {
            "mime_type": mime_type,
            "data": base64.b64encode(raw).decode("utf-8"),
        }

    @staticmethod
    async def _collect_images(attachments: list[disnake.Attachment]) -> list[dict]:
        images = []
        for attachment in attachments[:MAX_ATTACHMENTS]:
            image = await AiCog._download_image(attachment)
            if image:
                images.append(image)
        return images

    async def _send_signal(
            self,
            user: disnake.Member | disnake.User,
            prompt: str,
            answer: str,
            channel: disnake.abc.GuildChannel | None = None,
            attachments: list[disnake.Attachment] | None = None,
    ):
        guild = self.bot.get_guild(TECHNICAL_SUPPORT_SERVER)

        if guild is None:
            raise SignalException("Bot isn't added to the technical support server")

        try:
            attachment_text = (
                    "\n".join(
                        f"{attachment.filename}: {attachment.url}"
                        for attachment in (attachments or [])
                    )
                    or i18n.t("logs_cog.common.dash", locale=TECHNICAL_SUPPORT_SERVER)
            )

            return await send_log(
                TECHNICAL_SUPPORT_SERVER,
                i18n.t("ai_cog.signal_title", locale=TECHNICAL_SUPPORT_SERVER),
                color=LogColor.Moderation,
                fields=[
                    (
                        i18n.t("ai_cog.signal_member", locale=TECHNICAL_SUPPORT_SERVER),
                        f"{user.mention} (`{user.id}`)"
                    ),
                    (
                        i18n.t("ai_cog.signal_guild", locale=TECHNICAL_SUPPORT_SERVER),
                        f"**{channel.guild.name} ({channel.guild.id})**"
                        if channel and channel.guild
                        else i18n.t("logs_cog.common.dash", locale=TECHNICAL_SUPPORT_SERVER)
                    ),
                    (
                        i18n.t("ai_cog.signal_channel", locale=TECHNICAL_SUPPORT_SERVER),
                        channel.mention
                        if channel and hasattr(channel, "mention")
                        else i18n.t("logs_cog.common.dash", locale=TECHNICAL_SUPPORT_SERVER)
                    ),
                    (
                        i18n.t("ai_cog.signal_prompt", locale=TECHNICAL_SUPPORT_SERVER),
                        prompt[:1024]
                    ),
                    (
                        i18n.t("ai_cog.signal_answer", locale=TECHNICAL_SUPPORT_SERVER),
                        answer[:1024]
                    ),
                    (
                        i18n.t("ai_cog.signal_attachments", locale=TECHNICAL_SUPPORT_SERVER),
                        attachment_text[:1024]
                    ),
                ],
                channel_id=AI_SIGNAL_CHANNEL
            )

        except disnake.NotFound:
            raise SignalException("AI_SIGNAL_CHANNEL not found")

        except disnake.Forbidden:
            raise SignalException("Forbidden")

        except disnake.HTTPException as e:
            raise SignalException(f"HTTPException: {e}")

    def _error_message(self, error: Exception, locale) -> str:
        if isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError)):
            print(f"❌ AI network error: {error!r}")
            return i18n.t("ai_cog.network_error", locale=locale)

        print(f"❌ AI error: {error!r}")
        return i18n.t("ai_cog.generic_error", locale=locale)

    async def _generate_reply(
            self,
            *,
            guild_id: int,
            user: disnake.Member | disnake.User,
            prompt: str,
            images: list[dict],
            prompt_type: PromptType,
            max_tokens: int,
            gate: Gate,
            channel: disnake.abc.GuildChannel | None = None,
            attachments: list[disnake.Attachment] | None = None,
    ) -> str:
        """One paid request: history + prompt -> provider -> usage accounting -> memory. Returns the text to send."""
        messages = [
            {"role": "system", "content": self.format_system_prompt(prompt_type, user.id)},
            *load_history(guild_id, user.id),
            build_user_message(prompt, images, self.client.provider.image_detail),
        ]

        result = await self.client.chat(messages, has_images=bool(images), max_tokens=max_tokens)

        print(
            f"AI [{result.model}] in={result.prompt_tokens} (cached {result.cached_tokens}) "
            f"out={result.completion_tokens}"
            f"{f' (reasoning {result.reasoning_tokens})' if result.reasoning_tokens else ''} "
            f"~{money(result.cost_micro, ai_limits.CURRENCY)}"
        )

        try:
            ai_limits.record_usage(
                guild_id, user.id,
                prompt_tokens=result.prompt_tokens,
                cached_tokens=result.cached_tokens,
                completion_tokens=result.completion_tokens,
                cost_micro=result.cost_micro,
            )
        except Exception as e:  # accounting problems must not eat an answer that was already paid for
            print(f"❌ Couldn't record AI usage: {e!r}")

        answer = result.text
        if not answer:
            raise AiRequestError(f"Empty answer (finish_reason={result.finish_reason})")

        flagged = answer.endswith("-flag")
        print(f"AI answer flag: {flagged}")

        if flagged:
            answer = answer.removesuffix("-flag").rstrip()

            try:
                await self._send_signal(
                    user=user,
                    prompt=prompt,
                    answer=answer,
                    channel=channel,
                    attachments=attachments,
                )
            except SignalException as e:
                print(f"❌ {e}")
            except Exception as e:
                print(f"❌ Unexpected signal error: {e}")

        if result.finish_reason == "length":
            answer += "…"  # the answer hit AI_MAX_OUTPUT_TOKENS and was cut off
        if len(answer) > DISCORD_MESSAGE_LIMIT:
            answer = answer[:DISCORD_MESSAGE_LIMIT - 1] + "…"

        try:
            add_exchange(
                guild_id, user.id,
                " ".join(filter(None, [prompt, *[IMAGE_PLACEHOLDER] * len(images)])),
                answer,
                gate.max_messages, gate.max_chars,
            )
        except Exception as e:
            print(f"❌ Couldn't save AI memory: {e!r}")

        return answer

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if message.author.bot:
            return

        if not message.guild:
            return await self.bot.process_commands(message)

        if self.bot.user not in message.mentions:
            return await self.bot.process_commands(message)

        gid = message.guild.id
        user_id = message.author.id

        if has_permissions(message.author, message.channel, Permission.AiBlackList):
            await message.reply(i18n.t("ai_cog.blacklist", locale=gid))
            return await self.bot.process_commands(message)

        prompt = message.content
        for mention in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
            prompt = prompt.replace(mention, "")
        prompt = prompt.strip()

        if not prompt and not message.attachments:
            await message.reply(i18n.t("ai_cog.empty_prompt", locale=gid))
            return await self.bot.process_commands(message)

        if len(prompt) > MAX_INPUT_CHARS:
            await message.reply(i18n.t("ai_cog.too_long", locale=gid, length=len(prompt), max=MAX_INPUT_CHARS))
            return await self.bot.process_commands(message)

        gate = await self._gate(user_id, gid)
        if not gate.ok:
            await message.reply(gate.message)
            return await self.bot.process_commands(message)

        images = await self._collect_images(message.attachments)
        if not prompt and not images:
            await message.reply(i18n.t("ai_cog.empty_prompt", locale=gid))
            return await self.bot.process_commands(message)

        async with message.channel.typing():
            try:
                answer = await self._generate_reply(
                    guild_id=gid,
                    user=message.author,
                    prompt=prompt,
                    images=images,
                    prompt_type=PromptType.TEXT,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    gate=gate,
                    channel=message.channel,
                    attachments=message.attachments,
                )
            except Exception as e:
                await message.reply(self._error_message(e, gid))
                return await self.bot.process_commands(message)

        await message.reply(content=answer)

        return await self.bot.process_commands(message)

    @commands.slash_command(name="ai")
    async def ai_command(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @ai_command.sub_command(
        name="clear",
        description=localized("commands.ai_clear.description"),
    )
    async def ai_clear(
            self,
            inter: disnake.ApplicationCommandInteraction,
            delete_from_all_servers: bool = commands.Param(
                default=False,
                description=localized("commands.ai_clear.param_delete_from_all_servers"),
                name=localized("commands.ai_clear.param_delete_from_all_servers_name"),
                choices=yes_no_choices()
            )
    ):
        gid = inter.guild_id

        clear_memory(inter.author.id, None if delete_from_all_servers else gid)

        return await inter.response.send_message(
            i18n.t("ai_cog.memory_deleted", locale=gid), ephemeral=True
        )

    @ai_command.sub_command_group(name="ask")
    async def ai_ask_command(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @ai_ask_command.sub_command(
        name="text",
        description=localized("commands.ai_ask_text.description"),
    )
    @require_permissions([{Permission.AiBlackList: False}])
    async def ai_ask_text(
            self,
            inter: disnake.ApplicationCommandInteraction,
            text: str = commands.Param(
                description=localized("commands.ai_ask_text.param_text"),
                name=localized("commands.ai_ask_text.param_text_name")
            ),
            image: disnake.Attachment | None = commands.Param(
                default=None,
                description=localized("commands.ai_ask_text.param_image"),
                name=localized("commands.ai_ask_text.param_image_name")
            ),
            hidden: bool = commands.Param(
                default=False,
                description=localized("commands.ai_ask_text.param_hidden"),
                name=localized("commands.ai_ask_text.param_hidden_name"),
                choices=yes_no_choices()
            )
    ):
        gid = inter.guild_id
        user_id = inter.author.id
        prompt = text.strip()

        if len(prompt) > MAX_INPUT_CHARS:
            return await inter.response.send_message(
                i18n.t("ai_cog.too_long", locale=gid, length=len(prompt), max=MAX_INPUT_CHARS), ephemeral=True
            )

        gate = await self._gate(user_id, gid)
        if not gate.ok:
            return await inter.response.send_message(gate.message, ephemeral=True)

        await inter.response.defer(ephemeral=hidden)

        images = await self._collect_images([image]) if image else []

        try:
            answer = await self._generate_reply(
                guild_id=gid,
                user=inter.author,
                prompt=prompt,
                images=images,
                prompt_type=PromptType.TEXT,
                max_tokens=MAX_OUTPUT_TOKENS,
                gate=gate,
                channel=inter.channel,
                attachments=[image] if image else [],
            )
        except Exception as e:
            return await inter.followup.send(self._error_message(e, gid), ephemeral=hidden)

        return await inter.followup.send(content=answer, ephemeral=hidden)

    @ai_ask_command.sub_command(
        name="voice",
        description=localized("commands.ai_ask_voice.description"),
    )
    @require_permissions([{Permission.AiBlackList: False, Permission.TTS: True}, {Permission.AiBlackList: False, disnake.Permissions(administrator=True): True}])
    async def ai_ask_voice(
            self,
            inter: disnake.ApplicationCommandInteraction,
            text: str = commands.Param(
                description=localized("commands.ai_ask_voice.param_text"),
                name=localized("commands.ai_ask_voice.param_text_name")
            ),
    ):
        gid = inter.guild_id
        user_id = inter.author.id

        vc = inter.guild.voice_client
        if not vc:
            return await inter.response.send_message(i18n.t("voice_cog.not_in_voice", locale=gid), ephemeral=True)

        voice_cog = self.bot.get_cog("VoiceCog")
        if voice_cog is None:
            return await inter.response.send_message(i18n.t("ai_cog.voice_cog_missing", locale=gid), ephemeral=True)

        prompt = text.strip()
        if len(prompt) > MAX_INPUT_CHARS:
            return await inter.response.send_message(
                i18n.t("ai_cog.too_long", locale=gid, length=len(prompt), max=MAX_INPUT_CHARS), ephemeral=True
            )

        gate = await self._gate(user_id, gid)
        if not gate.ok:
            return await inter.response.send_message(gate.message, ephemeral=True)

        await inter.response.defer(ephemeral=False)

        bot_name = inter.guild.me.display_name

        try:
            answer = await self._generate_reply(
                guild_id=gid,
                user=inter.author,
                prompt=prompt,
                images=[],
                prompt_type=PromptType.VOICE,
                max_tokens=VOICE_MAX_TOKENS,
                gate=gate,
                channel=inter.channel,
                attachments=[],
            )
        except Exception as e:
            return await inter.followup.send(self._error_message(e, gid))

        max_length = MAX_TTS_LENGTH_VOTED if gate.voted else MAX_TTS_LENGTH

        spoken_text = answer if len(answer) <= max_length else answer[:max_length - 1] + "…"
        position = voice_cog.enqueue_tts(inter.guild.id, vc, spoken_text)

        return await inter.followup.send(
            i18n.t("ai_cog.ask_response", locale=gid, name=bot_name, text=answer, position=position)
        )

    @ai_command.sub_command(name="usage", description=localized("logs_cog.common.dash"))
    @require_permissions({Permission.Developer: True})
    async def ai_usage(self, inter: disnake.ApplicationCommandInteraction):
        """Today's (UTC) AI spend - the place to look when the provider bill surprises you."""
        await inter.response.defer(ephemeral=True)  # the balance lookup below is a network call

        provider = self.client.provider
        currency = ai_limits.CURRENCY
        summary = ai_limits.summary()

        cost_micro = summary["cost_micro"]
        budget = ai_limits.DAILY_BUDGET
        budget_text = f"{budget:.2f} {currency} ({cost_micro / ai_limits.DAILY_BUDGET_MICRO * 100:.0f}% used)" if budget > 0 else "off"
        cache_rate = f"{summary['cached_tokens'] / summary['prompt_tokens'] * 100:.0f}%" if summary["prompt_tokens"] else "-"
        per_request = money(round(cost_micro / summary["requests"]), currency) if summary["requests"] else "-"

        if not self.client.configured:
            state = "NOT CONFIGURED (AI_API_KEY is empty)"
        elif paused := self.client.blocked_for():
            state = f"PAUSED for {paused}s after: {self.client.last_error}"
        else:
            state = "ok" + (f" (last billing/auth error: {self.client.last_error})" if self.client.last_error else "")

        lines = [
            f"provider: {provider.name} | text: {provider.text_model} | vision: {provider.vision_model}",
            f"endpoint: {provider.base_url}",
            f"state: {state}",
        ]

        balance = await self.client.fetch_balance()
        if balance is not None:
            if "error" in balance:
                lines.append(f"provider balance: n/a ({balance['error']}; enable the balance request permission on the key)")
            elif isinstance(balance.get("budget"), dict):
                lines.append(f"key budget left: {balance.get('balance')} {currency} (limit {balance['budget'].get('limit')}, used {balance['budget'].get('used')})")
            else:
                lines.append(f"account balance: {balance.get('balance')} {currency}")

        lines += [
            "",
            f"today (UTC): {summary['requests']} requests, {summary['users']} users, {summary['guilds']} servers",
            f"tokens: in {summary['prompt_tokens']} (cache hit {cache_rate}), out {summary['completion_tokens']}",
            f"estimated cost: {money(cost_micro, currency)} of {budget_text}, {per_request} per request",
            f"limits: user {ai_limits.USER_DAILY_REQUESTS}/{ai_limits.USER_DAILY_REQUESTS_VOTED} (voted), server {ai_limits.GUILD_DAILY_REQUESTS} per day",
        ]

        # hidden reasoning is billed as output; this line shows whether the "reasoning off" switch really works
        if self.client.calls:
            lines.append(
                f"reasoning off: {self.client.reasoning_switch} - reasoning tokens still seen in "
                f"{self.client.reasoning_calls} of {self.client.calls} calls since start"
            )

        top = ai_limits.top_guilds()
        if top:
            lines += ["", "top servers by cost:"]
            lines += [f"{row['guild_id']}: {row['requests']} requests, {money(row['cost_micro'], currency)}" for row in top]

        return await inter.followup.send("```\n" + "\n".join(lines) + "\n```", ephemeral=True)

    @ai_command.sub_command(name="debug", description=localized("logs_cog.common.dash"))
    @require_permissions({Permission.Developer: True})
    async def ai_debug(
            self,
            inter: disnake.ApplicationCommandInteraction,
            m: int = commands.Param(
                default=1,
                name="mode",
                description=localized("logs_cog.common.dash"),
                choices={
                    "h": 0,
                    "d": 1
                }
            )
    ):

        global DEBUG_MODE

        match m:
            case 0:
                return await inter.response.send_message("h - help\nd - toggle debug (no filters) mode", ephemeral=True)
            case 1:
                DEBUG_MODE = not DEBUG_MODE
                return await inter.response.send_message(f"Debug: {DEBUG_MODE} (works only for devs)", ephemeral=True)
            case _:
                return await inter.response.send_message(f"Bro WTF?", ephemeral=True)

    # @ai_command.sub_command(name="instruction", description=localized("commands.ai_instruction.description"))
    # @require_permissions({Permission.Developer: True})
    # TODO: fix command
    async def ai_instruction(
            self,
            inter: disnake.ApplicationCommandInteraction,
            user_id: str = commands.Param(
                #name=localized("commands.ai_instruction.param_user_id_name"),
                #description=localized("commands.ai_instruction.param_user_id")
            ),
            action: str = commands.Param(
                #name=localized("commands.ai_instruction.param_action_name"),
                #description=localized("commands.ai_instruction.param_action"),
                choices=add_remove_check_choices()
            ),
            instruction: str = commands.Param(
                default="",
                #name=localized("commands.ai_instruction.param_instruction_name"),
                #description=localized("commands.ai_instruction.param_instruction")
            )
    ):
        try:
            int_id = int(user_id)
        except ValueError:
            return inter.response.send_message("user id is not int", ephemeral=True)

        match action:
            case "check":
                return inter.response.send_message(get_user_instruction(int_id) or "None", ephemeral=True)
            case "add":
                if not instruction:
                    delete_user_instruction(int_id)
                    return inter.response.send_message("Deleted", ephemeral=True)
                else:
                    set_user_instruction(int_id, instruction)
                    return inter.response.send_message("Saved", ephemeral=True)
            case _:
                delete_user_instruction(int_id)
                return inter.response.send_message("Deleted", ephemeral=True)


def setup(bot: commands.Bot):
    load_instructions()

    fixed = purge_stored_images()
    if fixed:
        print(f"AI memory: removed stored images from {fixed} old history rows")

    ai_limits.cleanup()

    bot.add_cog(AiCog(bot))
