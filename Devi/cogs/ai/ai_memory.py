"""Work with AI memory and context"""

import json

from db import db_cursor
from other_apis.topgg_utils import is_voted
from paths import env_var_to_int

# Multimodal history (text + images) is heavier than plain text, so we keep
# fewer turns than before to avoid bloating the DB / the request payload.
MAX_MESSAGES = env_var_to_int("AI_MAX_MESSAGES", "100")
MAX_MESSAGES_VOTED = env_var_to_int("AI_MAX_MESSAGES_VOTED", "200")


def load_memory(guild_id: int, user_id: int) -> list:
    """Loads chat history as a list of {"role": ..., "parts": [...]}.

    Each part is either {"type": "text", "text": "..."} or
    {"type": "image", "mime_type": "...", "data": "<base64>"}.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT role, content FROM ai_memory WHERE guild_id = ? AND user_id = ? ORDER BY id",
            (guild_id, user_id),
        )
        rows = cur.fetchall()

    messages = []
    for row in rows:
        try:
            parts = json.loads(row["content"])
            if not isinstance(parts, list):
                raise ValueError
        except (TypeError, ValueError):
            # Backwards compatibility with old plain-text rows
            parts = [{"type": "text", "text": row["content"]}]
        messages.append({"role": row["role"], "parts": parts})
    return messages


async def save_memory(guild_id: int, user_id: int, messages: list):
    max_messages = MAX_MESSAGES_VOTED if await is_voted(user_id) else MAX_MESSAGES
    trimmed = messages[-max_messages:]
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM ai_memory WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        cur.executemany(
            "INSERT INTO ai_memory (guild_id, user_id, role, content) VALUES (?, ?, ?, ?)",
            [(guild_id, user_id, m["role"], json.dumps(m["parts"], ensure_ascii=False)) for m in trimmed],
        )


async def add_message(guild_id: int, user_id: int, role: str, parts: list):
    """Appends a message to memory. `parts` is a list of part-dicts (see load_memory)."""
    memory = load_memory(guild_id, user_id)

    memory.append({
        "role": role,
        "parts": parts,
    })

    await save_memory(guild_id, user_id, memory)


def clear_memory(user_id: int, guild_id: int | None = None):
    with db_cursor(commit=True) as cur:
        if guild_id:
            cur.execute("DELETE FROM ai_memory WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        else:
            cur.execute("DELETE FROM ai_memory WHERE user_id = ?", (user_id,))