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

"""Aliases for locales that use another locale's translation file for a client."""
LOCALE_ALIASES: dict[str, str] = {
    "es-ES": "es-419",
    "en-UK": "en-US"
}

"""locale_code → nested dictionary of strings for this language"""
_translations: dict[str, dict] = {}


def _locale_file_path(locale: LocaleLike) -> Path:
    if isinstance(locale, disnake.Locale):
        locale = locale.value

    locale = LOCALE_ALIASES.get(locale, locale)

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
    """(Re)loads all locale files from disk into memory.
    Can be called repeatedly to hot-reload translations without restarting the bot.
    """
    global _translations

    loaded: dict[str, dict] = {}

    for locale in _discover_locale_codes():
        data = load_json_safe(_locale_file_path(locale), None)

        if not data or not isinstance(data, dict):
            logger.warning("Locale '%s' not loaded: file corrupted.", locale)
            continue

        loaded[locale] = data

    if DEFAULT_LOCALE not in loaded:
        logger.warning("DEFAULT_LOCALE '%s' not found in %s — translations unavailable.", DEFAULT_LOCALE, LOCALES_DIR)

    _translations = loaded


def available_locales() -> list[str]:
    """Returns all loaded locale files.
    Aliases are not included because they do not have their own translation files.
    """
    return sorted(_translations.keys())


def available_locale_codes() -> list[str]:
    """Returns all locale codes supported by the translation system, including aliases."""
    return sorted(set(_translations) | set(LOCALE_ALIASES))


def resolve_locale_code(locale: LocaleLike) -> str:
    """Resolves a locale code to the actual locale file used for translation.

    For example:
        es-ES -> es-419
        ru -> ru
    """
    if isinstance(locale, disnake.Locale):
        locale = locale.value

    return LOCALE_ALIASES.get(locale, locale)


def get_locale_display_name(locale: LocaleObject, with_flag: bool = True) -> str:
    """Returns a human-readable locale name.
    Aliases use the metadata of their target locale.
    """
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
    """Returns True if the locale exists directly or is a configured alias."""
    if isinstance(locale, disnake.Locale):
        locale = locale.value

    resolved_locale = resolve_locale_code(locale)

    return resolved_locale in _translations


def _resolve_path(data: dict, dotted_key: str):
    """Traverses nested dictionaries according to a key in the form of 'a.b.c'.
    Returns the found value or None if the path does not exist.
    """
    node = data

    for part in dotted_key.split("."):
        if part == "_meta":
            return None

        if not isinstance(node, dict) or part not in node:
            return None

        node = node[part]

    return node


def _resolve_locale(locale: LocaleObject = None) -> str:
    """Resolves a LocaleObject into a concrete locale file code.

    Priority:
      1. Interaction locale;
      2. explicit locale code;
      3. server locale by guild id;
      4. DEFAULT_LOCALE.

    Locale aliases are resolved automatically.
    """
    if isinstance(locale, Interaction):
        if is_valid_locale(locale.locale):
            return resolve_locale_code(locale.locale)

        locale = locale.guild_id

    if isinstance(locale, disnake.Locale):
        locale = locale.value

    if isinstance(locale, str):
        resolved = resolve_locale_code(locale)

        if resolved in _translations:
            return resolved

    if isinstance(locale, disnake.Guild):
        locale = locale.id

    if isinstance(locale, int):
        from localization import get_localization

        return resolve_locale_code(get_localization(locale))

    return DEFAULT_LOCALE


def get_raw(key: str, locale: LocaleObject = None):
    """Returns the raw value by key.
    Uses the resolved locale and falls back to DEFAULT_LOCALE if necessary.
    """
    resolved = _resolve_locale(locale)

    value = _resolve_path( _translations.get(resolved, {}), key)

    if value is None and resolved != DEFAULT_LOCALE:
        value = _resolve_path(_translations.get(DEFAULT_LOCALE, {}), key)

    return value


def t(key: str, locale: LocaleObject = None, **kwargs) -> str:
    """Returns a translated string by key.
    Locale aliases are resolved automatically."""
    resolved_locale = _resolve_locale(locale)

    value = get_raw(key, locale=resolved_locale)

    if value is None:
        logger.warning("Localization missing '%s' (locale '%s').", key, resolved_locale)
        return f"[{key}]"

    if not isinstance(value, str):
        logger.warning("Value of the key '%s' in locale '%s' is not a string.",key, resolved_locale)
        return f"[{key}]"

    try:
        return value.format(**kwargs)

    except (KeyError, IndexError) as e:
        logger.warning("Error of formating value of key '%s' (locale '%s'): %s",key, resolved_locale, e)
        return value


def ensure_locale_files_exist() -> None:
    """Creates the locales/ folder and the DEFAULT_LOCALE template
    if it does not already exist.
    """
    os.makedirs(LOCALES_DIR, exist_ok=True)

    default_path = _locale_file_path(DEFAULT_LOCALE)

    if not os.path.exists(default_path):
        from json_storage import save_json_atomic

        save_json_atomic(default_path,{ "_meta": { "name": "English", "flag": "🇺🇸"} }, indent=2)


ensure_locale_files_exist()
load_locales()