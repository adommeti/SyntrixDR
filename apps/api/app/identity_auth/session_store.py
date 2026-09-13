from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from redis.asyncio import Redis

from app.core.clock import Clock
from app.identity_auth.security import generate_csrf_token


@dataclass
class SessionData:
    """Session data held in Redis (and reconstructed from durable SessionRecord on login)."""

    session_id: str
    user_id: uuid.UUID
    identity_type: str
    csrf_token: str
    absolute_expires_at: datetime
    idle_expires_at: datetime


class SessionStore(Protocol):
    """Structural type for anything that can back a session (the real Redis-backed store,
    or a test double) — commands.py depends on this, not the concrete RedisSessionStore,
    so tests can pass an in-memory/mock implementation without inheriting from it."""

    async def create(
        self, session_id: uuid.UUID, user_id: uuid.UUID, identity_type: str, clock: Clock
    ) -> SessionData: ...

    async def read(self, session_id: str) -> SessionData | None: ...

    async def touch(self, session_id: str, clock: Clock) -> SessionData | None: ...

    async def revoke(self, session_id: str) -> None: ...


class RedisSessionStore:
    """Redis-backed session store with automatic TTL expiry."""

    def __init__(self, redis_client: Redis, *, idle_minutes: int, absolute_hours: int) -> None:
        self.redis = redis_client
        self.idle_minutes = idle_minutes
        self.absolute_hours = absolute_hours

    def _key(self, session_id: uuid.UUID | str) -> str:
        """Redis key for a session."""
        return f"drcc:session:{session_id}"

    async def create(
        self, session_id: uuid.UUID, user_id: uuid.UUID, identity_type: str, clock: Clock
    ) -> SessionData:
        """Create a new session in Redis.

        Args:
            session_id: UUID of the durable SessionRecord
            user_id: UUID of the user
            identity_type: 'ENTRA' | 'LOCAL'
            clock: Clock instance for setting expiry times

        Returns:
            SessionData ready to send to the client in a cookie
        """
        now = clock.now()
        csrf_token = generate_csrf_token()

        # Calculate expiry times
        from datetime import timedelta

        absolute_expires_at = now + timedelta(hours=self.absolute_hours)
        idle_expires_at = now + timedelta(minutes=self.idle_minutes)

        session_data = SessionData(
            session_id=str(session_id),
            user_id=user_id,
            identity_type=identity_type,
            csrf_token=csrf_token,
            absolute_expires_at=absolute_expires_at,
            idle_expires_at=idle_expires_at,
        )

        # Store in Redis as a hash
        key = self._key(session_id)
        ttl = min(
            int((absolute_expires_at - now).total_seconds()),
            int((idle_expires_at - now).total_seconds()),
        )

        await self.redis.hset(
            key,
            mapping={
                "user_id": str(user_id),
                "identity_type": identity_type,
                "csrf_token": csrf_token,
                "absolute_expires_at": absolute_expires_at.isoformat(),
                "idle_expires_at": idle_expires_at.isoformat(),
            },
        )
        await self.redis.expire(key, ttl)

        return session_data

    async def read(self, session_id: str) -> SessionData | None:
        """Read a session from Redis without advancing its TTL.

        Args:
            session_id: String UUID of the session

        Returns:
            SessionData if found and not expired, None otherwise
        """
        key = self._key(session_id)
        data = await self.redis.hgetall(key)

        if not data:
            return None

        try:
            absolute_expires_at = datetime.fromisoformat(data[b"absolute_expires_at"].decode())
            idle_expires_at = datetime.fromisoformat(data[b"idle_expires_at"].decode())

            return SessionData(
                session_id=session_id,
                user_id=uuid.UUID(data[b"user_id"].decode()),
                identity_type=data[b"identity_type"].decode(),
                csrf_token=data[b"csrf_token"].decode(),
                absolute_expires_at=absolute_expires_at,
                idle_expires_at=idle_expires_at,
            )
        except (KeyError, ValueError, AttributeError):
            # Malformed or missing data
            await self.redis.delete(key)
            return None

    async def touch(self, session_id: str, clock: Clock) -> SessionData | None:
        """Slide the idle expiry forward by idle_minutes. Respects absolute_expires_at cap.

        Returns None (and deletes the key) if the session is already past absolute_expires_at.

        Args:
            session_id: String UUID of the session
            clock: Clock instance for timestamp checks

        Returns:
            Updated SessionData if still valid, None if expired or deleted
        """
        key = self._key(session_id)
        data = await self.redis.hgetall(key)

        if not data:
            return None

        try:
            now = clock.now()
            absolute_expires_at = datetime.fromisoformat(data[b"absolute_expires_at"].decode())
            idle_expires_at = datetime.fromisoformat(data[b"idle_expires_at"].decode())

            # Check if already past absolute or idle expiry
            if now >= absolute_expires_at or now >= idle_expires_at:
                await self.redis.delete(key)
                return None

            # Slide idle expiry forward, capped at absolute expiry
            from datetime import timedelta

            new_idle_expires_at = now + timedelta(minutes=self.idle_minutes)
            new_idle_expires_at = min(new_idle_expires_at, absolute_expires_at)

            # Update Redis
            await self.redis.hset(key, "idle_expires_at", new_idle_expires_at.isoformat())

            # Reset TTL to minimum of absolute and idle
            ttl = min(
                int((absolute_expires_at - now).total_seconds()),
                int((new_idle_expires_at - now).total_seconds()),
            )
            await self.redis.expire(key, max(1, ttl))

            return SessionData(
                session_id=session_id,
                user_id=uuid.UUID(data[b"user_id"].decode()),
                identity_type=data[b"identity_type"].decode(),
                csrf_token=data[b"csrf_token"].decode(),
                absolute_expires_at=absolute_expires_at,
                idle_expires_at=new_idle_expires_at,
            )
        except (KeyError, ValueError, AttributeError):
            await self.redis.delete(key)
            return None

    async def revoke(self, session_id: str) -> None:
        """Delete a session from Redis immediately."""
        key = self._key(session_id)
        await self.redis.delete(key)
