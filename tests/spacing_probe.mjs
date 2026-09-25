/**
 * Measures the vertical rhythm of the table and asserts the bands are evenly
 * spaced.
 *
 * The bands are, top to bottom: the opponent ring, the centre cluster (turn
 * banner, piles, start button, my hand) and my own tile. "Evenly spaced" is a
 * statement about the gaps *between* them, so that is what is reported and
 * asserted — absolute positions would only prove the composition is centred.
 *
 * Two failures motivated this probe, both measured at 1280x720 before the fix:
 *   - six seats: the side seats overlapped the turn banner by 71px
 *   - a lone opponent: a 219px void above the piles and 9px below my tile
 */
export default async function run(page, ui) {
  const base = page.url().split('?')[0];
  const browser = page.context().browser();

  const measure = () => page.evaluate(() => {
    const box = (selector) => {
      const node = document.querySelector(selector);
      if (!node) return null;
      const r = node.getBoundingClientRect();
      if (!r.width && !r.height) return null;
      return { top: Math.round(r.top), bottom: Math.round(r.bottom) };
    };

    const bar = box('.top-bar');
    const controls = box('#controlBar');
    const centre = box('.table-center');
    const myPod = box('#myPodSlot');

    const seats = [...document.querySelectorAll('#seatLayer .seat-slot')].map(node => {
      const r = node.getBoundingClientRect();
      return {
        seat: node.dataset.seat,
        top: Math.round(r.top),
        bottom: Math.round(r.bottom),
        left: Math.round(r.left),
        right: Math.round(r.right),
      };
    });

    const span = controls.top - bar.bottom;
    const share = (value) => Math.round((value / span) * 1000) / 10;

    /* The host settings panel. It is absolutely positioned with no height of
       its own, so a row added to it can silently overlap the one below — the
       kind of collision a screenshot shows as a smear and the DOM shows
       exactly, which is why it is measured rather than eyeballed. */
    const panel = document.querySelector('.settings-panel');
    const panelCheck = panel && !panel.classList.contains('hidden') && panel.getBoundingClientRect().height
      ? (() => {
          const p = panel.getBoundingClientRect();
          const kids = [...panel.children].map(k => {
            const r = k.getBoundingClientRect();
            return { cls: k.className || k.tagName, top: Math.round(r.top), bottom: Math.round(r.bottom) };
          });
          const overlaps = [];
          for (let i = 0; i < kids.length; i++) {
            for (let j = i + 1; j < kids.length; j++) {
              if (kids[i].top < kids[j].bottom && kids[j].top < kids[i].bottom) {
                overlaps.push(`${kids[i].cls}~${kids[j].cls}`);
              }
            }
          }
          return {
            height: Math.round(p.height),
            scrollHeight: panel.scrollHeight,
            overlaps,
            spills: kids.filter(k => k.bottom > p.bottom + 1).map(k => k.cls),
          };
        })()
      : null;

    /* The ring, when there are opponents, sits between the bar and the centre
       cluster — so the first gap is measured against its highest tile. */
    const ringTop = seats.length ? Math.min(...seats.map(s => s.top)) : null;
    const ringBottom = seats.length ? Math.max(...seats.map(s => s.bottom)) : null;

    return {
      viewport: { w: innerWidth, h: innerHeight },
      seats,
      bands: { bar, ringTop, ringBottom, centre, myPod, controls },
      gaps: {
        barToCentre: centre.top - bar.bottom,
        ringToCentre: ringBottom != null ? centre.top - ringBottom : null,
        centreToMyPod: myPod.top - centre.bottom,
        myPodToControls: controls.top - myPod.bottom,
      },
      /* Gaps as a share of the play area, so the evenness check is
         resolution-independent. With a ring the first gap is measured from
         the ring; without one, from the top bar. */
      shares: {
        above: share(ringTop != null ? ringTop - bar.bottom : centre.top - bar.bottom),
        middle: share(myPod.top - centre.bottom),
        below: share(controls.top - myPod.bottom),
      },
      overlapsControls: Math.max(
        ...seats.map(s => s.bottom), centre.bottom, myPod.bottom,
      ) > controls.top,
      panelCheck,
    };
  });

  const results = {};

  /* --- case 1: a lone host, which used to leave the largest void ---------- */
  await page.evaluate(() => {
    document.querySelector('#inputName').value = 'Host';
    document.querySelector('#checkCamera').checked = false;
    document.querySelector('#btnCreateGame').click();
  });
  await page.waitForTimeout(900);
  await page.evaluate(() => document.querySelector('#btnEnterTable').click());
  await page.waitForTimeout(900);
  results.solo = await measure();

  /* --- cases 2-3: a sparse table, then a full one ------------------------ */
  const code = await page.evaluate(() => document.querySelector('#codeValue').textContent);
  const guests = [];
  const addGuest = async (label) => {
    const context = await browser.newContext();
    const guestPage = await context.newPage();
    await guestPage.goto(base, { waitUntil: 'domcontentloaded' });
    await guestPage.evaluate(({ joinCode, name }) => {
      document.querySelector('#inputName').value = name;
      document.querySelector('#checkCamera').checked = false;
      document.querySelector('#btnShowJoin').click();
      document.querySelector('#inputCode').value = joinCode;
      document.querySelector('#btnJoinWithCode').click();
    }, { joinCode: code, name: label });
    await guestPage.waitForTimeout(400);
    guests.push(context);
  };

  /* Two opponents is the flat-arc branch: the seats sit on the ends of the
     ring rather than rising over it, so it exercises different geometry from
     the full table below. */
  await addGuest('Guest1');
  await addGuest('Guest2');
  await page.waitForTimeout(1600);
  results.three = await measure();

  await addGuest('Guest3');
  await addGuest('Guest4');
  await addGuest('Guest5');
  await page.waitForTimeout(2200);
  results.six = await measure();

  /* The same full table at a different resolution. The bands are placed from
     measured geometry, so this is the case that proves the spacing is derived
     rather than tuned to one window — and it catches a band left pinned to a
     percentage of the reference frame. */
  await page.setViewportSize({ width: 1680, height: 900 });
  await page.waitForTimeout(700);
  results.sixWide = await measure();

  /* Captured last: a screenshot can disturb a page, so nothing is measured
     after it. */
  await page.screenshot({ path: 'tests/spacing-six.png' });

  for (const context of guests) await context.close();

  /* -------------------------------------------------------------- checks -- */
  const problems = [];
  const near = (a, b, tolerance) => Math.abs(a - b) <= tolerance;

  for (const [label, report] of Object.entries(results)) {
    const { gaps, shares, bands, seats } = report;

    /* No band may cross another: the overlap is the bug that started this. */
    if (gaps.barToCentre < 0) problems.push(`${label}: the centre cluster is under the top bar`);
    if (gaps.centreToMyPod < 0) problems.push(`${label}: my tile overlaps the centre cluster`);
    if (gaps.myPodToControls < 0) problems.push(`${label}: my tile overlaps the control bar`);
    if (gaps.ringToCentre != null && gaps.ringToCentre < 0) {
      problems.push(`${label}: the seat ring overlaps the centre cluster by ${-gaps.ringToCentre}px`);
    }
    if (report.overlapsControls) problems.push(`${label}: content runs under the control bar`);

    /* Evenness: the gaps around the centre cluster are what a viewer reads as
       the composition. Allow 3% of the play area of slack, which covers the
       rounding of pixel positions. */
    if (!near(shares.above, shares.middle, 3)) {
      problems.push(`${label}: uneven gaps — above ${shares.above}% vs middle ${shares.middle}%`);
    }
    if (!near(shares.middle, shares.below, 3)) {
      problems.push(`${label}: uneven gaps — middle ${shares.middle}% vs below ${shares.below}%`);
    }

    /* The bands must fill the play area rather than huddle at the top. */
    if (bands.centre.top > report.viewport.h * 0.55) {
      problems.push(`${label}: the centre cluster sits in the bottom half (top ${bands.centre.top})`);
    }
    for (const tile of seats) {
      if (tile.top < bands.bar.bottom) problems.push(`${label}: seat ${tile.seat} is under the top bar`);
      if (tile.left < 0 || tile.right > report.viewport.w) {
        problems.push(`${label}: seat ${tile.seat} overflows horizontally`);
      }
    }

    if (report.panelCheck && report.panelCheck.overlaps.length) {
      problems.push(`${label}: settings panel rows overlap: ${report.panelCheck.overlaps.join(', ')}`);
    }
    if (report.panelCheck && report.panelCheck.spills.length) {
      problems.push(`${label}: settings panel content spills out: ${report.panelCheck.spills.join(', ')}`);
    }
  }

  if (results.six.seats.length !== 5) {
    problems.push(`expected 5 opponent tiles, got ${results.six.seats.length}`);
  }

  return { ...results, problems, ok: problems.length === 0 };
}
