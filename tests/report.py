"""Prints a readable vertical-band report from a probe_table.py JSON report.

    python tests/report.py tests/phase_c.json

Rows are printed top-to-bottom in viewport y order, with the gap between each
consecutive pair, so crowding is measured rather than eyeballed.
"""
import json
import sys


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "tests/phase_c.json"
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)

    rows = [
        ("BANNER", d.get("banner")),
        ("PILES", d.get("piles")),
        ("LOOP", (d.get("loop") or {}).get("box")),
        ("CENTRE-SEATS", (d.get("centreSeats") or {}).get("box")),
        ("HAND-HEADER", d.get("myHandHeader")),
        ("MYHAND", d.get("myHand")),
        ("MYPOD", d.get("myPod")),
        ("CTRLBAR", d.get("controlBar")),
    ]
    print(f"{'band':14} {'top':>5} {'bottom':>7} {'h':>5} {'x':>5} {'right':>6} {'gap->next':>10}")
    prev = None
    for name, r in rows:
        if not r or not r.get("w"):
            print(f"{name:14} {'-':>5} {'-':>7} {'-':>5}")
            continue
        gap = "" if prev is None else f"{r['y'] - prev:>10}"
        print(f"{name:14} {r['y']:>5} {r['bottom']:>7} {r['h']:>5} {r['x']:>5} {r['right']:>6} {gap}")
        prev = r["bottom"]

    loop = d.get("loop") or {}
    print()
    print("loop box     :", loop.get("box"))
    print("loop ratio   : %.3f (target %.3f)" % (
        (loop["box"]["w"] / loop["box"]["h"]) if loop.get("box", {}).get("h") else 0, 340 / 190))
    print("head         :", loop.get("headTransform"))
    print("body offset  :", loop.get("arcInlineOffset") or loop.get("arcDashoffset"))
    print("hand cards   :", len(d.get("myHandCards") or []), [c["x"] for c in (d.get("myHandCards") or [])])
    print("centre pills :", len((d.get("centreSeats") or {}).get("pills") or []))

    print()
    for s in d.get("seats") or []:
        slot, tray = s["slot"], s["tray"]
        xs = [b["x"] for b in s["backRects"]]
        spread = (max(xs) - min(xs)) if xs else 0
        print(f"seat {s['seat']}: slot y {slot['y']}..{slot['bottom']} x {slot['x']}..{slot['right']} "
              f"| backs {s['backs']} spread {spread} slotW {slot['w']} "
              f"| label {s['count']!r} y {s['countRect']['y'] if s.get('countRect') else '-'}")
        if xs:
            print(f"          first back x {xs[0]} last {xs[-1] + 31} (tile {slot['x']}..{slot['right']})")
        nr, sr = s.get("nameRect"), s.get("scoreRect")
        if nr and sr:
            print(f"          name ends {nr['right']} / score starts {sr['x']} "
                  f"-> {'OVERLAP' if sr['x'] < nr['right'] else 'clear'}")


if __name__ == "__main__":
    main()
