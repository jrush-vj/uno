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
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import re
import time
import uuid
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import store
from store import normalise_code

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("uno")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

MAX_SEATS = 6
UNO_CATCH_PENALTY = 2
DRAW2_PENALTY = 2
DRAW4_PENALTY = 4
ACTION_CARD_POINTS = 20
WILD_CARD_POINTS = 50

# Allowed values for the host-controlled game settings. Anything outside
# these lists is rejected rather than trusted from the client.
VALID_STARTING_CARDS = (5, 7)
VALID_POINTS_MODES = ("official", "wins")
VALID_TURN_TIMERS = (0, 30, 60)   # seconds; 0 disables the timer

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
class GameSettings:
    """Host-controlled rules for a room.

    Validated on the way in: the client is never trusted to send a sane
    value, so each field is clamped to its allowed set.
    """

    starting_cards: int = 7
    points_mode: str = "official"      # "official" (card values) or "wins"
    turn_timer: int = 0                # seconds, 0 = off
    seat_order: list = field(default_factory=list)   # tokens, host-arranged

    def to_json(self) -> dict:
        return {
            "starting_cards": self.starting_cards,
            "points_mode": self.points_mode,
            "turn_timer": self.turn_timer,
            "seat_order": list(self.seat_order),
        }

    @classmethod
    def from_json(cls, payload: dict) -> "GameSettings":
        starting = int(payload.get("starting_cards", 7))
        timer = int(payload.get("turn_timer", 0))
        mode = str(payload.get("points_mode", "official"))
        return cls(
            starting_cards=starting if starting in VALID_STARTING_CARDS else 7,
            points_mode=mode if mode in VALID_POINTS_MODES else "official",
            turn_timer=timer if timer in VALID_TURN_TIMERS else 0,
            seat_order=[str(t) for t in payload.get("seat_order", []) if t],
        )


@dataclass
class Player:
    token: str
    peer_id: str
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
    round_points: int = 0          # points won in the most recent round
    lifetime_wins: int = 0         # reported by the stats store on join


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
    settings: "GameSettings" = field(default_factory=GameSettings)
    round_number: int = 0
    turn_deadline: Optional[float] = None   # epoch seconds; None = no timer

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

# Code reservation, expiry and per-client creation caps live here so multiple
# workers share one view once REDIS_URL is set.
registry = store.build_registry()

# Lifetime scores. Separate from the registry because it outlives any room.
stats_store = store.build_stats()

# How often the registry is swept for expired codes and abandoned rooms.
ROOM_REAP_INTERVAL_SECONDS = 15
TURN_CREDENTIAL_REQUEST_LIMIT = 12
TURN_CREDENTIAL_REQUEST_WINDOW = 60
_turn_credential_requests: dict[str, list[float]] = {}


def allow_turn_credential_request(client_ip: str, now: Optional[float] = None) -> bool:
    current_time = time.time() if now is None else now
    recent = [
        timestamp for timestamp in _turn_credential_requests.get(client_ip, [])
        if current_time - timestamp < TURN_CREDENTIAL_REQUEST_WINDOW
    ]
    if len(recent) >= TURN_CREDENTIAL_REQUEST_LIMIT:
        _turn_credential_requests[client_ip] = recent
        return False
    recent.append(current_time)
    _turn_credential_requests[client_ip] = recent
    return True


async def get_or_create_room(room_id: str) -> Room:
    async with _rooms_guard:
        if room_id not in rooms:
            rooms[room_id] = Room(room_id=room_id)
        return rooms[room_id]


