"""Compares head motion with a lobby-only table vs. a started match."""
import json
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8899/"


def observe(page, label, n=10, gap=400):
    seen = []
    for _ in range(n):
        seen.append(page.evaluate(
            "() => document.querySelector('#pileLoop .pile-loop-head').getAttribute('transform')"))
        page.wait_for_timeout(gap)
    print(f"{label}: {len(set(seen))} distinct of {n}")
    for s in seen[:4]:
        print("   ", s)
    return len(set(seen))


with sync_playwright() as p:
    b = p.chromium.launch()

    # Host alone, no guest at all.
    c1 = b.new_context(viewport={"width": 1600, "height": 900})
    pg = c1.new_page()
    pg.goto(URL, wait_until="domcontentloaded")
    pg.evaluate(
        """() => {
            document.querySelector('#inputName').value = 'H';
            document.querySelector('#checkCamera').checked = false;
            document.querySelector('#btnCreateGame').click();
        }"""
    )
    pg.wait_for_timeout(1000)
    pg.evaluate("() => document.querySelector('#btnEnterTable').click()")
    pg.wait_for_timeout(2000)
    solo = observe(pg, "host alone, pre-game")

    # Now with a guest seated.
    c2 = b.new_context(viewport={"width": 1600, "height": 900})
    gp = c2.new_page()
    code = pg.evaluate("() => document.querySelector('#codeValue').textContent")
    gp.goto(URL, wait_until="domcontentloaded")
    gp.evaluate(
        """(c) => {
            document.querySelector('#inputName').value = 'G';
            document.querySelector('#checkCamera').checked = false;
            document.querySelector('#btnShowJoin').click();
            document.querySelector('#inputCode').value = c;
            document.querySelector('#btnJoinWithCode').click();
        }""",
        code,
    )
    pg.wait_for_timeout(2000)
    guest = observe(pg, "host with guest seated")

    pg.evaluate("() => document.querySelector('#btnStartGame').click()")
    pg.wait_for_timeout(6000)
    started = observe(pg, "host after start")

    pg.screenshot(path="tests/state-check.png", full_page=False)
    b.close()