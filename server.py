"""
UNO Online — self-hosted 4-player multiplayer server with persistent scoring,
live video/audio (WebRTC mesh), reconnect-safe sessions, and a manual
"exit table" flow.

Scoring (official UNO style): when a player goes out, they score points for
cards left in ALL opponents' hands — number cards at face value,
Skip/Reverse/Draw-2 = 20, Wild/Wild-Draw-4 = 50. Totals persist across
rounds until the server restarts, and are sent to every client as a
leaderboard in the game_over message.

Turn flow (automatic, no Pass button):
  - On your turn, either play a playable card, or draw exactly one card.
  - When you draw, the server checks the drawn card against the discard pile:
      * If it IS playable, you keep the turn and MUST play that exact card
        (the only playable card this turn). The client highlights only it.
      * If it is NOT playable, your turn ends immediately and play moves
        to the next player automatically. No "Pass" button is ever shown.
  This is the standard "draw one, play it if you can" UNO rule, with the
  pass step removed entirely — the server decides for you.

UNO calls: a player may hit "Call UNO" as soon as they hold 2 cards
(declaring it right before playing down to 1) all the way through
holding just 1 card. If they never call it and get caught holding
exactly 1 card, any other player can catch them for a penalty draw.

Session persistence: each player gets a private `token` on join, saved by
the browser in localStorage. Refreshing the page, a dropped WiFi
connection, or the phone screen locking will NOT lose your seat — the
client automatically rejoins the same room with the same token and the
server hands your seat + hand straight back. Grace period for a dropped
connection is RECONNECT_GRACE_SECONDS; if you are the active player and
stay disconnected for AUTO_SKIP_DISCONNECTED_SECONDS, the server draws
(and thus passes) on your behalf so the table doesn't stall for
everyone else.

Run:
    pip install -r requirements.txt     # fastapi + uvicorn[standard]
    python server.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("uno")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

MAX_SEATS = 4
UNO_CATCH_PENALTY = 2
DRAW2_PENALTY = 2
DRAW4_PENALTY = 4
ACTION_CARD_POINTS = 20
WILD_CARD_POINTS = 50

RECONNECT_GRACE_SECONDS = 180        # how long a dropped seat is held open
CLEANUP_INTERVAL_SECONDS = 30        # how often we sweep for expired seats
AUTO_SKIP_DISCONNECTED_SECONDS = 25  # how long the *active* player can be
                                      # disconnected before we act for them
WATCHDOG_INTERVAL_SECONDS = 5

ROOM_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")

COLORS = ["red", "yellow", "green", "blue"]
NUMBER_VALUES = [str(n) for n in range(10)]
ACTION_VALUES = ["skip", "reverse", "draw2"]


# --------------------------------------------------------------------------
# Deck
# --------------------------------------------------------------------------

def _fisher_yates_shuffle(items: list) -> None:
    """In-place Fisher–Yates shuffle using a cryptographically stronger
    random source than the default Mersenne Twister, for a genuinely
    unpredictable deal every round."""
    for i in range(len(items) - 1, 0, -1):
        j = random.SystemRandom().randint(0, i)
        items[i], items[j] = items[j], items[i]


def shuffle_deck(deck: list) -> None:
    """Shuffle the deck in place with Fisher–Yates."""
    _fisher_yates_shuffle(deck)


def build_deck() -> list[dict]:
    """Standard 108-card UNO deck, well shuffled."""
    deck: list[dict] = []
    for color in COLORS:
        deck.append({"color": color, "value": "0"})
        for v in NUMBER_VALUES[1:]:
            deck.append({"color": color, "value": v})
            deck.append({"color": color, "value": v})
        for v in ACTION_VALUES:
            deck.append({"color": color, "value": v})
            deck.append({"color": color, "value": v})
    for _ in range(4):
        deck.append({"color": "black", "value": "wild"})
        deck.append({"color": "black", "value": "wild4"})
    for card in deck:
        card["id"] = f"{card['color']}_{card['value']}_{uuid.uuid4().hex[:8]}"
    shuffle_deck(deck)
    return deck


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

@dataclass
class Player:
    token: str
    seat: int
    name: str
    ws: Optional[WebSocket] = None
    hand: list = field(default_factory=list)
    connected: bool = True
    called_uno: bool = False
    score: int = 0                       # cumulative points
    last_seen: float = field(default_factory=time.time)
    cam_on: bool = False
    mic_on: bool = False
    drew_this_turn: bool = False   # player drew a playable card this turn:
                                    # they MUST play that exact card now
    last_drawn_card_id: Optional[str] = None


@dataclass
class Room:
    room_id: str
    players: dict[str, Player] = field(default_factory=dict)
    deck: list = field(default_factory=list)
    discard: list = field(default_factory=list)
    turn_seat: Optional[int] = None
    direction: int = 1
    current_color: Optional[str] = None
    started: bool = False
    host_token: Optional[str] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def seat_order(self) -> list[int]:
        return sorted(p.seat for p in self.players.values())

    def player_by_seat(self, seat: Optional[int]) -> Optional[Player]:
        if seat is None:
            return None
        for p in self.players.values():
            if p.seat == seat:
                return p
        return None

    def next_free_seat(self) -> Optional[int]:
        used = {p.seat for p in self.players.values()}
        for s in range(1, MAX_SEATS + 1):
            if s not in used:
                return s
        return None


rooms: dict[str, Room] = {}
_rooms_guard = asyncio.Lock()


async def get_or_create_room(room_id: str) -> Room:
    async with _rooms_guard:
        if room_id not in rooms:
            rooms[room_id] = Room(room_id=room_id)
        return rooms[room_id]


# --------------------------------------------------------------------------
# Deck / turn helpers
# --------------------------------------------------------------------------

def reshuffle_discard_into_deck(room: Room) -> None:
    if len(room.discard) <= 1:
        return
    top = room.discard[-1]
    rest = room.discard[:-1]
    shuffle_deck(rest)
    room.deck = rest
    room.discard = [top]


def draw_from_deck(room: Room, n: int) -> list[dict]:
    drawn: list[dict] = []
    for _ in range(n):
        if not room.deck:
            reshuffle_discard_into_deck(room)
            if not room.deck:
                break
        drawn.append(room.deck.pop())
    return drawn


def advance_turn(room: Room, steps: int = 1) -> None:
    """Move `steps` seats around the table in the current direction.

    `steps` is expressed in "seats to hop", not "players to skip":
      - a normal card play -> steps=1 (go to the very next seat)
      - a Skip card        -> steps=2 (hop over exactly one seat)
      - Draw-2 / Wild-4    -> two separate advance_turn(1) calls: one to
        land on the victim (who draws), one more to hop past them.

    This is the single source of truth for whose turn it is, so every
    action card funnels through here — that keeps Skip/Reverse/Draw-2/
    Wild-4 behaviour unambiguous and easy to verify.
    """
    order = room.seat_order()
    if not order:
        room.turn_seat = None
        return
    if room.turn_seat not in order:
        # The previous holder of the turn is gone (left / seat freed).
        # Fall back to the first seat in table order rather than crash.
        room.turn_seat = order[0]
        return
    idx = order.index(room.turn_seat)
    idx = (idx + steps * room.direction) % len(order)
    room.turn_seat = order[idx]


def card_playable(room: Room, card: dict) -> bool:
    if card["color"] == "black":
        return True
    if not room.discard:
        return True
    top_value = room.discard[-1]["value"]
    return card["color"] == room.current_color or card["value"] == top_value


def player_has_playable(room: Room, player: Player) -> bool:
    """True if the player currently holds at least one card that can be
    played on the discard pile. Used to decide whether a drawn turn can
    end automatically (no playable card) or must wait for a play."""
    return any(card_playable(room, c) for c in player.hand)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def card_points(card: dict) -> int:
    """Point value of a card left in a loser's hand (official rules)."""
    if card["color"] == "black":
        return WILD_CARD_POINTS
    if card["value"] in ACTION_VALUES:
        return ACTION_CARD_POINTS
    try:
        return int(card["value"])
    except ValueError:
        return 0


