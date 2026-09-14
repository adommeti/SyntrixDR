from __future__ import annotations

import json

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Request, Response
from redis.asyncio import Redis

from app.core.config import get_settings

oauth = OAuth()


def register_entra_oauth(redis: Redis) -> None:
    """Register Entra (Azure AD) as an OAuth provider."""
    settings = get_settings()

    if not settings.entra_client_id:
        # OIDC not configured; skip registration
        return

    oauth.register(
        name="entra",
        client_id=settings.entra_client_id,
        client_secret=settings.entra_client_secret,
        server_metadata_url=(
            f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid profile email"},
    )


async def get_entra_login_redirect(request: Request, redis: Redis) -> Response:
    """Generate an authorization redirect to Entra.

    Stores state and nonce in Redis for validation in the callback.

    Args:
        request: FastAPI Request
        redis: Redis client for state storage

    Returns:
        A redirect Response to the Entra authorization endpoint
    """
    if not oauth.entra:
        return Response(content="OIDC not configured", status_code=500)

    # Generate state and nonce, store in Redis
    import secrets

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    state_key = f"drcc:oidc_state:{state}"

    await redis.setex(
        state_key,
        600,  # 10 minutes
        json.dumps({"nonce": nonce}),
    )

    # Redirect to Entra
    redirect_uri = request.url_for("get_entra_callback")
    return await oauth.entra.authorize_redirect(request, redirect_uri, state=state, nonce=nonce)


async def handle_entra_callback(
    request: Request,
    redis: Redis,
) -> tuple[str, str, str]:
    """Validate OIDC callback and extract user info.

    Returns (entra_object_id, email, display_name).

    Args:
        request: FastAPI Request (contains code and state)
        redis: Redis client for state validation

    Returns:
        Tuple of (entra_object_id, email, display_name)

    Raises:
        OAuthError: OIDC validation or exchange failed
    """
    if not oauth.entra:
        raise OAuthError("OIDC not configured")

    # Validate state
    state = request.query_params.get("state")
    if not state:
        raise OAuthError("Missing state parameter")

    state_key = f"drcc:oidc_state:{state}"
    state_data = await redis.get(state_key)
    if not state_data:
        raise OAuthError("Invalid or expired state")

    await redis.delete(state_key)

    # Exchange code for token
    token = await oauth.entra.authorize_access_token(request)

    # Extract claims from ID token
    id_token = token.get("id_token")
    if not id_token:
        raise OAuthError("No ID token in response")

    # In production, validate the ID token signature and claims
    # For now, rely on authlib's validation
    user_info = token.get("userinfo")
    if not user_info:
        raise OAuthError("No user info in token")

    entra_object_id = user_info.get("oid") or user_info.get("sub")
    email = user_info.get("email") or user_info.get("preferred_username")
    display_name = user_info.get("name") or "User"

    if not entra_object_id or not email:
        raise OAuthError("Missing required claims (oid/email)")

    return entra_object_id, email, display_name
