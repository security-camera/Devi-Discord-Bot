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
PIPER_VOICES_DIR: Path = CONFIG_DIR / "voices"
ENV_FILE: Path = CONFIG_DIR / ".env"

def init():
    global CORE_DIR, COGS_DIR, CONFIG_DIR, LOCALES_DIR, AI_COG_DIR, PIPER_VOICES_DIR

    if CORE_DIR:
        os.makedirs(CORE_DIR, exist_ok=True)

    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(COGS_DIR, exist_ok=True)
    os.makedirs(AI_COG_DIR, exist_ok=True)
    os.makedirs(PIPER_VOICES_DIR, exist_ok=True)
    os.makedirs(LOCALES_DIR, exist_ok=True)

def env_var(var: str, default: Any = "") -> str:
    return os.environ.get(var, default)

def env_bool(var: str, default: str = "False") -> bool:
    return env_var(var, default).strip().lower() in ("1", "true", "yes", "on")

def env_int(var: str, default: int = None) -> int:
    raw = env_var(var).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a int, got {raw!r}") from None

def env_float(var: str, default: float = None) -> float:
    raw = env_var(var).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a float, got {raw!r}") from None