# Devi

A multifunctional Discord bot built on [disnake](https://github.com/DisnakeDev/disnake), providing moderation, automation, an AI assistant, voice features, giveaways and a flexible permission system. It has own [site](https://github.com/security-camera/Devi-Site) with dashboard.

## Table of Contents

- [Features](#features)
- [Configuration](#configuration)
- [Permission System](#-permission-system)
- [i18n System](#i18n-system)
- [Data Persistence](#-work-with-data-saveload)
- [Parsing & Encryption](#-data-parsing-and-encryption)
- [APIs integration](#-other-apis-integration)
- [Cogs details](#cogs-details)
- [Project Structure](#-project-structure)

## Features

|    |                                        |
|----|----------------------------------------|
| 📣 | User and role mentions                 |
| 💬 | Auto-reply system (triggers)           |
| ⚠️ | Warning system (Warns)                 |
| 🎭 | Temporary roles and bans               |
| 🔊 | Voice commands and TTS                 |
| 🔐 | Flexible custom permission system      |
| 📝 | Action logging                         |
| 🎲 | Fun commands                           |
| 🎉 | Giveaway system                        |
| 🎂 | Birthday tracking                      |
| 💬 | AI assistant powered by the Gemini API |
| 🛡 | Honeypot channels                      |

## Configuration

The bot is configured via `config/.env`. Two parallel sets of values are supported — production and test — selected at startup by `TEST_ENABLED`.

| Variable                                                     | Description                                                                                                                     |
|--------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| `TEST_ENABLED`                                               | Set to `1`/`true` to run the bot in test mode (uses the `*_TEST` variables below). Defaults to production mode.                 |
| `BOT_TOKEN` / `BOT_TOKEN_TEST`                               | Discord bot token, from the [Discord Developer Portal](https://discord.com/developers/applications).                            |
| `TECHNICAL_SUPPORT_SERVER` / `TECHNICAL_SUPPORT_SERVER_TEST` | Guild ID used for support-related features.                                                                                     |
| `AI_SIGNAL_CHANNEL` / `AI_SIGNAL_CHANNEL_TEST`               | Channel ID the AI cog uses for status/signal messages.                                                                          |
| `TEST_SERVER`                                                | Guild ID used to register commands instantly while developing (guild-scoped commands sync immediately, unlike global commands). |
| `DATABASE_URL`                                               | URL to PostgreSQL DB                                                                                                            |
| `ENCRYPTION_KEY`                                             | Key for [encryption](#-data-parsing-and-encryption)                                                                             |
| `TOP_GG_TOKEN`                                               | [Top.gg API token](#-other-apis-integration)                                                                                    |
| `DASHBOARD_API_TOKEN`                                        | Shared secret of the [dashboard API](#-dashboard-api-cogsdashboard_apipy) (24+ characters). Empty — the API is off.             |
| `DASHBOARD_API_HOST` / `DASHBOARD_API_PORT`                  | Address the dashboard API listens on. Default `127.0.0.1:8765`.                                                                 |

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
Saves permissions to DB.

#### `load()`
Loads permissions from DB.

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
if not await validate_permissions(inter, [
    {Permission.AiBlackList: False, Permission.TTS: True},
    {Permission.AiBlackList: False, disnake.Permissions(administrator=True): True},
]):
    return None

# command code...
```

> See `permissions.py` for the full documentation of this function.

#### `@require_permissions(permissions, *, member: str | None = None, channel: str | None = None)`
Method proceeds only if `validate_permissions(inter, permissions, member, channel)` is `true`

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

Devi stores its data in PostgreSQL. Access goes through [psycopg 3](https://www.psycopg.org/psycopg3/) with a connection pool (`psycopg_pool`). The connection string is read from the `DATABASE_URL` environment variable (see [Configuration](#configuration)):

```env
DATABASE_URL=postgresql://botuser:password@localhost:5432/botdb
```

How it works:

- The pool is created lazily on the first query (1 to 10 connections) and is thread-safe.
- Autocommit is off. Changes are saved only when a cursor is opened with `commit=True`.
- Rows are returned as `Row` objects that can be accessed by index and by column name, like `sqlite3.Row`.
- All calls are blocking (synchronous), so keep queries short inside event handlers and commands.

#### `get_dsn() -> str`

Returns the connection string from `DATABASE_URL`

#### `db_cursor(commit: bool = False)`

Context manager that yields a cursor and returns the connection to the pool afterwards. Pass `commit=True` for any operation that writes data.

```python
# Read
with db_cursor() as cur:
    cur.execute("SELECT channel_id FROM log_channels WHERE guild_id = %s", (guild_id,))
    row = cur.fetchone()
    if row:
        channel_id = row["channel_id"]  # same as row[0]

# Write
with db_cursor(commit=True) as cur:
    cur.execute("DELETE FROM log_channels")
    cur.executemany(
        "INSERT INTO log_channels (guild_id, channel_id) VALUES (%s, %s)",
        list(log_channels.items()),
    )
```

#### `Row`

Result row type. It is a `tuple` subclass that also supports access by column name:

```python
row[0]              # by index
row["channel_id"]   # by column name
row.keys()          # list of column names
dict(row)           # {"guild_id": ..., "channel_id": ...}
```

Accessing a column that does not exist raises `IndexError`.

#### `get_connection() -> psycopg.Connection`

Opens a standalone (non-pooled) connection that returns `Row` objects. The caller is responsible for closing it.

#### `close()`

Closes the connection pool. Call it when the bot shuts down. It is safe to call even if the pool was never opened.

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
if not await validate_vote(inter):
    return None

# command code...
```

#### `@require_vote`
Method proceeds only if `validate_vote(inter.author)`

#### `vote_value(user: disnake.User | disnake.Member | int, voted, not_voted, locale: LocaleObject = None) -> tuple[object, str]`
Returns `(voted, None)` if `is_voted` is `true`. Returns `(not_voted, top_gg_cog.voting_ad message)` if `is_voted` is `false`.

**Example**

```python
max_count, ad = await vote_value(user, MAX_COUNT_VOTED, MAX_COUNT, locale=locale)
if count > max_count:
    return inter.response.send_message(i18n.t("...", locale=locale) + ad)
```

---

## Cogs details

### 🔊 Text-To-Speech (`cogs/voice.py`)

`/tts` speaks text in the voice channel the bot is connected to. Synthesis is done **fully offline** by [Piper](https://github.com/OHF-voice/piper1-gpl) (`piper-tts`): no API keys, no network calls at runtime, no per-character costs.

#### Commands

| Command                  | Description                                                                            |
|--------------------------|----------------------------------------------------------------------------------------|
| `/join [channel]`        | Connects to the given voice channel (or the one the author is in)                      |
| `/leave`                 | Disconnects and clears the TTS queue                                                   |
| `/tts <text> [language]` | Queues `text` for speech. `language` is optional and defaults to the server's language |

`/tts` requires the `TTS` permission (or Discord's `administrator`). Text length is limited to `MAX_TTS_LENGTH` characters, or `MAX_TTS_LENGTH_VOTED` for users who [voted on top.gg](#-other-apis-integration). The bot leaves automatically after `EMPTY_CHANNEL_TIMEOUT` seconds in an empty channel.

#### How it works

- Every guild has its own queue and a background worker. Items are `(text, voice)` pairs, so messages in the same queue can use different languages.
- The next item is synthesized while the current one is playing.
- If music is playing on the same voice client, it is paused for the TTS clip and resumed afterwards.
- Loaded models are cached in memory (the first use of a voice takes a few seconds).
- All Piper calls go through a **single-thread executor**: Piper phonemizes via espeak-ng, whose C API is not thread-safe.

#### Voice selection

The voice is a Piper model name (file name without `.onnx`) stored in each locale file under the `voice_cog.tts-voice` key:

```json
{
    "voice_cog": {
        "tts-voice": "en_US-ryan-high"
    }
}
```

Resolution order for a request:

1. The `language` chosen in `/tts`, or the server's locale if none was chosen
2. `DEFAULT_LOCALE`
3. The `FALLBACK_VOICE` optional environment variable

Each step is used only if the model file actually exists in `PIPER_VOICES_DIR`.

#### Setup new voices

1. Download voices from [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main). **Both files** are required per voice: `<name>.onnx` and `<name>.onnx.json`. Put them into `PIPER_VOICES_DIR` (`config/voices` by default).
2. Set `voice_cog.tts-voice` in every locale file you want to support.

| Variable                | Default  | Description                                                              |
|-------------------------|----------|--------------------------------------------------------------------------|
| `FALLBACK_VOICE`        | `(None)` | Fallback voice used when a locale has no usable voice                    |
| `MAX_TTS_LENGTH`        | `400`    | Max text length for regular users                                        |
| `MAX_TTS_LENGTH_VOTED`  | `800`    | Max text length for users who voted on top.gg                            |
| `EMPTY_CHANNEL_TIMEOUT` | `60`     | Seconds in an empty voice channel before the bot leaves                  |
| `TTS_PLAYBACK_TIMEOUT`  | `60`     | Safety net: force-stops playback if the clip never reports completion    |

> ⚠️ On Windows,  keep the virtual environment (and preferably `PIPER_VOICES_DIR`) at a path with **only ASCII characters**. espeak-ng cannot read its data from paths with e.g. Cyrillic letters and fails with `Error processing file '...\phontab': No such file or directory`.

### 🖥️ Dashboard API (`cogs/dashboard_api.py`)

Internal HTTP API used by the [website](https://github.com/security-camera/Devi-Site) dashboard. The site signs users in with Discord OAuth2 and calls this API with a shared secret and the id of the signed-in user; it never touches the database or the bot token.

Changes go through the same in-memory caches and checks as the slash commands (the bot keeps permissions, log channels and triggers in memory and rewrites the whole table on every save, so the site must not write to the database directly). Access mirrors the commands:

| Dashboard section                         | Required                                   |
|-------------------------------------------|--------------------------------------------|
| Permissions, logs, birthdays, voice lobby | `bot.Admin` or Discord `Administrator`     |
| Honeypot, language                        | `bot.Admin` or Discord `Administrator`     |
| Triggers                                  | `bot.ManageTriggers` or `Administrator`    |

Every change is also written to the server's log channel.

Enable it by setting `DASHBOARD_API_TOKEN` (24+ characters) in `config/.env`; use the same value as `BOT_API_TOKEN` on the site. The API has no TLS and no rate limiting — keep it on `127.0.0.1` or a private network.

| Method and path                                                 | Description                                                                 |
|-----------------------------------------------------------------|-----------------------------------------------------------------------------|
| `POST /v1/access`                                               | Which of the given guilds have the bot and which sections the user may open |
| `GET /v1/guilds/{id}`                                           | Settings, channels, roles and limits of one guild                           |
| `PUT /v1/guilds/{id}/log-channel`                               | Set or clear the log channel                                                |
| `PUT /v1/guilds/{id}/birthday-channel`                          | Set or clear the birthday channel                                           |
| `PUT /v1/guilds/{id}/temp-voice`                                | Set or clear the lobby channel, category and name template                  |
| `PUT /v1/guilds/{id}/honeypot`                                  | Set or clear the honeypot channel and its punishment and duration           |
| `PUT /v1/guilds/{id}/language`                                  | Set the server language (`locale`)                                          |
| `PATCH /v1/guilds/{id}/permissions`                             | Set permission masks for the server, roles, channels and users              |
| `GET /v1/guilds/{id}/members?q=`                                | Search members by name or id                                                |
| `POST/PUT /v1/guilds/{id}/triggers`, `POST .../triggers/delete` | Create, edit and delete triggers                                            |

All requests need `Authorization: Bearer <token>` and `X-Discord-User-Id`. Snowflake ids are strings.

---

## 📁 Project Structure

```
bot_project/
├── README.md
├── LICENSE.md
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
    │   ├── .env                   # Environment variables
    │   ├── voices/                # TTS voices
    │   │   ├── voice-1.onnx
    │   │   ├── voice-1.onnx.json
    │   │   └── ...
    │   └── locales/               # Localizations
    │       ├── disnake-locale-code_1.json
    │       ├── disnake-locale-code_2.json
    │       └── ...
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
        ├── temp_bans.py               # /temp_ban + logic
        ├── traps.py                   # /trap commands + logic
        ├── dashboard_api.py           # Internal HTTP API for the website dashboar
        └── voice.py                   # /join, /leave, /tts
```
