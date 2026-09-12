# UNO Online

A self-hosted multiplayer UNO game with **live video and audio** between players.
Spin it up on your homelab with Docker and play with friends across the internet —
up to 7 players per table, WebRTC video chat, persistent scoring, and
reconnect-safe sessions.

<!-- Replace with a real screenshot once available -->
<!-- ![Screenshot](docs/screenshot.png) -->

[![CI](https://github.com/your-org/uno/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/uno/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## Features

- **Up to 7 players** per room, any number of concurrent rooms.
- **Live video + audio** via a WebRTC mesh, with automatic TURN relay for players
  on different networks.
- **Official UNO scoring** — when a player goes out, they score the value of every
  opponent's remaining cards. Totals persist across rounds for the server session.
- **No Pass button** — draw one card; if it's playable you must play it, otherwise
  the turn auto-advances. The standard draw-one-play-it rule.
- **Call UNO / catch UNO** with the usual penalties.
- **Reconnect-safe** — refresh the page, drop WiFi, or lock your phone and you keep
  your seat and hand via a saved session token.
- **Auto-skip for disconnected players** so the table never stalls.
- **One-command self-hosting** with Docker Compose (app + TURN + HTTPS).

## Quick start

```bash
git clone <your-repo-url> uno
cd uno
cp .env.example .env      # then edit DOMAIN and TURN_* values
docker compose up -d --build
```

Then open `https://<your-domain>`. See [docs/deployment.md](docs/deployment.md)
for the full walkthrough (DNS, ports, TURN, NAT).

For local development without Docker:

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py                                     # http://localhost:8899
```

## Configuration

All settings are environment variables in `.env`. See `.env.example` for the
annotated template.

| Variable | Default | Description |
|----------|---------|-------------|
| `DOMAIN` | — | Domain Caddy serves and obtains a TLS certificate for. |
| `TURN_URL` | — | TURN relay URL, e.g. `turn:203.0.113.5:3478`. Required for cross-network video. |
| `TURN_USERNAME` | — | TURN credential username. |
| `TURN_CREDENTIAL` | — | TURN credential password. |
| `TURN_REALM` | `uno.local` | TURN authentication realm. |
| `TURN_EXTERNAL_IP` | — | Public IP coturn advertises when the host is behind NAT. |
| `PORT` | `8899` | Internal HTTP port the app listens on. |
| `ALLOWED_ORIGINS` | *(empty)* | Comma-separated WebSocket origin allow-list. Empty = allow all. |
| `MAX_ROOMS` | `200` | Maximum concurrent rooms. |
| `MAX_MESSAGE_BYTES` | `65536` | Largest accepted WebSocket message. |
| `RATE_LIMIT_PER_SEC` | `100` | Per-connection message rate limit (0 disables). |

## Architecture

```
Browser (static/index.html)                Server (server.py)
  - renders server state        WebSocket   - authoritative game engine
  - WebRTC mesh to peers    <────────────>  - relays WebRTC signaling
  - captures cam/mic                         - in-memory rooms + scores
                          TURN (coturn) <- for cross-network media
                          Caddy         <- TLS termination + HTTPS/wss
```

- **`server.py`** — FastAPI app. Owns the rules engine, the JSON WebSocket
  protocol (`/ws/{room_id}`), two background loops (stale-seat cleanup and
  disconnected-player auto-skip), and small HTTP routes for the page, ICE config,
  and health.
- **`static/index.html`** — the entire frontend in one self-contained file: layout,
  styling, game rendering, and the WebRTC mesh (perfect-negotiation pattern).
- **State is in-memory.** Rooms and cumulative scores live for the lifetime of the
  process; restarting the server clears them. The app must run as a **single
  worker** for this reason.

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

## Troubleshooting

**Video/audio doesn't connect between players on different networks.**
Check that `TURN_URL`, `TURN_USERNAME`, and `TURN_CREDENTIAL` are set, that the
`coturn` service is running, and that UDP `3478` and `49160-49200` are reachable.
Behind NAT, set `TURN_EXTERNAL_IP` to your public IP.

**The browser won't grant camera/mic access.**
Media requires a secure context: serve over HTTPS (the bundled Caddy does this) or
use `localhost`. Plain HTTP on a LAN IP will not get camera/mic.

**"Table is full (7/7)".**
A table holds 7 seats. Free a seat (a player leaves) or create a new room.

**Scores reset after I restarted the server.**
Expected — state is in-memory by design.

## Roadmap

- House-rule toggle for Wild Draw 4 legality.
- Optional persistence for cumulative scores across restarts.
- Layout and UX polish for 2-player and 7-player tables.
- Rebranding to a trademark-safe name with a configurable app name.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE).

## Disclaimer

This is an unofficial, fan-made project. It is **not affiliated with, endorsed by,
or sponsored by Mattel, Inc.** "UNO" is a trademark of Mattel, Inc. This project
ships no official artwork, branding, or assets.