async def client_id_for(websocket: WebSocket) -> str:
    """Stable identity for rate limiting.

    Cloudflare is the only route in, so CF-Connecting-IP is the trustworthy
    client address; a direct connection falls back to the socket peer.
    """
    forwarded = websocket.headers.get("cf-connecting-ip")
    if forwarded:
        return forwarded.strip()
    return websocket.client.host if websocket.client else "unknown"


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
        arm_turn_timer(room)
        return
    idx = order.index(room.turn_seat)
    idx = (idx + steps * room.direction) % len(order)
    room.turn_seat = order[idx]
    # Every turn change re-arms the clock, so the countdown always describes
    # the player who currently owes a move.
    arm_turn_timer(room)


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
    """Award the round and return the winner's points for it.

    Under "official" scoring the winner takes the value of every card left in
    opponents' hands. Under "wins" scoring the round is worth a flat amount so
    a casual table is not decided by how badly someone was left holding cards.
    """
    if room.settings.points_mode == "wins":
        pts = store.WIN_BONUS_POINTS
    else:
        pts = 0
        for p in room.players.values():
            if p is not winner:
                for c in p.hand:
                    pts += card_points(c)
    winner.score += pts
    winner.round_points = pts
    return pts


def record_round_stats(room: Room, winner: Player, round_points: int) -> None:
    """Build the per-player round summary the stats store records.

    A player is credited with one round played whether they won it or not, so
    a win rate is meaningful. The UNO-call bonus is paid separately when the
    call happens, not here.
    """
    entries = []
    for p in room.players.values():
        won = p is winner
        entries.append({
            "name": p.name,
            "points": p.round_points if won else 0,
            "round_points": p.round_points if won else 0,
            "won": won,
            "uno_calls": 1 if (p.called_uno and won) else 0,
        })
    return entries


def leaderboard(room: Room, winner: Optional[Player] = None, round_points: int = 0) -> list[dict]:
    rows = [
        {
            "seat": p.seat,
            "name": p.name,
            "score": p.score,
            "round_points": p.round_points,
            "lifetime_wins": p.lifetime_wins,
            "is_host": p.token == room.host_token,
        }
        for p in room.players.values()
    ]
    rows.sort(key=lambda r: (-r["score"], r["seat"]))
    return rows


def apply_seat_order(room: Room) -> None:
    """Re-seat players according to the host's arrangement.

    ``settings.seat_order`` holds tokens. Any player missing from it keeps a
    seat after the arranged ones, so a stale order (someone left, someone
    joined) degrades gracefully instead of failing.
    """
    ordered = [t for t in room.settings.seat_order if t in room.players]
    for token in room.players:
        if token not in ordered:
            ordered.append(token)
    for index, token in enumerate(ordered, start=1):
        room.players[token].seat = index
    room.settings.seat_order = ordered


def shuffle_seats(room: Room) -> None:
    """Randomise seating, then persist the new order in the settings."""
    tokens = list(room.players.keys())
    for i in range(len(tokens) - 1, 0, -1):
        j = random.SystemRandom().randint(0, i)
        tokens[i], tokens[j] = tokens[j], tokens[i]
    room.settings.seat_order = tokens
    apply_seat_order(room)


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
    """Deal a fresh round using the room's configured rules."""
    apply_seat_order(room)
    room.deck = build_deck()
    room.discard = []
    room.direction = 1
    room.round_number += 1
    for p in room.players.values():
        p.hand = draw_from_deck(room, room.settings.starting_cards)
        p.called_uno = False
        p.drew_this_turn = False
        p.last_drawn_card_id = None
        p.round_points = 0

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
    if len(player.hand) in (1, 2) and not player.called_uno:
        player.called_uno = True
        # The bonus is paid once per round, at the moment of the call.
        player.score += store.UNO_CALL_POINTS


def arm_turn_timer(room: Room) -> None:
    """Start (or clear) the countdown for the player on turn.

    Called wherever the turn changes, so the deadline always describes the
    player who currently owes a move.
    """
    if room.settings.turn_timer and room.started:
        room.turn_deadline = time.time() + room.settings.turn_timer
    else:
        room.turn_deadline = None


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


def can_kick_player(room: Room, requester: Player, target: Optional[Player]) -> bool:
    return (
        requester.token == room.host_token
        and target is not None
        and target.token != requester.token
        and room.players.get(target.token) is target
    )


