# Contributing

Thanks for helping improve this project. Here's how to get set up and what to
keep in mind when opening a pull request.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
```

Run the server locally:

```bash
python server.py                 # serves http://localhost:8899
```

`localhost` counts as a secure context, so camera/mic work without TLS during
development. Open two browser windows/tabs to play against yourself.

## Tests and lint

```bash
pytest -q
ruff check .
```

Please make sure both pass before opening a PR. CI runs the same commands.

## Project layout

| Path | Purpose |
|------|---------|
| `server.py` | FastAPI app: game engine, WebSocket protocol, background loops, HTTP routes. |
| `static/index.html` | The entire frontend (HTML + CSS + JS) in one self-contained file. |
| `tests/` | pytest unit tests for the rules engine. |
| `Dockerfile`, `docker-compose.yml`, `Caddyfile` | Self-hosting stack (app + coturn + Caddy). |
| `docs/deployment.md` | Homelab deployment walkthrough. |

## Guidelines

- **The server is authoritative.** The client renders server state; never let the
  client decide game outcomes. Add rules logic to `server.py` and cover it with a
  test in `tests/`.
- **Keep the frontend single-file.** `static/index.html` intentionally inlines all
  CSS/JS so it can be served as one asset. Avoid splitting it into separate files
  unless there's a strong reason.
- **Match existing style.** Follow the surrounding code's patterns, naming, and
  comment density. Avoid drive-by refactors in an unrelated PR.
- **Add tests** for new rules or protocol behavior.

## Pull requests

- Keep changes focused; one logical change per PR.
- Describe *what* changed and *how you verified it*.
- Update `README.md` / `docs/` when you change configuration or deployment.
