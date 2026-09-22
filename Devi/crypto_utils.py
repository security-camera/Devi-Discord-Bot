"""Work with encryption and decryption of cryptographic data."""

import os

from cryptography.fernet import Fernet, InvalidToken

ENV_VAR = "ENCRYPTION_KEY"

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet

    key = os.environ.get(ENV_VAR)
    if not key:
        raise RuntimeError(f"{ENV_VAR} is not set in the environment. Generate a key once with the following command:\n"
            f'  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
            f"and add it to environment.env alongside BOT_TOKEN. "
            f"IMPORTANT: losing this key makes all encrypted records in the database unrecoverable — "
            f"keep a backup of it as carefully as the bot token itself, and never commit it to git.")

    try:
        _fernet = Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"{ENV_VAR} is not a valid Fernet key: {e}") from None

    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypts a string and returns a text token safe for storage in a TEXT column."""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    """Decrypts a token produced by encrypt(). Raises ValueError if the token
    is corrupted or was encrypted with a different key (including if it is
    not a Fernet token at all, such as an old unencrypted value).
    """
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        raise ValueError("Failed to decrypt the value — invalid encryption key or corrupted data") from None