# Homelab Deployment

This walks through deploying the game on a home server so friends can join from
anywhere with working video and audio.

## What you need

- A host that runs Docker and Docker Compose (a NAS, mini PC, Proxmox CT/VM, etc.).
- A domain (or subdomain) whose DNS `A`/`AAAA` record points at your public IP —
  required for automatic HTTPS. Without HTTPS, browsers refuse camera/mic access
  on anything except `localhost`.
- Ports `80/tcp`, `443/tcp`, `3478/udp`, `3478/tcp`, and `49160-49200/udp`
  forwarded/allowed to the host.

## 1. Get the code

```bash
git clone <your-repo-url> uno
cd uno
```

## 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

- `DOMAIN` — e.g. `uno.example.com`. Caddy will get a TLS certificate for it.
- `TURN_EXTERNAL_IP` — your **public** IP. Required when the host is behind NAT
  (most homes are). Leave blank only if the host has a directly routable public IP.
- `TURN_URL` — `turn:<public-ip-or-host>:3478` (use the same public IP/hostname).
- `TURN_USERNAME` / `TURN_CREDENTIAL` — set to a strong password. These authenticate
  the relay and are served to clients via `/api/ice-config`.
- `ALLOWED_ORIGINS` — recommended for public deployments, e.g.
  `https://uno.example.com`. Leave empty to allow any origin.

## 3. Start it

```bash
docker compose up -d --build
docker compose ps          # all three services should be running/healthy
curl -k https://uno.example.com/api/health
```

Open `https://uno.example.com`, enter a name and room, and you're in.

## Why TURN matters

WebRTC tries to connect peers directly. On the same LAN that usually works with
STUN alone, but between two different networks (phone on cellular ↔ laptop on
WiFi, strict or symmetric NATs) a direct path often can't be found. `coturn`
provides a relay both peers can reach, which is what makes those calls reliable.
The bundled `coturn` service is preconfigured and wired to the app via the
`TURN_*` variables — you don't need to run anything separately.

The app also advertises a `turns:`/5349 (TURN over TLS) variant automatically as
a fallback for networks that block UDP.

## LAN-only setup (no domain)

If you only ever play on your local network, edit `Caddyfile`: comment out the
`{$DOMAIN}` block and uncomment the `:80` block. Then reach the game at
`http://<host-ip>`. Remember that camera/mic are unavailable to players on other
machines without HTTPS — only the machine visiting `localhost` gets media access.

## Behind a reverse proxy you already run

If you already terminate TLS elsewhere (nginx, Traefik, etc.), you can drop the
`caddy` service from `docker-compose.yml`, expose the `uno` service's port `8899`
to your proxy, and point it at `uno:8899` (WebSocket upgrades must be enabled).
You still need the `coturn` service for cross-network video.

## Updating

```bash
git pull
docker compose up -d --build
```

Game state is in-memory, so restarting clears active rooms and scores. Clients
reconnect automatically to the same room using their saved session token.

## Notes for Windows / macOS hosts

`TURN_EXTERNAL_IP` behavior depends on your network. If Docker Desktop's NAT makes
relay advertisement unreliable, the most robust option is to run `coturn` on the
host network or on a separate Linux box on the same LAN and point `TURN_URL` at it.
