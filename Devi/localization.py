"""Work with localization configuration for guilds"""

from db import db_cursor

# guild_id -> localization
_localizations: dict[int, str] = {}


def _load_localizations() -> dict[int, str]:
    with db_cursor() as cur:
        cur.execute("SELECT guild_id, localization FROM guild_locale")
        rows = cur.fetchall()
    return {row["guild_id"]: row["localization"] for row in rows}


def _save_localizations(localizations: dict[int, str]):
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM guild_locale")
        cur.executemany(
            "INSERT INTO guild_locale (guild_id, localization) VALUES (?, ?)",
            list(localizations.items()),
        )


_localizations = _load_localizations()


def set_localization(guild_id: int, localization: str) -> None:
    _localizations[guild_id] = localization
    _save_localizations(_localizations)


def get_localization(guild_id: int) -> str:
    from i18n import DEFAULT_LOCALE  # Local import — prevents a circular dependency.
    return _localizations.get(guild_id, DEFAULT_LOCALE)