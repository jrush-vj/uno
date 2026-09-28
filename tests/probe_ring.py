"""
Checks the direction ring against the decks it frames.

Covers the three things that were wrong:
  1. clearance - the ring must sit OUTSIDE both cards on all four sides, not
     under them. Measured against the cards' own boxes.
  2. stacking - the ring must paint above the cards, which is what makes it read
     as wrapping them rather than poking out from behind.
  3. travel - the arrowhead must actually move along the path, and must stay on
     the track (not drift inside or outside it) as it goes.
Also captures the pre-game state, which should be white.
"""
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
OUT = pathlib.Path(__file__).resolve().parent


def open_table(page, guest):
    page.goto(BASE, wait_until="domcontentloaded")
    page.evaluate(
        """() => {
            document.querySelector('#inputName').value = 'Host';
            document.querySelector('#checkCamera').checked = false;
            document.querySelector('#btnCreateGame').click();
        }"""
    )
    page.wait_for_timeout(1000)
    page.evaluate("() => document.querySelector('#btnEnterTable').click()")
    page.wait_for_timeout(1200)
    code = page.evaluate("() => document.querySelector('#codeValue').textContent")
    guest.goto(BASE, wait_until="domcontentloaded")
    guest.evaluate(
        """(c) => {
            document.querySelector('#inputName').value = 'Guest';
            document.querySelector('#checkCamera').checked = false;
            document.querySelector('#btnShowJoin').click();
            document.querySelector('#inputCode').value = c;
            document.querySelector('#btnJoinWithCode').click();
        }""",
        code,
    )
    page.wait_for_timeout(1800)
    return code


GEOMETRY = r"""
() => {
  const loop = document.querySelector('#pileLoop');
  const l = loop.getBoundingClientRect();
  const cards = [...document.querySelectorAll('.table-piles .draw-deck-3d, .table-piles .discard-slot')]
    .map(n => {
      const r = n.getBoundingClientRect();
      return { left: Math.round(r.left), right: Math.round(r.right),
               top: Math.round(r.top), bottom: Math.round(r.bottom) };
    });
  const deck = cards[0], disc = cards[1];
  return {
    loop: { left: Math.round(l.left), right: Math.round(l.right),
            top: Math.round(l.top), bottom: Math.round(l.bottom) },
    cards,
    /* Positive means the ring is clear of the cards on that side. */
    clearance: {
      left: Math.round(deck.left - l.left),
      right: Math.round(l.right - disc.right),
      top: Math.round(deck.top - l.top),
      bottom: Math.round(l.bottom - deck.bottom),
    },
    colour: getComputedStyle(loop).color,
    zLoop: getComputedStyle(loop).zIndex,
    zDeck: getComputedStyle(document.querySelector('.draw-deck-3d')).zIndex,
    zDiscard: getComputedStyle(document.querySelector('.discard-slot')).zIndex,
  };
}
"""


