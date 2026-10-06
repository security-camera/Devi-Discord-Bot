"""Internal HTTP API for the Devi website dashboard.

The website never touches the database or the bot token. It signs the user in
with Discord OAuth2 and then calls this API with a shared secret and the id of
the signed-in user. Every change therefore goes through the same in-memory
caches, validation and permission rules as the slash commands.

Setup (config/.env):

    DASHBOARD_API_TOKEN=<random string, 24+ characters>   # enables the API
    DASHBOARD_API_HOST=127.0.0.1                          # default
    DASHBOARD_API_PORT=8765                               # default

The same token must be configured on the website (BOT_API_TOKEN).
Bind the API to a loopback or private address only: it is not meant to be public.

Access mirrors the slash commands:
    permissions, logs, birthdays, voice -> bot.Admin or Discord "Administrator"
    triggers                            -> bot.ManageTriggers or Discord "Administrator"

All snowflake ids are sent as strings: JavaScript cannot hold 64-bit integers.
"""

import asyncio
import hmac
import json
import re
from types import SimpleNamespace

import disnake
from aiohttp import web
from disnake import ChannelType
from disnake.ext import commands

import i18n
import permissions
from cogs.birthdays import (
    BIRTHDAY_CHECK_HOUR_UTC,
    get_birthday_channel,
    remove_birthday_channel,
    set_birthday_channel,
)
from cogs.permissions_commands import PERMISSION_LABELS, describe_permissions
from cogs.temp_voices import DEFAULT_NAME_TEMPLATE, get_guild_config, set_guild_config
from cogs.triggers import save_triggers
from db import db_cursor
from localization import get_localization
from logs import LogColor, get_log_channel_id, remove_log_channel, send_log, set_log_channel_id
from paths import env_var, env_var_to_int
from permissions import Permission, PermissionCheckType, has_permissions

API_TOKEN = env_var("DASHBOARD_API_TOKEN")
API_HOST = env_var("DASHBOARD_API_HOST", "127.0.0.1")
API_PORT = env_var_to_int("DASHBOARD_API_PORT", "8765")
MIN_TOKEN_LENGTH = 24

SECTIONS = ("permissions", "logs", "birthdays", "voice", "triggers")
ADMIN_SECTIONS = {"permissions", "logs", "birthdays", "voice"}

MAX_TRIGGERS_PER_GUILD = 100
MAX_RESPONSES_PER_TRIGGER = 25
MAX_PATTERN_LENGTH = 500
MAX_RESPONSE_LENGTH = 2000  # Discord message limit
MAX_NAME_TEMPLATE_LENGTH = 100  # Discord channel name limit
MAX_PERMISSION_CHANGES = 25
MEMBER_SEARCH_LIMIT = 10
MAX_BODY_BYTES = 64 * 1024

# Every bit the dashboard may write: exactly the toggles of the bot's own /permissions manage panel.
ALLOWED_MASK = 0
for _flag in PERMISSION_LABELS:
    ALLOWED_MASK |= int(_flag)
ALLOWED_MASK |= int(Permission.Admin)

SCOPES = {
    "guild": PermissionCheckType.Guild,
    "role": PermissionCheckType.Role,
    "channel": PermissionCheckType.Channel,
    "user": PermissionCheckType.User,
}
SCOPE_EMOJI = {"guild": "🌐", "role": "🎭", "channel": "📺", "user": "👤"}