def settle_round(room: Room, winner: Player) -> int:
    """Winner scores the total value of cards left in opponents' hands."""
    pts = 0
    for p in room.players.values():
        if p is not winner:
            for c in p.hand:
                pts += card_points(c)
    winner.score += pts
    return pts


def leaderboard(room: Room, winner: Optional[Player] = None, round_points: int = 0) -> list[dict]:
    rows = [
        {
            "seat": p.seat,
            "name": p.name,
            "score": p.score,
            "round_points": round_points if p is winner else 0,
        }
        for p in room.players.values()
    ]
    rows.sort(key=lambda r: (-r["score"], r["seat"]))
    return rows


# --------------------------------------------------------------------------
# Game actions
# --------------------------------------------------------------------------

def clear_all_hands(room: Room) -> None:
    """Called whenever a round is NOT in progress.

    Nobody holds cards outside a live round — if the round is over or was
    abandoned (a seat emptied), every hand is emptied so no client can show
    cards that were never dealt for the current match.
    """
    for p in room.players.values():
        p.hand = []
        p.called_uno = False
        p.drew_this_turn = False
        p.last_drawn_card_id = None


def start_game(room: Room) -> None:
    room.deck = build_deck()
    room.discard = []
    room.direction = 1
    for p in room.players.values():
        p.hand = draw_from_deck(room, 7)
        p.called_uno = False
        p.drew_this_turn = False
        p.last_drawn_card_id = None

    first = draw_from_deck(room, 1)[0]
    while first["color"] == "black":
        room.deck.insert(0, first)
        shuffle_deck(room.deck)
        first = draw_from_deck(room, 1)[0]

    room.discard = [first]
    room.current_color = first["color"]
    order = room.seat_order()
    room.turn_seat = order[0]
    room.started = True

    # turn_seat is already ON order[0] at kickoff, so an opening
    # Skip / Draw-2 advances only ONE seat (skipping order[0] entirely).
    if first["value"] == "skip":
        advance_turn(room, 1)
    elif first["value"] == "reverse":
        room.direction = -1
        # In 2-player UNO Reverse acts like a Skip: dealer plays again.
        if len(order) > 2:
            advance_turn(room, 1)
    elif first["value"] == "draw2":
        victim = room.player_by_seat(room.turn_seat)
        if victim:
            victim.hand.extend(draw_from_deck(room, DRAW2_PENALTY))
        advance_turn(room, 1)


def sync_uno_flag(player: Player) -> None:
    """The 'called UNO' declaration only makes sense while a player holds
    1 or 2 cards (you may call it as soon as you're about to play down to
    your last card, or right up until you're caught at 1). Drop the flag
    once a hand leaves that range — e.g. after drawing penalty cards — so
    a stale declaration can never linger and matter later."""
    if len(player.hand) not in (1, 2):
        player.called_uno = False


def handle_play(room: Room, player: Player, card_id: str, chosen_color: Optional[str]) -> tuple[bool, Optional[Player]]:
    """Returns (is_winner, victim_who_drew_cards).

    NOTE on Draw-2 / Wild-4 / Skip in a 2-player game: with only one
    opponent, "skip the next player" necessarily lands back on you —
    that's standard UNO, not a bug. Draw a Draw-2 with 2 players and the
    official rule is: opponent draws 2 and is skipped, so you go again.

    Turn/play rules (no Pass button):
      - if you drew a playable card this turn you may ONLY play that
        exact card, and doing so ends your turn immediately.
      - otherwise you may play any playable card from your hand.
      - if you have NO playable card at the start of your turn, you must
        draw; the draw handler auto-passes the turn if the drawn card
        is not playable.
    """
    if not room.started:
        raise ValueError("Game has not started yet")
    if room.turn_seat != player.seat:
        raise ValueError("Not your turn")
    card = next((c for c in player.hand if c["id"] == card_id), None)
    if card is None:
        raise ValueError("That card is not in your hand")
    if not card_playable(room, card):
        raise ValueError("Card does not match the pile")
    if player.drew_this_turn and card["id"] != player.last_drawn_card_id:
        raise ValueError("After drawing you may only play the card you just drew")
    if card["color"] == "black":
        if chosen_color not in COLORS:
            raise ValueError("Choose a color for the wild card")
    elif chosen_color:
        chosen_color = None

    player.hand.remove(card)
    room.discard.append(card)
    room.current_color = chosen_color if card["color"] == "black" else card["color"]
    # Preserve a pre-emptive UNO call made while the player still held 2
    # cards; only clear it if they never called and just dropped to 1.
    sync_uno_flag(player)

    if len(player.hand) == 0:
        room.started = False
        return True, None  # winner

    value = card["value"]
    victim: Optional[Player] = None

    # The drawn-then-played card is the only card you may play after
    # drawing. Playing it ends your turn normally.
    player.drew_this_turn = False
    player.last_drawn_card_id = None

    if value == "skip":
        # Hop 2 seats: skip exactly one player.
        advance_turn(room, 2)
    elif value == "reverse":
        room.direction *= -1
        # In 2-player UNO, Reverse acts like a Skip (same player goes again).
        if len(room.seat_order()) > 2:
            advance_turn(room, 1)
    elif value == "draw2":
        advance_turn(room, 1)
        victim = room.player_by_seat(room.turn_seat)
        if victim:
            victim.hand.extend(draw_from_deck(room, DRAW2_PENALTY))
            sync_uno_flag(victim)
        advance_turn(room, 1)
    elif value == "wild4":
        advance_turn(room, 1)
        victim = room.player_by_seat(room.turn_seat)
        if victim:
            victim.hand.extend(draw_from_deck(room, DRAW4_PENALTY))
            sync_uno_flag(victim)
        advance_turn(room, 1)
    else:
        advance_turn(room, 1)
    return False, victim