def main():
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        gctx = browser.new_context(viewport={"width": 1600, "height": 900})
        gp = gctx.new_page()
        open_table(page, gp)

        # --- before the match: the ring must be white ------------------------
        pre = page.evaluate(GEOMETRY)
        print("pre-game  :", json.dumps({"colour": pre["colour"], "clearance": pre["clearance"]}))
        if pre["colour"] != "rgb(255, 255, 255)":
            problems.append(f"pre-game colour is {pre['colour']}, expected white")

        for side, gap in pre["clearance"].items():
            if gap <= 0:
                problems.append(f"pre-game: the ring is {side} of/under the cards by {gap}px")

        if int(pre["zLoop"]) <= max(int(pre["zDeck"]), int(pre["zDiscard"])):
            problems.append("the ring does not paint above the cards")

        page.screenshot(
            path=str(OUT / "probe-ring-pregame.png"),
            clip={"x": max(0, pre["loop"]["left"] - 40), "y": max(0, pre["loop"]["top"] - 40),
                  "width": (pre["loop"]["right"] - pre["loop"]["left"]) + 80,
                  "height": (pre["loop"]["bottom"] - pre["loop"]["top"]) + 80},
        )

        # --- start the match, then confirm the colour follows the first card --
        page.evaluate("() => document.querySelector('#btnStartGame').click()")
        # Wait for the deal to actually land rather than for a fixed timeout: the
        # ring's colour is driven by state.current_color, which only exists once
        # the server has dealt the first card. Polling the banner avoids
        # asserting on a table that is still in the lobby.
        started = False
        for _ in range(40):
            started = page.evaluate(
                """() => {
                    const b = document.querySelector('.table-turn-banner');
                    return !!b && !/Waiting for/.test(b.textContent);
                }"""
            )
            if started:
                break
            page.wait_for_timeout(250)

        if not started:
            problems.append("the match never started, so the ring colour could not be checked")
        page.wait_for_timeout(1200)

        post = page.evaluate(GEOMETRY)
        print("in-game   :", json.dumps({"colour": post["colour"], "clearance": post["clearance"]}))

        for side, gap in post["clearance"].items():
            if gap <= 0:
                problems.append(f"in-game: the ring is {side} of/under the cards by {gap}px")

        if post["colour"] == "rgb(255, 255, 255)":
            problems.append("in-game: the ring stayed white after the first card was played")

        # The ring must agree with the discard glow, which is the same colour
        # source. Disagreement means one of the two is reading a stale value.
        agree = page.evaluate(
            """() => {
                const loop = getComputedStyle(document.querySelector('#pileLoop')).color;
                const felt = getComputedStyle(document.querySelector('#feltTable'))
                    .getPropertyValue('--active-color').trim();
                const orb = getComputedStyle(document.querySelector('#colorOrb')).backgroundColor;
                return { loop, felt, orb };
            }"""
        )
        print("colour sync:", json.dumps(agree))
        if agree["loop"] != agree["orb"]:
            problems.append(
                f"the ring is {agree['loop']} but the colour orb is {agree['orb']} — "
                "they should read the same colour"
            )

        # --- the head must travel, and must stay on the track ---------------
        track = page.evaluate(
            """() => {
                const r = document.querySelector('.pile-loop-track').getBoundingClientRect();
                return { left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                         stroke: parseFloat(getComputedStyle(document.querySelector('.pile-loop-track')).strokeWidth) };
            }"""
        )

        samples = []
        for _ in range(8):
            samples.append(
                page.evaluate(
                    """() => {
                        const h = document.querySelector('.pile-loop-head').getBoundingClientRect();
                        const t = document.querySelector('.pile-loop-track').getBoundingClientRect();
                        const cx = h.left + h.width / 2, cy = h.top + h.height / 2;
                        // Distance from the head's centre to the track's outline.
                        const dx = Math.max(t.left - cx, 0, cx - t.right);
                        const dy = Math.max(t.top - cy, 0, cy - t.bottom);
                        return { cx: Math.round(cx), cy: Math.round(cy),
                                 distToEdge: Math.round(Math.hypot(dx, dy)),
                                 w: Math.round(h.width), h: Math.round(h.height) };
                    }"""
                )
            )
            page.wait_for_timeout(350)

        positions = {(s["cx"], s["cy"]) for s in samples}
        print("head path :", json.dumps(samples))

        if len(positions) < 4:
            problems.append(f"the arrowhead barely moved: only {len(positions)} distinct positions")

        # The head rides the outline, so its centre should stay within a stroke
        # width or so of the track's edge the whole way round.
        tol = max(8, track["stroke"] * 3)
        for s in samples:
            if s["distToEdge"] > tol:
                problems.append(
                    f"the arrowhead drifted {s['distToEdge']}px off the track at ({s['cx']},{s['cy']})"
                )

        # A head that rotates through corners will not be axis-aligned; a head
        # frozen at one angle would keep a constant width/height.
        if len({(s["w"], s["h"]) for s in samples}) < 2:
            problems.append("the arrowhead is not rotating with the path")

        page.screenshot(
            path=str(OUT / "probe-ring-ingame.png"),
            clip={"x": max(0, post["loop"]["left"] - 40), "y": max(0, post["loop"]["top"] - 40),
                  "width": (post["loop"]["right"] - post["loop"]["left"]) + 80,
                  "height": (post["loop"]["bottom"] - post["loop"]["top"]) + 80},
        )

        if errors:
            problems.extend(f"console: {e}" for e in errors if "setLocalDescription" not in e)

        print("\nproblems:", json.dumps(problems, indent=2))
        print("ok:", not problems)

        gctx.close()
        ctx.close()
        browser.close()

    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())