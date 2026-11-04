"""Short-lived slot holds in Redis.

Key: hold:{business}:{staff|any}:{start epoch}. A hold is created with SET NX PX so only one
caller wins a given key, and released with a compare-and-delete Lua script so an expired hold
that was re-taken by someone else can never be deleted by its previous owner.
Expiry is also stored inside the value so it is exact even if the Redis TTL lags.
"""

import json
import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_redis import get_redis_connection
from redis.exceptions import RedisError

from apps.backoffice import config_service

logger = logging.getLogger(__name__)

RELEASE_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
local d = cjson.decode(v)
if d.token == ARGV[1] then
  redis.call('DEL', KEYS[1])
  redis.call('DEL', KEYS[2])
  return 1
end
return 0
"""


@dataclass(frozen=True)
class Hold:
    token: str
    user_id: int
    business_id: int
    staff_id: int | None
    start: datetime
    end: datetime
    expires_at: datetime
    service_ids: tuple[int, ...] = ()

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": self.token,
                "user_id": self.user_id,
                "business_id": self.business_id,
                "staff_id": self.staff_id,
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
                "expires_at": self.expires_at.isoformat(),
                "service_ids": list(self.service_ids),
            }
        )

    @staticmethod
    def from_json(raw: bytes | str) -> "Hold":
        d: dict[str, Any] = json.loads(raw)
        start, end, exp = (parse_datetime(d[k]) for k in ("start", "end", "expires_at"))
        assert start and end and exp
        return Hold(
            d["token"],
            d["user_id"],
            d["business_id"],
            d["staff_id"],
            start,
            end,
            exp,
            tuple(d.get("service_ids", ())),
        )


def hold_ttl_seconds() -> int:
    return int(config_service.get("slot_hold_seconds"))


def hold_key(business_id: int, staff_id: int | None, start: datetime, slot: int = 0) -> str:
    """`slot` distinguishes concurrent holds of one start time in capacity mode."""
    who = staff_id if staff_id is not None else "any"
    suffix = f":{slot}" if slot else ""
    return f"hold:{business_id}:{who}:{int(start.timestamp())}{suffix}"


def token_key(token: str) -> str:
    return f"holdtok:{token}"


def _conn() -> Any:
    return get_redis_connection("default")


@contextmanager
def hold_lock(business_id: int, staff_id: int | None) -> Iterator[None]:
    """Serialise check-then-hold for one bookable resource."""
    with cache.lock(  # type: ignore[attr-defined]
        f"lock:hold:{business_id}:{staff_id if staff_id is not None else 'any'}",
        timeout=10,
        blocking_timeout=10,
    ):
        yield


def place_hold(
    *,
    user_id: int,
    business_id: int,
    staff_id: int | None,
    start: datetime,
    end: datetime,
    service_ids: tuple[int, ...] = (),
    capacity: int = 1,
    ttl_seconds: int | None = None,
) -> Hold | None:
    """Create a hold with SET NX PX. None when every slot of that start is already held.

    Raises redis.exceptions.RedisError when Redis is unavailable.
    """
    ttl = ttl_seconds or hold_ttl_seconds()
    hold = Hold(
        token=secrets.token_urlsafe(24),
        user_id=user_id,
        business_id=business_id,
        staff_id=staff_id,
        start=start,
        end=end,
        expires_at=timezone.now() + timedelta(seconds=ttl),
        service_ids=service_ids,
    )
    conn = _conn()
    for slot in range(max(capacity, 1)):
        key = hold_key(business_id, staff_id, start, slot)
        if conn.set(key, hold.to_json(), nx=True, px=ttl * 1000):
            conn.set(token_key(hold.token), key, px=ttl * 1000)
            return hold
    return None


def get_hold(token: str) -> Hold | None:
    """Return the live hold for a token, or None when unknown/expired. Raises RedisError."""
    conn = _conn()
    key = conn.get(token_key(token))
    if key is None:
        return None
    raw = conn.get(key)
    if raw is None:
        return None
    hold = Hold.from_json(raw)
    if hold.token != token or hold.expires_at <= timezone.now():
        return None
    return hold


def release_hold(token: str) -> bool:
    """Compare-and-delete. True when this token's hold was removed. Raises RedisError."""
    conn = _conn()
    key = conn.get(token_key(token))
    if key is None:
        return False
    key_s = key.decode() if isinstance(key, bytes) else key
    script = conn.register_script(RELEASE_LUA)
    return bool(script(keys=[key_s, token_key(token)], args=[token]))


def active_holds(
    business_id: int,
    window_start: datetime,
    window_end: datetime,
    ignore_tokens: frozenset[str] = frozenset(),
) -> list[Hold]:
    """Unexpired holds overlapping the window. Redis outage degrades to no holds (logged)."""
    try:
        conn = _conn()
        keys = list(conn.scan_iter(match=f"hold:{business_id}:*", count=200))
        raws = conn.mget(keys) if keys else []
    except RedisError:
        logger.warning("Redis unavailable while reading holds; ignoring holds")
        return []
    now = timezone.now()
    holds = []
    for raw in raws:
        if raw is None:
            continue
        hold = Hold.from_json(raw)
        if (
            hold.token not in ignore_tokens
            and hold.expires_at > now
            and hold.start < window_end
            and hold.end > window_start
        ):
            holds.append(hold)
    return holds
