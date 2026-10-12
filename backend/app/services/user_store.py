"""Signed-in users (AppUser table, see backend/db/migrations/003_users_and_ownership.sql)."""
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict

from app.core.auth import CurrentUser
from app.core.logger import logger

USER_TABLE = "AppUser"
REFRESH_SECONDS = 300  # last_seen_at is written at most every 5 minutes per user

_last_recorded: Dict[str, float] = {}


def _default_client():
    from app.services.conversation_service import get_supabase_client
    return get_supabase_client()


# Tests replace this with a fake client factory.
client_factory: Callable[[], Any] = _default_client


def record_user(user: CurrentUser) -> None:
    """Creates/updates the user's AppUser row. Never blocks the request if it fails."""
    now = time.monotonic()
    if now - _last_recorded.get(user.user_id, -REFRESH_SECONDS) < REFRESH_SECONDS:
        return
    try:
        client_factory().table(USER_TABLE).upsert({
            "user_id": user.user_id,
            "tenant_id": user.tenant_id,
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id").execute()
        _last_recorded[user.user_id] = now
    except Exception as e:
        logger.warning(f"[UserStore] Could not record user '{user.user_id}' "
                       f"(run backend/db/migrations/003_users_and_ownership.sql?): {e}")
