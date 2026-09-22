"""Parses strings '10m', '2h', '7d', '30s', '4w' — the unit letters depend on the server's locale (see duration_utils.duration_units_chars in each locales/*.json)."""

import re
from datetime import datetime, timedelta, timezone

import i18n
from i18n import LocaleObject

# Unit key -> seconds. Locale-INDEPENDENT — only the single-letter symbol
# attached to each key changes between locales, not how many seconds it is.
UNIT_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "year": 31536000,
}

_UNIT_ORDER = ("second", "minute", "hour", "day", "week", "year")
_EXAMPLE_AMOUNT = {"second": 30, "minute": 10, "hour": 2, "day": 5, "week": 4, "year": 2}

_FALLBACK_CHARS = {"second": "s", "minute": "m", "hour": "h", "day": "d", "week": "w", "year": "y"}


def _duration_chars_for_locale(locale: LocaleObject) -> dict[str, str]:
    """unit_key -> single-letter symbol for this locale"""
    raw = i18n.get_raw("duration_utils.duration_units_chars", locale=locale)

    if not isinstance(raw, dict) or not raw:
        return dict(_FALLBACK_CHARS)

    chars = {unit: str(raw[unit]).lower() for unit in _UNIT_ORDER if raw.get(unit)}
    return chars or dict(_FALLBACK_CHARS)


def _duration_units_map(locale: LocaleObject) -> dict[str, int]:
    """symbol -> seconds, for this specific locale."""
    chars = _duration_chars_for_locale(locale)
    return {char: UNIT_SECONDS[unit] for unit, char in chars.items()}


def _format_hint(locale: LocaleObject, invalid: str) -> str:
    chars = _duration_chars_for_locale(locale)
    examples = ", ".join(f"`{_EXAMPLE_AMOUNT[unit]}{char} ({i18n.t('duration_utils.duration_units.' + unit, locale=locale)})`" for unit, char in chars.items())
    return i18n.t("duration_utils.invalid_format", locale=locale, invalid=invalid, examples=examples)


def parse_duration_seconds(duration_str: str, locale: LocaleObject = None) -> "tuple[int | None, str | None]":
    """Parses strings like '10m' into seconds, using the unit letters of `locale`."""
    cleaned = duration_str.strip().lower()

    units = _duration_units_map(locale)
    unit_chars = "".join(re.escape(char) for char in units)

    match = re.fullmatch(rf"(\d+)\s*([{unit_chars}])", cleaned)
    if not match:
        return None, _format_hint(locale, duration_str)

    amount, unit = match.groups()
    return int(amount) * units[unit], None


def parse_duration(duration_str: str, locale: LocaleObject = None) -> "tuple[str | None, str | None]":
    """Returns (expires_at_iso, error). expires_at_iso = None means "forever"."""
    cleaned = duration_str.strip().lower()

    if not cleaned:
        return None, None

    seconds, error = parse_duration_seconds(cleaned, locale=locale)
    if error:
        return None, error

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return expires_at.isoformat(), None


def parse_timedelta(duration_str: str, locale: LocaleObject = None) -> "tuple[timedelta | None, str | None]":
    """Parses a string into a timedelta."""
    seconds, error = parse_duration_seconds(duration_str, locale=locale)
    if error:
        return None, error
    return timedelta(seconds=seconds), None