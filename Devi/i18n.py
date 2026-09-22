"""Bot string localization system."""

import os
import logging
from pathlib import Path
from typing import Any

import disnake
from disnake import Interaction
from disnake.interactions.base import ClientT

from paths import LOCALES_DIR
from json_storage import load_json_safe

"""Object with locale name"""
LocaleLike = str | disnake.Locale

"""Object with guild id"""
GuildLike = int | disnake.Guild

"""Object which can contain information about locale"""
LocaleObject = LocaleLike | GuildLike | Interaction[ClientT] | None

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "en-US"

"""locale_code → nested dictionary of strings for this language"""
_translations: dict[str, dict] = {}


def _locale_file_path(locale: LocaleLike) -> Path:
    if isinstance(locale, disnake.Locale):
        locale = locale.value
    return LOCALES_DIR / f"{locale}.json"


def _discover_locale_codes() -> list[str]:
    """Finds all *.json files in the locales/ folder and returns their names without the extension."""
    if not os.path.isdir(LOCALES_DIR):
        return []

    return sorted(
        os.path.splitext(filename)[0]
        for filename in os.listdir(LOCALES_DIR)
        if filename.endswith(".json")
    )


def load_locales() -> None:
    """(Re)loads all locale files from disk into memory. Can be called repeatedly
    to hot-reload translations without restarting the bot."""
    global _translations

    loaded: dict[str, dict] = {}

    for locale in _discover_locale_codes():
        data = load_json_safe(_locale_file_path(locale), None)
        if data is None or not isinstance(data, dict):
            logger.warning("Locale '%s' not loaded: file corrupted.", locale)
            continue
        loaded[locale] = data

    if DEFAULT_LOCALE not in loaded:
        logger.warning(
            "DEFAULT_LOCALE '%s' not found in %s — translations unavailable.",
            DEFAULT_LOCALE, LOCALES_DIR
        )

    _translations = loaded


def available_locales() -> list[str]:
    """List of all loaded locales."""
    return sorted(_translations.keys())


def get_locale_display_name(locale: LocaleObject, with_flag: bool = True) -> str:
    """Human-readable locale name. Taken from "_meta.name" in the locale file; if it is missing, the locale code
    itself is returned."""
    resolved_locale = _resolve_locale(locale)

    meta = _translations.get(resolved_locale, {}).get("_meta", {})
    if isinstance(meta, dict):
        name = meta.get("name")
        flag = meta.get("flag")
        if isinstance(name, str) and name:
            if with_flag and isinstance(flag, str) and flag:
                return f"{flag} {name}"
            return name
    return resolved_locale


def is_valid_locale(locale: LocaleLike) -> bool:
    if isinstance(locale, disnake.Locale):
        locale = locale.value
    return locale in _translations


def _resolve_path(data: dict, dotted_key: str):
    """Traverses nested dictionaries according to a key in the form of 'a.b.c'.
    Returns the found value or None if the path does not exist."""
    node = data
    for part in dotted_key.split("."):
        if part == "_meta":
            return None  # meta data
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _resolve_locale(locale: LocaleObject = None) -> str:
    """Resolves a polymorphic LocaleObject into a concrete locale code.

    Priority:
      1. inter locale -> str (locale code) or int (guild id);
      2. explicit locale code (str) or disnake.Locale;
      2. server locale by guild id (int) or disnake.Guild, via localization.get_localization;
      3. DEFAULT_LOCALE (used both as the final fallback and when nothing usable was given).
    """
    if isinstance(locale, Interaction):
        if is_valid_locale(locale.locale):
            return locale.locale.value
        else:
            locale = locale.guild_id

    if isinstance(locale, disnake.Locale):
        locale = locale.value

    if isinstance(locale, str) and is_valid_locale(locale):
        return locale

    if isinstance(locale, disnake.Guild):
        locale = locale.id

    if isinstance(locale, int):
        from localization import get_localization  # Local import — prevents a circular dependency.
        return get_localization(locale)

    return DEFAULT_LOCALE


def get_raw(key: str, locale: LocaleObject = None):
    """Returns the raw value by key (string, list, number — anything from JSON),
    without formatting. Useful for lists (for example, /ball answer options).

    Uses the same locale resolution logic as t(): explicit locale/disnake.Locale ->
    server locale by guild id/disnake.Guild -> DEFAULT_LOCALE, with fallback to
    DEFAULT_LOCALE if the key is not found in the selected locale.

    Returns None if the key is not found anywhere."""
    resolved = _resolve_locale(locale)

    value = _resolve_path(_translations.get(resolved, {}), key)

    if value is None and resolved != DEFAULT_LOCALE:
        value = _resolve_path(_translations.get(DEFAULT_LOCALE, {}), key)

    return value


def t(key: str, locale: LocaleObject = None, **kwargs) -> str:
    """Returns a translated string by key (dot-separated path, for example
    "warns.created"), substituting kwargs using str.format.

    Locale resolution order:
      1. explicitly provided `locale` (str or disnake.Locale);
      2. server locale by guild id (int or disnake.Guild), via localization.get_localization;
      3. DEFAULT_LOCALE.

    If the key is not found in the selected locale, DEFAULT_LOCALE is used.

    If the key is not found anywhere, the key itself is returned in square
    brackets so missing translations are easy to notice in chat instead of
    causing the command to fail.
    """
    resolved_locale = _resolve_locale(locale)

    value = get_raw(key, locale=resolved_locale)

    if value is None:
        logger.warning("Localization missing '%s' (locale '%s').", key, resolved_locale)
        return f"[{key}]"

    if not isinstance(value, str):
        logger.warning("Value of the key '%s' in locale '%s' is not a string.", key, resolved_locale)
        return f"[{key}]"

    try:
        return value.format(**kwargs)
    except (KeyError, IndexError) as e:
        logger.warning("Error of formating value of key '%s' (locale '%s'): %s", key, resolved_locale, e)
        return value


def ensure_locale_files_exist() -> None:
    """Creates the locales/ folder and empty template files for DEFAULT_LOCALE
    if they do not already exist. Does not overwrite existing files."""
    os.makedirs(LOCALES_DIR, exist_ok=True)

    default_path = _locale_file_path(DEFAULT_LOCALE)
    if not os.path.exists(default_path):
        from json_storage import save_json_atomic
        save_json_atomic(default_path, {"_meta": {"name": "English", "flag": "🇺🇸"}}, indent=2)


ensure_locale_files_exist()
load_locales()