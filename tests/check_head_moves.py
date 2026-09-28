"""Checks whether the loop head is actually moving, and how far."""
import json
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch()
    c = b.new_context(viewport={"width": 1600, "height": 900})
    pg = c.new_page()
    g = b.new_context(viewport={"width": 1600, "height": 900}).new_page()
    pg.goto("http://127.0.0.1:8899/", wait_until="domcontentloaded")
    pg.evaluate(
        """() => {
            document.querySelector('#inputName').value = 'H';
            document.querySelector('#checkCamera').checked = false;
            document.querySelector('#btnCreateGame').click();
        }"""
    )
    pg.wait_for_timeout(1000)
    pg.evaluate("() => document.querySelector('#btnEnterTable').click()")
    pg.wait_for_timeout(1200)
    code = pg.evaluate("() => document.querySelector('#codeValue').textContent")
    g.goto("http://127.0.0.1:8899/", wait_until="domcontentloaded")
    g.evaluate(
        """(c) => {
            document.querySelector('#inputName').value = 'G';
            document.querySelector('#checkCamera').checked = false;
            document.querySelector('#btnShowJoin').click();
            document.querySelector('#inputCode').value = c;
            document.querySelector('#btnJoinWithCode').click();
        }""",
        code,
    )
    pg.wait_for_timeout(1800)

    seen = []
    for i in range(14):
        tf = pg.evaluate("() => document.querySelector('#pileLoop .pile-loop-head').getAttribute('transform')")
        seen.append(tf)
        print(i, tf)
        pg.wait_for_timeout(450)

    print("\ndistinct transforms:", len(set(seen)))
    b.close()