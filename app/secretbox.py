"""
Encryption for stored third-party credentials.

The key lives outside the database, so a dump of `diet.garmin_credentials` --
or of the whole cluster -- is not a set of working Garmin sessions. That
separation is the entire point; keeping the key in a column would be
theatre.

Fernet (AES-128-CBC + HMAC-SHA256) is enough here: the threat is an offline
copy of the data, not an attacker who already holds the key file.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DEFAULT_KEY_PATH = Path("/run/secrets/credentials.key")

# Stored alongside each ciphertext so a future key rotation can tell which key
# a row was written with, instead of guessing.
KEY_ID = "fernet-1"


class KeyUnavailable(RuntimeError):
    pass


def key_path() -> Path:
    return Path(os.environ.get("CREDENTIALS_KEY_FILE", str(DEFAULT_KEY_PATH)))


def generate_key() -> bytes:
    return Fernet.generate_key()


def _load_key() -> bytes:
    p = key_path()
    if p.is_dir():
        # Docker creates a directory for a bind mount whose source is missing,
        # so this is what a forgotten `generate-key` actually looks like.
        raise KeyUnavailable(
            f"{p} is a directory, not a key file. The bind mount source was "
            "missing when the container started: create it with "
            "`python -m app.manage generate-key > secrets/credentials.key`, "
            "then recreate the container.")
    try:
        raw = p.read_bytes().strip()
    except PermissionError as exc:
        # The container runs as an unprivileged uid that is not the host user
        # who created the key, so a 0600 file owned by the host user is
        # unreadable here. This is the common first-run failure.
        raise KeyUnavailable(
            f"cannot read the credentials key at {p}: {exc}. The container runs "
            f"as uid {os.getuid()}; give that uid read access on the host, e.g. "
            "`sudo chown 10001:10001 secrets/credentials.key && chmod 400 "
            "secrets/credentials.key`."
        ) from exc
    except OSError as exc:
        raise KeyUnavailable(
            f"cannot read the credentials key at {p}: {exc}. Create one with "
            "`openssl rand -base64 32 | tr '+/' '-_' > secrets/credentials.key`."
        ) from exc
    if not raw:
        raise KeyUnavailable(f"the credentials key at {p} is empty")
    return raw


def check_usable() -> None:
    """
    Prove the key can be read and used. Raises KeyUnavailable if not.

    Exists so an interactive flow can fail *before* spending something scarce.
    """
    Fernet(_load_key()).decrypt(Fernet(_load_key()).encrypt(b"probe"))


def encrypt(plaintext: str) -> tuple[bytes, str]:
    """Returns (ciphertext, key_id) for storage."""
    return Fernet(_load_key()).encrypt(plaintext.encode()), KEY_ID


def decrypt(ciphertext: bytes, key_id: str | None = None) -> str:
    if key_id and key_id != KEY_ID:
        raise KeyUnavailable(
            f"row was encrypted with key {key_id!r}, this deployment has {KEY_ID!r}")
    try:
        return Fernet(_load_key()).decrypt(bytes(ciphertext)).decode()
    except InvalidToken as exc:
        raise KeyUnavailable(
            "stored credential could not be decrypted with the current key; "
            "the key file has changed or the row belongs to another deployment"
        ) from exc
