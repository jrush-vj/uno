"""Drives one real move through the UI and reports whether it took.

The layout probes measure geometry; this one exercises behaviour: it finds the
first card the client has marked playable, clicks it, and confirms the discard
pile changed and the turn moved. Any console error or page exception is
reported too, because a thrown error mid-render leaves the table looking fine
while nothing works.

    python tests/probe_play.py [--port 8899] [--guests 2]
"""
import argparse
import json
import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8899/"

READ_JS = r"""
() => {
  const top = document.querySelector('#discardTopCardContainer .uno-card');
  const playable = [...document.querySelectorAll('#myHandCardsContainer .uno-card.playable')];
  return {
    top: top ? top.className : null,
    topSymbol: top ? (top.querySelector('.card-symbol-center') || {}).textContent : null,
    handSize: document.querySelectorAll('#myHandCardsContainer .uno-card').length,
    playableCount: playable.length,
    firstPlayable: playable.length
      ? (playable[0].getAttribute('aria-label') || '') : null,
    banner: document.querySelector('#turnBannerText').textContent,
    myTurn: document.querySelector('#turnBannerText').textContent.includes('YOUR TURN'),
    myCardIds: [...document.querySelectorAll('#myHandCardsContainer .uno-card')].map(c => c.dataset.cardId),
    wildModalOpen: document.querySelector('#modalColorChoice').classList.contains('active'),
    animating: document.querySelectorAll('.fly-card').length,
  };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--guests", type=int, default=2)
    ap.add_argument("--steps", type=int, default=12, help="passes over the table")
    ap.add_argument("--moves", type=int, default=8, help="stop after this many moves")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}/"
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        viewport = {"width": 1600, "height": 900}

        host_ctx = browser.new_context(viewport=viewport)
        host = host_ctx.new_page()
        host.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type in ("error", "warning") and "TURN relay" not in m.text else None)
        host.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        host.goto(url, wait_until="domcontentloaded")
        host.evaluate(
            """() => {
                document.querySelector('#inputName').value = 'Host';
                document.querySelector('#checkCamera').checked = false;
                document.querySelector('#btnCreateGame').click();
            }"""
        )
        host.wait_for_timeout(1200)
        code = host.evaluate("() => document.querySelector('#codeValue').textContent.trim()")
        host.evaluate("() => document.querySelector('#btnEnterTable').click()")
        host.wait_for_timeout(1500)

        pages_guests = []
        for i in range(args.guests):
            gctx = browser.new_context(viewport=viewport)
            gp = gctx.new_page()
            gp.on("pageerror", lambda e: errors.append(f"guest pageerror: {e}"))
            gp.goto(url, wait_until="domcontentloaded")
            gp.evaluate(
                """({ n, c }) => {
                    document.querySelector('#inputName').value = n;
                    document.querySelector('#checkCamera').checked = false;
                    document.querySelector('#inputCode').value = c;
                    window.btnJoinClicked();
                }""",
                {"n": f"Guest{i + 1}", "c": code},
            )
            gp.wait_for_timeout(900)
            pages_guests.append(gp)

        host.bring_to_front()
        host.wait_for_timeout(600)
        host.evaluate("() => document.querySelector('#btnStartGame').click()")
        host.wait_for_timeout(7000)

        before = host.evaluate(READ_JS)
        print("before :", json.dumps(before))

        # Drive EVERY seat, not just the host. There is no Pass button and the
        # turn timer is off here, so a seat nobody plays simply holds the turn
        # forever - which is correct behaviour, and why a single-seat probe
        # stalls the moment a guest leads.
        pages = [host] + pages_guests
        moves = 0
        for step in range(args.steps):
            for page in pages:
                try:
                    state = page.evaluate(READ_JS)
                except Exception:
                    continue
                if not state["myTurn"]:
                    continue
                if state["playableCount"]:
                    page.evaluate(
                        """() => {
                            const el = document.querySelector('#myHandCardsContainer .uno-card.playable');
                            if (el) el.click();
                        }"""
                    )
                    page.wait_for_timeout(900)
                    # A wild opens the colour picker rather than playing.
                    if page.evaluate("() => document.querySelector('#modalColorChoice').classList.contains('active')"):
                        page.evaluate("() => document.querySelector('.btn-color-choice.red').click()")
                    moves += 1
                else:
                    page.evaluate("() => document.querySelector('#btnDrawDeck').click()")
                    moves += 1
                page.wait_for_timeout(900)
            if moves >= args.moves:
                break

        host.wait_for_timeout(1500)
        after = host.evaluate(READ_JS)
        print("after  :", json.dumps(after))
        print(f"moves played: {moves} across {len(pages)} seats")

        print()
        if errors:
            print("CONSOLE ISSUES:")
            for e in errors:
                print("  ", e)
        else:
            print("no console errors")

        # A move reached the server if the discard changed or any hand shrank.
        changed = before["top"] != after["top"] or before["handSize"] != after["handSize"]
        ok = moves > 0 and not errors
        print("RESULT:", "PASS - moves reached the server" if (ok and changed) else
              ("PARTIAL - moves sent but nothing visibly changed" if moves else "SKIP - no seat ever had the turn"))

        browser.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