# Audit-log wording. Kept here so the cog works without touching the locale files.
_AUDIT_TEXT = {
    "en-US": {
        "title": "Settings changed on the website dashboard",
        "section": "Section",
        "sections": {"permissions": "Permissions", "logs": "Logs", "birthdays": "Birthdays",
                     "voice": "Temporary voice channels", "triggers": "Triggers"},
    },
    "ru": {
        "title": "Настройки изменены через панель управления на сайте",
        "section": "Раздел",
        "sections": {"permissions": "Права", "logs": "Логи", "birthdays": "Дни рождения",
                     "voice": "Временные голосовые каналы", "triggers": "Триггеры"},
    },
    "de": {
        "title": "Einstellungen im Website-Dashboard geändert",
        "section": "Bereich",
        "sections": {"permissions": "Berechtigungen", "logs": "Logs", "birthdays": "Geburtstage",
                     "voice": "Temporäre Sprachkanäle", "triggers": "Trigger"},
    },
    "fi": {
        "title": "Asetuksia muutettu verkkosivun hallintapaneelissa",
        "section": "Osio",
        "sections": {"permissions": "Oikeudet", "logs": "Lokit", "birthdays": "Syntymäpäivät",
                     "voice": "Väliaikaiset äänikanavat", "triggers": "Triggerit"},
    },
    "uk": {
        "title": "Налаштування змінено в панелі керування на сайті",
        "section": "Розділ",
        "sections": {"permissions": "Права", "logs": "Логи", "birthdays": "Дні народження",
                     "voice": "Тимчасові голосові канали", "triggers": "Тригери"},
    },
    "es-419": {
        "title": "Configuración modificada desde el panel web",
        "section": "Sección",
        "sections": {"permissions": "Permisos", "logs": "Registros", "birthdays": "Cumpleaños",
                     "voice": "Canales de voz temporales", "triggers": "Disparadores"},
    },
    "bg": {
        "title": "Настройките са променени от таблото на сайта",
        "section": "Раздел",
        "sections": {"permissions": "Права", "logs": "Логове", "birthdays": "Рождени дни",
                     "voice": "Временни гласови канали", "triggers": "Тригери"},
    },
}


class ApiError(Exception):
    """Error that is returned to the website as {"error": code, "detail": ...}."""

    def __init__(self, status: int, code: str, detail: str | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail


# ------------------------------------------------------------------ helpers


class _NoChannel:
    """Dashboard actions are not performed in any channel, so channel-scoped grants never apply."""

    id = 0


_NO_CHANNEL = _NoChannel()


def _sid(value) -> str | None:
    return None if value is None else str(value)


def _parse_id(raw, *, code: str = "invalid_id") -> int:
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ApiError(400, code)
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, code) from None
    if value <= 0 or value >= 1 << 63:
        raise ApiError(400, code)
    return value


def _channel_kind(channel) -> str | None:
    kind = channel.type
    if kind in (ChannelType.text, ChannelType.news):
        return "text"
    if kind == ChannelType.voice:
        return "voice"
    if kind == ChannelType.stage_voice:
        return "stage"
    # ChannelType.media only exists in newer disnake releases.
    if kind in (ChannelType.forum, getattr(ChannelType, "media", ChannelType.forum)):
        return "forum"
    if kind == ChannelType.category:
        return "category"
    return None


def _asset_url(asset, size: int = 64) -> str | None:
    if asset is None:
        return None
    try:
        return asset.with_size(size).url
    except Exception:  # noqa: BLE001 - an avatar must never break an API answer
        return None


def _locale_for(guild_id: int) -> str:
    code = i18n.resolve_locale_code(get_localization(guild_id))
    return code if code in _AUDIT_TEXT else "en-US"


def _clip(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _flag_defs() -> list[dict]:
    """Permission toggles, in the same set and order the bot's /permissions manage panel uses."""
    defs = []
    for flag in PERMISSION_LABELS:
        if flag == Permission.Admin:
            kind = "admin"
        elif flag.name.endswith("BlackList"):
            kind = "restrict"
        else:
            kind = "allow"
        defs.append({"name": flag.name, "value": int(flag), "kind": kind})
    return defs


def _sections_for(member) -> set[str]:
    """Which dashboard sections the member may use. Mirrors the checks of the slash commands."""
    if member.guild_permissions.administrator:
        return set(SECTIONS)

    sections: set[str] = set()
    if has_permissions(member, _NO_CHANNEL, Permission.Admin):
        sections |= ADMIN_SECTIONS
    if has_permissions(member, _NO_CHANNEL, Permission.ManageTriggers):
        sections.add("triggers")
    return sections


async def _get_member(guild, user_id: int, *, fetch: bool = True):
    member = guild.get_member(user_id)
    if member is None and fetch:
        try:
            member = await guild.fetch_member(user_id)
        except (disnake.NotFound, disnake.HTTPException):
            return None
    return member


async def _read_json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "invalid_json") from None
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json")
    return body


