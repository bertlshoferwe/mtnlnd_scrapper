"""
Encrypt/decrypt a site's saved login password.

Used on both sides of the split: the Vercel dashboard (api/index.py)
encrypts the password when a site is saved, and the GitHub Actions scan
worker (scraper.py) decrypts it right before logging in. Both need the same
CREDENTIALS_KEY env var set — generate one with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

and set it as an env var on the Vercel project and as a GitHub Actions
secret. Losing/rotating the key makes every already-saved password
undecryptable — sites would need their login re-entered.
"""

import os

from cryptography.fernet import Fernet


def _fernet():
    key = os.environ.get("CREDENTIALS_KEY")
    if not key:
        raise RuntimeError(
            "CREDENTIALS_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "and set it as an env var (Vercel project settings, and the GitHub Actions "
            "workflow's secrets)."
        )
    return Fernet(key.encode())


def encrypt_password(plain):
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_password(token):
    return _fernet().decrypt(token.encode()).decode()
