"""Work with AI memory and context.

The history is stored per (server, user). With one shared API key every old message that is
re-sent on each request costs money, so:
  * images are never stored (a short placeholder is kept instead) - they would be re-sent and re-billed every time,
  * the history is capped by the number of messages AND by characters,
  * trimming happens in batches (down to ~70% of the limit). Between two trims the history only grows at
    its end, so the provider's prefix cache keeps matching and the old part of the prompt is billed at the
    cheap cache-hit price (DeepSeek and Qwen both cache a repeated prompt prefix automatically).
"""

import json

from db import db_cursor
from paths import env_var_to_int

MAX_MESSAGES = env_var_to_int("AI_MAX_MESSAGES", "20")
MAX_MESSAGES_VOTED = env_var_to_int("AI_MAX_MESSAGES_VOTED", "40")
MAX_HISTORY_CHARS = env_var_to_int("AI_HISTORY_MAX_CHARS", "8000")
MAX_HISTORY_CHARS_VOTED = env_var_to_int("AI_HISTORY_MAX_CHARS_VOTED", "16000")

TRIM_TO_RATIO = 0.7
IMAGE_PLACEHOLDER = "[image omitted]"


def _content_to_text(raw: str) -> str:
    """Rows are stored as a JSON list of parts ([{"type": "text", "text": ...}]). Older rows may also contain
    image parts (base64) or plain text - everything is flattened to text here."""
    try:
        parts = json.loads(raw)
    except (TypeError, ValueError):
        return raw or ""
    if not isinstance(parts, list):
        return raw or ""

    chunks = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict):
            if part.get("type") == "text" and part.get("text"):
                chunks.append(part["text"])
            elif part.get("type") == "image":
                chunks.append(IMAGE_PLACEHOLDER)
    return "\n".join(chunks)


def _text_to_content(text: str) -> str:
    return json.dumps([{"type": "text", "text": text}], ensure_ascii=False)


def load_history(guild_id: int, user_id: int) -> list[dict]:
    """Chat history as OpenAI-style messages: [{"role": "user" | "assistant", "content": "..."}]."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT role, content FROM ai_memory WHERE guild_id = %s AND user_id = %s ORDER BY id",
            (guild_id, user_id),
        )
        rows = cur.fetchall()

    history = []
    for row in rows:
        text = _content_to_text(row["content"])
        if text:
            history.append({"role": row["role"], "content": text})
    return history


def plan_trim(rows: list[tuple[int, str, int]], max_messages: int, max_chars: int) -> list[int]:
    """Decides which rows (id, role, size) to delete, oldest first. Returns [] while the history fits.

    Once a limit is exceeded the history is cut down to ~70% of the limits, so the next several requests
    can append to an unchanged prefix. The last exchange is never touched and the history always starts
    with a user message.
    """
    count = len(rows)
    total = sum(size for _, _, size in rows)
    if count <= max_messages and total <= max_chars:
        return []

    target_count = max(2, int(max_messages * TRIM_TO_RATIO))
    target_chars = int(max_chars * TRIM_TO_RATIO)

    drop: list[int] = []
    index = 0
    while index < count - 2 and (count - len(drop) > target_count or total > target_chars):
        row_id, _, size = rows[index]
        drop.append(row_id)
        total -= size
        index += 1

    while index < count - 1 and rows[index][1] != "user":
        drop.append(rows[index][0])
        index += 1

    return drop


def add_exchange(guild_id: int, user_id: int, user_text: str, assistant_text: str, max_messages: int, max_chars: int):
    """Saves one question/answer pair and trims the history if it got too long."""
    new_rows = [
        (guild_id, user_id, "user", _text_to_content(user_text)),
        (guild_id, user_id, "assistant", _text_to_content(assistant_text)),
    ]

    with db_cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO ai_memory (guild_id, user_id, role, content) VALUES (%s, %s, %s, %s)",
            new_rows,
        )

        cur.execute(
            "SELECT id, role, LENGTH(content) AS size FROM ai_memory WHERE guild_id = %s AND user_id = %s ORDER BY id",
            (guild_id, user_id),
        )
        rows = [(int(r["id"]), r["role"], int(r["size"])) for r in cur.fetchall()]

        drop = plan_trim(rows, max_messages, max_chars)
        if drop:
            cur.execute("DELETE FROM ai_memory WHERE id = ANY(%s)", (drop,))


def clear_memory(user_id: int, guild_id: int | None = None):
    with db_cursor(commit=True) as cur:
        if guild_id:
            cur.execute("DELETE FROM ai_memory WHERE guild_id = %s AND user_id = %s", (guild_id, user_id))
        else:
            cur.execute("DELETE FROM ai_memory WHERE user_id = %s", (user_id,))


def purge_stored_images() -> int:
    with db_cursor() as cur:
        cur.execute("SELECT id, content FROM ai_memory WHERE content LIKE %s", ('%"type": "image"%',))
        rows = cur.fetchall()

    if not rows:
        return 0

    updates = [(_text_to_content(_content_to_text(row["content"])), int(row["id"])) for row in rows]

    with db_cursor(commit=True) as cur:
        cur.executemany("UPDATE ai_memory SET content = %s WHERE id = %s", updates)
    return len(updates)
