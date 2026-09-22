"""Work with bot`s DataBase"""

import sqlite3
from contextlib import contextmanager

from paths import DB_FILE

_initialized = False


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_cursor(commit: bool = False):
    """Context manager that yields a cursor and closes the connection afterward.

    Pass commit=True for any operation that writes data.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()


def init():
    """Creates all tables if they don't exist yet. Safe to call multiple times."""
    global _initialized
    if _initialized:
        return

    with db_cursor(commit=True) as cur:
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS warns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                duration_raw TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE INDEX IF NOT EXISTS idx_warns_guild_user ON warns(guild_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_warns_status ON warns(status);

            CREATE TABLE IF NOT EXISTS temp_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_temp_roles_expires ON temp_roles(expires_at);

            CREATE TABLE IF NOT EXISTS triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                pattern TEXT NOT NULL,
                sort_order INTEGER NOT NULL,
                UNIQUE(guild_id, pattern)
            );
            CREATE TABLE IF NOT EXISTS trigger_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_id INTEGER NOT NULL REFERENCES triggers(id) ON DELETE CASCADE,
                response TEXT NOT NULL,
                sort_order INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trigger_responses_trigger ON trigger_responses(trigger_id);

            CREATE TABLE IF NOT EXISTS giveaways (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                host_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                prize TEXT,
                winners_count INTEGER NOT NULL DEFAULT 1,
                end_time TEXT NOT NULL,
                ended INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS giveaway_participants (
                giveaway_message_id INTEGER NOT NULL REFERENCES giveaways(message_id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (giveaway_message_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                guild_id INTEGER PRIMARY KEY,
                api_key TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_memory_guild_user ON ai_memory(guild_id, user_id, id);

            CREATE TABLE IF NOT EXISTS guild_locale (
                guild_id INTEGER PRIMARY KEY,
                localization TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS log_channels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS permission_grants (
                guild_id INTEGER NOT NULL,
                target_type TEXT NOT NULL CHECK (target_type IN ('guild', 'user', 'role', 'channel')),
                target_id INTEGER NOT NULL,
                value INTEGER NOT NULL,
                PRIMARY KEY (guild_id, target_type, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_permission_grants_guild ON permission_grants(guild_id);
            
            CREATE TABLE IF NOT EXISTS ai_custom_user_instructions (
                user_id INTEGER NOT NULL,
                instruction TEXT NOT NULL
            );
            
            CREATE TABLE IF NOT EXISTS birthdays (
                user_id INTEGER PRIMARY KEY,
                day INTEGER NOT NULL,
                month INTEGER NOT NULL,
                ping_on_servers INTEGER NOT NULL DEFAULT 1
            );
 
            CREATE TABLE IF NOT EXISTS birthday_channels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            );
            
            CREATE TABLE IF NOT EXISTS dm_opt_out (
                user_id INTEGER PRIMARY KEY,
                allow_dm INTEGER NOT NULL DEFAULT 0
            );
            
            CREATE TABLE IF NOT EXISTS temp_voice_config (
                guild_id INTEGER PRIMARY KEY,
                lobby_channel_id INTEGER NOT NULL,
                category_id INTEGER,
                name_template TEXT
            );
 
            CREATE TABLE IF NOT EXISTS temp_voice_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
 
            CREATE TABLE IF NOT EXISTS temp_voice_user_settings (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                user_limit INTEGER,
                locked INTEGER NOT NULL DEFAULT 0,
                bitrate INTEGER,
                rtc_region TEXT
            );
 
            CREATE TABLE IF NOT EXISTS temp_voice_user_overwrites (
                user_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                allowed INTEGER NOT NULL,
                PRIMARY KEY (user_id, target_id)
            );
            
            CREATE TABLE IF NOT EXISTS sticky_messages (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                created_by INTEGER NOT NULL
            );
            """
        )

    _initialized = True