"""Container with all directories` path as a variables"""

import os
import platform
from pathlib import Path
from typing import Any

CORE_DIR: Path = Path(__file__).parent

CONFIG_DIR: Path = CORE_DIR / "config"
COGS_DIR: Path = CORE_DIR / "cogs"
AI_COG_DIR: Path = COGS_DIR / "ai"
LOCALES_DIR: Path = CONFIG_DIR / "locales"
DB_FILE: Path = CONFIG_DIR / "bot.db"
ENV_FILE: Path = CONFIG_DIR / ".env"

def init():
    global CORE_DIR, COGS_DIR, CONFIG_DIR, LOCALES_DIR, AI_COG_DIR

    if CORE_DIR:
        os.makedirs(CORE_DIR, exist_ok=True)

    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(COGS_DIR, exist_ok=True)
    os.makedirs(LOCALES_DIR, exist_ok=True)

def env_var(var: str, default: Any = "") -> str:
    return os.environ.get(var, default)

def env_var_to_bool(var: str, default: Any = "False") -> bool:
    return os.environ.get(var, default).strip().lower() in ("1", "true", "yes", "on")

def env_var_to_int(var: str, default: Any = "0") -> int:
    return int(env_var(var, default))