"""Integration tests for room creation, code validation and AFK reaping.

These drive the FastAPI handlers directly with a minimal fake request rather
than through httpx, so the test suite needs no extra dependency.
"""

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import store
import server
from server import create_room, rooms


class FakeRequest:
    """Just enough of a Starlette Request for the handler under test."""

    def __init__(self, client_ip="10.0.0.1"):
        self.headers = {"CF-Connecting-IP": client_ip}
        self.client = type("C", (), {"host": client_ip})()


class RoomCreationTests(unittest.TestCase):
    def setUp(self):
        store._ip_room_creations.clear()
        store._codes.clear()
        rooms.clear()
        self.registry = store.MemoryRegistry()
        self.registry._codes.clear()
        patcher = patch.object(server, "registry", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_room_returns_a_reserved_code(self):
        response = asyncio.run(create_room(FakeRequest()))

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(len(payload["code"]), store.CODE_LENGTH)
        self.assertEqual(payload["expires_in"], store.CODE_TTL_SECONDS)
        # the code must actually be registered, or the join would be refused
        self.assertIsNotNone(asyncio.run(self.registry.get(payload["code"])))

    def test_each_created_room_gets_a_distinct_code(self):
        # distinct clients, because one client is capped at MAX_ROOMS_PER_CLIENT
        codes = {
            json.loads(asyncio.run(create_room(FakeRequest(f"10.0.0.{index}"))).body)["code"]
            for index in range(5)
        }
        self.assertEqual(len(codes), 5)

    def test_creation_is_capped_per_client(self):
        statuses = [
            asyncio.run(create_room(FakeRequest())).status_code
            for _ in range(store.MAX_ROOMS_PER_CLIENT + 2)
        ]

        self.assertTrue(all(status == 200 for status in statuses[:store.MAX_ROOMS_PER_CLIENT]))
        self.assertTrue(all(status == 429 for status in statuses[store.MAX_ROOMS_PER_CLIENT:]))

        # a different client is unaffected by the first one hitting its cap
        self.assertEqual(asyncio.run(create_room(FakeRequest("10.0.0.2"))).status_code, 200)

    def test_capacity_limit_returns_503(self):
        with patch.object(self.registry, "active_count", new=AsyncMock(return_value=store.MAX_ACTIVE_ROOMS)):
            response = asyncio.run(create_room(FakeRequest()))

        self.assertEqual(response.status_code, 503)


class JoinValidationTests(unittest.TestCase):
    """The WebSocket endpoint refuses unknown codes.

    Driving the real handshake needs a websocket client, so the validation
    rule itself is asserted directly: an unresolved code must not create a
    room. This is the behaviour the endpoint branches on.
    """

    def setUp(self):
        store._codes.clear()
        rooms.clear()
        self.registry = store.MemoryRegistry()
        self.registry._codes.clear()
        patcher = patch.object(server, "registry", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unknown_and_badly_typed_codes_do_not_resolve(self):
        self.assertIsNone(asyncio.run(self.registry.get("ZZZZZZ")))
        self.assertIsNone(asyncio.run(self.registry.get("")))
        self.assertIsNone(asyncio.run(self.registry.get("not a code")))

    def test_a_reserved_code_resolves_case_insensitively(self):
        response = asyncio.run(create_room(FakeRequest()))
        code = json.loads(response.body)["code"]

        self.assertIsNotNone(asyncio.run(self.registry.get(code.lower())))
        self.assertIsNotNone(asyncio.run(self.registry.get(f" {code} ")))


class AfkReaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_reservation_closes_the_waiting_host(self):
        registry = store.MemoryRegistry()
        registry._codes.clear()
        # a code reserved long ago that nobody joined
        registry._codes["OLD123"] = store.CodeRecord(
            code="OLD123", owner_key="h", created_at=0.0
        )
        ws = AsyncMock()
        player = server.Player(token="t", peer_id="p", seat=1, name="Host", ws=ws)
        room = server.Room(room_id="OLD123")
        room.players["t"] = player
        rooms["OLD123"] = room

        with patch.object(server, "registry", registry):
            reaped = await registry.reap_expired()

        self.assertIn("OLD123", reaped)

    async def test_live_room_with_one_player_is_afk_after_the_window(self):
        registry = store.MemoryRegistry()
        registry._codes.clear()
        registry._codes["LIVE12"] = store.CodeRecord(
            code="LIVE12",
            owner_key="h",
            created_at=time.time(),
            live=True,
            last_activity=time.time() - (store.AFK_ROOM_SECONDS + 5),
        )
        record = await registry.get("LIVE12")

        # the code TTL no longer applies once live
        self.assertTrue(record.live)
        self.assertIsNone(record.expires_at)
        # but the AFK rule does
        self.assertGreater(
            time.time() - record.last_activity, store.AFK_ROOM_SECONDS
        )

    async def test_reaper_keeps_a_freshly_created_room(self):
        registry = store.MemoryRegistry()
        registry._codes.clear()
        code = await registry.reserve_code("host")

        reaped = await registry.reap_expired()

        self.assertEqual(reaped, [])
        self.assertIsNotNone(await registry.get(code))


class HostAssignmentTests(unittest.IsolatedAsyncioTestCase):
    """The creator of the code hosts, not whoever reaches the table first."""

    async def asyncSetUp(self):
        store._codes.clear()
        self.registry = store.MemoryRegistry()
        self.registry._codes.clear()

    async def test_owner_of_returns_the_reserving_client(self):
        code = await self.registry.reserve_code("client-abc")

        self.assertEqual(await self.registry.owner_of(code), "client-abc")
        self.assertEqual(await self.registry.owner_of(code.lower()), "client-abc")

    async def test_owner_of_is_none_for_an_unknown_code(self):
        self.assertIsNone(await self.registry.owner_of("NOPE12"))

    def _room(self, host_key="creator-key"):
        return server.Room(room_id="ABCDEF", host_key=host_key)

    def test_creator_arriving_first_hosts_immediately(self):
        room = self._room()

        self.assertTrue(server.assign_room_host(room, "creator", "creator-key"))
        self.assertEqual(room.host_token, "creator")
        self.assertEqual(room.owner_token, "creator")

    def test_guest_arriving_first_hosts_only_in_the_meantime(self):
        """A friend opening the invite link must not keep the host controls.

        The guest hosts while the creator is still on their way, then hands
        over the moment the creator joins with the matching key.
        """
        room = self._room()

        self.assertTrue(server.assign_room_host(room, "guest", None))
        self.assertEqual(room.host_token, "guest")
        self.assertIsNone(room.owner_token)

        self.assertTrue(server.assign_room_host(room, "creator", "creator-key"))
        self.assertEqual(room.host_token, "creator")
        self.assertEqual(room.owner_token, "creator")

    def test_a_guest_never_takes_the_host_with_the_wrong_key(self):
        room = self._room()
        server.assign_room_host(room, "first", None)

        self.assertFalse(server.assign_room_host(room, "impostor", "some-other-key"))
        self.assertEqual(room.host_token, "first")
        self.assertIsNone(room.owner_token)

    def test_creator_arriving_late_in_a_full_round_still_takes_over(self):
        room = self._room()
        server.assign_room_host(room, "first", None)
        server.assign_room_host(room, "second", None)

        self.assertTrue(server.assign_room_host(room, "creator", "creator-key"))
        self.assertEqual(room.host_token, "creator")

    def test_a_late_guest_cannot_reclaim_the_host_from_the_creator(self):
        """Only the key-matching player holds the host, once it is claimed."""
        room = self._room()
        server.assign_room_host(room, "creator", "creator-key")

        self.assertFalse(server.assign_room_host(room, "latecomer", None))
        self.assertFalse(server.assign_room_host(room, "latecomer", "wrong-key"))
        self.assertEqual(room.host_token, "creator")

    def test_rooms_without_a_host_key_keep_first_come_first_served(self):
        """A room whose code record is gone must still get a host."""
        room = self._room(host_key=None)

        self.assertTrue(server.assign_room_host(room, "first", None))
        self.assertFalse(server.assign_room_host(room, "second", None))
        self.assertEqual(room.host_token, "first")

    async def test_host_key_is_resolved_once_and_survives_the_code_expiring(self):
        """A running game must not lose track of its host.

        A reserved code expires after CODE_TTL_SECONDS even while a game is
        being played, after which the lookup would only ever return None. The
        key is cached on the room, so it is read once and kept.
        """
        code = await self.registry.reserve_code("client-abc")
        room = server.Room(room_id=code)
        with patch.object(server, "registry", self.registry):
            await server.resolve_room_host_key(room, code)
            self.assertEqual(room.host_key, "client-abc")

            # the reservation expires mid-game
            await self.registry.release(code)
            await server.resolve_room_host_key(room, code)

        self.assertEqual(room.host_key, "client-abc", "the cached key must survive")
        self.assertTrue(server.assign_room_host(room, "creator", "client-abc"))


if __name__ == "__main__":
    unittest.main()
