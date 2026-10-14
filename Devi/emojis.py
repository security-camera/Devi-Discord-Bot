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