"""
Checks the loop in both play directions and reports console errors.

The clockwise case is the one the screenshot covers; this adds the anticlockwise
case, which is the same DOM with `.reverse` (scaleX(-1)) applied, and prints any
console error raised while the table was up.
"""
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
OUT = pathlib.Path(__file__).resolve().parent

MEASURE = r"""
() => {
  const loop = document.querySelector('#pileLoop');
  const l = loop.getBoundingClientRect();
  const piles = document.querySelector('.table-piles').getBoundingClientRect();
  const head = loop.querySelector('.pile-loop-head').getBoundingClientRect();
  const track = loop.querySelector('.pile-loop-track').getBoundingClientRect();
  const svg = loop.querySelector('svg');
  return {
    reversed: loop.classList.contains('reverse'),
    transform: getComputedStyle(loop).transform,
    // Concentric with the decks, whichever way it is mirrored.
    dx: Math.round((l.left + l.width / 2) - (piles.left + piles.width / 2)),
    dy: Math.round((l.top + l.height / 2) - (piles.top + piles.height / 2)),
    // The head's centre must stay on the track's top edge after mirroring.
    headOnTrack: Math.round((head.top + head.height / 2) - track.top) === 0,
    // Mirroring must not push the head or the ring outside the box.
    headInside: head.left >= l.left - 1 && head.right <= l.right + 1
                && head.top >= l.top - 1 && head.bottom <= l.bottom + 1,
    preserveAspectRatio: svg.getAttribute('preserveAspectRatio'),
  };
}
"""


def main():
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        console_errors = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))

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
        page.wait_for_timeout(1000)
        code = page.evaluate("() => document.querySelector('#codeValue').textContent")

        gctx = browser.new_context(viewport={"width": 1600, "height": 900})
        gp = gctx.new_page()
        gp.goto(BASE, wait_until="domcontentloaded")
        gp.evaluate(
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
        page.evaluate("() => document.querySelector('#btnStartGame').click()")
        page.wait_for_timeout(6000)

        fwd = page.evaluate(MEASURE)

        # Force the anticlockwise branch. The reverse class is what updateUI
        # toggles from state.direction, so setting it directly exercises the
        # same styling path the server would drive.
        page.evaluate("() => document.querySelector('#pileLoop').classList.add('reverse')")
        page.wait_for_timeout(500)
        rev = page.evaluate(MEASURE)

        lb = page.evaluate(
            """() => {
                const l = document.querySelector('#pileLoop').getBoundingClientRect();
                return { left: Math.round(l.left), top: Math.round(l.top),
                         w: Math.round(l.width), h: Math.round(l.height) };
            }"""
        )
        page.screenshot(
            path=str(OUT / "probe-loop-reverse.png"),
            clip={
                "x": max(0, lb["left"] - 45),
                "y": max(0, lb["top"] - 45),
                "width": lb["w"] + 90,
                "height": lb["h"] + 90,
            },
        )

        print("clockwise     :", json.dumps(fwd))
        print("anticlockwise :", json.dumps(rev))

        for label, r in (("clockwise", fwd), ("anticlockwise", rev)):
            if r["dx"] != 0 or r["dy"] != 0:
                problems.append(f"{label}: ring is off-centre by dx={r['dx']} dy={r['dy']}")
            if not r["headOnTrack"]:
                problems.append(f"{label}: arrowhead is not centred on the track edge")
            if not r["headInside"]:
                problems.append(f"{label}: arrowhead is clipped by the loop box")
            if r["preserveAspectRatio"] != "none":
                problems.append(f"{label}: preserveAspectRatio is {r['preserveAspectRatio']}")

        if rev["transform"] == fwd["transform"]:
            problems.append("anticlockwise: mirror is not applied (transform unchanged)")

        if console_errors:
            problems.extend(f"console: {e}" for e in console_errors)

        print("\nproblems:", json.dumps(problems, indent=2))
        print("ok:", not problems)

        gctx.close()
        ctx.close()
        browser.close()

    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())