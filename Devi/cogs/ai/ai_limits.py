"""Daily quotas, global budget guard and usage accounting for the shared AI key.

Every successful AI call is stored in `ai_usage` (one row per UTC day / server / user), so the
limits survive bot restarts and /ai usage has real numbers to show.

Limits (all reset at 00:00 UTC):
  AI_USER_DAILY_REQUESTS / AI_USER_DAILY_REQUESTS_VOTED - requests per user
  AI_GUILD_DAILY_REQUESTS                               - requests per server
  AI_DAILY_BUDGET_USD                                   - estimated spend of the whole bot (0 = off)
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from cogs.ai.ai_client import env_float
from db import db_cursor
from paths import env_var_to_int

USER_DAILY_REQUESTS = env_var_to_int("AI_USER_DAILY_REQUESTS", "30")
USER_DAILY_REQUESTS_VOTED = env_var_to_int("AI_USER_DAILY_REQUESTS_VOTED", "100")
GUILD_DAILY_REQUESTS = env_var_to_int("AI_GUILD_DAILY_REQUESTS", "300")

DAILY_BUDGET_USD = env_float("AI_DAILY_BUDGET_USD", 1.0)
DAILY_BUDGET_MICRO = int(DAILY_BUDGET_USD * 1_000_000)

KEEP_DAYS = 90


@dataclass
class Usage:
    user_requests: int
    guild_requests: int
    cost_micro: int


def today() -> date:
    return datetime.now(timezone.utc).date()


def seconds_until_reset() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((tomorrow - now).total_seconds())


def hours_until_reset() -> int:
    return max(1, math.ceil(seconds_until_reset() / 3600))


def budget_exhausted(cost_micro: int) -> bool:
    return DAILY_BUDGET_MICRO > 0 and cost_micro >= DAILY_BUDGET_MICRO


def get_usage(user_id: int, guild_id: int) -> Usage:
    """What this user, this server and the whole bot have used today."""
    with db_cursor() as cur:
        cur.execute(
            """SELECT COALESCE(SUM(requests) FILTER (WHERE user_id = %s), 0) AS user_requests,
                      COALESCE(SUM(requests) FILTER (WHERE guild_id = %s), 0) AS guild_requests,
                      COALESCE(SUM(cost_micro), 0) AS cost_micro
               FROM ai_usage WHERE day = %s""",
            (user_id, guild_id, today()),
        )
        row = cur.fetchone()
    return Usage(int(row["user_requests"]), int(row["guild_requests"]), int(row["cost_micro"]))


def record_usage(guild_id: int, user_id: int, *, prompt_tokens: int, cached_tokens: int, completion_tokens: int, cost_micro: int):
    with db_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO ai_usage (day, guild_id, user_id, requests, prompt_tokens, cached_tokens, completion_tokens, cost_micro)
               VALUES (%s, %s, %s, 1, %s, %s, %s, %s)
               ON CONFLICT (day, guild_id, user_id) DO UPDATE SET
                   requests = ai_usage.requests + 1,
                   prompt_tokens = ai_usage.prompt_tokens + excluded.prompt_tokens,
                   cached_tokens = ai_usage.cached_tokens + excluded.cached_tokens,
                   completion_tokens = ai_usage.completion_tokens + excluded.completion_tokens,
                   cost_micro = ai_usage.cost_micro + excluded.cost_micro""",
            (today(), guild_id, user_id, prompt_tokens, cached_tokens, completion_tokens, cost_micro),
        )


def summary() -> dict:
    """Totals for today (UTC) for the whole bot."""
    with db_cursor() as cur:
        cur.execute(
            """SELECT COALESCE(SUM(requests), 0) AS requests,
                      COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                      COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                      COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                      COALESCE(SUM(cost_micro), 0) AS cost_micro,
                      COUNT(DISTINCT user_id) AS users,
                      COUNT(DISTINCT guild_id) AS guilds
               FROM ai_usage WHERE day = %s""",
            (today(),),
        )
        row = cur.fetchone()
    return {key: int(row[key]) for key in row.keys()}


def top_guilds(limit: int = 3) -> list[dict]:
    """The servers that spent the most today - the first place to look when the bill is high."""
    with db_cursor() as cur:
        cur.execute(
            """SELECT guild_id, SUM(requests) AS requests, SUM(cost_micro) AS cost_micro
               FROM ai_usage WHERE day = %s
               GROUP BY guild_id ORDER BY SUM(cost_micro) DESC LIMIT %s""",
            (today(), limit),
        )
        rows = cur.fetchall()
    return [{"guild_id": int(r["guild_id"]), "requests": int(r["requests"]), "cost_micro": int(r["cost_micro"])} for r in rows]


def cleanup(keep_days: int = KEEP_DAYS) -> int:
    """Deletes accounting rows older than `keep_days`. Returns the number of deleted rows."""
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM ai_usage WHERE day < %s", (today() - timedelta(days=keep_days),))
        return cur.rowcount
