"""Permissions system"""

from disnake.flags import flag_value

from enum import IntFlag, IntEnum
from typing import Optional

import disnake

import i18n
from db import db_cursor

_bot: Optional[disnake.Client] = None


def init(bot: disnake.Client) -> None:
    """Call once when the bot starts — required so get_permissions can retrieve the user's roles."""
    global _bot
    _bot = bot
    load_permissions()


class Permission(IntFlag):
    NONE = 0
    Developer = -1

    ManageMention = 1 << 0
    MentionBlackList = 1 << 1
    ManageTriggers = 1 << 2
    TTS = 1 << 3
    Send = 1 << 4
    SpecialAdminPermission = 1 << 5
    AiBlackList = 1 << 6
    Giveaways = 1 << 7
    Warnings = 1 << 8
    MusicBlackList = 1 << 9

    Admin = (
            ManageMention
            | ManageTriggers
            | TTS
            | Send
            | SpecialAdminPermission
            | Giveaways
            | Warnings
    )


class PermissionCheckType(IntEnum):
    NONE = 0    # set/get_permissions -> exception
    User = 1
    Role = 2
    Channel = 3
    Guild = 4   # Guild-wide permission

DEVELOPER_LIST = [695556106591928351]
ADMIN_EXCLUDED = Permission.MentionBlackList | Permission.AiBlackList | Permission.MusicBlackList | Permission.Developer


# guild_id -> {"guild": Permission, "users": {...}, "roles": {...}, "channels": {...}}
_permissions: dict[int, dict] = {}


def load_permissions() -> None:
    global _permissions

    with db_cursor() as cur:
        cur.execute("SELECT guild_id, target_type, target_id, value FROM permission_grants")
        rows = cur.fetchall()

    _permissions = {}
    for row in rows:
        gid = row["guild_id"]
        gp = _permissions.setdefault(
            gid, {"guild": Permission.NONE, "users": {}, "roles": {}, "channels": {}}
        )
        value = Permission(row["value"])
        target_type = row["target_type"]

        if target_type == "guild":
            gp["guild"] = value
        elif target_type == "user":
            gp["users"][row["target_id"]] = value
        elif target_type == "role":
            gp["roles"][row["target_id"]] = value
        elif target_type == "channel":
            gp["channels"][row["target_id"]] = value


def save_permissions() -> None:
    rows = []
    for guild_id, gp in _permissions.items():
        if gp["guild"] != Permission.NONE:
            rows.append((guild_id, "guild", guild_id, int(gp["guild"])))
        for uid, v in gp["users"].items():
            rows.append((guild_id, "user", uid, int(v)))
        for rid, v in gp["roles"].items():
            rows.append((guild_id, "role", rid, int(v)))
        for cid, v in gp["channels"].items():
            rows.append((guild_id, "channel", cid, int(v)))

    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM permission_grants")
        if rows:
            cur.executemany(
                "INSERT INTO permission_grants (guild_id, target_type, target_id, value) VALUES (?, ?, ?, ?)",
                rows,
            )


def _target_dict(gp: dict, object_type: PermissionCheckType) -> dict[int, Permission] | Permission:
    """Returns target dict of permissions for an object type. Returns Permission value if object type is a guild"""
    match object_type:
        case PermissionCheckType.User:
            return gp["users"]
        case PermissionCheckType.Role:
            return gp["roles"]
        case PermissionCheckType.Channel:
            return gp["channels"]
        case PermissionCheckType.Guild:
            return gp["guild"]
        case _:
            raise ValueError(f"Unsupported objectType: {object_type}")


def set_permissions(
    guild_id: int,
    object_type: PermissionCheckType,
    id: int,
    permissions: Permission,
) -> None:
    if object_type == PermissionCheckType.NONE:
        raise ValueError("PermissionCheckType.NONE is not a valid target for set_permissions")

    gp = _permissions.setdefault(
        guild_id,
        {"guild": Permission.NONE, "users": {}, "roles": {}, "channels": {}},
    )

    if object_type == PermissionCheckType.Guild:
        gp["guild"] = permissions
    else:
        _target_dict(gp, object_type)[id] = permissions

    save_permissions()


