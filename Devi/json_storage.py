"""Safe work with JSONs"""

import json
import os
import tempfile


def load_json_safe(path, default):
    """Safely loads JSON. If the file is missing or contains invalid data, returns
    the default value without overwriting anything on disk."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json_atomic(path, data, **json_kwargs):
    """Atomically saves JSON"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, **json_kwargs)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)
    except Exception:
        # If something goes wrong (for example, the disk is full), remove the
        # temporary file and re-raise the error without modifying the original file.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise