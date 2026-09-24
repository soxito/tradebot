"""Encryption at rest for broker and exchange credentials.

Fernet (AES-128-CBC + HMAC). The key comes from ``CREDENTIALS_KEY`` in the
environment, or from ``backend/data/.credentials_key`` — generated once with
mode 0600 when neither exists. ``SECRET_KEY`` is deliberately not used: it is
regenerated on every boot when left at its default, which would make every
stored credential unreadable after a restart.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "fernet:"
_KEY_FILE = Path(__file__).resolve().parents[2] / "data" / ".credentials_key"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = (os.environ.get("CREDENTIALS_KEY") or "").strip()
    if not key:
        try:
            from app.core.config import settings

            key = (getattr(settings, "CREDENTIALS_KEY", "") or "").strip()
        except Exception:  # noqa: BLE001
            key = ""
    if not key:
        if _KEY_FILE.exists():
            key = _KEY_FILE.read_text().strip()
        else:
            _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key().decode()
            fd = os.open(str(_KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(key)
    return Fernet(key.encode())


def is_encrypted(value: Optional[str]) -> bool:
    return bool(value) and str(value).startswith(_PREFIX)


def encrypt(plain: Optional[str]) -> Optional[str]:
    if plain is None or plain == "":
        return plain
    if is_encrypted(plain):
        return plain
    return _PREFIX + _fernet().encrypt(str(plain).encode()).decode()


def decrypt(stored: Optional[str]) -> Optional[str]:
    """Plaintext for a stored value. Legacy unencrypted values pass through."""
    if not stored or not is_encrypted(stored):
        return stored
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise ValueError("stored credential cannot be decrypted with the current CREDENTIALS_KEY") from exc


try:
    from sqlalchemy.types import Text, TypeDecorator

    class EncryptedText(TypeDecorator):
        """A Text column that is encrypted in the database and plain in Python.

        Legacy plaintext rows still read back unchanged; they are encrypted the
        next time the value is written (see ``reencrypt_mt5_passwords``).
        """

        impl = Text
        cache_ok = True

        def process_bind_param(self, value, dialect):  # noqa: D401
            return encrypt(value)

        def process_result_value(self, value, dialect):
            return decrypt(value)
except ImportError:  # pragma: no cover — sqlalchemy is always present in the app
    EncryptedText = None  # type: ignore[assignment]


async def reencrypt_mt5_passwords() -> int:
    """One-time: encrypt MT5 passwords still stored as plaintext."""
    from sqlalchemy import text as _text

    from app.core.database import engine

    changed = 0
    async with engine.begin() as conn:
        try:
            rows = (await conn.execute(_text(
                "SELECT id, password_encrypted FROM mt5_accounts"
            ))).all()
        except Exception:  # noqa: BLE001 — plugin table absent
            return 0
        for row_id, stored in rows:
            if stored and not is_encrypted(stored):
                await conn.execute(
                    _text("UPDATE mt5_accounts SET password_encrypted = :p WHERE id = :i"),
                    {"p": encrypt(stored), "i": row_id},
                )
                changed += 1
    return changed
