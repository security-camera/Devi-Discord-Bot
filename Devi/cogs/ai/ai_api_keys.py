"""Work with API keys for guilds"""

from db import db_cursor
from crypto_utils import encrypt, decrypt


def get_api_key(guild_id: int) -> str | None:
    """Returns Gemini API key for guild."""
    with db_cursor() as cur:
        cur.execute("SELECT api_key FROM api_keys WHERE guild_id = %s", (guild_id,))
        row = cur.fetchone()
    return decrypt(row["api_key"]) if row else None


def set_api_key(guild_id: int, api_key: str):
    """Sets Gemini API key for guild."""
    encrypted_key = encrypt(api_key)
    with db_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO api_keys (guild_id, api_key) VALUES (%s, %s)
               ON CONFLICT(guild_id) DO UPDATE SET api_key = excluded.api_key""",
            (guild_id, encrypted_key),
        )


def remove_api_key(guild_id: int):
    """Removes API key for guild."""
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM api_keys WHERE guild_id = %s", (guild_id,))