def handle_draw(room: Room, player: Player) -> tuple[list[dict], bool]:
    """Draw exactly one card from the pile.

    If the drawn card is playable on the discard pile, the player keeps
    their turn and MUST play that exact card (the client highlights only
    that card). If the drawn card is NOT playable, the turn automatically
    advances to the next player — no Pass button is ever needed.
    Returns (drawn_cards, is_playable).
    """
    if not room.started:
        raise ValueError("Game has not started yet")
    if room.turn_seat != player.seat:
        raise ValueError("Not your turn")
    if player.drew_this_turn:
        # Already drew a playable card — must play it, can't draw again.
        raise ValueError("You already drew a card this turn — play it")
    drawn = draw_from_deck(room, 1)
    if not drawn:
        advance_turn(room, 1)
        return [], False
    player.hand.extend(drawn)
    sync_uno_flag(player)

    drawn_card = drawn[0]
    is_playable = card_playable(room, drawn_card)
    if is_playable:
        player.drew_this_turn = True
        player.last_drawn_card_id = drawn_card["id"]
    else:
        # Not playable: turn ends immediately, pass to the next player.
        player.drew_this_turn = False
        player.last_drawn_card_id = None
        advance_turn(room, 1)
    return drawn, is_playable


def handle_call_uno(player: Player) -> None:
    """A player may declare UNO as soon as they're down to 2 cards (i.e.
    about to play their second-to-last card) all the way through holding
    just 1 — matching how most UNO apps let you call it early so you
    can't get caught by a faster opponent."""
    if len(player.hand) in (1, 2):
        player.called_uno = True


def handle_catch_uno(room: Room, target_seat: int) -> bool:
    target = room.player_by_seat(target_seat)
    if target and len(target.hand) == 1 and not target.called_uno:
        target.hand.extend(draw_from_deck(room, UNO_CATCH_PENALTY))
        target.called_uno = True
        return True
    return False


async def remove_player(room: Room, player: Player) -> Optional[Player]:
    """Fully removes a player from the room (manual 'exit table').

    If it was their turn, the turn is advanced first so the rotation
    stays correct. Returns the player who now holds the turn (so the
    caller can push them a fresh hand), or None.
    """
    async with room.lock:
        was_current_turn = room.started and room.turn_seat == player.seat
        if was_current_turn:
            advance_turn(room, 1)
        next_turn_player = room.player_by_seat(room.turn_seat) if room.started else None
        room.players.pop(player.token, None)
        if room.host_token == player.token:
            room.host_token = next(iter(room.players), None)
        if room.started and len(room.players) < 2:
            room.started = False
            next_turn_player = None
        if not room.started:
            clear_all_hands(room)
        else:
            for p in room.players.values():
                p.drew_this_turn = False
                p.last_drawn_card_id = None
    return next_turn_player