async def announce_player_removed(room: Room, player_name: str, next_turn_player: Optional[Player]) -> None:
    await broadcast_state(room)
    await broadcast_peers(room)
    await broadcast_notice(room, player_name)
    if next_turn_player:
        await send_hand(next_turn_player)
    elif len(room.players) < 2:
        for remaining_player in room.players.values():
            await send_hand(remaining_player)


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
                "name": p.name,
                "hand_count": len(p.hand),
                "connected": p.connected,
                "cam_on": p.cam_on,
                "mic_on": p.mic_on,
                "called_uno": p.called_uno,
                "drew_this_turn": p.drew_this_turn,
                "is_host": p.token == room.host_token,
                "score": p.score,
                "round_points": p.round_points,
                "lifetime_wins": p.lifetime_wins,
            }
    top = room.discard[-1] if room.discard else None
    host_seat = None
    if room.host_token and room.host_token in room.players:
        host_seat = room.players[room.host_token].seat
    # The countdown is sent as seconds remaining rather than an absolute
    # deadline, because client clocks cannot be trusted to agree.
    seconds_left = None
    if room.turn_deadline is not None:
        seconds_left = max(0, int(room.turn_deadline - time.time()))
    return {
        "room": room.room_id,
        "seats": seats,
        # Seat numbers to peer tokens. The host's seat-arranging UI needs to
        # send an order the server can apply, and the server orders by token.
        "seat_tokens": {str(p.seat): p.token for p in room.players.values()},
        "turn_seat": room.turn_seat,
        "direction": room.direction,
        "current_color": room.current_color,
        "top_card": {"color": top["color"], "value": top["value"]} if top else None,
        "draw_pile_count": len(room.deck),
        "started": room.started,
        "host_seat": host_seat,
        "player_count": len(room.players),
        "max_seats": MAX_SEATS,
        "settings": room.settings.to_json(),
        "round_number": room.round_number,
        "turn_seconds_left": seconds_left,
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
            "peer_id": p.peer_id,
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
            await registry.release(rid)
            log.info("Removed empty room: %s", rid)


async def _room_reaper_loop() -> None:
    """Expires reservation codes and closes AFK rooms.

    Two separate rules, both aimed at the host's resources being wasted by
    rooms nobody is playing in:

    * a code nobody joined inside ``CODE_TTL_SECONDS`` is released, so
      abandoned reservations cannot accumulate;
    * a room with fewer than two players for ``AFK_ROOM_SECONDS`` is closed,
      and its occupant is told why rather than silently disconnected.
    """
    while True:
        await asyncio.sleep(ROOM_REAP_INTERVAL_SECONDS)
        try:
            for code in await registry.reap_expired():
                room = rooms.pop(code, None)
                log.info("Reaped expired code: %s", code)
                if room:
                    for p in list(room.players.values()):
                        if p.connected:
                            await send_json(p.ws, {
                                "type": "kicked",
                                "message": "That game expired because nobody joined in time.",
                            })
                            try:
                                await p.ws.close(code=4002, reason="Room expired")
                            except Exception:
                                pass

            # AFK: any room sitting with fewer than two players. This covers
            # both a host waiting alone from the moment they created the
            # game, and a table that emptied back down to one after a leave.
            # Checked here rather than in Redis so the socket can be closed
            # politely with an explanation.
            now = time.time()
            for rid, room in list(rooms.items()):
                if len(room.players) > 1:
                    continue
                record = await registry.get(rid)
                if not record:
                    continue
                idle_for = now - record.last_activity
                # A reserved code nobody ever joined is left to the code TTL;
                # a room with an actual player is subject to the AFK window.
                if room.players and idle_for <= store.AFK_ROOM_SECONDS:
                    continue
                if not room.players and idle_for <= store.CODE_TTL_SECONDS:
                    continue
                for p in list(room.players.values()):
                    if p.connected:
                        await send_json(p.ws, {
                            "type": "kicked",
                            "message": "Game closed: nobody joined within two minutes.",
                        })
                        try:
                            await p.ws.close(code=4002, reason="AFK")
                        except Exception:
                            pass
                rooms.pop(rid, None)
                await registry.release(rid)
                log.info("Reaped AFK room: %s", rid)
        except Exception:
            log.exception("Room reaper failed")


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


async def _turn_timer_loop() -> None:
    """Auto-draws for a player who lets the turn clock run out.

    Mirrors the disconnected-player watchdog but is driven by the configured
    ``turn_timer`` rather than by connectivity: an idle player still holds a
    live socket, they just have not acted. Drawing on their behalf keeps the
    table moving and matches the no-Pass rule the rest of the game follows.
    """
    while True:
        await asyncio.sleep(1)
        for room in list(rooms.values()):
            if not room.started or room.turn_seat is None or not room.turn_deadline:
                continue
            if time.time() < room.turn_deadline:
                continue
            acted_player = None
            async with room.lock:
                current = room.player_by_seat(room.turn_seat)
                if current is None:
                    room.turn_deadline = None
                    continue
                try:
                    if not current.drew_this_turn:
                        handle_draw(room, current)
                    # If a playable card was drawn the turn stays with them, so
                    # the clock is re-armed rather than skipping them.
                    arm_turn_timer(room)
                    acted_player = current
                except ValueError:
                    room.turn_deadline = None
            if acted_player:
                await broadcast_state(room)
                await send_hand(acted_player)
                await broadcast_notice(
                    room, f"{acted_player.name} ran out of time — drew a card"
                )


@asynccontextmanager
async def lifespan(_: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_loop())
    watchdog_task = asyncio.create_task(_turn_watchdog_loop())
    reaper_task = asyncio.create_task(_room_reaper_loop())
    timer_task = asyncio.create_task(_turn_timer_loop())
    log.info(
        "UNO server ready — %d seats per room, code TTL %ss, AFK %ss, serving %s",
        MAX_SEATS, store.CODE_TTL_SECONDS, store.AFK_ROOM_SECONDS, STATIC_DIR,
    )
    try:
        yield
    finally:
        cleanup_task.cancel()
        watchdog_task.cancel()
        reaper_task.cancel()
        timer_task.cancel()


app = FastAPI(title="UNO Online", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=512)


# --------------------------------------------------------------------------
# WebSocket endpoint
# --------------------------------------------------------------------------

@app.websocket("/ws/{room_id}")
async def ws_endpoint(websocket: WebSocket, room_id: str) -> None:
    # A code is the only way in. Canonicalise before validating so a user who
    # types lowercase, or pastes a spaced code, still reaches their room.
    room_id = normalise_code(room_id)
    await websocket.accept()
    player: Optional[Player] = None

    # Reconnects carry a saved token, so the room must still exist for them.
    # Everything else needs a reserved code, which is what stops random
    # names from spawning unlimited rooms on the host.
    record = await registry.get(room_id)
    if record is None:
        await send_json(websocket, {
            "type": "error",
            "message": "That game code is not valid or has expired.",
        })
        await websocket.close()
        return

    room = await get_or_create_room(room_id)

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
                        player.peer_id = uuid.uuid4().hex
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
                        player = Player(
                            token=new_token,
                            peer_id=uuid.uuid4().hex,
                            seat=seat,
                            name=name,
                            ws=websocket,
                        )
                        room.players[new_token] = player
                        is_new_player = True
                        if room.host_token is None:
                            room.host_token = new_token
                    if "cam_on" in msg:
                        player.cam_on = bool(msg.get("cam_on"))
                    if "mic_on" in msg:
                        player.mic_on = bool(msg.get("mic_on"))
                    # Two or more players means the host is no longer waiting
                    # alone, so the code stops expiring on the reservation TTL.
                    if len(room.players) >= 2:
                        await registry.mark_live(room_id, len(room.players))
                    else:
                        await registry.touch(room_id, len(room.players))
                    log.info("'%s' joined room=%s seat=%s cam=%s mic=%s reconnect=%s",
                             player.name, room_id, player.seat, player.cam_on, player.mic_on, existing is not None)
                    await send_json(websocket, {
                        "type": "joined",
                        "token": player.token,
                        "peer_id": player.peer_id,
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
                        arm_turn_timer(room)
                    await broadcast_state(room)
                    for p in room.players.values():
                        await send_hand(p)
                    await broadcast_notice(
                        room, f"Round {room.round_number} started!"
                    )

            # ---- host edits the rules ------------------------------------
            elif mtype == "settings":
                if player.token != room.host_token:
                    await send_json(websocket, {"type": "error", "message": "Only the host can change settings."})
                    continue
                incoming = msg.get("settings") or {}
                async with room.lock:
                    if room.started:
                        # Changing the deck size mid-round would desync every
                        # hand, so rules are frozen once a round is live.
                        await send_json(websocket, {
                            "type": "error",
                            "message": "Settings are locked during a round.",
                        })
                        continue
                    room.settings = GameSettings.from_json(incoming)
                    apply_seat_order(room)
                await broadcast_state(room)
                await broadcast_peers(room)
                await broadcast_notice(room, "The host updated the game settings.")

            # ---- host arranges the seating -------------------------------
            elif mtype == "seat_order":
                if player.token != room.host_token:
                    await send_json(websocket, {"type": "error", "message": "Only the host can arrange seats."})
                    continue
                async with room.lock:
                    requested = [str(t) for t in (msg.get("order") or []) if t]
                    room.settings.seat_order = requested
                    apply_seat_order(room)
                await broadcast_state(room)
                await broadcast_peers(room)

            elif mtype == "shuffle_seats":
                if player.token != room.host_token:
                    await send_json(websocket, {"type": "error", "message": "Only the host can shuffle seats."})
                    continue
                async with room.lock:
                    shuffle_seats(room)
                await broadcast_state(room)
                await broadcast_peers(room)
                await broadcast_notice(room, "The host shuffled the seating.")

            # ---- draw ------------------------------------------------
            elif mtype == "draw":
                try:
                    playable = False
                    async with room.lock:
                        drawn, playable = handle_draw(room, player)
                        arm_turn_timer(room)
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
                        stats_entries = []
                        if winner:
                            round_points = settle_round(room, player)
                            stats_entries = record_round_stats(room, player, round_points)
                            # settle first (it scores the losers' hands), then
                            # the table is cleared — no cards outside a round
                            clear_all_hands(room)
                        else:
                            arm_turn_timer(room)
                    await broadcast_state(room)
                    if winner:
                        # Record lifetime scores before the round is reset, so
                        # the leaderboard survives a restart.
                        await stats_store.record_round(stats_entries)
                        board = leaderboard(room)
                        all_time = await stats_store.top_players(8)
                        for p in room.players.values():
                            await send_json(p.ws, {
                                "type": "game_over",
                                "winner_seat": player.seat,
                                "winner_name": player.name,
                                "round_points": round_points,
                                "scores": board,
                                "all_time": all_time,
                                "round_number": room.round_number,
                                "points_mode": room.settings.points_mode,
                            })
                        for p in room.players.values():
                            await send_hand(p)
                    else:
                        await send_hand(player)
                        if victim:
                            await send_hand(victim)
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
                await announce_player_removed(room, f"{left_name} left the table", next_turn_player)
                await send_json(websocket, {"type": "left_ok"})
                # The client closes its own socket as soon as it sends 'leave',
                # so by the time this runs the close frame has usually already
                # been sent and closing again raises. The player is out of the
                # room by now, so this close is only a courtesy - and an
                # unguarded one logs a traceback for every normal exit.
                try:
                    await websocket.close()
                except Exception:
                    pass
                return

            elif mtype == "kick":
                if player.token != room.host_token:
                    await send_json(websocket, {"type": "error", "message": "Only the host can remove players."})
                    continue
                try:
                    target_seat = int(msg.get("target_seat"))
                except (TypeError, ValueError):
                    await send_json(websocket, {"type": "error", "message": "Invalid player seat."})
                    continue
                target = room.player_by_seat(target_seat)
                if not can_kick_player(room, player, target):
                    await send_json(websocket, {"type": "error", "message": "That player cannot be removed."})
                    continue
                target_ws = target.ws
                target_name = target.name
                if target_ws is not None:
                    await send_json(target_ws, {"type": "kicked", "message": "The host removed you from the table."})
                target.ws = None
                next_turn_player = await remove_player(room, target)
                if target_ws is not None:
                    try:
                        await target_ws.close(code=4001, reason="Removed by host")
                    except Exception:
                        pass
                await announce_player_removed(room, f"{target_name} was removed by the host", next_turn_player)

            # ---- WebRTC signaling relay ----------------------------------
            elif mtype == "webrtc":
                target_peer_id = msg.get("target")
                target = next(
                    (p for p in room.players.values() if p.peer_id == target_peer_id),
                    None,
                )
                if target and target.connected:
                    await send_json(target.ws, {
                        "type": "webrtc",
                        "from": player.peer_id,
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


@app.post("/api/leave")
async def leave_on_page_exit(request: Request) -> JSONResponse:
    """Release a seat immediately when a browser tab closes or navigates away."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    room_id = str(payload.get("room") or "")
    token = str(payload.get("token") or "")
    if not room_id or not token:
        return JSONResponse({"ok": False}, status_code=400)

    room_id = ROOM_ID_RE.sub("", room_id)[:32] or "main"
    room = rooms.get(room_id)
    player = room.players.get(token) if room else None
    if player is None:
        return JSONResponse({"ok": True, "left": False})

    player.ws = None
    name = player.name
    next_turn_player = await remove_player(room, player)
    await announce_player_removed(room, f"{name} left the table", next_turn_player)
    return JSONResponse({"ok": True, "left": True})


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


PUBLIC_STUN_SERVERS = [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
        {"urls": "stun:stun2.l.google.com:19302"},
        {"urls": "stun:stun3.l.google.com:19302"},
        {"urls": "stun:stun4.l.google.com:19302"},
        {"urls": "stun:stun.cloudflare.com:3478"},
        {"urls": "stun:global.stun.twilio.com:3478"},
        {"urls": "stun:stun.services.mozilla.com"},
    ]


def _without_known_blocked_turn_urls(ice_servers: list[dict]) -> list[dict]:
    """Drop Cloudflare's documented browser-incompatible alternate port 53."""
    filtered = []
    for server in ice_servers:
        urls = server.get("urls", [])
        if isinstance(urls, str):
            urls = [urls]
        urls = [url for url in urls if ":53?" not in url and not url.endswith(":53")]
        if urls:
            filtered.append({**server, "urls": urls})
    return filtered


def generate_cloudflare_turn_credentials(
    turn_key_id: str,
    turn_key: str,
    ttl_seconds: int = 86400,
    timeout_seconds: float = 8,
) -> list[dict]:
    """Ask Cloudflare Realtime TURN for short-lived credentials.

    The TURN key and TURN key ID stay on the server. Only the returned
    expiring ICE credentials are sent to the browser.
    """
    # Pasted dashboard values often carry a trailing newline or space, which
    # silently corrupts an Authorization header into a 401.
    turn_key_id = (turn_key_id or "").strip()
    turn_key = (turn_key or "").strip()
    ttl = max(60, min(int(ttl_seconds), 86400))
    endpoint = (
        "https://rtc.live.cloudflare.com/v1/turn/keys/"
        f"{turn_key_id}/credentials/generate-ice-servers"
    )
    payload = json.dumps({"ttl": ttl}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Bearer {turn_key}",
            "Content-Type": "application/json",
            # Cloudflare's edge rejects Python's default urllib User-Agent with
            # HTTP 403 / error code 1010 (a browser-signature block) before the
            # request ever reaches the TURN API. An explicit UA avoids that.
            "User-Agent": "uno-server/1.0 (+https://github.com/jrush-vj/uno)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Cloudflare's error body explains *why* (bad key, no subscription,
        # revoked key) and never echoes the secret back.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise ValueError(f"Cloudflare TURN API HTTP {exc.code}: {detail}") from exc
    ice_servers = result.get("iceServers")
    if not isinstance(ice_servers, list) or not ice_servers:
        raise ValueError(f"Cloudflare TURN returned no ICE servers: {str(result)[:200]}")
    return _without_known_blocked_turn_urls(ice_servers)


def _turn_url_variants(turn_url: str) -> list[str]:
    """Expand a coturn URL into UDP/TCP plus TURN-over-TLS fallbacks.

    Restrictive networks (college Wi-Fi, some mobile carriers) block UDP, so
    offering the TCP and TLS transports lets the relay stay usable there.
    Duplicates are removed so a two-URL input does not list the same endpoint
    twice.
    """
    base = turn_url.split("?")[0]
    if not base.startswith("turn:"):
        return [turn_url]

    if "transport=" in turn_url:
        variants = [turn_url]
    else:
        variants = [f"{base}?transport=udp", f"{base}?transport=tcp"]

    # TURN over TLS uses the conventional 5349 port; TLS already implies TCP,
    # so no transport parameter is needed on this URL.
    tls_base = base.replace("turn:", "turns:", 1)
    if ":3478" in tls_base:
        tls_base = tls_base.replace(":3478", ":5349", 1)
    variants.append(tls_base)

    unique: list[str] = []
    for variant in variants:
        if variant not in unique:
            unique.append(variant)
    return unique


def build_ice_servers(
    turn_url: Optional[str],
    turn_shared_secret: Optional[str] = None,
    turn_ttl_seconds: int = 3600,
    now: Optional[int] = None,
    turn_username: Optional[str] = None,
    turn_credential: Optional[str] = None,
) -> list[dict]:
    """Build public STUN plus optional TURN entries.

    Two credential styles are supported so any free relay can be used:

    * **Static** (`turn_username` + `turn_credential`) — a long-lived
      username/password pair, which is what most free/self-hosted TURN
      services issue. Credentials come from the server environment and are
      handed to the browser because the browser is the TURN client.
    * **coturn REST** (`turn_shared_secret`) — time-limited HMAC credentials
      derived from a shared secret that never leaves the server.
    """
    servers = list(PUBLIC_STUN_SERVERS)
    if not turn_url:
        return servers

    if turn_credential and turn_shared_secret is None:
        username = turn_username or "uno"
        credential = turn_credential
    elif turn_shared_secret:
        ttl = max(60, min(int(turn_ttl_seconds), 86400))
        expiry = (int(time.time()) if now is None else now) + ttl
        identity = turn_username or os.environ.get("TURN_USERNAME", "uno")
        username = f"{expiry}:{identity}"
        digest = hmac.new(
            turn_shared_secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1
        ).digest()
        credential = base64.b64encode(digest).decode("ascii")
    else:
        return servers

    urls: list[str] = []
    for part in (segment.strip() for segment in turn_url.split(",") if segment.strip()):
        for variant in _turn_url_variants(part):
            if variant not in urls:
                urls.append(variant)

    for url in urls:
        servers.append({
            "urls": url,
            "username": username,
            "credential": credential,
        })
    return servers


@app.get("/api/ice-config")
async def ice_config(request: Request) -> JSONResponse:
    """Returns the ICE servers the browser uses to establish WebRTC
    peer connections. STUN helps peers discover their public address;
    TURN is a relay used when a direct connection is impossible (strict
    NATs, symmetric NAT, phone-on-cellular ↔ laptop-on-WiFi, etc.).

    Cloudflare TURN credentials are generated server-side and expire. The
    Cloudflare API token and TURN key ID must never be sent to the browser.
    Coturn REST credentials remain available for self-hosted relays.
    """
    cloudflare_key_id = os.environ.get("CLOUDFLARE_TURN_KEY_ID")
    cloudflare_turn_key = os.environ.get("CLOUDFLARE_TURN_KEY")
    if cloudflare_key_id and cloudflare_turn_key:
        client_ip = request.headers.get("CF-Connecting-IP")
        if not client_ip:
            client_ip = request.client.host if request.client else "unknown"
        if not allow_turn_credential_request(client_ip):
            return JSONResponse(
                {"error": "Too many TURN credential requests"},
                status_code=429,
                headers={"Cache-Control": "no-store", "Retry-After": "60"},
            )
        try:
            servers = await asyncio.to_thread(
                generate_cloudflare_turn_credentials,
                cloudflare_key_id,
                cloudflare_turn_key,
                int(os.environ.get("TURN_TTL_SECONDS", "86400")),
            )
            return JSONResponse(servers, headers={"Cache-Control": "no-store"})
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            log.error("Cloudflare TURN credential request failed: %s", exc)
            body = {"error": "TURN credentials temporarily unavailable"}
            # TURN_DEBUG=1 surfaces Cloudflare's reason (never the secret) so a
            # misconfigured key or missing subscription can be diagnosed.
            if os.environ.get("TURN_DEBUG"):
                body["detail"] = str(exc)[:300]
                body["config"] = {
                    "CLOUDFLARE_TURN_KEY_ID_length": len(cloudflare_key_id or ""),
                    "CLOUDFLARE_TURN_KEY_length": len(cloudflare_turn_key or ""),
                    "CLOUDFLARE_TURN_KEY_has_whitespace": (
                        (cloudflare_turn_key or "") != (cloudflare_turn_key or "").strip()
                    ),
                    "TURN_TTL_SECONDS": os.environ.get("TURN_TTL_SECONDS", "unset"),
                }
            return JSONResponse(
                body,
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

    servers = build_ice_servers(
        turn_url=os.environ.get("TURN_URL"),
        turn_shared_secret=os.environ.get("TURN_SHARED_SECRET"),
        turn_ttl_seconds=int(os.environ.get("TURN_TTL_SECONDS", "3600")),
        turn_username=os.environ.get("TURN_USERNAME"),
        turn_credential=os.environ.get("TURN_CREDENTIAL"),
    )
    return JSONResponse(servers, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "rooms": {rid: len(r.players) for rid, r in rooms.items()},
        "active_codes": await registry.active_count(),
        # Report the backend actually in use, not merely "healthy" — the
        # in-memory fallback is healthy too, so a bare boolean was misleading.
        "storage": type(registry).__name__,
        "storage_url_configured": bool(os.environ.get("REDIS_URL")),
    })


@app.get("/api/leaderboard")
async def all_time_leaderboard(limit: int = 10) -> JSONResponse:
    """Lifetime standings, independent of any live room."""
    rows = await stats_store.top_players(max(1, min(limit, 50)))
    return JSONResponse({"rows": rows}, headers={"Cache-Control": "no-store"})


@app.post("/api/rooms")
async def create_room(request: Request) -> JSONResponse:
    """Reserve a fresh room code for a new game.

    The code is generated here rather than typed by the host so it is
    unguessable, and it expires unless somebody joins in time.
    """
    client_id = request.headers.get("CF-Connecting-IP")
    if not client_id:
        client_id = request.client.host if request.client else "unknown"
    if not await registry.allow_creation(client_id):
        return JSONResponse(
            {"error": "Too many games created. Please wait a few minutes."},
            status_code=429,
        )
    code = await registry.reserve_code(host_token="")
    if code is None:
        return JSONResponse(
            {"error": "The server is at capacity. Please try again shortly."},
            status_code=503,
        )
    return JSONResponse({
        "code": code,
        "expires_in": store.CODE_TTL_SECONDS,
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8899")),
        workers=1,
    )