def _pick_channel(guild, raw, kinds: set[str], *, nullable: bool = True):
    """Resolves a channel id from a request body, checking that it belongs to the guild and has the right type."""
    if raw is None or raw == "":
        if nullable:
            return None
        raise ApiError(400, "invalid_channel")

    channel = guild.get_channel(_parse_id(raw, code="invalid_channel"))
    if channel is None or _channel_kind(channel) not in kinds:
        raise ApiError(400, "invalid_channel")
    return channel


# ---------------------------------------------------------------- serializers


def _serialize_channels(guild) -> tuple[list[dict], list[dict]]:
    me = guild.me
    categories, channels = [], []

    for channel in guild.channels:
        kind = _channel_kind(channel)
        if kind is None:
            continue

        if kind == "category":
            categories.append({"id": str(channel.id), "name": channel.name, "position": channel.position})
            continue

        can_send = False
        if kind == "text" and me is not None:
            perms = channel.permissions_for(me)
            can_send = bool(perms.view_channel and perms.send_messages)

        category = getattr(channel, "category", None)
        channels.append({
            "id": str(channel.id),
            "name": channel.name,
            "kind": kind,
            "category_id": _sid(category.id) if category else None,
            "position": channel.position,
            "can_send": can_send,
        })

    categories.sort(key=lambda item: (item["position"], int(item["id"])))
    order = {item["id"]: index for index, item in enumerate(categories)}
    channels.sort(key=lambda item: (order.get(item["category_id"], -1), item["kind"] != "text", item["position"], int(item["id"])))
    return channels, categories


def _serialize_roles(guild) -> list[dict]:
    roles = []
    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if role.id == guild.id:  # @everyone is covered by the server-wide scope
            continue
        color = role.color.value
        roles.append({
            "id": str(role.id),
            "name": role.name,
            "color": f"#{color:06x}" if color else None,
            "managed": bool(role.managed),
        })
    return roles


def _serialize_permissions(guild) -> dict:
    snapshot = permissions.all_permissions(guild.id)

    users = []
    for user_id, mask in snapshot["users"].items():
        if not int(mask):
            continue
        member = guild.get_member(user_id)
        users.append({
            "id": str(user_id),
            "mask": int(mask),
            "name": member.display_name if member else None,
            "username": member.name if member else None,
            "avatar": _asset_url(member.display_avatar) if member else None,
        })

    return {
        "guild": int(snapshot["guild"]),
        "roles": {str(rid): int(mask) for rid, mask in snapshot["roles"].items() if int(mask)},
        "channels": {str(cid): int(mask) for cid, mask in snapshot["channels"].items() if int(mask)},
        "users": users,
    }


def _serialize_triggers(cog, guild_id: int) -> list[dict]:
    return [
        {"pattern": pattern, "responses": list(responses)}
        for pattern, responses in cog.triggers.get(guild_id, {}).items()
    ]


def _serialize_voice(guild) -> dict:
    config = get_guild_config(guild.id)
    me = guild.me
    can_manage = bool(me and me.guild_permissions.manage_channels and me.guild_permissions.move_members)
    return {
        "lobby_channel_id": _sid(config["lobby_channel_id"]) if config else None,
        "category_id": _sid(config["category_id"]) if config else None,
        "name_template": config["name_template"] if config else None,
        "default_name_template": DEFAULT_NAME_TEMPLATE,
        "bot_can_manage": can_manage,
    }


# ------------------------------------------------------------------- the API


