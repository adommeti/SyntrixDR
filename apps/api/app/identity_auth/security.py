from __future__ import annotations

import secrets

import pyotp
from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.errors import AppError
from app.identity_auth.denylist import COMMON_BREACHED_PASSWORDS


class PasswordHasher:
    """Wraps argon2-cffi for password hashing with consistent configuration."""

    def __init__(self) -> None:
        self._hasher = Argon2PasswordHasher()

    def hash_password(self, password: str) -> str:
        """Hash a password using Argon2id."""
        return self._hasher.hash(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        """Verify a password against its hash. Returns False on mismatch (never raises)."""
        try:
            self._hasher.verify(password_hash, password)
            return True
        except VerifyMismatchError:
            return False


class PasswordPolicyError(AppError):
    code = "PASSWORD_POLICY_VIOLATION"
    status_code = 422

    def __init__(self, message: str) -> None:
        super().__init__(message)


def validate_password_policy(password: str) -> None:
    """Validate password against policy: length 12–128 chars, not a known breached password.

    Raises PasswordPolicyError if policy is violated.
    """
    if len(password) < 12:
        raise PasswordPolicyError("Password must be at least 12 characters long.")
    if len(password) > 128:
        raise PasswordPolicyError("Password must be at most 128 characters long.")
    if is_breached_password(password):
        raise PasswordPolicyError("This password has been compromised in public data breaches.")


def is_breached_password(password: str) -> bool:
    """Check if password is in the static denylist of common breached passwords."""
    return password.lower() in COMMON_BREACHED_PASSWORDS


def generate_totp_secret() -> str:
    """Generate a random base32-encoded TOTP secret."""
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str, issuer: str = "SyntrixDR") -> str:
    """Generate the provisioning URI for a TOTP secret (for QR codes).

    Args:
        secret: Base32-encoded TOTP secret
        email: User's email (account name)
        issuer: Issuer name (defaults to "SyntrixDR")

    Returns:
        A provisioning URI suitable for QR code encoding.
    """
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=issuer)


def verify_totp_code(secret: str, code: str) -> bool:
    """Verify a TOTP code against a secret. Returns False if invalid or expired."""
    try:
        totp = pyotp.TOTP(secret)
        return totp.verify(code)
    except Exception:
        return False


def generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token."""
    return secrets.token_urlsafe(32)