# --------------------------------------------------------------------------
# Messaging
# --------------------------------------------------------------------------

async def send_json(ws: Optional[WebSocket], data: dict) -> None:
    if ws is None:
        return
    try:
        await ws.send_text(json.dumps(data))
    except Exception:
        pass


def public_state(room: Room) -> dict:
    seats = {}
    for s in range(1, MAX_SEATS + 1):
        p = room.player_by_seat(s)
        if p:
            seats[str(s)] = {
                "token": p.token,
                "name": p.name,
                "hand_count": len(p.hand),
                "connected": p.connected,
                "cam_on": p.cam_on,
                "mic_on": p.mic_on,
                "called_uno": p.called_uno,
                "drew_this_turn": p.drew_this_turn,
                "is_host": p.token == room.host_token,
                "score": p.score,
            }
    top = room.discard[-1] if room.discard else None
    host_seat = None
    if room.host_token and room.host_token in room.players:
        host_seat = room.players[room.host_token].seat
    return {
        "room": room.room_id,
        "seats": seats,
        "turn_seat": room.turn_seat,
        "direction": room.direction,
        "current_color": room.current_color,
        "top_card": {"color": top["color"], "value": top["value"]} if top else None,
        "draw_pile_count": len(room.deck),
        "started": room.started,
        "host_seat": host_seat,
        "player_count": len(room.players),
        "max_seats": MAX_SEATS,
    }


async def broadcast_state(room: Room) -> None:
    msg = {"type": "state", "state": public_state(room)}
    for p in room.players.values():
        if p.connected:
            await send_json(p.ws, msg)


async def send_hand(player: Player) -> None:
    await send_json(player.ws, {"type": "hand", "cards": player.hand})


async def broadcast_peers(room: Room) -> None:
    """Peer roster for the WebRTC mesh.

    Includes each peer's authoritative cam_on/mic_on so a client that
    receives `peers` BEFORE the matching `state` (or while its own state
    is stale) can still render tiles correctly and decide whether to
    expect a video track. This is what makes late-join / reconnect
    converge instantly instead of showing a frozen or missing feed.
    """
    peers = [
        {
            "token": p.token,
            "seat": p.seat,
            "name": p.name,
            "cam_on": p.cam_on,
            "mic_on": p.mic_on,
        }
        for p in room.players.values()
        if p.connected
    ]
    msg = {"type": "peers", "peers": peers}
    for p in room.players.values():
        if p.connected:
            await send_json(p.ws, msg)


async def broadcast_notice(room: Room, message: str) -> None:
    payload = {"type": "notice", "message": message}
    for p in room.players.values():
        if p.connected:
            await send_json(p.ws, payload)


async def broadcast_chat(room: Room, player: Player, text: str) -> None:
    """Table chat. Relayed verbatim to every connected seat, including the
    sender, so all clients render the same ordered transcript."""
    payload = {
        "type": "chat",
        "seat": player.seat,
        "name": player.name,
        "text": text,
        "ts": time.time(),
    }
    for p in room.players.values():
        if p.connected:
            await send_json(p.ws, payload)


# --------------------------------------------------------------------------
# Background loops
# --------------------------------------------------------------------------

