/**
 * Measures the generated opponent tiles at the maximum table size.
 *
 * The previous layout placed tiles by percentage of the viewport, which tucked
 * the topmost tile under the top bar. This script seats six players, then
 * asserts every tile sits fully inside the play area with no overlap.
 */
export default async function run(page, ui) {
  const base = page.url().split('?')[0];
  const browser = page.context().browser();

  await page.evaluate(() => {
    document.querySelector('#inputName').value = 'Host';
    document.querySelector('#checkCamera').checked = false;
    document.querySelector('#btnCreateGame').click();
  });
  await page.waitForTimeout(800);
  const code = await page.evaluate(() => document.querySelector('#codeValue').textContent);
  await page.evaluate(() => document.querySelector('#btnEnterTable').click());
  await page.waitForTimeout(900);

  /* Five more players, each in its own context so they are distinct sessions. */
  const guests = [];
  for (let index = 1; index <= 5; index++) {
    const context = await browser.newContext();
    const guestPage = await context.newPage();
    await guestPage.goto(base, { waitUntil: 'domcontentloaded' });
    await guestPage.evaluate(({ joinCode, label }) => {
      document.querySelector('#inputName').value = `Guest${label}`;
      document.querySelector('#checkCamera').checked = false;
      document.querySelector('#btnShowJoin').click();
      document.querySelector('#inputCode').value = joinCode;
      document.querySelector('#btnJoinWithCode').click();
    }, { joinCode: code, label: index });
    await guestPage.waitForTimeout(400);
    guests.push(context);
  }

  await page.waitForTimeout(1800);

  const report = await page.evaluate(() => {
    const bar = document.querySelector('.top-bar').getBoundingClientRect();
    const layer = document.querySelector('#seatLayer').getBoundingClientRect();
    const tiles = [...document.querySelectorAll('#seatLayer .seat-slot')].map(node => {
      const r = node.getBoundingClientRect();
      return {
        seat: node.dataset.seat,
        top: Math.round(r.top),
        bottom: Math.round(r.bottom),
        left: Math.round(r.left),
        right: Math.round(r.right),
      };
    });
    const overlaps = [];
    for (let i = 0; i < tiles.length; i++) {
      for (let j = i + 1; j < tiles.length; j++) {
        const a = tiles[i];
        const b = tiles[j];
        if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) {
          overlaps.push(`${a.seat}~${b.seat}`);
        }
      }
    }
    return {
      viewport: { w: window.innerWidth, h: window.innerHeight },
      barBottom: Math.round(bar.bottom),
      layer: { top: Math.round(layer.top), height: Math.round(layer.height) },
      tileCount: tiles.length,
      tiles,
      overlaps,
    };
  });

  for (const context of guests) {
    await context.close();
  }

  const problems = [];
  if (report.tileCount !== 5) problems.push(`expected 5 opponent tiles, got ${report.tileCount}`);
  for (const tile of report.tiles) {
    if (tile.top < report.barBottom) {
      problems.push(`seat ${tile.seat} is under the top bar (top ${tile.top} < ${report.barBottom})`);
    }
    if (tile.left < 0 || tile.top < 0) problems.push(`seat ${tile.seat} is off-screen`);
    if (tile.right > report.viewport.w || tile.bottom > report.viewport.h) {
      problems.push(`seat ${tile.seat} overflows the viewport`);
    }
  }
  if (report.overlaps.length) problems.push(`tiles overlap: ${report.overlaps.join(', ')}`);

  return { ...report, problems, ok: problems.length === 0 };
}
