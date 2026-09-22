import base64
import io
import os
import time

import aiohttp
import disnake
from disnake.ext import commands

import i18n
from logs import send_log, LogColor
from permissions import validate_permissions, Permission, has_permissions, DEVELOPER_LIST
from discord_i18n import localized, yes_no_choices, add_remove_check_choices
from cogs.voice import MAX_TTS_LENGTH, MAX_TTS_LENGTH_VOTED
from cogs.ai.ai_memory import load_memory, add_message, clear_memory
from cogs.ai.ai_api_keys import set_api_key, remove_api_key, get_api_key
from cogs.ai.ai_prompts import (
    SYSTEM_PROMPT, VOICE_SYSTEM_PROMPT, NO_LIMIT_SYSTEM_PROMPT, NO_LIMIT_VOICE_SYSTEM_PROMPT, IMAGE_SYSTEM_PROMPT, NO_LIMIT_IMAGE_SYSTEM_PROMPT,
    CUSTOM_USER_INSTRUCTIONS, get_user_instruction, set_user_instruction, delete_user_instruction, load_instructions, PRONOUNS
)

from storage import AI_SIGNAL_CHANNEL, TECHNICAL_SUPPORT_SERVER
from other_apis.topgg_utils import is_voted
from paths import env_var_to_int, env_var
from enum import IntEnum

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

# NOTE: Google's Gemini lineup moves fast (frequent deprecations/GA promotions) — double-check
# the exact current model IDs in Google AI Studio's model picker before deploying, and update
# these two constants if they've changed.

# Cheap, high-quota model: normal chat + vision (it can read images the user attaches).
# This is what handles every regular message, so it should NOT be the scarce image model.
GEMINI_TEXT_MODEL = env_var("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-lite")
# Only called when the text model decides an image is actually needed
GEMINI_IMAGE_MODEL = env_var("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-lite-image")

REQUEST_TIMEOUT_SECONDS = env_var_to_int("AI_REQUEST_TIMEOUT_SECONDS", "60")
COOLDOWN_SECONDS = env_var_to_int("AI_COOLDOWN_SECONDS", "30")
COOLDOWN_SECONDS_VOTED = env_var_to_int("AI_COOLDOWN_SECONDS", "10")

DISCORD_MESSAGE_LIMIT = 2000
MAX_ATTACHMENTS = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB per image, sane upper bound for inline base64 upload

class PromptType(IntEnum):
    NONE = 0

    TEXT = 1
    VOICE = 2
    IMAGE = 3

PROMPTS_BY_TYPE: dict[PromptType, list[str]] = {
    PromptType.TEXT: [SYSTEM_PROMPT, NO_LIMIT_SYSTEM_PROMPT],
    PromptType.VOICE: [VOICE_SYSTEM_PROMPT, NO_LIMIT_VOICE_SYSTEM_PROMPT],
    PromptType.IMAGE: [IMAGE_SYSTEM_PROMPT, NO_LIMIT_IMAGE_SYSTEM_PROMPT]
}

# Function declaration exposed to GEMINI_TEXT_MODEL so IT decides when an image is actually needed,
# instead of every single message being routed through the expensive image model.
IMAGE_TOOL = {
    "functionDeclarations": [
        {
            "name": "generate_image",
            "description": (
                "Generates or edits an image and shows it to the user. Call this ONLY when the "
                "user explicitly asks to draw, create, generate, or edit an image/picture. "
                "Do not call it for plain text questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "A detailed, self-contained prompt describing the image to generate, "
                            "or the edit to apply to any image(s) the user attached."
                        ),
                    }
                },
                "required": ["prompt"],
            },
        }
    ]
}

VOICE_MAX_TOKENS = env_var_to_int("AI_VOICE_MAX_TOKENS", "350")

DEBUG_MODE = False

class SignalException(Exception):
    PREFIX = "An error detected while sending a suspicious AI prompt signal: "

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.PREFIX + message)

class AiCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cooldowns: dict[int, float] = {}

    async def _check_cooldown(self, user_id: int) -> int | None:
        """Checks user cooldown"""
        cooldown = COOLDOWN_SECONDS_VOTED if await is_voted(user_id) else COOLDOWN_SECONDS

        now = time.time()
        last_time = self.cooldowns.get(user_id, 0)
        if now - last_time < cooldown:
            return int(cooldown - (now - last_time))

        self.cooldowns[user_id] = now
        return None

    def format_system_prompt(self, prompt_type: PromptType, user: int):
        prompt = PROMPTS_BY_TYPE[prompt_type][int(DEBUG_MODE if user in DEVELOPER_LIST else 0)].format(bot_name=self.bot.user.name, pronouns=PRONOUNS)

        custom = get_user_instruction(user) or "This user doesn't have any custom instructions."

        return prompt + custom

    @staticmethod
    async def _download_image(attachment: disnake.Attachment) -> dict | None:
        """Downloads a Discord attachment and turns it into an internal image part, if it's an image."""
        if not attachment.content_type or not attachment.content_type.startswith("image/"):
            return None
        if attachment.size > MAX_IMAGE_BYTES:
            return None

        raw = await attachment.read()
        return {
            "type": "image",
            "mime_type": attachment.content_type.split(";")[0].strip(),
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

    @staticmethod
    def _to_gemini_parts(parts: list[dict]) -> list[dict]:
        """Converts internal parts (text/image) into Gemini API `parts` payload."""
        gemini_parts = []
        for part in parts:
            if part["type"] == "text":
                if part["text"]:
                    gemini_parts.append({"text": part["text"]})
            elif part["type"] == "image":
                gemini_parts.append({
                    "inlineData": {
                        "mimeType": part["mime_type"],
                        "data": part["data"],
                    }
                })
            # function_call parts are intentionally not replayed into history
        return gemini_parts

    @staticmethod
    def _from_gemini_parts(parts: list[dict]) -> list[dict]:
        """Converts Gemini API response `parts` into internal parts (text/image/function_call)."""
        result = []
        for part in parts:
            if "text" in part and part["text"]:
                result.append({"type": "text", "text": part["text"]})
            elif "inlineData" in part:
                inline = part["inlineData"]
                result.append({
                    "type": "image",
                    "mime_type": inline.get("mimeType", "image/png"),
                    "data": inline.get("data", ""),
                })
            elif "functionCall" in part:
                call = part["functionCall"]
                result.append({
                    "type": "function_call",
                    "name": call.get("name", ""),
                    "args": call.get("args") or {},
                })
        return result

    @staticmethod
    async def _build_discord_files(images: list[dict]) -> list[disnake.File]:
        files = []
        for index, image in enumerate(images):
            try:
                raw = base64.b64decode(image["data"])
            except (ValueError, TypeError):
                continue
            extension = image["mime_type"].split("/")[-1] if "/" in image["mime_type"] else "png"
            files.append(disnake.File(io.BytesIO(raw), filename=f"gemini_{index}.{extension}"))
        return files

    @staticmethod
    def _build_contents(guild_id: int, user_id: int, user_parts: list[dict]) -> list[dict]:
        contents = []
        for message in load_memory(guild_id, user_id):
            role = "model" if message["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": AiCog._to_gemini_parts(message["parts"])})
        contents.append({"role": "user", "parts": AiCog._to_gemini_parts(user_parts)})
        return contents

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

    @staticmethod
    async def _call_gemini(
            api_key: str,
            model: str,
            system_prompt: str,
            contents: list[dict],
            max_tokens: int | None = None,
            allow_image_output: bool = False,
            tools: list[dict] | None = None,
    ) -> list[dict]:
        """Sends a single request to the Gemini API and returns the parsed response parts."""
        generation_config = {}
        if max_tokens:
            generation_config["maxOutputTokens"] = max_tokens
        if allow_image_output:
            generation_config["responseModalities"] = ["TEXT", "IMAGE"]

        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
        }
        if generation_config:
            payload["generationConfig"] = generation_config
        if tools:
            payload["tools"] = tools

        url = GEMINI_API_URL.format(model=model, key=api_key)
        headers = {"Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    print(f"❌ Gemini ({model}) returned {resp.status}: {error_text[:300]}")
                    raise RuntimeError(f"Gemini API returned {resp.status}: {error_text[:300]}")
                data = await resp.json()

        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback", {})
            raise RuntimeError(f"Gemini returned no candidates (promptFeedback={feedback})")

        return AiCog._from_gemini_parts(candidates[0].get("content", {}).get("parts", []))

    async def _generate_reply(
            self,
            guild_id: int,
            user_id: int,
            prompt: str,
            images: list[dict],
            system_prompt: str,
            user: disnake.Member | disnake.User,
            channel: disnake.abc.GuildChannel | None = None,
            attachments: list[disnake.Attachment] | None = None,
    ) -> tuple[str, list[dict]]:
        """Full chat pipeline: the cheap text model answers directly, or — only if it decides an
        image is genuinely needed — calls the expensive image model once. Persists the exchange
        to memory either way."""
        api_key = get_api_key(guild_id)

        user_parts = [{"type": "text", "text": prompt}]
        if images:
            user_parts.extend(images)

        contents = AiCog._build_contents(guild_id, user_id, user_parts)

        first_pass = await AiCog._call_gemini(
            api_key, GEMINI_TEXT_MODEL, system_prompt, contents, tools=[IMAGE_TOOL],
        )

        #function_call = next(
        #    (p for p in first_pass if p["type"] == "function_call" and p["name"] == "generate_image"),
        #    None,
        #)

        function_call = None

        if function_call is None:
            response_parts = [p for p in first_pass if p["type"] != "function_call"]
        else:
            image_prompt = (function_call.get("args") or {}).get("prompt") or prompt
            # Keep prior history for continuity (e.g. "edit the picture from before"),
            # but replace the current turn with the model's own image prompt.
            image_contents = contents[:-1] + [{
                "role": "user",
                "parts": AiCog._to_gemini_parts([{"type": "text", "text": image_prompt}] + images),
            }]
            response_parts = await AiCog._call_gemini(
                api_key, GEMINI_IMAGE_MODEL, self.format_system_prompt(PromptType.IMAGE, user_id), image_contents,
                allow_image_output=True,
            )

        if not response_parts:
            response_parts = [{"type": "text", "text": ""}]

        answer_text = "\n".join(p["text"] for p in response_parts if p["type"] == "text").strip()
        generated_images = [p for p in response_parts if p["type"] == "image"]

        await add_message(guild_id, user_id, "user", user_parts)
        await add_message(guild_id, user_id, "assistant", response_parts)

        flagged = answer_text.endswith("-flag")
        print(f"AI answer flag: {flagged}")

        if flagged:
            answer_text = answer_text.removesuffix("-flag").rstrip()

            try:
                await self._send_signal(
                    user=user,
                    prompt=prompt,
                    answer=answer_text,
                    channel=channel,
                    attachments=attachments,
                )
            except SignalException as e:
                print(f"❌ {e}")
            except Exception as e:
                print(f"❌ Unexpected signal error: {e}")

        return answer_text, generated_images

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if message.author.bot:
            return

        if message.guild is None:
            await self.bot.process_commands(message)
            return

        if self.bot.user not in message.mentions:
            await self.bot.process_commands(message)
            return

        gid = message.guild.id
        user_id = message.author.id

        if has_permissions(message.author, message.channel, Permission.AiBlackList):
            await message.reply(i18n.t("ai_cog.blacklist", locale=gid))
            await self.bot.process_commands(message)
            return

        remaining = await self._check_cooldown(user_id)
        if remaining:
            vote_ad = "" if await is_voted(user_id) else "\n\n" + i18n.t("top_gg_cog.voting_ad", locale=gid)
            await message.reply(i18n.t("ai_cog.cooldown" + vote_ad, locale=gid, seconds=remaining))
            await self.bot.process_commands(message)
            return

        # remove bot mentions from prompt
        prompt = message.content
        for mention in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
            prompt = prompt.replace(mention, "")
        prompt = prompt.strip()

        images = await self._collect_images(message.attachments)

        if not prompt and not images:
            await message.reply(i18n.t("ai_cog.empty_prompt", locale=gid))
            await self.bot.process_commands(message)
            return

        key = get_api_key(gid)

        if not key:
            await message.reply(i18n.t("ai_cog.not_configured", locale=gid))
            await self.bot.process_commands(message)
            return

        async with message.channel.typing():
            try:
                answer, generated_images = await self._generate_reply(
                    gid,
                    user_id,
                    prompt,
                    images,
                    system_prompt=self.format_system_prompt(PromptType.TEXT, user_id),
                    user=message.author,
                    channel=message.channel,
                    attachments=message.attachments,
                )
            except (aiohttp.ClientError, TimeoutError) as e:
                print(f"❌ Network AI error: {e}")
                await message.reply(i18n.t("ai_cog.network_error", locale=gid))
                await self.bot.process_commands(message)
                return
            except Exception as e:
                print(f"❌ AI error: {e}")
                await message.reply(i18n.t("ai_cog.generic_error", locale=gid))
                await self.bot.process_commands(message)
                return

        if len(answer) > DISCORD_MESSAGE_LIMIT:
            answer = answer[:DISCORD_MESSAGE_LIMIT - 1] + "…"

        files = await self._build_discord_files(generated_images)
        await message.reply(content=answer or None, files=files)
        await self.bot.process_commands(message)

    @commands.slash_command(name="ai")
    async def ai_command(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
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

    @ai_command.sub_command(
        name="key",
        description=localized("commands.ai_key.description"),
    )
    async def set_ai_key_command(
            self,
            inter: disnake.ApplicationCommandInteraction,
            key: str | None = commands.Param(
                default=None,
                description=localized("commands.ai_key.param_key"),
                name=localized("commands.ai_key.param_key_name"),
            )
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.Admin: True}, {disnake.Permissions(administrator=True): True}]):
            return None

        old_key = None #get_api_key(gid)

        if key is None:
            if old_key is None:
                return await inter.response.send_message(
                    i18n.t("ai_cog.key_tutorial", locale=gid), ephemeral=True
                )

            remove_api_key(gid)
            return await inter.response.send_message(
                i18n.t("ai_cog.key_deleted", locale=gid), ephemeral=True
            )

        set_api_key(gid, key)
        return await inter.response.send_message(
            i18n.t("ai_cog.key_updated", locale=gid), ephemeral=True
        )

    @ai_command.sub_command_group(name="ask")
    async def ai_ask_command(self, inter: disnake.ApplicationCommandInteraction):
        # Sub command group
        pass

    # Bot mention also calls this command
    @ai_ask_command.sub_command(
        name="text",
        description=localized("commands.ai_ask_text.description"),
    )
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
                name=localized("commands.ai_ask_text.param_hidden_name")
            )
    ):
        gid = inter.guild_id
        user_id = inter.author.id

        if await validate_permissions(inter, [{Permission.AiBlackList: False}]):
            return None

        remaining = await self._check_cooldown(user_id)
        if remaining:
            vote_ad = "" if await is_voted(user_id) else "\n\n" + i18n.t("top_gg_cog.voting_ad", locale=gid)
            return await inter.response.send_message(i18n.t("ai_cog.cooldown", locale=gid, seconds=remaining) + vote_ad, ephemeral=True)

        key = get_api_key(gid)

        if not key:
            return await inter.response.send_message(i18n.t("ai_cog.not_configured", locale=gid), ephemeral=True)

        images = await self._collect_images([image]) if image else []

        await inter.response.defer(ephemeral=hidden)

        try:
            answer, generated_images = await self._generate_reply(
                gid,
                user_id,
                text.strip(),
                images,
                system_prompt=self.format_system_prompt(PromptType.TEXT, user_id),
                user=inter.author,
                channel=inter.channel,
                attachments=[image] if image else [],
            )
        except (aiohttp.ClientError, TimeoutError) as e:
            print(f"❌ Network AI error: {e}")
            return await inter.followup.send(i18n.t("ai_cog.network_error", locale=gid), ephemeral=hidden)
        except Exception as e:
            print(f"❌ AI error: {e}")
            return await inter.followup.send(i18n.t("ai_cog.generic_error", locale=gid), ephemeral=hidden)

        if len(answer) > DISCORD_MESSAGE_LIMIT:
            answer = answer[:DISCORD_MESSAGE_LIMIT - 1] + "…"

        files = await self._build_discord_files(generated_images)
        return await inter.followup.send(content=answer or None, files=files, ephemeral=hidden)

    @ai_ask_command.sub_command(
        name="voice",
        description=localized("commands.ai_ask_voice.description"),
    )
    async def ai_ask_voice(
            self,
            inter: disnake.ApplicationCommandInteraction,
            text: str = commands.Param(
                description=localized("commands.ai_ask_voice.param_text"),
                name=localized("commands.ai_ask_voice.param_text_name")
            )
    ):
        gid = inter.guild_id

        if await validate_permissions(inter, [{Permission.AiBlackList: False, Permission.TTS: True}, {Permission.AiBlackList: False, disnake.Permissions(administrator=True): True}]):
            return None

        vc = inter.guild.voice_client
        if vc is None:
            return await inter.response.send_message(
                i18n.t("voice_cog.not_in_voice", locale=gid), ephemeral=True
            )

        key = get_api_key(gid)

        if not key:
            return await inter.response.send_message(
                i18n.t("ai_cog.not_configured", locale=gid), ephemeral=True
            )

        voice_cog = self.bot.get_cog("VoiceCog")
        if voice_cog is None:
            return await inter.response.send_message(
                i18n.t("ai_cog.voice_cog_missing", locale=gid), ephemeral=True
            )

        remaining = await self._check_cooldown(inter.author.id)
        if remaining:
            vote_ad = "" if await is_voted(inter.author.id) else "\n\n" + i18n.t("top_gg_cog.voting_ad", locale=gid)
            return await inter.response.send_message(
                i18n.t("ai_cog.cooldown", locale=gid, seconds=remaining) + vote_ad, ephemeral=True
            )

        await inter.response.defer(ephemeral=False)

        bot_name = inter.guild.me.display_name
        user_id = inter.author.id

        # Voice replies are spoken aloud: text-only model, no image tool, no image output.
        user_parts = [{"type": "text", "text": text}]
        contents = AiCog._build_contents(gid, user_id, user_parts)

        try:
            response_parts = await self._call_gemini(
                key, GEMINI_TEXT_MODEL,
                self.format_system_prompt(PromptType.VOICE, user_id),
                contents, max_tokens=VOICE_MAX_TOKENS,
            )
        except (aiohttp.ClientError, TimeoutError) as e:
            print(f"❌ Network AI error (/ai ask): {e}")
            return await inter.followup.send(i18n.t("ai_cog.network_error", locale=gid))
        except Exception as e:
            print(f"❌ AI error (/ai ask): {e}")
            return await inter.followup.send(i18n.t("ai_cog.generic_error", locale=gid))

        if not response_parts:
            response_parts = [{"type": "text", "text": ""}]
        answer = "\n".join(p["text"] for p in response_parts if p["type"] == "text").strip()

        flagged = answer.endswith("-flag")
        print(f"AI answer flag: {flagged}")

        if flagged:
            answer = answer.removesuffix("-flag").rstrip()

            try:
                await self._send_signal(
                    user=inter.author,
                    prompt=text,
                    answer=answer,
                    channel=inter.channel,
                    attachments=[],
                )
            except SignalException as e:
                print(f"❌ {e}")
            except Exception as e:
                print(f"❌ Unexpected signal error: {e}")

        await add_message(gid, user_id, "user", user_parts)
        await add_message(gid, user_id, "assistant", response_parts)

        max_length = MAX_TTS_LENGTH_VOTED if await is_voted(user_id) else MAX_TTS_LENGTH

        spoken_text = answer if len(answer) <= max_length else answer[:max_length - 1] + "…"
        position = voice_cog.enqueue_tts(inter.guild.id, vc, spoken_text)

        return await inter.followup.send(
            i18n.t("ai_cog.ask_response", locale=gid, name=bot_name, text=answer, position=position)
        )

    @ai_command.sub_command(name="debug", description=localized("logs_cog.common.dash"))
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
        if await validate_permissions(inter, {Permission.Developer: True}):
            return None

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
        if not await validate_permissions(inter, {Permission.Developer: True}):
            return None

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
    bot.add_cog(AiCog(bot))