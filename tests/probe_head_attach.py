"""
Measures the arrowhead's position relative to the track, edge by edge.

An earlier check used bounding-box overlap on one axis, which a head floating
outside the ring can still satisfy: the overlap is measured along x, so a head
parked to the right of the stroke's x-range but level with it reports full
coverage. This instead computes the TRUE perpendicular distance from the head's
centroid to the nearest edge's centreline, and the signed direction, per edge.
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
  const corners = pts.map(toS);

  const trackEl = svg.querySelector('.pile-loop-track');
  const tb = trackEl.getBoundingClientRect();
  const sw = parseFloat(getComputedStyle(trackEl).strokeWidth);

  const c = {
    x: (corners[0].x + corners[1].x + corners[2].x) / 3,
    y: (corners[0].y + corners[1].y + corners[2].y) / 3,
  };

  const edges = [
    { name: 'top',    axis: 'y', pos: tb.top + sw / 2,    outward: -1 },
    { name: 'bottom', axis: 'y', pos: tb.bottom - sw / 2, outward:  1 },
    { name: 'left',   axis: 'x', pos: tb.left + sw / 2,   outward: -1 },
    { name: 'right',  axis: 'x', pos: tb.right - sw / 2,  outward:  1 },
  ];

  let near = null, best = Infinity;
  for (const e of edges) {
    const d = Math.abs(c[e.axis] - e.pos);
    if (d < best) { best = d; near = e; }
  }
  const signed = near.outward * (c[near.axis] - near.pos);

  return {
    edge: near.name,
    centroid: { x: +c.x.toFixed(1), y: +c.y.toFixed(1) },
    perpendicularOffStroke: +best.toFixed(2),
    signedOutward: +signed.toFixed(2),
    strokeWidth: sw,
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

        samples = []
        # The lap is 5s. Sampling every 120ms for 46 steps covers slightly more
        # than one revolution, so every edge and corner is visited.
        for _ in range(46):
            samples.append(page.evaluate(SAMPLE))
            page.wait_for_timeout(120)

        # Guard against a probe that silently samples one edge only: the lap must
        # be seen all the way round, or the per-edge report below is a fiction.
        if len({s["edge"] for s in samples}) < 4:
            problems.append(
                "probe only sampled edges: " + ", ".join(sorted({s["edge"] for s in samples}))
            )

        by_edge = {}
        for s in samples:
            by_edge.setdefault(s["edge"], []).append(s)

        fieldnames = sorted(by_edge)
        for edge in fieldnames:
            rows = by_edge[edge]
            offs = [r["perpendicularOffStroke"] for r in rows]
            outs = [r["signedOutward"] for r in rows]
            print("%-7s n=%-3d perp off stroke: %5.2f..%5.2f   signed outward: %5.2f..%5.2f"
                  % (edge, len(rows), min(offs), max(offs), min(outs), max(outs)))

        sw = samples[0]["strokeWidth"]
        print("\nstroke width: %.1f px" % sw)

        tol = sw / 2
        bad = [s for s in samples if s["perpendicularOffStroke"] > tol]
        if bad:
            worst = max(bad, key=lambda s: s["perpendicularOffStroke"])
            problems.append(
                f"head centroid is {worst['perpendicularOffStroke']:.2f}px from the stroke "
                f"centreline on the {worst['edge']} edge (tolerance {tol:.1f}px)"
            )

        out = [s for s in samples if s["signedOutward"] > tol]
        if out:
            worst = max(out, key=lambda s: s["signedOutward"])
            problems.append(
                f"head is pushed {worst['signedOutward']:.2f}px OUTWARD off the "
                f"{worst['edge']} edge"
            )

        print("\nproblems:", json.dumps(problems, indent=2))
        print("ok:", not problems)

        loop = page.evaluate(
            """() => {
                const l = document.querySelector('#pileLoop').getBoundingClientRect();
                return { left: l.left, top: l.top, w: l.width, h: l.height };
            }"""
        )
        page.screenshot(
            path=str(OUT / "probe-head-attached.png"),
            clip={"x": max(0, loop["left"] - 30), "y": max(0, loop["top"] - 30),
                  "width": loop["w"] + 60, "height": loop["h"] + 60},
        )

        gctx.close()
        ctx.close()
        browser.close()

    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())