import base64
import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from unittest.mock import patch

from server import (
    Player,
    Room,
    app,
    broadcast_peers,
    build_ice_servers,
    can_kick_player,
    leave_on_page_exit,
    public_state,
    rooms,
)


class IceConfigTests(unittest.TestCase):
    def test_stun_only_when_turn_is_not_configured(self):
        servers = build_ice_servers("turn:turn.example.test:3478", None, now=1000)

        self.assertTrue(servers)
        self.assertTrue(all(server["urls"].startswith("stun:") for server in servers))

    def test_turn_uses_expiring_coturn_rest_credentials(self):
        urls = (
            "turn:turn.example.test:3478?transport=udp,"
            "turn:turn.example.test:3478?transport=tcp"
        )
        servers = build_ice_servers(urls, "test-shared-secret", 600, now=1000)
        turn_servers = [
            server for server in servers
            if server["urls"].startswith(("turn:", "turns:"))
        ]

        self.assertEqual(len(turn_servers), 3)
        username = "1600:uno"
        expected = base64.b64encode(
            hmac.new(b"test-shared-secret", username.encode(), hashlib.sha1).digest()
        ).decode("ascii")
        self.assertTrue(all(server["username"] == username for server in turn_servers))
        self.assertTrue(all(server["credential"] == expected for server in turn_servers))
        self.assertEqual(turn_servers[-1]["urls"], "turns:turn.example.test:5349")

    def test_turn_ttl_is_bounded(self):
        servers = build_ice_servers("turn:turn.example.test:3478", "secret", 999999, now=1000)
        turn_server = next(server for server in servers if server["urls"].startswith("turn:"))

        self.assertEqual(turn_server["username"], "87400:uno")


class PublicStateSecurityTests(unittest.TestCase):
    def setUp(self):
        self.room = Room(room_id="test")
        self.player = Player(
            token="private-reconnect-token",
            peer_id="ephemeral-webrtc-peer-id",
            seat=1,
            name="Player",
        )
        self.room.players[self.player.token] = self.player

    def test_public_state_does_not_expose_reconnect_token(self):
        seat = public_state(self.room)["seats"]["1"]

        self.assertNotIn("token", seat)

    def test_peer_id_is_separate_from_reconnect_token(self):
        self.assertNotEqual(self.player.peer_id, self.player.token)


class PeerRosterTests(unittest.IsolatedAsyncioTestCase):
    async def test_peer_roster_sends_peer_id_but_never_reconnect_token(self):
        room = Room(room_id="test")
        players = [
            Player(
                token=f"private-reconnect-token-{seat}",
                peer_id=f"ephemeral-peer-id-{seat}",
                seat=seat,
                name=f"Player {seat}",
                ws=AsyncMock(),
            )
            for seat in (1, 2)
        ]
        room.players = {player.token: player for player in players}

        await broadcast_peers(room)

        for player in players:
            sent = player.ws.send_text.await_args.args[0]
            self.assertIn(player.peer_id, sent)
            self.assertNotIn(player.token, sent)


class PlayerLifecycleTests(unittest.TestCase):
    def setUp(self):
        rooms.clear()
        self.room = Room(room_id="lifecycle")
        self.host = Player(
            token="host-token", peer_id="host-peer", seat=1, name="Host", ws=AsyncMock()
        )
        self.guest = Player(
            token="guest-token", peer_id="guest-peer", seat=2, name="Guest", ws=AsyncMock()
        )
        self.room.players = {self.host.token: self.host, self.guest.token: self.guest}
        self.room.host_token = self.host.token
        rooms[self.room.room_id] = self.room

    def tearDown(self):
        rooms.clear()

    def test_only_host_can_kick_another_player(self):
        self.assertTrue(can_kick_player(self.room, self.host, self.guest))
        self.assertFalse(can_kick_player(self.room, self.guest, self.host))
        self.assertFalse(can_kick_player(self.room, self.host, self.host))

    def test_page_exit_endpoint_releases_seat_immediately(self):
        class FakeRequest:
            async def json(self):
                return {"room": "lifecycle", "token": "guest-token"}

        async def run_test():
            with (
                patch("server.broadcast_state", new=AsyncMock()),
                patch("server.broadcast_peers", new=AsyncMock()),
                patch("server.broadcast_notice", new=AsyncMock()),
                patch("server.send_hand", new=AsyncMock()),
            ):
                return await leave_on_page_exit(FakeRequest())

        from fastapi.responses import JSONResponse
        import asyncio

        response = asyncio.run(run_test())

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)["left"])
        self.assertNotIn(self.guest.token, self.room.players)
        self.assertEqual(len(self.room.players), 1)


if __name__ == "__main__":
    unittest.main()
