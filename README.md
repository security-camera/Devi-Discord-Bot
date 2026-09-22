from permissions import validate_permissions

# Devi

A multifunctional Discord bot built on [disnake](https://github.com/DisnakeDev/disnake), providing moderation, automation, an AI assistant, voice features, giveaways and a flexible permission system. Has own [site](https://github.com/security-camera/Devi-Site).

## Table of Contents

- [Features](#features)
- [Configuration](#configuration)
- [Permission System](#-permission-system)
- [i18n System](#i18n-system)
- [Data Persistence](#-work-with-data-saveload)
- [Parsing & Encryption](#-data-parsing-and-encryption)
- [APIs integration](#-other-apis-integration)
- [Project Structure](#-project-structure)

## Features

|    |                                        |
|----|----------------------------------------|
| 📣 | User and role mention management       |
| 💬 | Auto-reply system (triggers)           |
| ⚠️ | Warning system (Warns)                 |
| 🎭 | Temporary roles                        |
| 🔊 | Voice commands and TTS                 |
| 🔐 | Flexible permission system             |
| 📝 | Action logging                         |
| 🎲 | Fun commands                           |
| 🎉 | Giveaway system                        |
| 🎂 | Birthday tracking                      |
| 💬 | AI assistant powered by the Gemini API |

## Configuration

The bot is configured via `config/environment.env`. Two parallel sets of values are supported — production and test — selected at startup by `TEST_ENABLED`.

| Variable                                                     | Description                                                                                                                     |
|--------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| `TEST_ENABLED`                                               | Set to `1`/`true` to run the bot in test mode (uses the `*_TEST` variables below). Defaults to production mode.                 |
| `BOT_TOKEN` / `BOT_TOKEN_TEST`                               | Discord bot token, from the [Discord Developer Portal](https://discord.com/developers/applications).                            |
| `TECHNICAL_SUPPORT_SERVER` / `TECHNICAL_SUPPORT_SERVER_TEST` | Guild ID used for support-related features.                                                                                     |
| `AI_SIGNAL_CHANNEL` / `AI_SIGNAL_CHANNEL_TEST`               | Channel ID the AI cog uses for status/signal messages.                                                                          |
| `TEST_SERVER`                                                | Guild ID used to register commands instantly while developing (guild-scoped commands sync immediately, unlike global commands). |
| `ENCRYPTION_KEY`                                             | Key for [encryption](#-data-parsing-and-encryption)                                                                             |
| `TOP_GG_TOKEN`                                               | [Top.gg API token](#-other-apis-integration)                                                                                    |

> ⚠️ `TEST_ENABLED` is read as a raw string and compared case-insensitively against `1`/`true`/`yes`/`on`. Any other non-empty value (including the literal string `"False"`) is treated as **disabled** — double-check this variable if the bot boots in the wrong mode.

## 🔐 Permission System

The bot uses its own custom permission system, independent of Discord's native role permissions.

### Available permissions

| Permission                             | Description                                                                                                          |
|----------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| `ManageMention`                        | Manage mention commands                                                                                              |
| `MentionBlackList`                     | Blacklisted from using mention commands                                                                              |
| `AiBlackList`                          | Blacklisted from using AI commands                                                                                   |
| `ManageTriggers`                       | Manage auto-replies (triggers)                                                                                       |
| `TTS`                                  | Use text-to-speech                                                                                                   |
| `Send`                                 | Send messages in servers and DMs on behalf of the bot                                                                |
| `Giveaways`                            | Manage giveaways                                                                                                     |
| `Warnings`                             | Manage warnings                                                                                                      |
| `Admin` (and `SpecialAdminPermission`) | Administrative permission set — grants all permissions except blacklist permissions                                  |
| `Developer`                            | Developer permission set. Does **not** include `Admin`. Cannot be granted via command — hardcoded by Discord user ID |

### Permission scopes

| Type      | Description                                                                                                          |
|-----------|----------------------------------------------------------------------------------------------------------------------|
| `User`    | Assigned to a specific user (`user_id`) — personal to that user within the guild                                     |
| `Role`    | Assigned to a role (`role_id`) — applies to all members holding that role                                            |
| `Channel` | Assigned to a channel (`channel_id`) — applies to actions performed in that channel, regardless of who performs them |
| `Guild`   | Assigned to the entire guild (`guild_id`) — the base level, applying to all members by default                       |

### Management functions

#### `save()`
Saves permissions to disk.

#### `load()`
Loads permissions from disk.

#### `get_permissions(guild_id: int, object_type: PermissionCheckType, id: int)`
Returns the permissions of object `id` (of type `object_type`) on guild `guild_id`.

#### `set_permissions(guild_id: int, object_type: PermissionCheckType, id: int, permissions: Permission)`
Sets the permissions of object `id` (of type `object_type`) on guild `guild_id` to `permissions`.

#### `grant_permissions(guild_id: int, object_type: PermissionCheckType, id: int, permission: Permission)`
Grants `permission` to an object of type `object_type` with the given `id` on guild `guild_id`.

#### `revoke_permissions(guild_id: int, object_type: PermissionCheckType, id: int, permission: Permission)`
Revokes `permission` from an object of type `object_type` with the given `id` on guild `guild_id`.

#### `has_permissions(member: disnake.Member, channel: disnake.abc.GuildChannel, permission: Permission, check_type: PermissionCheckType = PermissionCheckType.NONE)`
Returns `True` if `member` has `permission` in `channel`, evaluated with the given `check_type`.

#### `clear_permissions(guild_id: int, object_type: PermissionCheckType, id: int)`
Clears all permissions of object `id` (of type `object_type`) on guild `guild_id`.

#### `validate_permissions(inter, permissions, member=None, channel=None)`

```python
def validate_permissions(
    inter: disnake.ApplicationCommandInteraction,
    permissions: list[dict[Permission | disnake.Permissions | int | flag_value[disnake.Permissions], bool | tuple[bool, PermissionCheckType]]]
                | dict[Permission | disnake.Permissions | int | flag_value[disnake.Permissions], bool | tuple[bool, PermissionCheckType]],
    member: disnake.Member | None = None,
    channel: disnake.abc.GuildChannel | None = None,
)
```

Checks a list of permissions (of **any** type) and returns an error message if `member` doesn't satisfy them.

**Example** — command proceeds only if the user can use AI and either has `TTS` or the `administrator` permission:

```python
if await validate_permissions(inter, [
    {Permission.AiBlackList: False, Permission.TTS: True},
    {Permission.AiBlackList: False, disnake.Permissions(administrator=True): True},
]):
    return None

# command code...
```

> See `permissions.py` for the full documentation of this function.

---

## i18n System

Bot string localization system.

Each language is stored as a separate JSON file under `locales/`:

```
locales/
    ru.json
    en-US.json
    ...
```

The file name (without `.json`) is the disnake locale code (`ru`, `en-US`, …).

The file format is an arbitrarily nested dictionary of strings:

```json
{
    "_meta": {
        "name": "English"
    },
    "warns": {
        "created": "{member} received a warning (ID {id}).",
        "removed": "Warning ID {id} has been removed."
    }
}
```

The `"_meta"` key is reserved for locale metadata and is never returned as a translatable string.

### Types
LocaleLike (object with locale name) - `disnake.Locale | str`

GuildLike (object with guild id) - `disnake.Guild | disnake.Channel | int`

LocaleObject (object which can contain locale) - `LocaleLike (locale name) | GuildLike (guild locale) | Interaction[ClientT] (inter.locale) | None (DEFAULT_LOCALE)`

### Functions

#### `load_locales()`
(Re)loads all locale files from disk into memory

#### `is_valid_locale(locale: LocaleLike) -> bool`
Returns `true` if locale is existing

#### `available_locales() -> list[str]`
Returns all available locales

#### `get_locale_display_name(locale: LocaleLike, with_flag: bool = True) -> str`
Returns human-readable locale name

#### `t(key, locale) -> str`
Returns translated `key`, where it’s a dot-separated path, e.g. `"warns.created"`.

#### `get_raw(key, locale) -> Any`
Returns raw `key` value

> Localization content itself is not part of the codebase — `locales/*.json` files are created empty (aside from `"_meta"`) and must be filled in with translations separately.

### `discord_i18n.py`

Bridge between `i18n.py` and Discord. Contains reusable `disnake.Localized` templates such as `yes_no_choices()`, `add_remove_check_choices()` and `localization_choices()`.

#### `localized(key: str)`
Creates a `disnake.Localized` object from an i18n key, e.g. `localized("commands.help.description")`.

#### `bool_to_yes_no_str(b, locale: LocaleObject = None, inversed: bool = False) -> str`
Converts any object to localized `yes` or `no`

---

## 📁 Work with Data Save/Load

### Work with JSON (`json_storage.py`)

Safely loads and saves JSON.

#### `save_json_atomic(path, data, **json_kwargs)`
Atomically saves JSON: writes to a temporary file in the same directory, flushes it to disk (`fsync`), then replaces the target file via `os.replace`.

Since `os.replace` is atomic at the OS level, an interrupted write (e.g. `ENOSPC` — "No space left on device") never leaves the target file partially written: either the write fully succeeds and the file is updated, or it fails and the original data is preserved untouched.

```python
save_json_atomic(default_path, {"_meta": {"name": "English", "flag": "🇺🇸"}}, indent=2)
```

#### `load_json_safe(path, default)`
Safely loads JSON. If the file is missing or contains invalid data, returns `default` without modifying anything on disk.

```python
data = load_json_safe(_locale_file_path(locale), None)
```

### Work with the Database (`db.py`)

#### `init_db()`
Initializes the database once, from `main.py`.

#### `get_connection()`
Returns a connection to the SQLite3 database.

#### `db_cursor(commit: bool = False)`
Context manager that yields a cursor and closes the connection afterward. Pass `commit=True` for any operation that writes data.

```python
# init_db() must already have run

with db_cursor(commit=True) as cur:
    cur.execute("DELETE FROM log_channels")
    cur.executemany(
        "INSERT INTO log_channels (guild_id, channel_id) VALUES (?, ?)",
        list(log_channels.items()),
    )
```

---

## 🔒 Data Parsing and Encryption

### Encryption (`crypto_utils.py`)

#### `encrypte(plaintext: str)`
Encrypts a string and returns a text token safe for storage in a `TEXT` column.

#### `decrypte(token: str)`
Decrypts a token produced by `encrypte()`. Raises `ValueError` if the token is corrupted or was encrypted with a different key — including if it isn't a Fernet token at all (e.g. an unencrypted value).

### Duration Parsing (`duration_utils.py`)

#### `parse_duration_seconds(duration_str: str)`
Parses strings like `'10м'`, `'2ч'`, `'7d'`, `'30s'`, `'4w'` into seconds. (supports duration chars from ANY locale by `duration_utils.~~~` code)

#### `parse_timedelta(duration_str: str)`
Parses a duration string into a `timedelta`.

#### `parse_duration(duration_str: str)`
Returns `(expires_at_iso, error)`. `expires_at_iso = None` means "forever".

---

## 🖥️ Other APIs integration

### [top.gg API](https://docs.top.gg/api/v1/introduction) `other_apis/topgg_utils.py`

#### `is_voted(user: disnake.User | disnake.Member | int) -> bool`
Checks status of user’s vote.

#### `validate_vote(inter: disnake.ApplicationCommandInteraction)`
Returns a message `top_gg_cog.locked_command` if `is_voted` is `false`.

**Example** — command proceeds only if the user is voted

```python
if await validate_vote(inter):
    return None

# command code...
```

---

## 📁 Project Structure

```
bot_project/
├── README.md
├── LICENCE
├── .gitignore
└── Devi/                          # Core directory
    ├── main.py                    # Entry point
    ├── paths.py                   # Single source of truth for paths
    ├── storage.py                 # Global constants
    ├── json_storage.py            # load_json_safe(), save_json_atomic()
    ├── logs.py                    # Log channel, send_log(), get_audit_executor()
    ├── duration_utils.py          # Parsing "30s"/"10м"/"2ч"/"7d"/"4w"
    ├── permissions.py             # Permission system
    ├── requirements.txt           # Dependencies
    ├── i18n.py                    # i18n system
    ├── discord_i18n.py            # Bridge between i18n and Discord
    ├── db.py                      # Database access
    ├── crypto_utils.py            # Encryption helpers
    ├── other_apis/                # Work with APIs of other projects
    │   └──topgg_utils.py
    ├── config/                    # Bot data
    │   ├── environment.env        # Environment variables
    │   ├── locales/               # Localizations
    │   │   ├── ru.json
    │   │   ├── en-US.json
    │   │   ├── fi.json
    │   │   ├── uk.json
    │   │   └── de.json
    │   └── bot.db                 # Database
    └── cogs/
        ├── ai/
        │   ├── ai_memory.py           # Context for ai.py
        │   ├── ai.py                  # @Mention AI, /ai commands
        │   ├── ai_prompts.py          # Prompts and custom user instructions
        │   └── ai_api_keys.py         # Guild API keys
        ├── admin.py                   # /set_log_channel, /set_language
        ├── mentions.py                # /mention commands
        ├── triggers.py                # /trigger, /triggers + trigger logic
        ├── utils.py                     # /random, /coin, /ball, /qr, /preview, /avatar
        ├── warns.py                   # /warn commands + logic
        ├── temp_roles.py              # /temp_role + logic
        ├── clear.py                   # /clear
        ├── logging_events.py          # Logging logic
        ├── permissions_commands.py    # /permissions commands
        ├── giveaways.py               # /giveaway commands + logic
        ├── send.py                    # /send commands
        ├── help.py                    # /help, /command_id
        ├── birthdays.py               # /birthday commands + logic
        ├── developer.py               # Developer-only commands
        ├── music.py                   # /music commands + logic
        ├── topgg.py                   # /vote
        ├── temp_voices.py             # /voice commands + temp voices logic
        └── voice.py                   # /join, /leave, /tts
```