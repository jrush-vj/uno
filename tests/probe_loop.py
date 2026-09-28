"""
Measures the play-direction loop around the two piles in a live table.

Starts a real 2-player game, waits for the deal, then reports the loop's box,
its clearances against the turn banner / start button / hand, and the shape of
every SVG child. Writes crops so the ring can be looked at, not just reasoned
about.
"""
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
OUT = pathlib.Path(__file__).resolve().parent

PROBE_JS = r"""
() => {
  const loop = document.querySelector('#pileLoop');
  if (!loop) return { error: 'no #pileLoop' };
  const l = loop.getBoundingClientRect();
  const centre = document.querySelector('.table-center').getBoundingClientRect();
  const piles = document.querySelector('.table-piles').getBoundingClientRect();
  const banner = document.querySelector('.table-turn-banner').getBoundingClientRect();
  const startBtn = document.querySelector('#btnStartGame');
  const hand = document.querySelector('.my-hand-panel').getBoundingClientRect();
  const svg = loop.querySelector('svg');
  const cs = getComputedStyle(loop);
  const svgcs = getComputedStyle(svg);
  const startBox = startBtn && getComputedStyle(startBtn).display !== 'none'
    ? startBtn.getBoundingClientRect() : null;

  const shapes = [...svg.querySelectorAll('rect, path, polygon')].map(n => {
    const s = getComputedStyle(n);
    const b = n.getBoundingClientRect();
    return {
      tag: n.tagName,
      cls: n.getAttribute('class'),
      strokeWidth: s.strokeWidth,
      fill: s.fill,
      box: { w: Math.round(b.width), h: Math.round(b.height) },
    };
  });

  return {
    loopBox: {
      w: Math.round(l.width), h: Math.round(l.height),
      top: Math.round(l.top), bottom: Math.round(l.bottom),
      left: Math.round(l.left), right: Math.round(l.right),
      cx: Math.round(l.left + l.width / 2),
      cy: Math.round(l.top + l.height / 2),
    },
    pilesBox: {
      w: Math.round(piles.width), h: Math.round(piles.height),
      top: Math.round(piles.top), bottom: Math.round(piles.bottom),
      left: Math.round(piles.left), right: Math.round(piles.right),
      cx: Math.round(piles.left + piles.width / 2),
      cy: Math.round(piles.top + piles.height / 2),
    },
    // A ring that is concentric with the piles has equal insets all round.
    insets: {
      left: Math.round(piles.left - l.left),
      right: Math.round(l.right - piles.right),
      top: Math.round(piles.top - l.top),
      bottom: Math.round(l.bottom - piles.bottom),
    },
    // Off-centre in either axis means the ring does not sit around the decks.
    offset: {
      dx: Math.round((l.left + l.width / 2) - (piles.left + piles.width / 2)),
      dy: Math.round((l.top + l.height / 2) - (piles.top + piles.height / 2)),
    },
    clearance: {
      aboveToBanner: Math.round(l.top - banner.bottom),
      belowToStartBtn: startBox ? Math.round(startBox.top - l.bottom) : null,
      belowToHand: Math.round(hand.top - l.bottom),
    },
    loopAspect: +(l.width / l.height).toFixed(3),
    viewBox: svg.getAttribute('viewBox'),
    preserveAspectRatio: svg.getAttribute('preserveAspectRatio') || '(default: xMidYMid meet)',
    svgBox: { w: Math.round(svg.getBoundingClientRect().width), h: Math.round(svg.getBoundingClientRect().height) },
    /* The head must sit ON the top edge of the track, not float above it or
       hang below it. Both are measured in screen pixels against the same box,
       and the track's top edge is the reference. */
    head: (() => {
      const head = svg.querySelector('.pile-loop-head');
      const track = svg.querySelector('.pile-loop-track');
      if (!head || !track) return { error: 'missing shape' };
      const h = head.getBoundingClientRect();
      const t = track.getBoundingClientRect();
      return {
        headTop: Math.round(h.top),
        headBottom: Math.round(h.bottom),
        headCentreY: Math.round(h.top + h.height / 2),
        trackTop: Math.round(t.top),
        // 0 means the head's tip is exactly on the track line.
        tipOffsetFromTrack: Math.round(h.top - t.top),
        // Negative means part of the head is outside the SVG and gets clipped.
        clippedAbove: Math.round(h.top - l.top) < 0,
        clippedBelow: Math.round(l.bottom - h.bottom) < 0,
        widthPx: Math.round(h.width),
        heightPx: Math.round(h.height),
      };
    })(),
    loopCss: { width: cs.width, height: cs.height, color: cs.color },
    svgCss: { width: svgcs.width, height: svgcs.height },
    insideCentre: l.left >= centre.left - 1 && l.right <= centre.right + 1,
    shapes,
  };
}
"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)

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

        guest_ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        guest = guest_ctx.new_page()
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

        page.evaluate("() => document.querySelector('#btnStartGame').click()")
        page.wait_for_timeout(6000)  # let the deal finish

        result = page.evaluate(PROBE_JS)
        print(json.dumps(result, indent=2))

        page.screenshot(path=str(OUT / "probe-loop-full.png"))
        page.screenshot(
            path=str(OUT / "probe-loop-crop.png"),
            clip={"x": 480, "y": 230, "width": 640, "height": 400},
        )
        # Tight crop right around the ring, so the stroke and arrowhead are
        # legible rather than a few pixels in a full-frame shot.
        lb = result["loopBox"]
        page.screenshot(
            path=str(OUT / "probe-loop-zoom.png"),
            clip={
                "x": max(0, lb["left"] - 45),
                "y": max(0, lb["top"] - 45),
                "width": lb["w"] + 90,
                "height": lb["h"] + 90,
            },
        )

        if errors:
            print("\nCONSOLE ERRORS:")
            for e in errors:
                print(" ", e)

        guest_ctx.close()
        ctx.close()
        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
