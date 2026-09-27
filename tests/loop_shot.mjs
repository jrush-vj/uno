/**
 * Starts a round and captures the centre of the table, so the play-direction
 * ring around the two piles can be looked at rather than reasoned about.
 *
 * The ring is drawn from a stretched viewBox, so its geometry only shows up
 * once it is painted at a real size: the screenshot is the check.
 */
export default async function run(page, ui) {
  const base = page.url().split('?')[0];
  const browser = page.context().browser();

  await page.setViewportSize({ width: 1600, height: 900 });
  void ui;

  await page.evaluate(() => {
    document.querySelector('#inputName').value = 'Host';
    document.querySelector('#checkCamera').checked = false;
    document.querySelector('#btnCreateGame').click();
  });
  await page.waitForTimeout(900);
  await page.evaluate(() => document.querySelector('#btnEnterTable').click());
  await page.waitForTimeout(900);

  const code = await page.evaluate(() => document.querySelector('#codeValue').textContent);

  const guestContext = await browser.newContext();
  const guestPage = await guestContext.newPage();
  await guestPage.goto(base, { waitUntil: 'domcontentloaded' });
  await guestPage.evaluate((joinCode) => {
    document.querySelector('#inputName').value = 'Guest';
    document.querySelector('#checkCamera').checked = false;
    document.querySelector('#btnShowJoin').click();
    document.querySelector('#inputCode').value = joinCode;
    document.querySelector('#btnJoinWithCode').click();
  }, code);
  await page.waitForTimeout(1600);

  await page.evaluate(() => document.querySelector('#btnStartGame').click());
  await page.waitForTimeout(4000);

  /* Both play directions, measured and photographed. The ring is mirrored
     rather than spun for anticlockwise play, so the mirror is the thing worth
     looking at: it must flip the head and the dash together. The reverse
     branch is forced directly, so this does not have to wait for a Reverse
     card to come up. */
  const probe = () => page.evaluate(() => {
    const loop = document.querySelector('#pileLoop');
    const l = loop.getBoundingClientRect();
    const centre = document.querySelector('.table-center').getBoundingClientRect();
    const piles = document.querySelector('.table-piles').getBoundingClientRect();
    const banner = document.querySelector('.table-turn-banner').getBoundingClientRect();
    const hand = document.querySelector('.my-hand-panel').getBoundingClientRect();
    const svg = loop.querySelector('svg');
    return {
      reverse: loop.classList.contains('reverse'),
      transform: getComputedStyle(loop).transform,
      loopBox: { top: Math.round(l.top), bottom: Math.round(l.bottom), left: Math.round(l.left), right: Math.round(l.right) },
      clearance: {
        aboveRingToBanner: Math.round(banner.bottom - l.top),
        ringBelowToHand: Math.round(hand.top - l.bottom),
      },
      pileInsets: {
        left: Math.round(piles.left - l.left),
        right: Math.round(l.right - piles.right),
        top: Math.round(piles.top - l.top),
        bottom: Math.round(l.bottom - piles.bottom),
      },
      ringInsideCentre: l.left >= centre.left - 1 && l.right <= centre.right + 1,
      shapes: [...svg.querySelectorAll('path, polygon')].map(n => ({
        cls: n.getAttribute('class'),
        strokeWidth: getComputedStyle(n).strokeWidth,
        fill: getComputedStyle(n).fill,
      })),
    };
  });

  const forward = await probe();
  await page.screenshot({ path: 'tests/loop-table.png', clip: { x: 0, y: 0, width: 1600, height: 900 } });
  await page.screenshot({ path: 'tests/loop-forward.png', clip: { x: 520, y: 250, width: 560, height: 330 } });

  await page.evaluate(() => document.querySelector('#pileLoop').classList.add('reverse'));
  await page.waitForTimeout(300);
  const reverse = await probe();
  await page.screenshot({ path: 'tests/loop-reverse.png', clip: { x: 520, y: 250, width: 560, height: 330 } });

  await guestContext.close();

  const problems = [];
  if (forward.reverse) problems.push('the ring starts mirrored');
  if (!reverse.reverse) problems.push('the reverse class did not apply');
  if (reverse.transform !== 'matrix(-1, 0, 0, 1, 0, 0)') {
    problems.push(`the mirrored ring is not a plain X mirror: ${reverse.transform}`);
  }
  if (reverse.loopBox.top !== forward.loopBox.top) {
    problems.push('mirroring moved the ring vertically');
  }
  if (forward.clearance.aboveRingToBanner < 0) problems.push('the ring overlaps the turn banner');
  if (forward.clearance.ringBelowToHand < 0) problems.push('the ring overlaps my hand');
  if (!forward.ringInsideCentre) problems.push('the ring is wider than the centre cluster');
  for (const s of forward.shapes) {
    if (s.cls !== 'pile-loop-head' && s.strokeWidth !== '4.5px') {
      problems.push(`${s.cls} stroke is ${s.strokeWidth}, expected 4.5px`);
    }
  }

  return { forward, reverse, problems, ok: problems.length === 0 };
}