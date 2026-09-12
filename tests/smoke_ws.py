"""Manual smoke test: drives two WebSocket clients through join + start.

Not part of the pytest suite (it needs a running server). Run it against a
live instance to sanity-check the protocol end to end:

    python tests/smoke_ws.py ws://127.0.0.1:8999/ws/smoketest
"""

import asyncio
import json
import sys

import websockets


async def recv_until(ws, wanted, timeout=5.0):
    """Read messages until one of `wanted` types arrives; return it."""
    async def _loop():
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") in wanted:
                return msg
    return await asyncio.wait_for(_loop(), timeout)


async def main(url: str) -> int:
    async with websockets.connect(url) as a, websockets.connect(url) as b:
        await a.send(json.dumps({"type": "join", "name": "Alice"}))
        joined_a = await recv_until(a, {"joined"})
        assert joined_a["seat"] == 1, joined_a
        print(f"OK  Alice joined seat {joined_a['seat']}")

        await b.send(json.dumps({"type": "join", "name": "Bob"}))
        joined_b = await recv_until(b, {"joined"})
        assert joined_b["seat"] == 2, joined_b
        print(f"OK  Bob joined seat {joined_b['seat']}")

        await recv_until(a, {"hand"})
        print("OK  received private hand")

        await a.send(json.dumps({"type": "start"}))
        state = await recv_until(a, {"state"}, timeout=5.0)
        while not state["state"]["started"]:
            state = await recv_until(a, {"state"}, timeout=5.0)
        assert state["state"]["player_count"] == 2
        assert state["state"]["top_card"]["color"] in ("red", "yellow", "green", "blue")
        print(f"OK  game started; top card {state['state']['top_card']}, "
              f"turn seat {state['state']['turn_seat']}")

        # Ping/pong round-trip.
        await a.send(json.dumps({"type": "ping"}))
        await recv_until(a, {"pong"}, timeout=5.0)
        print("OK  ping -> pong")

    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8999/ws/smoketest"
    sys.exit(asyncio.run(main(target)))
