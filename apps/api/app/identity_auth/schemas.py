from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LocalLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: str
    totp_code: str | None = None


class LocalLoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    csrf_token: str
    user_id: str


class LogoutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str


class ReauthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    password: str | None = None
    totp_code: str | None = None


class ReauthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    granted_at: str
    expires_at: str


class PasswordResetRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str


class PasswordResetRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str


class PasswordResetConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    new_password: str


class PasswordResetConfirmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str


class TotpEnrolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: str
    provisioning_uri: str


class TotpVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str


class TotpVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str


class CsrfTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csrf_token: str