class DashboardApi:
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -- plumbing

    def triggers_cog(self):
        cog = self.bot.get_cog("TriggersCog")
        if cog is None:
            raise ApiError(503, "triggers_unavailable")
        return cog

    @staticmethod
    def user_id(request: web.Request) -> int:
        return _parse_id(request.headers.get("X-Discord-User-Id"), code="invalid_user")

    async def context(self, request: web.Request, section: str | None):
        """Resolves guild + member for a request and checks that the member may use `section`."""
        guild = self.bot.get_guild(_parse_id(request.match_info["guild_id"], code="invalid_guild"))
        if guild is None:
            raise ApiError(404, "guild_not_found")

        member = await _get_member(guild, self.user_id(request))
        if member is None:
            raise ApiError(403, "forbidden")

        sections = _sections_for(member)
        if not sections or (section is not None and section not in sections):
            raise ApiError(403, "forbidden")

        return guild, member, sections

    async def audit(self, guild, member, section: str, details: str) -> None:
        """Writes the change to the guild's log channel, like the slash commands do."""
        text = _AUDIT_TEXT[_locale_for(guild.id)]
        try:
            await send_log(
                guild,
                text["title"],
                color=LogColor.Bot,
                fields=[
                    (i18n.t("logs_cog.fields.moderator", locale=guild.id), f"{member.mention} ({member.id})"),
                    (text["section"], text["sections"][section]),
                    ("—", _clip(details) or "—"),
                ],
            )
        except Exception as error:  # noqa: BLE001 - an audit failure must not fail the change itself
            print(f"[dashboard_api] audit log failed: {error}")

    # -- endpoints

    async def ping(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def access(self, request: web.Request) -> web.Response:
        """Which of the given guilds have the bot, and which sections the user may open in each."""
        user_id = self.user_id(request)
        body = await _read_json(request)

        raw_ids = body.get("guild_ids")
        if not isinstance(raw_ids, list) or len(raw_ids) > 400:
            raise ApiError(400, "invalid_guild_ids")

        result = {}
        for raw in raw_ids:
            guild = self.bot.get_guild(_parse_id(raw, code="invalid_guild_ids"))
            if guild is None:
                continue
            member = guild.get_member(user_id)  # cache only: never one REST call per guild
            result[str(guild.id)] = {"sections": sorted(_sections_for(member)) if member else []}

        return web.json_response({"guilds": result})

    async def snapshot(self, request: web.Request) -> web.Response:
        guild, _member, sections = await self.context(request, None)

        settings: dict = {}
        if "logs" in sections:
            settings["logs"] = {"channel_id": _sid(get_log_channel_id(guild.id))}
        if "birthdays" in sections:
            settings["birthdays"] = {
                "channel_id": _sid(get_birthday_channel(guild.id)),
                "hour_utc": BIRTHDAY_CHECK_HOUR_UTC,
            }
        if "voice" in sections:
            settings["voice"] = _serialize_voice(guild)
        if "permissions" in sections:
            settings["permissions"] = _serialize_permissions(guild)
        if "triggers" in sections:
            settings["triggers"] = _serialize_triggers(self.triggers_cog(), guild.id)

        data = {
            "guild": {"id": str(guild.id), "name": guild.name, "icon": _asset_url(guild.icon, 128)},
            "sections": [name for name in SECTIONS if name in sections],
            "settings": settings,
            "permission_flags": _flag_defs(),
            "limits": {
                "max_triggers": MAX_TRIGGERS_PER_GUILD,
                "max_responses": MAX_RESPONSES_PER_TRIGGER,
                "max_pattern": MAX_PATTERN_LENGTH,
                "max_response": MAX_RESPONSE_LENGTH,
                "max_name_template": MAX_NAME_TEMPLATE_LENGTH,
            },
            "channels": [],
            "categories": [],
            "roles": [],
        }

        # Channel and role names are only for people who configure the bot; a triggers-only
        # editor does not need to see private channels.
        if sections & ADMIN_SECTIONS:
            data["channels"], data["categories"] = _serialize_channels(guild)
            data["roles"] = _serialize_roles(guild)

        return web.json_response(data)

    async def put_log_channel(self, request: web.Request) -> web.Response:
        guild, member, _ = await self.context(request, "logs")
        channel = _pick_channel(guild, (await _read_json(request)).get("channel_id"), {"text"})

        if channel is None:
            remove_log_channel(guild.id)
        else:
            set_log_channel_id(guild.id, channel.id)

        await self.audit(guild, member, "logs", channel.mention if channel else "—")
        return web.json_response({"channel_id": _sid(channel.id if channel else None)})

    async def put_birthday_channel(self, request: web.Request) -> web.Response:
        guild, member, _ = await self.context(request, "birthdays")
        channel = _pick_channel(guild, (await _read_json(request)).get("channel_id"), {"text"})

        if channel is None:
            remove_birthday_channel(guild.id)
        else:
            set_birthday_channel(guild.id, channel.id)

        await self.audit(guild, member, "birthdays", channel.mention if channel else "—")
        return web.json_response({"channel_id": _sid(channel.id if channel else None)})

    async def put_temp_voice(self, request: web.Request) -> web.Response:
        guild, member, _ = await self.context(request, "voice")
        body = await _read_json(request)

        lobby = _pick_channel(guild, body.get("lobby_channel_id"), {"voice"})

        if lobby is None:
            # No lobby means "turn temporary voice channels off for this server".
            with db_cursor(commit=True) as cur:
                cur.execute("DELETE FROM temp_voice_config WHERE guild_id = %s", (guild.id,))
            details = "—"
        else:
            category = _pick_channel(guild, body.get("category_id"), {"category"})

            template = body.get("name_template")
            if template is not None and not isinstance(template, str):
                raise ApiError(400, "invalid_name_template")
            template = (template or "").strip() or None
            if template and len(template) > MAX_NAME_TEMPLATE_LENGTH:
                raise ApiError(400, "name_template_too_long")

            set_guild_config(guild.id, lobby.id, category.id if category else None, template)

            details = f"🔊 {lobby.mention}"
            if category:
                details += f"\n📁 {category.name}"
            if template:
                details += "\n`" + template.replace("`", "'") + "`"

        await self.audit(guild, member, "voice", details)
        return web.json_response(_serialize_voice(guild))

    async def patch_permissions(self, request: web.Request) -> web.Response:
        guild, member, _ = await self.context(request, "permissions")
        body = await _read_json(request)

        changes = body.get("changes")
        if not isinstance(changes, list) or not changes:
            raise ApiError(400, "invalid_changes")
        if len(changes) > MAX_PERMISSION_CHANGES:
            raise ApiError(400, "too_many_changes")

        planned = []  # (scope, check type, target id, mask, display text)
        seen = set()

        for change in changes:
            if not isinstance(change, dict):
                raise ApiError(400, "invalid_changes")

            scope = change.get("scope")
            if scope not in SCOPES:
                raise ApiError(400, "invalid_scope")

            mask = change.get("mask")
            if isinstance(mask, bool) or not isinstance(mask, int) or mask < 0 or mask & ~ALLOWED_MASK:
                raise ApiError(400, "invalid_mask")

            if scope == "guild":
                target_id, label = guild.id, f"**{guild.name}**"
            else:
                target_id = _parse_id(change.get("id"), code="invalid_target")
                label = None

                if scope == "role":
                    role = guild.get_role(target_id)
                    if target_id == guild.id or (role is None and mask):
                        raise ApiError(400, "invalid_target")
                    label = role.mention if role else f"`{target_id}`"

                elif scope == "channel":
                    channel = guild.get_channel(target_id)
                    if mask and (channel is None or _channel_kind(channel) in (None, "category")):
                        raise ApiError(400, "invalid_target")
                    label = channel.mention if channel else f"`{target_id}`"

                else:  # user
                    if mask and await _get_member(guild, target_id) is None:
                        raise ApiError(400, "unknown_user")
                    label = f"<@{target_id}>"

            if (scope, target_id) in seen:
                raise ApiError(400, "duplicate_target")
            seen.add((scope, target_id))
            planned.append((scope, SCOPES[scope], target_id, mask, label))

        try:
            for _scope, check_type, target_id, mask, _label in planned:
                if mask:
                    permissions.set_permissions(guild.id, check_type, target_id, Permission(mask))
                else:
                    permissions.clear_permissions(guild.id, check_type, target_id)
        except Exception:
            permissions.load_permissions()  # drop half-applied changes: memory must match the database
            raise

        lines = []
        for scope, _check_type, _target_id, mask, label in planned:
            names = describe_permissions(guild.id, Permission(mask)) if mask else []
            lines.append(f"{SCOPE_EMOJI[scope]} {label}: {', '.join(names) if names else '—'}")
        await self.audit(guild, member, "permissions", "\n".join(lines))

        return web.json_response(_serialize_permissions(guild))

    async def search_members(self, request: web.Request) -> web.Response:
        guild, _member, _ = await self.context(request, "permissions")
        query = request.query.get("q", "").strip()
        found = []

        if query.isdigit() and len(query) >= 15:
            candidate = await _get_member(guild, _parse_id(query, code="invalid_id"))
            found = [candidate] if candidate else []
        elif len(query) >= 2:
            needle = query.lower()
            for candidate in guild.members:
                names = (candidate.display_name, candidate.name, getattr(candidate, "global_name", None))
                if any(name and needle in name.lower() for name in names):
                    found.append(candidate)
                    if len(found) >= MEMBER_SEARCH_LIMIT:
                        break

        return web.json_response({"members": [
            {
                "id": str(item.id),
                "name": item.display_name,
                "username": item.name,
                "avatar": _asset_url(item.display_avatar),
                "bot": bool(item.bot),
            }
            for item in found
        ]})

    # -- triggers

    @staticmethod
    def _clean_pattern(raw) -> str:
        if not isinstance(raw, str):
            raise ApiError(400, "empty_pattern")
        pattern = raw.strip()
        if not pattern:
            raise ApiError(400, "empty_pattern")
        if len(pattern) > MAX_PATTERN_LENGTH:
            raise ApiError(400, "pattern_too_long")
        try:
            re.compile(pattern, re.IGNORECASE)  # the bot matches with re.IGNORECASE
        except re.error as error:
            raise ApiError(400, "invalid_regex", str(error)) from None
        return pattern

    @staticmethod
    def _clean_responses(raw) -> list[str]:
        if not isinstance(raw, list) or not raw:
            raise ApiError(400, "no_responses")
        if len(raw) > MAX_RESPONSES_PER_TRIGGER:
            raise ApiError(400, "too_many_responses")

        cleaned: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise ApiError(400, "empty_response")
            text = item.strip()
            if len(text) > MAX_RESPONSE_LENGTH:
                raise ApiError(400, "response_too_long")
            if text in cleaned:
                raise ApiError(400, "duplicate_response")
            cleaned.append(text)
        return cleaned

    @staticmethod
    def _find_key(guild_triggers: dict, pattern: str) -> str | None:
        lowered = pattern.lower()
        return next((key for key in guild_triggers if key.lower() == lowered), None)

    @staticmethod
    def _save_triggers(cog) -> None:
        try:
            save_triggers(cog.triggers)
        except Exception:
            cog.reload_triggers_from_disk()  # drop the unsaved change: memory must match the database
            raise

    async def create_trigger(self, request: web.Request) -> web.Response:
        guild, member, _ = await self.context(request, "triggers")
        body = await _read_json(request)
        cog = self.triggers_cog()

        pattern = self._clean_pattern(body.get("pattern"))
        responses = self._clean_responses(body.get("responses"))

        guild_triggers = cog.get_guild_triggers(guild.id)
        if self._find_key(guild_triggers, pattern) is not None:
            raise ApiError(409, "trigger_exists")
        if len(guild_triggers) >= MAX_TRIGGERS_PER_GUILD:
            raise ApiError(400, "too_many_triggers")

        guild_triggers[pattern] = responses
        self._save_triggers(cog)

        await self.audit(guild, member, "triggers", "➕ `" + pattern.replace("`", "'") + "`")
        return web.json_response({"triggers": _serialize_triggers(cog, guild.id)})

    async def update_trigger(self, request: web.Request) -> web.Response:
        guild, member, _ = await self.context(request, "triggers")
        body = await _read_json(request)
        cog = self.triggers_cog()

        guild_triggers = cog.get_guild_triggers(guild.id)
        old_pattern = body.get("pattern")
        key = self._find_key(guild_triggers, old_pattern) if isinstance(old_pattern, str) else None
        if key is None:
            raise ApiError(404, "trigger_not_found")

        new_pattern = key
        if body.get("new_pattern") is not None:
            new_pattern = self._clean_pattern(body.get("new_pattern"))
            clash = self._find_key(guild_triggers, new_pattern)
            if clash is not None and clash != key:
                raise ApiError(409, "trigger_exists")

        responses = self._clean_responses(body.get("responses"))

        # Rebuild the dict so the trigger keeps its position (/trigger addresses triggers by position).
        cog.triggers[guild.id] = {
            (new_pattern if existing == key else existing): (responses if existing == key else existing_responses)
            for existing, existing_responses in guild_triggers.items()
        }
        self._save_triggers(cog)

        await self.audit(guild, member, "triggers", "✏️ `" + new_pattern.replace("`", "'") + "`")
        return web.json_response({"triggers": _serialize_triggers(cog, guild.id)})

    async def delete_trigger(self, request: web.Request) -> web.Response:
        guild, member, _ = await self.context(request, "triggers")
        body = await _read_json(request)
        cog = self.triggers_cog()

        guild_triggers = cog.get_guild_triggers(guild.id)
        pattern = body.get("pattern")
        key = self._find_key(guild_triggers, pattern) if isinstance(pattern, str) else None
        if key is None:
            raise ApiError(404, "trigger_not_found")

        del guild_triggers[key]
        self._save_triggers(cog)

        await self.audit(guild, member, "triggers", "🗑️ `" + key.replace("`", "'") + "`")
        return web.json_response({"triggers": _serialize_triggers(cog, guild.id)})


def build_app(bot: commands.Bot, token: str | None = None) -> web.Application:
    secret = (token if token is not None else API_TOKEN).encode()

    @web.middleware
    async def errors(request: web.Request, handler):
        try:
            return await handler(request)
        except ApiError as error:
            payload = {"error": error.code}
            if error.detail:
                payload["detail"] = error.detail
            return web.json_response(payload, status=error.status)
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "payload_too_large"}, status=413)
        except web.HTTPException as error:
            return web.json_response({"error": "http_error", "detail": error.reason}, status=error.status)
        except Exception as error:  # noqa: BLE001
            print(f"[dashboard_api] {request.method} {request.path} failed: {error!r}")
            return web.json_response({"error": "internal"}, status=500)

    @web.middleware
    async def auth(request: web.Request, handler):
        scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), secret):
            raise ApiError(401, "unauthorized")
        return await handler(request)

    api = DashboardApi(bot)
    app = web.Application(middlewares=[errors, auth], client_max_size=MAX_BODY_BYTES)
    app.add_routes([
        web.get("/v1/ping", api.ping),
        web.post("/v1/access", api.access),
        web.get("/v1/guilds/{guild_id}", api.snapshot),
        web.put("/v1/guilds/{guild_id}/log-channel", api.put_log_channel),
        web.put("/v1/guilds/{guild_id}/birthday-channel", api.put_birthday_channel),
        web.put("/v1/guilds/{guild_id}/temp-voice", api.put_temp_voice),
        web.patch("/v1/guilds/{guild_id}/permissions", api.patch_permissions),
        web.get("/v1/guilds/{guild_id}/members", api.search_members),
        web.post("/v1/guilds/{guild_id}/triggers", api.create_trigger),
        web.put("/v1/guilds/{guild_id}/triggers", api.update_trigger),
        web.post("/v1/guilds/{guild_id}/triggers/delete", api.delete_trigger),
    ])
    return app


class DashboardApiCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._runner: web.AppRunner | None = None

    @commands.Cog.listener()
    async def on_ready(self):
        if self._runner is not None:  # on_ready fires again after every reconnect
            return

        try:
            runner = web.AppRunner(build_app(self.bot))
            await runner.setup()
            await web.TCPSite(runner, API_HOST, API_PORT).start()
        except OSError as error:
            print(f"❌ Dashboard API could not listen on {API_HOST}:{API_PORT}: {error}")
            return

        self._runner = runner
        print(f"✅ Dashboard API listening on http://{API_HOST}:{API_PORT}")

    def cog_unload(self):
        if self._runner is not None:
            asyncio.ensure_future(self._runner.cleanup())
            self._runner = None


def setup(bot: commands.Bot):
    if len(API_TOKEN) < MIN_TOKEN_LENGTH:
        print(f"ℹ️ Dashboard API is off: set DASHBOARD_API_TOKEN ({MIN_TOKEN_LENGTH}+ characters) in config/.env to enable it.")
        return

    bot.add_cog(DashboardApiCog(bot))
