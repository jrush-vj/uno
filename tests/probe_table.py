"""Tableside probe: seats a few players, starts a round, then reports the
geometry that the layout/arrow work depends on.

Run with the server already listening (default 127.0.0.1:8899):

    python tests/probe_table.py [--port 8899] [--guests 3] [--shot out.png]

Every player gets its OWN browser context. That matters: `localStorage` holds
the seat token (`uno_token_<code>`), so two tabs in one context make the second
one rejoin the FIRST player's seat instead of taking a new one - the server log
then reads `reconnect=True` and the table stays at one player. Separate contexts
give each player a clean storage partition, which is what a separate device is.

Everything printed is read from ONE `page.evaluate` per step, in the same call
as the measurement, so a stale frame can never be reported as truth.
"""
import argparse
import json
import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8899/"


MEASURE_JS = r"""
() => {
  const rect = el => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
             right: Math.round(r.right), bottom: Math.round(r.bottom) };
  };
  const out = { viewport: { w: innerWidth, h: innerHeight }, hidden: document.hidden };

  const loop = document.querySelector('#pileLoop');
  const head = document.querySelector('#pileLoop .pile-loop-head');
  const arc = document.querySelector('#pileLoop .pile-loop-arc');
  out.loop = {
    box: rect(loop),
    viewBox: document.querySelector('.pile-loop-svg')?.getAttribute('viewBox'),
    headTransform: head ? head.getAttribute('transform') : null,
    arcDashoffset: arc ? getComputedStyle(arc).strokeDashoffset : null,
    arcDasharray: arc ? getComputedStyle(arc).strokeDasharray : null,
    arcAnimation: arc ? getComputedStyle(arc).animationName : null,
    arcInlineOffset: arc ? arc.style.strokeDashoffset : null,
    headBBox: head ? rect(head) : null,
    reverse: loop ? loop.classList.contains('reverse') : null,
    preserveAspect: document.querySelector('.pile-loop-svg')?.getAttribute('preserveAspectRatio'),
  };

  out.seats = [...document.querySelectorAll('#seatLayer .seat-slot')].map(slot => {
    const fan = slot.querySelector('.opp-card-fan');
    const backs = fan ? [...fan.querySelectorAll('.card-back')] : [];
    return {
      seat: slot.dataset.seat,
      slot: rect(slot),
      tray: rect(slot.querySelector('.opp-card-tray')),
      count: slot.querySelector('.opp-hand-count') ? slot.querySelector('.opp-hand-count').textContent : null,
      countRect: rect(slot.querySelector('.opp-hand-count')),
      countHidden: slot.querySelector('.opp-hand-count') ? slot.querySelector('.opp-hand-count').hidden : null,
      backs: backs.length,
      backRects: backs.map(rect),
      backTransforms: backs.map(b => getComputedStyle(b).transform),
      scoreText: slot.querySelector('.seat-score-text') ? slot.querySelector('.seat-score-text').textContent : null,
      nameRect: rect(slot.querySelector('.cam-name-text')),
      scoreRect: rect(slot.querySelector('.seat-score-text')),
    };
  });

  const centre = document.querySelector('#feltTable');
  out.centre = { box: rect(centre), top: centre ? getComputedStyle(centre).top : null };

  out.centreSeats = {
    box: rect(document.querySelector('#centreSeats')),
    pills: [...document.querySelectorAll('.centre-seat')].map(p => ({
      seat: p.dataset.seat,
      text: p.querySelector('.cs-name').textContent + ' / ' + p.querySelector('.cs-count').textContent,
      box: rect(p),
    })),
  };
  out.myPod = rect(document.querySelector('#myPodSlot'));
  out.myHand = rect(document.querySelector('#myHandPanel'));
  out.myHandHeader = rect(document.querySelector('.my-hand-header'));
  out.myHandCards = [...document.querySelectorAll('#myHandCardsContainer .uno-card')].map(rect);
  out.banner = rect(document.querySelector('#turnBannerText') ? document.querySelector('#tableTurnBanner') : null);
  out.piles = rect(document.querySelector('.table-piles'));
  out.controlBar = rect(document.querySelector('#controlBar'));
  return out;
}
"""

JOIN_JS = r"""
({ n, c }) => {
  document.querySelector('#inputName').value = n;
  document.querySelector('#checkCamera').checked = false;
  document.querySelector('#inputCode').value = c;
  window.btnJoinClicked();
  return {
    name: document.querySelector('#inputName').value,
    code: document.querySelector('#inputCode').value,
  };
}
"""

DIAG_JS = r"""
() => ({
  players: document.querySelector('#playerCountChip').textContent.trim(),
  seats: document.querySelectorAll('#seatLayer .seat-slot').length,
  pills: document.querySelectorAll('.centre-seat').length,
  chat: [...document.querySelectorAll('.chat-line')].slice(-6).map(l => l.textContent.trim()),
  lobbyErr: document.querySelector('#lobbyErr') ? document.querySelector('#lobbyErr').textContent.trim() : '',
})
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--guests", type=int, default=3)
    ap.add_argument("--start", type=int, default=1, help="1 to start the round")
    ap.add_argument("--out", default="", help="write the JSON report here (utf-8)")
    ap.add_argument("--shot", default="", help="write a screenshot here (taken LAST)")
    args = ap.parse_args()

    global URL
    URL = f"http://127.0.0.1:{args.port}/"
    viewport = {"width": args.width, "height": args.height}

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # Host in its own context so its seat token never leaks to a guest.
        host_ctx = browser.new_context(viewport=viewport)
        host = host_ctx.new_page()
        host.goto(URL, wait_until="domcontentloaded")
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

        for i in range(args.guests):
            gctx = browser.new_context(viewport=viewport)
            gp = gctx.new_page()
            gp.goto(URL, wait_until="domcontentloaded")
            gp.evaluate(JOIN_JS, {"n": f"Guest{i + 1}", "c": code})
            gp.wait_for_timeout(900)
            print(f"  guest {i + 1}: {gp.evaluate(DIAG_JS)}")

        if args.guests:
            host.bring_to_front()
            host.wait_for_timeout(1000)

        if args.start:
            host.evaluate("() => document.querySelector('#btnStartGame').click()")
            host.wait_for_timeout(7000)

        print("  host diag:", host.evaluate(DIAG_JS))
        data = host.evaluate(MEASURE_JS)
        data["room"] = code
        report = json.dumps(data, indent=2)
        print(report)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(report)

        # Screenshot LAST: it can perturb the viewport, so nothing is measured
        # after it.
        if args.shot:
            host.screenshot(path=args.shot, full_page=False)

        browser.close()


if __name__ == "__main__":
    sys.exit(main())