def _user_roles_permission(guild_id: int, gp: dict, user_id: int) -> Permission:
    if _bot is None:
        return Permission.NONE

    guild = _bot.get_guild(guild_id)
    if guild is None:
        return Permission.NONE

    member = guild.get_member(user_id)
    if member is None:
        return Permission.NONE

    combined = Permission.NONE
    for role in member.roles:
        combined |= gp["roles"].get(role.id, Permission.NONE)
    return combined


def get_permissions(guild_id: int, object_type: PermissionCheckType, id: int) -> Permission:
    if object_type == PermissionCheckType.NONE:
        raise ValueError("PermissionCheckType.NONE is not a valid target for get_permissions")

    gp = _permissions.get(guild_id)
    if gp is None:
        return Permission.NONE

    if object_type == PermissionCheckType.Guild:
        return gp["guild"]

    if object_type == PermissionCheckType.NONE:
        # Object here is user WITH ALL OTHER PERMISSIONS (guild+personal+roles) INCLUDED
        return (
            gp["guild"]
            | gp["users"].get(id, Permission.NONE)
            | _user_roles_permission(guild_id, gp, id)
        )

    return _target_dict(gp, object_type).get(id, Permission.NONE)

def _resolve_permission(channel: disnake.abc.GuildChannel, member: disnake.Member, check_type: PermissionCheckType) -> Permission:
    guild_id = member.guild.id

    if check_type == PermissionCheckType.Guild:
        return get_permissions(guild_id, PermissionCheckType.Guild, guild_id)

    if check_type == PermissionCheckType.User:
        return get_permissions(guild_id, PermissionCheckType.User, member.id)

    if check_type == PermissionCheckType.Role:
        gp = _permissions.get(guild_id)
        if gp is None:
            return Permission.NONE
        combined = Permission.NONE
        for role in member.roles:
            combined |= gp["roles"].get(role.id, Permission.NONE)
        return combined

    if check_type == PermissionCheckType.Channel:
        return get_permissions(guild_id, PermissionCheckType.Channel, channel.id)

    # NONE -> get_permissions(User) already includes guild + personal + role permissions; only add channel permissions
    combined = get_permissions(guild_id, PermissionCheckType.User, member.id)
    combined |= get_permissions(guild_id, PermissionCheckType.Channel, channel.id)

    return combined

def has_permissions(
    member: disnake.Member,
    channel: disnake.abc.GuildChannel,
    permission: Permission,
    check_type: PermissionCheckType = PermissionCheckType.NONE,
) -> bool:
    if permission == Permission.Developer:
        return member.id in DEVELOPER_LIST

    resolved = _resolve_permission(channel, member, check_type)

    is_admin = (resolved & Permission.Admin) == Permission.Admin

    if is_admin:
        if permission & ADMIN_EXCLUDED:
            return (resolved & permission) == permission
        return True

    return (resolved & permission) == permission

def permissions_to_text(perms: disnake.Permissions | int, *, separator: str = ", ") -> str:
    """List of permissions in a readable format: 'Administrator, Manage Roles, ...'"""
    if isinstance(perms, int):
        perms = disnake.Permissions(perms)

    enabled_names = [name for name, value in perms if value]

    if not enabled_names:
        return NONE_PLACEHOLDER

    return separator.join(name for name in enabled_names)

