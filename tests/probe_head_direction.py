"""
Measures whether the arrowhead points the way it is travelling.

The head is a polygon with its tip on +x, rotated by `offset-rotate: auto` so
its +x axis follows the path tangent. "Ahead, attached" means the tip must be
FURTHER ALONG the path than the base - i.e. the tip leads.

This walks the head along the top edge (where travel is left-to-right, so the
comparison is a plain x comparison) and reports the tip/base relationship at
several instants.
"""
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
OUT = pathlib.Path(__file__).resolve().parent

# Reads the polygon's own geometry and the transform the browser applied, then
# converts the tip and the base-midpoint into screen space.
SAMPLE = r"""
() => {
  const head = document.querySelector('.pile-loop-head');
  const svg = head.ownerSVGElement;
  const pts = head.getAttribute('points').trim().split(/\s+/).map(p => {
    const [x, y] = p.split(',').map(Number);
    return { x, y };
  });
  // points is tip-first: ["20,0", "0,-9", "0,9"]
  const tip = pts[0];
  const baseMid = { x: (pts[1].x + pts[2].x) / 2, y: (pts[1].y + pts[2].y) / 2 };

  // The CTM maps the polygon's user space to screen space, so applying it to
  // the local tip/base gives their real positions after offset rotation.
  const ctm = head.getScreenCTM();
  const toScreen = (p) => ({
    x: ctm.a * p.x + ctm.c * p.y + ctm.e,
    y: ctm.b * p.x + ctm.d * p.y + ctm.f,
  });
  const sTip = toScreen(tip);
  const sBase = toScreen(baseMid);

  // Which way is the path heading here? Compare against the next frame's
  // position of the same point, so this does not assume a direction.
  return {
    tip: { x: +sTip.x.toFixed(1), y: +sTip.y.toFixed(1) },
    base: { x: +sBase.x.toFixed(1), y: +sBase.y.toFixed(1) },
    offsetDistance: getComputedStyle(head).offsetDistance,
    offsetRotate: getComputedStyle(head).offsetRotate,
    offsetAnchor: getComputedStyle(head).offsetAnchor,
    transform: getComputedStyle(head).transform,
  };
}
"""


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

        # Freeze the animation and step it by hand, so a sample is exactly one
        # known point on the path rather than "whenever the frame landed".
        # Setting animationDelay to a negative value seeks the running animation
        # to that offset, which is how a CSS animation is scrubbed from script.
        page.evaluate(
            """() => {
                const h = document.querySelector('.pile-loop-head');
                h.style.animationPlayState = 'paused';
            }"""
        )

        rows = []
        for step in range(0, 100, 5):
            page.evaluate(
                """(pct) => {
                    const h = document.querySelector('.pile-loop-head');
                    // 5s duration, so pct% of the lap is pct/100*5 seconds.
                    h.style.animationDelay = (-(pct / 100) * 5) + 's';
                }""",
                step,
            )
            page.wait_for_timeout(60)
            a = page.evaluate(SAMPLE)
            # Step one percent forward to learn which way the path moves here.
            page.evaluate(
                """(pct) => {
                    const h = document.querySelector('.pile-loop-head');
                    h.style.animationDelay = (-((pct + 1) / 100) * 5) + 's';
                }""",
                step,
            )
            page.wait_for_timeout(60)
            b = page.evaluate(SAMPLE)

            # Direction of travel at this point on the path, as a unit vector.
            dx = b["tip"]["x"] - a["tip"]["x"]
            dy = b["tip"]["y"] - a["tip"]["y"]
            mag = (dx * dx + dy * dy) ** 0.5

            # Tip must lead the base ALONG THE DIRECTION OF TRAVEL.
            #
            # The earlier version of this check multiplied the tip-base offset
            # by the travel vector, which is only meaningful if the shape's
            # local +x happens to align with travel. It reported "ahead" for a
            # head pointing backwards, because the tip is at +x in the shape's
            # own frame no matter how the shape is rotated. Projecting onto the
            # unit travel vector is what actually answers "is the tip forward?".
            if mag < 1e-6:
                continue
            ux, uy = dx / mag, dy / mag
            lead = (a["tip"]["x"] - a["base"]["x"]) * ux + (a["tip"]["y"] - a["base"]["y"]) * uy

            rows.append({
                "pct": step,
                "tip": a["tip"],
                "base": a["base"],
                "travel": {"dx": round(dx, 2), "dy": round(dy, 2)},
                "lead": round(lead, 2),
                "ahead": lead > 0,
            })

        rows_out = []
        for r in rows:
            rows_out.append({k: r[k] for k in ("pct", "lead", "ahead", "travel")})
        print(json.dumps(rows_out, indent=1))

        backwards = [r for r in rows if r["lead"] < 0]
        if backwards:
            worst = min(backwards, key=lambda r: r["lead"])
            problems.append(
                f"the arrowhead points BACKWARDS at {len(backwards)}/{len(rows)} "
                f"points on the path (worst lead {worst['lead']} at {worst['pct']}%)"
            )

        print("\nproblems:", json.dumps(problems, indent=2))
        print("ok:", not problems)

        # A still frame with the head parked part-way along the top edge, for a
        # visual check.
        page.evaluate(
            """() => {
                const h = document.querySelector('.pile-loop-head');
                h.style.animationDelay = '-1.5s';   // 30% of a 5s lap
            }"""
        )
        page.wait_for_timeout(200)
        loop = page.evaluate(
            """() => {
                const l = document.querySelector('#pileLoop').getBoundingClientRect();
                return { left: l.left, top: l.top, w: l.width, h: l.height };
            }"""
        )
        page.screenshot(
            path=str(OUT / "probe-head-direction.png"),
            clip={"x": max(0, loop["left"] - 30), "y": max(0, loop["top"] - 30),
                  "width": loop["w"] + 60, "height": loop["h"] + 60},
        )

        gctx.close()
        ctx.close()
        browser.close()

    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())