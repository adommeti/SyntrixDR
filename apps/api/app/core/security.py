from __future__ import annotations

import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False


async def get_current_user_id(request: Request) -> uuid.UUID:
    """Resolves the authenticated actor's user id.

    Real Entra OIDC / local-credential session resolution lands in BUILD-02
    (identity_auth). Tests override this dependency directly.
    """
    raise NotImplementedError("Authentication is not wired until BUILD-02 (identity_auth).")
