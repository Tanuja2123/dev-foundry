"""
API authentication with Microsoft Entra ID.

The frontend signs the user in with MSAL and sends the ID token as `Authorization: Bearer <token>`.
The token is validated here (signature from the tenant's signing keys, issuer, audience = app client id, expiry)
and the user is identified by the immutable Entra object id (`oid`), as Microsoft recommends:
https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference
"""
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Tuple

import jwt
from fastapi import Header, HTTPException, status

from app.core.logger import logger

LEEWAY_SECONDS = 60          # tolerated clock skew
JWKS_CACHE_SECONDS = 3600    # signing keys are cached and refreshed hourly


@dataclass(frozen=True)
class CurrentUser:
    user_id: str                 # Entra object id (oid): immutable, used as the owner of conversations
    tenant_id: str
    username: Optional[str]      # preferred_username (display only, can change)
    display_name: Optional[str]  # name (display only)
    email: Optional[str]


class AuthError(Exception):
    pass


def get_auth_settings() -> Tuple[Optional[str], Optional[str]]:
    """(tenant id, client id). Falls back to the frontend's VITE_* values in the shared root .env."""
    tenant = (os.getenv("AZURE_AD_TENANT_ID") or os.getenv("VITE_AZURE_TENANT_ID") or "").strip() or None
    client = (os.getenv("AZURE_AD_CLIENT_ID") or os.getenv("VITE_AZURE_CLIENT_ID") or "").strip() or None
    return tenant, client


@lru_cache(maxsize=4)
def _jwks_client(tenant_id: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
                           cache_keys=True, lifespan=JWKS_CACHE_SECONDS)


def validate_token(token: str, tenant_id: str, client_id: str, jwks_client=None) -> CurrentUser:
    """Validates a v2.0 ID token issued by the tenant for this app. Raises jwt.PyJWTError / AuthError."""
    signing_key = (jwks_client or _jwks_client(tenant_id)).get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=client_id,
        issuer=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        leeway=LEEWAY_SECONDS,
        options={"require": ["exp", "iat", "aud", "iss"]},
    )
    if not claims.get("oid"):
        raise AuthError("The token has no 'oid' claim (the 'profile' scope is required).")
    if claims.get("tid") and claims["tid"] != tenant_id:
        raise AuthError("The token was issued for a different tenant.")
    return CurrentUser(
        user_id=claims["oid"],
        tenant_id=claims.get("tid") or tenant_id,
        username=claims.get("preferred_username"),
        display_name=claims.get("name"),
        email=claims.get("email") or claims.get("preferred_username"),
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
                         headers={"WWW-Authenticate": "Bearer"})


def get_current_user(authorization: Optional[str] = Header(None)) -> CurrentUser:
    """FastAPI dependency: the signed-in user, or 401. Also records the user in the AppUser table."""
    tenant_id, client_id = get_auth_settings()
    if not tenant_id or not client_id:
        logger.error("[Auth] AZURE_AD_TENANT_ID / AZURE_AD_CLIENT_ID (or VITE_AZURE_*) are not configured.")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Authentication is not configured on the server.")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("Sign in required.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        user = validate_token(token, tenant_id, client_id)
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Your session has expired. Please sign in again.")
    except (jwt.PyJWTError, AuthError) as e:
        logger.warning(f"[Auth] Rejected token: {type(e).__name__}: {e}")
        raise _unauthorized("Invalid sign-in token.")

    from app.services.user_store import record_user
    record_user(user)
    return user