async def _cleanup_loop() -> None:
    """Frees seats that have been disconnected past the grace period."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        now = time.time()
        empty_rooms = []
        for rid, room in list(rooms.items()):
            changed = False
            removed_names: list[str] = []
            async with room.lock:
                stale = [
                    t for t, p in room.players.items()
                    if not p.connected and (now - p.last_seen) > RECONNECT_GRACE_SECONDS
                ]
                for t in stale:
                    p = room.players[t]
                    removed_names.append(p.name)
                    if room.started and room.turn_seat == p.seat:
                        advance_turn(room, 1)
                    del room.players[t]
                    changed = True
                if room.host_token not in room.players and room.players:
                    room.host_token = next(iter(room.players))
                if room.started and len(room.players) < 2:
                    room.started = False
                if not room.started and room.players:
                    clear_all_hands(room)
                if not room.players:
                    empty_rooms.append(rid)
            if changed and room.players:
                await broadcast_state(room)
                for p in room.players.values():
                    await send_hand(p)
                await broadcast_peers(room)
                for nm in removed_names:
                    await broadcast_notice(room, f"{nm}'s seat was released (disconnected too long)")
        for rid in empty_rooms:
            rooms.pop(rid, None)
            log.info("Removed empty room: %s", rid)


async def _turn_watchdog_loop() -> None:
    """Keeps the table moving if the active player is disconnected: after
    AUTO_SKIP_DISCONNECTED_SECONDS we draw a card on their behalf. Per the
    no-Pass rule, if that drawn card is not playable the turn auto-advances
    inside handle_draw; if it IS playable, the watchdog leaves the turn
    with them (they must play it when they reconnect)."""
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
        for room in list(rooms.values()):
            if not room.started or room.turn_seat is None:
                continue
            acted_player = None
            async with room.lock:
                current = room.player_by_seat(room.turn_seat)
                if (
                    current
                    and not current.connected
                    and (time.time() - current.last_seen) > AUTO_SKIP_DISCONNECTED_SECONDS
                ):
                    try:
                        if not current.drew_this_turn:
                            handle_draw(room, current)
                            # If the drawn card was playable, the turn stays
                            # with this (disconnected) player — they must
                            # play it when they reconnect. Only broadcast
                            # the auto-pass notice if the turn actually moved.
                            if not current.drew_this_turn:
                                current.last_seen = time.time()
                                acted_player = current
                    except ValueError:
                        pass
            if acted_player:
                await broadcast_state(room)
                await send_hand(acted_player)
                await broadcast_notice(room, f"{acted_player.name} was away — drew and passed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_loop())
    watchdog_task = asyncio.create_task(_turn_watchdog_loop())
    log.info("UNO server ready — %d seats per room, serving %s", MAX_SEATS, STATIC_DIR)
    try:
        yield
    finally:
        cleanup_task.cancel()
        watchdog_task.cancel()


app = FastAPI(title="UNO Online", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=512)


# --------------------------------------------------------------------------
# WebSocket endpoint
# --------------------------------------------------------------------------

@app.websocket("/ws/{room_id}")
async def ws_endpoint(websocket: WebSocket, room_id: str) -> None:
    room_id = ROOM_ID_RE.sub("", room_id)[:32] or "main"
    await websocket.accept()
    room = await get_or_create_room(room_id)
    player: Optional[Player] = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")

            # ---- join / reconnect -------------------------------------
            if mtype == "join":
                is_new_player = False
                async with room.lock:
                    token = msg.get("token")
                    name = (msg.get("name") or "Player").strip()[:24] or "Player"
                    existing = room.players.get(token) if token else None
                    if existing is not None:
                        player = existing
                        player.ws = websocket
                        player.connected = True
                        player.last_seen = time.time()
                        if name:
                            player.name = name
                    else:
                        seat = room.next_free_seat()
                        if seat is None:
                            await send_json(websocket, {
                                "type": "error",
                                "message": f"Table is full ({MAX_SEATS}/{MAX_SEATS})",
                            })
                            await websocket.close()
                            return
                        new_token = uuid.uuid4().hex
                        player = Player(token=new_token, seat=seat, name=name, ws=websocket)
                        room.players[new_token] = player
                        is_new_player = True
                        if room.host_token is None:
                            room.host_token = new_token
                    if "cam_on" in msg:
                        player.cam_on = bool(msg.get("cam_on"))
                    if "mic_on" in msg:
                        player.mic_on = bool(msg.get("mic_on"))
                    log.info("'%s' joined room=%s seat=%s cam=%s mic=%s reconnect=%s",
                             player.name, room_id, player.seat, player.cam_on, player.mic_on, existing is not None)
                    await send_json(websocket, {
                        "type": "joined",
                        "token": player.token,
                        "seat": player.seat,
                        "room": room_id,
                    })
                await broadcast_state(room)
                await send_hand(player)
                await broadcast_peers(room)
                await broadcast_notice(
                    room,
                    f"{player.name} joined the table" if is_new_player else f"{player.name} reconnected",
                )

            elif player is None:
                continue

            # ---- host starts the game -----------------------------------
            elif mtype == "start":
                if player.token == room.host_token:
                    async with room.lock:
                        if room.started:
                            continue
                        if len(room.players) < 2:
                            await send_json(websocket, {"type": "error", "message": "Need at least 2 players"})
                            continue
                        start_game(room)
                    await broadcast_state(room)
                    for p in room.players.values():
                        await send_hand(p)
                    await broadcast_notice(room, "New round started!")

            # ---- draw ------------------------------------------------
            elif mtype == "draw":
                try:
                    playable = False
                    async with room.lock:
                        drawn, playable = handle_draw(room, player)
                    await broadcast_state(room)
                    await send_hand(player)
                except ValueError as e:
                    await send_json(websocket, {"type": "error", "message": str(e)})

            # ---- play card ---------------------------------------------
            elif mtype == "play":
                try:
                    async with room.lock:
                        winner, victim = handle_play(room, player, msg.get("card_id"), msg.get("chosen_color"))
                        round_points = 0
                        if winner:
                            round_points = settle_round(room, player)
                            # settle first (it scores the losers' hands), then
                            # the table is cleared — no cards outside a round
                            clear_all_hands(room)
                    await broadcast_state(room)
                    if winner:
                        # the round is over and every hand was just emptied,
                        # so push the (now empty) hand to all clients
                        for p in room.players.values():
                            await send_hand(p)
                    else:
                        await send_hand(player)
                        if victim:
                            await send_hand(victim)
                    if winner:
                        board = leaderboard(room, winner=player, round_points=round_points)
                        for p in room.players.values():
                            await send_json(p.ws, {
                                "type": "game_over",
                                "winner_seat": player.seat,
                                "winner_name": player.name,
                                "round_points": round_points,
                                "scores": board,
                            })
                except ValueError as e:
                    await send_json(websocket, {"type": "error", "message": str(e)})

            # ---- uno call / catch ----------------------------------------
            elif mtype == "call_uno":
                async with room.lock:
                    handle_call_uno(player)
                await broadcast_state(room)

            elif mtype == "catch_uno":
                target_seat = msg.get("target_seat")
                async with room.lock:
                    ok = handle_catch_uno(room, target_seat)
                if ok:
                    await broadcast_state(room)
                    victim = room.player_by_seat(target_seat)
                    if victim:
                        await send_hand(victim)
                        await broadcast_notice(room, f"{victim.name} got caught without calling UNO!")

            # ---- manual exit ----------------------------------------------
            elif mtype == "leave":
                player.ws = None  # stop the finally-block below from double-handling this
                left_name = player.name
                next_turn_player = await remove_player(room, player)
                await broadcast_state(room)
                await broadcast_peers(room)
                await broadcast_notice(room, f"{left_name} left the table")
                if next_turn_player:
                    await send_hand(next_turn_player)
                elif len(room.players) < 2:
                    # the round was abandoned — everybody's hand just emptied
                    for p in room.players.values():
                        await send_hand(p)
                await send_json(websocket, {"type": "left_ok"})
                await websocket.close()
                return

            # ---- WebRTC signaling relay ----------------------------------
            elif mtype == "webrtc":
                target = room.players.get(msg.get("target"))
                if target and target.connected:
                    await send_json(target.ws, {
                        "type": "webrtc",
                        "from": player.token,
                        "from_seat": player.seat,
                        "payload": msg.get("payload"),
                    })

            # ---- camera/mic on-off signal (authoritative for the UI) -----
            elif mtype == "media_state":
                new_cam_on = bool(msg.get("cam_on"))
                new_mic_on = bool(msg.get("mic_on"))
                if player.cam_on != new_cam_on or player.mic_on != new_mic_on:
                    async with room.lock:
                        player.cam_on = new_cam_on
                        player.mic_on = new_mic_on
                    # Broadcast BOTH state and peers so every client —
                    # including one that is mid-renegotiation and only
                    # looking at the peers roster — converges on the new
                    # media reality immediately.
                    await broadcast_state(room)
                    await broadcast_peers(room)

            elif mtype == "chat":
                text = (msg.get("text") or "").strip()
                if text:
                    await broadcast_chat(room, player, text[:240])

            elif mtype == "ping":
                await send_json(websocket, {"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Unhandled error in websocket loop (room=%s)", room_id)
    finally:
        if player is not None and player.ws is websocket:
            player.connected = False
            player.ws = None
            player.cam_on = False   # nobody is streaming while disconnected
            player.mic_on = False   # reset mic state too
            player.last_seen = time.time()
            log.info("'%s' disconnected from room=%s seat=%s", player.name, room_id, player.seat)
            await broadcast_state(room)
            await broadcast_peers(room)
            await broadcast_notice(room, f"{player.name} disconnected")


# --------------------------------------------------------------------------
# HTTP routes
# --------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# The client is a single-page app with no build step, so a stale cached
# script is indistinguishable from a broken feature. Nothing here is worth
# caching across reloads.
NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/")
async def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return JSONResponse(
            {"ok": False, "error": f"No index.html found. Put the HTML file at: {index_path}"},
            status_code=404,
        )
    return FileResponse(index_path, headers=NO_CACHE)


@app.get("/style.css")
async def style_css():
    css_path = STATIC_DIR / "style.css"
    if not css_path.exists():
        return JSONResponse({"ok": False, "error": "style.css not found"}, status_code=404)
    return FileResponse(css_path, media_type="text/css", headers=NO_CACHE)


@app.get("/app.js")
async def app_js():
    js_path = STATIC_DIR / "app.js"
    if not js_path.exists():
        return JSONResponse({"ok": False, "error": "app.js not found"}, status_code=404)
    return FileResponse(js_path, media_type="application/javascript", headers=NO_CACHE)


@app.get("/api/ice-config")
async def ice_config() -> JSONResponse:
    """Returns the ICE servers the browser uses to establish WebRTC
    peer connections. STUN helps peers discover their public address;
    TURN is a relay used when a direct connection is impossible (strict
    NATs, symmetric NAT, phone-on-cellular ↔ laptop-on-WiFi, etc.).

    For FLAWLESS cross-network video you MUST run a TURN server. The
    easiest option is `coturn` on the same Proxmox CT103 host:

        # on ct103 (Debian/Ubuntu)
        apt-get install coturn
        turnserver -a -o -v --no-loopback-peers \
            --listening-port=3478 --tls-listening-port=5349 \
            --relay-ip=<CT103_LAN_IP> --external-ip=<CT103_PUBLIC_IP> \
            --user=uno:STRONG_PASSWORD --lt-cred-mech --realm=uno.local

    Then launch this server with the matching env vars:
        TURN_URL=turn:CT103_LAN_IP:3478
        TURN_USERNAME=uno
        TURN_CREDENTIAL=STRONG_PASSWORD

    (If CT103 is behind a NAT/router, also forward UDP 3478/5349 and set
    --external-ip to the public IP, or use a TURN-over-TCP/TLS fallback.)
    """
    servers = [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
        {"urls": "stun:stun2.l.google.com:19302"},
        {"urls": "stun:stun3.l.google.com:19302"},
        {"urls": "stun:stun4.l.google.com:19302"},
        {"urls": "stun:stun.cloudflare.com:3478"},
        {"urls": "stun:global.stun.twilio.com:3478"},
        {"urls": "stun:stun.services.mozilla.com"},
    ]
    # Optional TURN relay (strongly recommended for phone↔laptop).
    turn_url = os.environ.get("TURN_URL")
    if turn_url:
        entry: dict = {"urls": turn_url}
        if os.environ.get("TURN_USERNAME"):
            entry["username"] = os.environ["TURN_USERNAME"]
        if os.environ.get("TURN_CREDENTIAL"):
            entry["credential"] = os.environ["TURN_CREDENTIAL"]
        # Also allow TURN over TCP/TLS as a fallback when UDP is blocked.
        tcp_variant = turn_url.replace("turn:", "turns:").replace(":3478", ":5349")
        if tcp_variant != turn_url:
            tcp_entry = dict(entry)
            tcp_entry["urls"] = tcp_variant
            servers.append(tcp_entry)
        servers.append(entry)
    return JSONResponse(servers, headers={"Cache-Control": "public, max-age=60"})


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "rooms": {rid: len(r.players) for rid, r in rooms.items()},
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8899")),
        workers=1,
    )
