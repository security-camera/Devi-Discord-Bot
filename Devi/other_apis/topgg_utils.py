from datetime import datetime, timezone

import aiohttp
import disnake

import i18n
from i18n import LocaleObject

from paths import env_var

TOP_GG_TOKEN = env_var("TOP_GG_TOKEN")

if not TOP_GG_TOKEN:
    raise RuntimeError("TOP_GG_TOKEN environment variable is not set")

TOP_GG_API_BASE = "https://top.gg/api/v1"


async def is_voted(user: disnake.User | disnake.Member | int) -> bool:
    """Checks status of user's vote | Request: GET /v1/projects/@me/votes/user_id | Docs: https://docs.top.gg/docs/API/v1"""
    if isinstance(user, (disnake.User, disnake.Member)):
        user_id = user.id
    elif isinstance(user, int):
        user_id = user
    else:
        raise TypeError("user must be disnake.User, disnake.Member or int")

    url = f"{TOP_GG_API_BASE}/projects/@me/votes/{user_id}"

    headers = {
        "Authorization": f"Bearer {TOP_GG_TOKEN}",
        "Accept": "application/json",
    }

    # source is required for correctly id
    params = {
        "source": "discord",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers=headers,
            params=params,
        ) as response:
            if response.status == 404:
                # Doesn`t vote EVER
                return False

            if response.status != 200:
                text = await response.text()
                raise RuntimeError(f"Top.gg API returned HTTP {response.status}: {text}")

            data = await response.json()

    expires_at_raw = data.get("expires_at")
    if not expires_at_raw:
        return False

    # fromisoformat Python < 3.11 doesn`t support suffix "Z" — replace to +00:00
    expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))

    return datetime.now(timezone.utc) < expires_at

async def validate_vote(inter: disnake.ApplicationCommandInteraction, locale: LocaleObject) -> bool:
    """ Returns True if user ISN'T voted (the interaction response has already been
    sent — the calling code must immediately `return`). Returns False if user is voted."""
    voted = await is_voted(inter.author)

    link = i18n.t("top_gg_cog.voting_link", locale=locale)
    text = i18n.t("top_gg_cog.locked_command", locale=locale, link=link)

    if not voted:
        if inter.response.is_done():
            await inter.edit_original_response(content=text, embed=None, embeds=[], components=[])
        else:
            await inter.response.send_message(content=text, ephemeral=True)

    return not voted