async def validate_permissions(
    inter: disnake.ApplicationCommandInteraction,
    permissions: list[dict[Permission | disnake.Permissions | int | flag_value[disnake.Permissions], bool | tuple[bool, PermissionCheckType]]]
                | dict[Permission | disnake.Permissions | int | flag_value[disnake.Permissions], bool | tuple[bool, PermissionCheckType]],
    member: disnake.Member | None = None,
    channel: disnake.abc.GuildChannel | None = None,
) -> bool:
    """
    Semantics: keys within the same dict are AND (a group is considered passed
    only if ALL conditions in it are satisfied). List elements are OR
    (the user is granted access if at least one group passes completely).

    Returns True if access is DENIED (the interaction response has already been
    sent — the calling code must immediately `return`). Returns False if at
    least one group passes completely — no message is sent in this case.

    If access is denied, the message displays the COMPLETE requirements tree,
    for example: (1 AND [NO] 2) OR ([NO] 3 AND 4) OR 5 OR [NO] 6
    """
    member = member or inter.author
    channel = channel or inter.channel

    if isinstance(permissions, dict):  # Only 1 group
        permissions = [permissions]

    failed_groups: list[list[str]] = []

    for group in permissions:
        group_passed = True
        group_names: list[str] = []

        for permission, value in group.items():
            try:
                must_have_permission, check_type = value
            except TypeError:
                must_have_permission, check_type = value, PermissionCheckType.NONE

            has_permission = not must_have_permission  # default if permission is incorrect
            permission_name = "Error"

            guild_permission = None
            if isinstance(permission, disnake.Permissions):
                guild_permission = permission
                permission = permission.value

            if isinstance(permission, Permission):
                has_permission = has_permissions(member, channel, permission, check_type)
                permission_name = f"bot.{permission.name}" if must_have_permission else f"[NO] bot.{permission.name}"

            elif isinstance(permission, int):
                has_permission = member.guild_permissions.administrator or (member.guild_permissions.value & permission) == permission
                readable = permissions_to_text(guild_permission or disnake.Permissions(permission))
                permission_name = f"guild.{readable}" if must_have_permission else f"[NO] guild.{readable}"

            # The condition name is always included — it describes the requirement,
            # not the result of a specific check, and is needed for the tree when access is denied
            group_names.append(permission_name)

            if must_have_permission != has_permission:
                group_passed = False

        if group_passed:
            return False  # This group (AND) passed completely -> access is granted, send nothing

        failed_groups.append(group_names)

    # None of the groups (OR) passed completely -> access is denied,
    # respond EXACTLY ONCE, show the entire requirements tree
    permission_tree = " OR ".join(
        names[0] if len(names) == 1 else "(" + " AND ".join(names) + ")"
        for names in failed_groups
    )

    access_denied = i18n.t("commands.common.access_denied", locale=inter.guild_id, permission=permission_tree)

    if inter.response.is_done():
        await inter.edit_original_response(content=access_denied, embed=None, embeds=[], components=[])
    else:
        await inter.response.send_message(content=access_denied, ephemeral=True)

    return True

def grant_permissions(
    guild_id: int,
    object_type: PermissionCheckType,
    id: int,
    permission: Permission,
) -> None:
    if object_type == PermissionCheckType.NONE:
        raise ValueError("PermissionCheckType.NONE is not a valid target for grant_permissions")

    gp = _permissions.setdefault(
        guild_id,
        {"guild": Permission.NONE, "users": {}, "roles": {}, "channels": {}},
    )

    if object_type == PermissionCheckType.Guild:
        gp["guild"] |= permission
    else:
        target_dict = _target_dict(gp, object_type)
        target_dict[id] = target_dict.get(id, Permission.NONE) | permission

    save_permissions()


def revoke_permissions(
    guild_id: int,
    object_type: PermissionCheckType,
    id: int,
    permission: Permission,
) -> None:
    if object_type == PermissionCheckType.NONE:
        raise ValueError("PermissionCheckType.NONE is not a valid target for revoke_permissions")

    gp = _permissions.get(guild_id)
    if gp is None:
        return

    if object_type == PermissionCheckType.Guild:
        gp["guild"] &= ~permission
    else:
        target_dict = _target_dict(gp, object_type)
        if id not in target_dict:
            return
        target_dict[id] &= ~permission

    save_permissions()


def clear_permissions(
    guild_id: int,
    object_type: PermissionCheckType,
    id: int,
) -> None:
    if object_type == PermissionCheckType.NONE:
        raise ValueError("PermissionCheckType.NONE is not a valid target for clear_permissions")

    gp = _permissions.get(guild_id)
    if gp is None:
        return

    if object_type == PermissionCheckType.Guild:
        gp["guild"] = Permission.NONE
    else:
        _target_dict(gp, object_type).pop(id, None)

    save_permissions()


def all_permissions(guild_id: int) -> dict:
    gp = _permissions.get(guild_id)
    if gp is None:
        return {"guild": Permission.NONE, "users": {}, "roles": {}, "channels": {}}

    return {
        "guild": gp["guild"],
        "users": gp["users"].copy(),
        "roles": gp["roles"].copy(),
        "channels": gp["channels"].copy(),
    }