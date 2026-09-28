"""
Checks the direction arrowhead: does the tip lead the travel direction?

The head is driven by app.js writing its transform each frame, so this simply
watches it over time. At every sample the tip must be displaced from the base
along the direction the path is moving, and the tip must stay on the track.
"""
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
OUT = pathlib.Path(__file__).resolve().parent

SAMPLE = r"""
() => {
  const poly = document.querySelector('#pileLoop .pile-loop-head');
  const svg = poly.ownerSVGElement;
  const pts = poly.getAttribute('points').trim().split(/\s+/).map(q => {
    const [x, y] = q.split(',').map(Number);
    return [x, y];
  });
  const ctm = poly.getScreenCTM();
  const toS = (p) => ({ x: ctm.a * p[0] + ctm.c * p[1] + ctm.e,
                        y: ctm.b * p[0] + ctm.d * p[1] + ctm.f });
  const t = svg.querySelector('.pile-loop-track').getBoundingClientRect();
  return {
    tip: toS(pts[0]),
    b1: toS(pts[1]),
    b2: toS(pts[2]),
    track: { left: t.left, right: t.right, top: t.top, bottom: t.bottom },
    transform: poly.getAttribute('transform'),
  };
}
"""


def dist_to_edge(pt, t):
    dx = max(t["left"] - pt["x"], 0.0, pt["x"] - t["right"])
    dy = max(t["top"] - pt["y"], 0.0, pt["y"] - t["bottom"])
    return (dx * dx + dy * dy) ** 0.5


def main():
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        gctx = browser.new_context(viewport={"width": 1600, "height": 900})
        gp = gctx.new_page()

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

        rows = []
        prev = page.evaluate(SAMPLE)
        for i in range(60):
            page.wait_for_timeout(85)
            cur = page.evaluate(SAMPLE)

            vx = cur["tip"]["x"] - prev["tip"]["x"]
            vy = cur["tip"]["y"] - prev["tip"]["y"]
            mag = (vx * vx + vy * vy) ** 0.5
            if mag > 0.5:
                base = {"x": (cur["b1"]["x"] + cur["b2"]["x"]) / 2,
                        "y": (cur["b1"]["y"] + cur["b2"]["y"]) / 2}
                lead = ((cur["tip"]["x"] - base["x"]) * vx
                        + (cur["tip"]["y"] - base["y"]) * vy) / mag
                rows.append({
                    "i": i,
                    "tip": [round(cur["tip"]["x"]), round(cur["tip"]["y"])],
                    "lead": round(lead, 1),
                    "ahead": lead > 0,
                    "tipOff": round(dist_to_edge(cur["tip"], cur["track"]), 1),
                    "tf": cur["transform"],
                })
            prev = cur

        for r in rows[::4]:
            print(json.dumps(r))

        if not rows:
            problems.append("the arrowhead never moved")
        else:
            back = [r for r in rows if not r["ahead"]]
            if back:
                worst = min(back, key=lambda r: r["lead"])
                problems.append(
                    f"tip trails travel at {len(back)}/{len(rows)} samples "
                    f"(worst {worst['lead']} at tip {worst['tip']})"
                )
            off = [r for r in rows if r["tipOff"] > 4.0]
            if off:
                problems.append(
                    f"tip leaves the track at {len(off)}/{len(rows)} samples "
                    f"(worst {max(r['tipOff'] for r in off)}px)"
                )
            if len({tuple(r["tip"]) for r in rows}) < 6:
                problems.append("the head barely moved around the lap")

        print("\nproblems:", json.dumps(problems, indent=2))
        print("ok:", not problems)

        loop = page.evaluate(
            """() => {
                const l = document.querySelector('#pileLoop').getBoundingClientRect();
                return { left: l.left, top: l.top, w: l.width, h: l.height };
            }"""
        )
        page.screenshot(
            path=str(OUT / "probe-head-live.png"),
            clip={"x": max(0, loop["left"] - 30), "y": max(0, loop["top"] - 30),
                  "width": loop["w"] + 60, "height": loop["h"] + 60},
        )

        gctx.close()
        ctx.close()
        browser.close()

    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())