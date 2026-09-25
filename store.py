"""Room registry for UNO Online.

Room **game state** lives in the server process (see `server.py`), because a
``Player`` holds a live WebSocket object that cannot be serialised. What this
module owns is the surrounding *registry*: which room codes exist, when they
expire, and how many rooms a client may create.

Backend selection
-----------------
``REDIS_URL`` selects Redis; without it the same interface is served from
process memory. The memory backend is what the test suite and a laptop run
use, so the two must stay behaviourally identical — every method has a
matching test against both.

Why a registry at all: the host used to be able to type any room name, so
rooms accumulated forever and nothing bounded resource use. Codes are now
generated here, expire on a timer, and are capped per client.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol

# Ambiguous glyphs (0/O, 1/I/L) are excluded so a code read aloud or typed
# from a screenshot cannot be transcribed wrongly.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6

# A code is reserved while the host waits for the first joiner. Once somebody
# else joins, the room is "live" and the reservation timer stops mattering —
# the room then lives until it is empty (see EMPTY_ROOM_GRACE_SECONDS).
CODE_TTL_SECONDS = int(os.environ.get("CODE_TTL_SECONDS", "300"))

# A room with fewer than two players for this long is abandoned and reaped.
# This is the "AFK host" case: somebody creates a game and wanders off.
AFK_ROOM_SECONDS = int(os.environ.get("AFK_ROOM_SECONDS", "120"))

# Upper bounds that protect the host from runaway usage.
MAX_ACTIVE_ROOMS = int(os.environ.get("MAX_ROOMS", "200"))
MAX_ROOMS_PER_CLIENT = int(os.environ.get("MAX_ROOMS_PER_CLIENT", "3"))
CLIENT_ROOM_WINDOW_SECONDS = int(os.environ.get("ROOM_CREATE_WINDOW", "300"))

_ip_room_creations: dict[str, list[float]] = {}
_codes: dict[str, "CodeRecord"] = {}


@dataclass
class CodeRecord:
    """One reserved room code and the bookkeeping that expires it."""

    code: str
    host_token: str
    created_at: float
    player_count: int = 1
    live: bool = False          # a second player arrived, so no longer expiring
    last_activity: float = field(default_factory=time.time)

    @property
    def expires_at(self) -> Optional[float]:
        """When this code dies, or ``None`` once the room is live."""
        if self.live:
            return None
        return self.created_at + CODE_TTL_SECONDS

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "host_token": self.host_token,
            "created_at": self.created_at,
            "player_count": self.player_count,
            "live": self.live,
            "last_activity": self.last_activity,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "CodeRecord":
        return cls(
            code=payload["code"],
            host_token=payload["host_token"],
            created_at=float(payload["created_at"]),
            player_count=int(payload.get("player_count", 1)),
            live=bool(payload.get("live", False)),
            last_activity=float(payload.get("last_activity", time.time())),
        )


class RoomRegistry(Protocol):
    """The storage contract both backends implement."""

    async def reserve_code(self, host_token: str) -> Optional[str]: ...
    async def get(self, code: str) -> Optional[CodeRecord]: ...
    async def mark_live(self, code: str, players: int) -> None: ...
    async def touch(self, code: str, players: int) -> None: ...
    async def release(self, code: str) -> None: ...
    async def active_count(self) -> int: ...
    async def reap_expired(self) -> list[str]: ...
    async def allow_creation(self, client_id: str) -> bool: ...
    async def healthy(self) -> bool: ...


# Scoring extras layered on top of the official card values settled in
# server.py. These reward the two things the rules care about beyond raw cards.
WIN_BONUS_POINTS = int(os.environ.get("WIN_BONUS_POINTS", "25"))
UNO_CALL_POINTS = int(os.environ.get("UNO_CALL_POINTS", "5"))
UNO_CAUGHT_PENALTY = int(os.environ.get("UNO_CAUGHT_PENALTY", "10"))


@dataclass
class PlayerStats:
    """Lifetime record for one player name.

    Keyed by display name rather than a login identity, because the app has
    no accounts. Names are normalised to lowercase for the key so "Jerush"
    and "jerush" accumulate together.
    """

    name: str
    points: int = 0
    wins: int = 0
    rounds: int = 0
    uno_calls: int = 0
    best_round: int = 0
    last_seen: float = field(default_factory=time.time)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.rounds) if self.rounds else 0.0

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "points": self.points,
            "wins": self.wins,
            "rounds": self.rounds,
            "uno_calls": self.uno_calls,
            "best_round": self.best_round,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "PlayerStats":
        return cls(
            name=payload["name"],
            points=int(payload.get("points", 0)),
            wins=int(payload.get("wins", 0)),
            rounds=int(payload.get("rounds", 0)),
            uno_calls=int(payload.get("uno_calls", 0)),
            best_round=int(payload.get("best_round", 0)),
            last_seen=float(payload.get("last_seen", time.time())),
        )


_player_stats: dict[str, PlayerStats] = {}


def stats_key(name: str) -> str:
    return (name or "player").strip().lower()[:24]


class StatsStore(Protocol):
    """Lifetime scores, kept separately from the room registry."""

    async def record_round(self, players: list[dict]) -> None: ...
    async def top_players(self, limit: int = 10) -> list[dict]: ...
    async def get(self, name: str) -> Optional[PlayerStats]: ...


class MemoryStats:
    def __init__(self) -> None:
        self._stats = _player_stats

    async def record_round(self, players: list[dict]) -> None:
        for entry in players:
            name = stats_key(entry.get("name", ""))
            if not name:
                continue
            stats = self._stats.setdefault(name, PlayerStats(name=entry["name"].strip()[:24]))
            stats.points += int(entry.get("points", 0))
            stats.rounds += 1
            stats.uno_calls += int(entry.get("uno_calls", 0))
            stats.best_round = max(stats.best_round, int(entry.get("round_points", 0)))
            if entry.get("won"):
                stats.wins += 1
            stats.last_seen = time.time()

    async def top_players(self, limit: int = 10) -> list[dict]:
        rows = sorted(
            self._stats.values(),
            key=lambda s: (-s.points, -s.wins, s.name.lower()),
        )[:max(1, limit)]
        return [
            {**row.to_json(), "win_rate": round(row.win_rate, 3)}
            for row in rows
        ]

    async def get(self, name: str) -> Optional[PlayerStats]:
        return self._stats.get(stats_key(name))


class RedisStats:
    """Redis-backed lifetime scores.

    A sorted set holds the authoritative point totals so the leaderboard is a
    single ZREVRANGE, while a hash per player keeps the remaining detail.
    """

    BOARD_KEY = "uno:stats:board"
    PLAYER_PREFIX = "uno:stats:player:"

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self._redis = redis.from_url(url, decode_responses=True)

    async def record_round(self, players: list[dict]) -> None:
        for entry in players:
            name = stats_key(entry.get("name", ""))
            if not name:
                continue
            key = self.PLAYER_PREFIX + name
            existing = await self._redis.hgetall(key)
            stats = PlayerStats.from_json(existing) if existing else PlayerStats(name=entry["name"].strip()[:24])
            stats.points += int(entry.get("points", 0))
            stats.rounds += 1
            stats.uno_calls += int(entry.get("uno_calls", 0))
            stats.best_round = max(stats.best_round, int(entry.get("round_points", 0)))
            if entry.get("won"):
                stats.wins += 1
            stats.last_seen = time.time()
            await self._redis.hset(key, mapping={k: str(v) for k, v in stats.to_json().items()})
            await self._redis.zadd(self.BOARD_KEY, {name: stats.points})

    async def top_players(self, limit: int = 10) -> list[dict]:
        names = await self._redis.zrevrange(self.BOARD_KEY, 0, max(0, limit - 1))
        rows = []
        for name in names:
            payload = await self._redis.hgetall(self.PLAYER_PREFIX + name)
            if payload:
                stats = PlayerStats.from_json(payload)
                rows.append({**stats.to_json(), "win_rate": round(stats.win_rate, 3)})
        return rows

    async def get(self, name: str) -> Optional[PlayerStats]:
        payload = await self._redis.hgetall(self.PLAYER_PREFIX + stats_key(name))
        return PlayerStats.from_json(payload) if payload else None


def build_stats() -> StatsStore:
    """Mirror of build_registry: Redis when configured, memory otherwise."""
    url = os.environ.get("REDIS_URL")
    if not url:
        return MemoryStats()
    try:
        return RedisStats(url)
    except Exception:
        return MemoryStats()


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalise_code(raw: str) -> str:
    """Canonical form of a user-supplied code.

    People type lowercase, and paste codes with spaces or dashes, so the
    lookup key is folded rather than rejecting those inputs.
    """
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())[:CODE_LENGTH]


class MemoryRegistry:
    """Process-local registry: the default and the test double."""

    def __init__(self) -> None:
        self._codes: dict[str, CodeRecord] = _codes

    async def reserve_code(self, host_token: str) -> Optional[str]:
        if await self.active_count() >= MAX_ACTIVE_ROOMS:
            return None
        for _ in range(50):
            code = generate_code()
            if code not in self._codes:
                self._codes[code] = CodeRecord(code=code, host_token=host_token, created_at=time.time())
                return code
        return None

    async def get(self, code: str) -> Optional[CodeRecord]:
        return self._codes.get(normalise_code(code))

    async def mark_live(self, code: str, players: int) -> None:
        record = self._codes.get(normalise_code(code))
        if record:
            record.live = True
            record.player_count = players
            record.last_activity = time.time()

    async def touch(self, code: str, players: int) -> None:
        record = self._codes.get(normalise_code(code))
        if record:
            record.player_count = players
            record.last_activity = time.time()

    async def release(self, code: str) -> None:
        self._codes.pop(normalise_code(code), None)

    async def active_count(self) -> int:
        return len(self._codes)

    async def reap_expired(self) -> list[str]:
        now = time.time()
        dead = [
            code for code, record in self._codes.items()
            if not record.live and now > record.created_at + CODE_TTL_SECONDS
        ]
        for code in dead:
            self._codes.pop(code, None)
        return dead

    async def allow_creation(self, client_id: str) -> bool:
        now = time.time()
        recent = [
            stamp for stamp in _ip_room_creations.get(client_id, [])
            if now - stamp < CLIENT_ROOM_WINDOW_SECONDS
        ]
        if len(recent) >= MAX_ROOMS_PER_CLIENT:
            _ip_room_creations[client_id] = recent
            return False
        recent.append(now)
        _ip_room_creations[client_id] = recent
        return True

    async def healthy(self) -> bool:
        return True


class RedisRegistry:
    """Redis-backed registry, shared by every worker.

    Codes are stored as JSON strings with a TTL so Redis expires them even if
    the process dies. The per-client creation counter uses a sorted set scored
    by timestamp, which lets us drop old entries without a second key.
    """

    PREFIX = "uno:room:"
    CREATE_PREFIX = "uno:create:"

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # imported lazily so Redis stays optional

        self._redis = redis.from_url(url, decode_responses=True)

    async def reserve_code(self, host_token: str) -> Optional[str]:
        if await self.active_count() >= MAX_ACTIVE_ROOMS:
            return None
        for _ in range(50):
            code = generate_code()
            record = CodeRecord(code=code, host_token=host_token, created_at=time.time())
            # NX so two workers can never claim the same code.
            claimed = await self._redis.set(
                self.PREFIX + code,
                _dump(record),
                nx=True,
                ex=CODE_TTL_SECONDS,
            )
            if claimed:
                return code
        return None

    async def get(self, code: str) -> Optional[CodeRecord]:
        payload = await self._redis.get(self.PREFIX + normalise_code(code))
        return CodeRecord.from_json(_load(payload)) if payload else None

    async def mark_live(self, code: str, players: int) -> None:
        record = await self.get(code)
        if not record:
            return
        record.live = True
        record.player_count = players
        record.last_activity = time.time()
        # persist so the key outlives CODE_TTL_SECONDS; a live room is reaped
        # by the AFK sweep instead of by Redis's TTL.
        await self._redis.set(self.PREFIX + record.code, _dump(record))

    async def touch(self, code: str, players: int) -> None:
        record = await self.get(code)
        if not record:
            return
        record.player_count = players
        record.last_activity = time.time()
        if record.live:
            await self._redis.set(self.PREFIX + record.code, _dump(record))
        else:
            await self._redis.set(self.PREFIX + record.code, _dump(record), ex=CODE_TTL_SECONDS)

    async def release(self, code: str) -> None:
        await self._redis.delete(self.PREFIX + normalise_code(code))

    async def active_count(self) -> int:
        return len(await self._scan_codes())

    async def reap_expired(self) -> list[str]:
        now = time.time()
        dead = []
        for code in await self._scan_codes():
            record = await self.get(code)
            if not record:
                continue
            if record.live:
                # live rooms are reaped by the AFK rule, not the code TTL
                if now - record.last_activity > AFK_ROOM_SECONDS:
                    dead.append(code)
            elif now > record.created_at + CODE_TTL_SECONDS:
                dead.append(code)
        for code in dead:
            await self.release(code)
        return dead

    async def allow_creation(self, client_id: str) -> bool:
        key = self.CREATE_PREFIX + client_id
        now = time.time()
        await self._redis.zremrangebyscore(key, 0, now - CLIENT_ROOM_WINDOW_SECONDS)
        if await self._redis.zcard(key) >= MAX_ROOMS_PER_CLIENT:
            return False
        await self._redis.zadd(key, {str(now): now})
        await self._redis.expire(key, CLIENT_ROOM_WINDOW_SECONDS)
        return True

    async def healthy(self) -> bool:
        try:
            await self._redis.ping()
            return True
        except Exception:
            return False

    async def _scan_codes(self) -> list[str]:
        codes = []
        async for key in self._redis.scan_iter(match=self.PREFIX + "*"):
            codes.append(key[len(self.PREFIX):])
        return codes


def _dump(record: CodeRecord) -> str:
    import json

    return json.dumps(record.to_json())


def _load(payload: str) -> dict:
    import json

    return json.loads(payload)


def build_registry() -> RoomRegistry:
    """Pick the backend from the environment, falling back safely.

    A bad REDIS_URL must not take the server down — an unreachable cache is
    still worse than no cache, but refusing to boot is worse again.
    """
    url = os.environ.get("REDIS_URL")
    if not url:
        return MemoryRegistry()
    try:
        return RedisRegistry(url)
    except Exception:
        return MemoryRegistry()
