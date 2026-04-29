"""Supabase JWT verification for FastAPI.

Supports both signing regimes Supabase projects use:

- **Asymmetric JWT Signing Keys** (ES256 / RS256 / EdDSA). New Supabase
  projects sign user access tokens with an asymmetric key and publish the
  public key at `<supabase_url>/auth/v1/.well-known/jwks.json`. We fetch
  and cache JWKS in-process via PyJWT's `PyJWKClient`.
- **Legacy HS256 JWT Secret**. Older projects — and long-lived API keys
  (anon, service_role) even on new projects — are signed with a shared
  HMAC secret (`SUPABASE_JWT_SECRET`). Used as a fallback.

Dispatch is on the token's `alg` header, so the same code path handles
local dev (typically HS256) and prod (typically asymmetric) without
caller awareness.

`AUTH_ENABLED=0` short-circuits verification entirely and returns an
empty `AuthUser` — used for local dev frictionless access.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient

from rumil.database import DB
from rumil.settings import Settings, get_settings


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    email: str


_jwks_clients: dict[str, PyJWKClient] = {}


def _get_jwks_client(supabase_url: str) -> PyJWKClient:
    key = supabase_url.rstrip("/")
    if key not in _jwks_clients:
        _jwks_clients[key] = PyJWKClient(
            f"{key}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
            lifespan=3600,
        )
    return _jwks_clients[key]


def _decode(token: str, settings: Settings) -> dict:
    try:
        headers = jwt.get_unverified_header(token)
    except jwt.DecodeError as exc:
        raise jwt.InvalidTokenError("malformed token") from exc
    alg = headers.get("alg", "HS256")

    common: dict = {
        "audience": "authenticated",
        "options": {"require": ["exp", "sub"]},
    }

    if alg == "HS256":
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            **common,
        )
    if alg in {"ES256", "RS256", "EdDSA"}:
        url, _ = settings.get_supabase_credentials(prod=settings.is_prod_db)
        signing_key = _get_jwks_client(url).get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[alg],
            **common,
        )
    raise jwt.InvalidAlgorithmError(f"Unsupported JWT algorithm: {alg}")


def get_current_user(authorization: str | None = Header(default=None)) -> AuthUser:
    settings = get_settings()
    if not settings.auth_enabled:
        return AuthUser(user_id="", email="")

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = _decode(token, settings)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    user_id = claims.get("sub") or ""
    email = claims.get("email") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing subject")
    return AuthUser(user_id=user_id, email=email)


async def is_admin(user: AuthUser, db: DB) -> bool:
    """Authoritative admin check: queries the user_admins table.

    `AUTH_ENABLED=0` grants admin so local dev keeps full access without
    having to seed user_admins.
    """
    if not get_settings().auth_enabled:
        return True
    return await db.is_admin_user(user.user_id)


def get_optional_user(authorization: str | None = Header(default=None)) -> AuthUser | None:
    """For endpoints that behave differently when authenticated but don't require it."""
    if not authorization:
        return None
    try:
        return get_current_user(authorization)
    except HTTPException:
        return None


async def _get_admin_db(
    _user: AuthUser = Depends(get_current_user),
) -> AsyncIterator[DB]:
    """A no-query-param DB factory for admin-status lookups.

    The full request DB factory accepts ``project_id`` / ``staged_run_id`` as
    query params; routing those through endpoints that don't care leaks them
    into the OpenAPI surface, so admin checks use this instead.
    """
    prod = get_settings().is_prod_db
    db = await DB.create(run_id=str(uuid.uuid4()), prod=prod)
    try:
        yield db
    finally:
        await db.close()


async def require_admin(
    user: AuthUser = Depends(get_current_user),
    db: DB = Depends(_get_admin_db),
) -> AuthUser:
    if not await is_admin(user, db):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
