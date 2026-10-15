"""Work with application`s emojis"""

from __future__ import annotations

import logging
import re

import disnake

logger = logging.getLogger(__name__)

# name -> discord emoji, for example: mark -> <:mark:123456789012345678>.
DEVI_EMOJIS: dict[str, str] = {}

_EMOJI_PLACEHOLDER_RE = re.compile(r";([A-Za-z0-9_]+);")
_emojis_loaded = False

# Unicode alternatives are used where Discord's API accepts text but has no
# dedicated custom-emoji field (for example, slash-command option choices).
EMOJI_UNICODE_FALLBACKS: dict[str, str] = {
    "cake": "🎂", "crown": "👑", "lock": "🔒", "lock_open": "🔓",
    "hourglass_end": "⌛", "hourglass_start": "⏳", "robot": "🤖",
    "microphone": "🎙️", "hammer": "🔨", "broom": "🧹", "chat": "💬",
    "megaphone": "📣", "globe": "🌐", "brain": "🧠", "gear": "⚙️",
    "clipboard": "📋", "folder": "📁", "music_note": "🎵", "party": "🥳",
    "muted": "🔇", "bell_muted": "🔕", "plus": "➕", "pause": "⏸️",
    "play": "▶️", "skip_next": "⏭️", "stop": "⏹️", "repeat": "🔁",
    "scroll": "📜", "musical_notes": "🎶", "chart": "📊", "tv": "📺",
    "trash": "🗑️", "flask": "🧪", "star": "⭐", "no_entry": "⛔",
    "door": "🚪", "writing": "✍️", "user": "👤", "volume": "🔊",
    "map": "🗺️", "hiking_boot": "🥾", "purple_circle": "🟣", "dice": "🎲",
    "books": "📚", "shield": "🛡️", "arrows_left_right": "↔️",
    "black_circle": "⚫", "pencil": "✏️", "inbox": "📥", "boot": "👢",
    "outbox": "📤", "memo": "📝", "theater_masks": "🎭",
    "flag_triangle": "🚩", "crying_face": "😢", "play_pause": "⏯️",
    "rocket": "🚀", "loudspeaker": "📢", "lightning": "⚡", "heart": "❤️",
    "earth_americas": "🌎", "flag_bg": "🇧🇬", "flag_de": "🇩🇪",
    "flag_us": "🇺🇸", "flag_gb": "🇬🇧", "flag_fi": "🇫🇮",
    "flag_ru": "🇷🇺", "flag_ua": "🇺🇦",
    "mark": "✅", "no": "❌", "warning": "⚠️",
}


async def load_custom_emojis(bot: disnake.Client, *, force: bool = False) -> dict[str, str]:
    global _emojis_loaded

    if _emojis_loaded and not force:
        return DEVI_EMOJIS

    try:
        emojis = await bot.fetch_application_emojis()
    except disnake.HTTPException:
        logger.exception("Failed to load application emojis")
        return DEVI_EMOJIS

    DEVI_EMOJIS.clear()
    DEVI_EMOJIS.update({emoji.name: str(emoji) for emoji in emojis})
    _emojis_loaded = True

    logger.info("Application emojis: %d", len(DEVI_EMOJIS))
    return DEVI_EMOJIS


def format_custom_emojis(text: str) -> str:
    return _EMOJI_PLACEHOLDER_RE.sub(
        lambda match: DEVI_EMOJIS.get(match.group(1), match.group(0)),
        text,
    )

def format_unicode_emoji_placeholders(text: str) -> str:
    """Replace custom-emoji placeholders with Unicode text fallbacks."""
    return _EMOJI_PLACEHOLDER_RE.sub(
        lambda match: EMOJI_UNICODE_FALLBACKS.get(match.group(1), ""),
        text,
    )


def get_component_emoji(name: str, fallback: str | None = None) -> disnake.PartialEmoji | str | None:
    """Return a custom app emoji suitable for a component, or a Unicode fallback."""
    raw_emoji = DEVI_EMOJIS.get(name)
    if raw_emoji:
        try:
            return disnake.PartialEmoji.from_str(raw_emoji)
        except (TypeError, ValueError):
            logger.warning("Invalid application emoji cached for %s: %r", name, raw_emoji)

    if fallback is not None:
        return fallback
    return EMOJI_UNICODE_FALLBACKS.get(name)


def split_custom_emoji_prefix(text: str) -> tuple[str, disnake.PartialEmoji | str | None]:
    """Split a leading `;emoji_name;` from a label into text and component emoji."""
    match = _EMOJI_PLACEHOLDER_RE.match(text)
    if match is None:
        return text, None

    emoji = get_component_emoji(match.group(1))
    return text[match.end():].lstrip(), emoji


def component_option_kwargs(text: str | None, *, value: str) -> dict[str, object]:
    """Build SelectOption kwargs, splitting a leading custom emoji placeholder."""
    if not isinstance(text, str):
        text = str(value)
    label, emoji = split_custom_emoji_prefix(text)
    return {"label": label or str(value), "value": value, "emoji": emoji}
