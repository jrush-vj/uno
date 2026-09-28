"""Verifies that the play-direction arrow's HEAD and BODY are attached.

The invariant: the body dash's leading edge and the head's base are the SAME
point on the track. Both are read from the live SVG, so nothing here
re-implements the app's geometry:

  * the head's base is its transform's translation (the polygon's base midpoint
    is deliberately at its local origin);
  * the body's leading edge is `getPointAtLength` on the same <rect>, at the
    lap fraction implied by its dash and offset.

Samples several frames and reports the worst gap, in viewBox units. The track
is 320x170 with a 24 radius, so a true attachment is a fraction of a unit;
anything near the 7-unit stroke width means the two have drifted apart.

    python tests/probe_arrow.py [--port 8899] [--frames 40]
"""
import argparse
import json
import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8899/"

# Read head base + body leading edge from the live SVG.
SAMPLE_JS = r"""
() => {
  const svg = document.querySelector('.pile-loop-svg');
  if (!svg) return { error: 'no loop svg' };
  const head = svg.querySelector('.pile-loop-head');
  const arc = svg.querySelector('.pile-loop-arc');
  const m = head.transform.baseVal.consolidate().matrix;
  const headBase = { x: m.e, y: m.f };

  const cs = getComputedStyle(arc);
  const dashArr = cs.strokeDasharray.split(',').map(parseFloat);
  const dash = dashArr[0];
  const gap = dashArr.length > 1 ? dashArr[1] : NaN;
  const off = parseFloat(cs.strokeDashoffset) || 0;

  const total = arc.getTotalLength();
  // Leading edge of the dash, as a fraction of the lap. pathLength="100" puts
  // dash and offset in the same percent-of-lap units.
  let leadPct = (dash - off) % 100;
  if (leadPct < 0) leadPct += 100;
  const p = arc.getPointAtLength((leadPct / 100) * total);

  return {
    headBase,
    leadPoint: { x: p.x, y: p.y },
    gapBetween: Math.round(Math.hypot(p.x - headBase.x, p.y - headBase.y) * 1000) / 1000,
    dash, gap, off, total: Math.round(total * 100) / 100,
    dashPeriod: Math.round((dash + gap) * 1000) / 1000,
    headTransform: head.getAttribute('transform'),
  };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--interval", type=int, default=120)
    ap.add_argument("--crop", default="", help="write a magnified crop of the loop here")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}/"
    worst = 0.0
    periods = set()
    samples = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 900},
                                  device_scale_factor=3)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.evaluate(
            """() => {
                document.querySelector('#inputName').value = 'Arrow';
                document.querySelector('#checkCamera').checked = false;
                document.querySelector('#btnCreateGame').click();
            }"""
        )
        page.wait_for_timeout(1200)
        page.evaluate("() => document.querySelector('#btnEnterTable').click()")
        page.wait_for_timeout(1800)

        for i in range(args.frames):
            s = page.evaluate(SAMPLE_JS)
            if s.get("error"):
                print(json.dumps(s))
                return 2
            samples.append(s)
            worst = max(worst, s["gapBetween"])
            periods.add(s["dashPeriod"])
            page.wait_for_timeout(args.interval)

        # A magnified crop of the ring, so the head can be seen joining the
        # body. Taken from a bounding box (not element.screenshot, which came
        # back wrong from this harness) and last, so nothing is measured after.
        if args.crop:
            box = page.evaluate(
                """() => {
                    const r = document.querySelector('#pileLoop').getBoundingClientRect();
                    return { x: r.x - 6, y: r.y - 6, width: r.width + 12, height: r.height + 12 };
                }"""
            )
            page.screenshot(path=args.crop, clip=box)

        browser.close()

    first = samples[0]
    print(f"frames sampled      : {len(samples)}")
    print(f"dash period         : {sorted(periods)}  (must be exactly 100)")
    print(f"stroke total length : {first['total']} user units "
          f"(rect perimeter is 320*2 + 170*2 + 2*pi*24-corner correction)")
    print(f"worst head/body gap : {worst} viewBox units")

    ok = worst < 1.5 and periods == {100.0}
    print("RESULT:", "PASS - head and body are attached" if ok else "FAIL")

    # A couple of raw samples help when it fails.
    for s in samples[:3]:
        print("  ", s["headTransform"], "| lead", s["leadPoint"], "| off", s["off"])

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
