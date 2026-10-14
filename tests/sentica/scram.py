"""SCRAM-SHA-256 verifiers for tenant roles.

The database container receives only this verifier. The plaintext password
stays in the proxy userlist.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os


def scram_verifier(password: str, *, iterations: int = 4096) -> str:
    """Build a Postgres SCRAM-SHA-256 secret for ``password``."""
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def _b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return (
        f"SCRAM-SHA-256${iterations}:{_b64(salt)}${_b64(stored_key)}:{_b64(server_key)}"
    )
