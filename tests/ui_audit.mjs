/**
 * Audits the game view for overlapping and inconsistent UI.
 *
 * The seat ring, centre cluster and my own tile are placed by layoutTable()
 * from measured heights, so those are covered by spacing_probe. What that does
 * NOT cover is the set of panels that are pinned to the viewport independently
 * — the host settings panel, the mid-game leaderboard, the chat box and the
 * control bar. They know nothing about each other or about the centre cluster,
 * so at some window sizes they land on top of one another.
 *
 * Overlaps are reported as the area of intersection, because a 1px graze and a
 * panel sitting wholly inside another are very different problems.
 */
export default async function run(page, ui) {
  const base = page.url().split('?')[0];

  const audit = () => page.evaluate(() => {
    const rect = (selector) => {
      const node = document.querySelector(selector);
      if (!node) return null;
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') return null;
      /* The round summary is always in the DOM and always over the centre of
         the table; it is simply transparent until a round begins. Counting it
         as a collision would report the same "overlap" at every size. */
      if (Number(style.opacity) === 0) return null;
      const r = node.getBoundingClientRect();
      if (!r.width || !r.height) return null;
      return { name: selector, ...['top', 'bottom', 'left', 'right', 'width', 'height']
        .reduce((acc, k) => ({ ...acc, [k]: Math.round(r[k]) }), {}) };
    };

    const boxes = [
      '.top-bar', '.table-center', '.settings-panel', '.match-leaderboard',
      '.chat-panel', '.control-bar', '#myPodSlot', '#seatLayer .seat-slot',
      '#roundToast',
    ].map(rect).filter(Boolean);

    const overlaps = [];
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i];
        const b = boxes[j];
        const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (w > 1 && h > 1) {
          overlaps.push({
            a: a.name, b: b.name, area: w * h,
            /* how much of the smaller box is covered, as a percentage */
            coverage: Math.round((w * h) / Math.min(a.width * a.height, b.width * b.height) * 100),
          });
        }
      }
    }

    /* Anything pushed off the edges is a layout failure rather than a
       collision, and reads to a player as content that has vanished. */
    const offscreen = boxes.filter(box =>
      box.left < -1 || box.top < -1
      || box.right > innerWidth + 1 || box.bottom > innerHeight + 1)
      .map(box => ({
        name: box.name,
        beyond: {
          left: Math.min(0, box.left), top: Math.min(0, box.top),
          right: Math.max(0, box.right - innerWidth),
          bottom: Math.max(0, box.bottom - innerHeight),
        },
      }));

    /* Type and spacing consistency. The design scales everything from --u, so
       a stray px font-size or a hardcoded colour is an inconsistency a player
       can see as one label not matching its neighbours. */
    const fontSizes = {};
    document.querySelectorAll('.settings-panel, .match-leaderboard, .chat-panel, .table-turn-banner')
      .forEach(root => {
        root.querySelectorAll('*').forEach(node => {
          const size = getComputedStyle(node).fontSize;
          (fontSizes[size] = fontSizes[size] || []).push(
            `${root.className.split(' ')[0]} > ${node.className || node.tagName}`);
        });
      });

    return {
      viewport: [innerWidth, innerHeight],
      boxes,
      overlaps: overlaps.sort((x, y) => y.area - x.area),
      offscreen,
      distinctFontSizes: Object.keys(fontSizes).length,
      fontSizes,
    };
  });

  await page.evaluate(() => {
    document.querySelector('#inputName').value = 'Audit';
    document.querySelector('#checkCamera').checked = false;
    document.querySelector('#btnCreateGame').click();
  });
  await page.waitForTimeout(900);
  await page.evaluate(() => document.querySelector('#btnEnterTable').click());
  await page.waitForTimeout(1600);

  const results = {};
  for (const [w, h] of [[1920, 1080], [1440, 900], [1280, 720], [1150, 700], [1024, 640]]) {
    await page.setViewportSize({ width: w, height: h });
    await page.waitForTimeout(450);
    results[`${w}x${h}`] = await audit();
  }

  /* Summarise which collisions are real: a panel pair that overlaps at ANY
     size is a bug, and the worst coverage is what a player would notice. */
  const worst = new Map();
  for (const [size, report] of Object.entries(results)) {
    for (const overlap of report.overlaps) {
      const key = `${overlap.a} <-> ${overlap.b}`;
      const current = worst.get(key);
      if (!current || overlap.coverage > current.coverage) {
        worst.set(key, { ...overlap, size });
      }
    }
  }

  /* ------------------------------------------------------- type checks -----
     The view scales from --u, so a size that does not is a visible mismatch
     rather than a theoretical one. Two rules a player can actually see:
       - nothing inside the turn banner may be larger than the banner's text
       - the side panels should track the same scale, not freeze at px */
  const typeProblems = [];
  const banner = results['1280x720'].fontSizes;
  const sizeOf = (label) => {
    const entry = Object.entries(banner).find(([, nodes]) =>
      nodes.some(node => node.includes(label)));
    return entry ? parseFloat(entry[0]) : null;
  };

  const bannerText = sizeOf('turn-banner-text');
  for (const child of ['turn-timer-chip', 'direction-arrow-orb']) {
    const size = sizeOf(child);
    if (size != null && bannerText != null && size > bannerText + 0.5) {
      typeProblems.push(`${child} (${size}px) is larger than the banner text (${bannerText}px)`);
    }
    if (size != null && size < 10) {
      typeProblems.push(`${child} is ${size}px, below the 10px readability floor`);
    }
  }

  /* The chat box and the lines it produces must match, or typing looks like it
     is changing size. */
  const chatInput = sizeOf('chat-input');
  const chatLine = sizeOf('chat-line is-system');
  if (chatInput != null && chatLine != null && Math.abs(chatInput - chatLine) > 0.5) {
    typeProblems.push(`the chat box is ${chatInput}px but its messages are ${chatLine}px`);
  }

  /* Panels that ignore the scale stay put while the table grows. Compare the
     settings rows at the largest and smallest window under test. */
  const large = results['1920x1080'].fontSizes;
  const small = results['1024x640'].fontSizes;
  const settingsAt = (sizes) => {
    const entry = Object.entries(sizes).find(([, nodes]) =>
      nodes.some(node => node.startsWith('settings-panel > settings-row')));
    return entry ? parseFloat(entry[0]) : null;
  };
  const settingsLarge = settingsAt(large);
  const settingsSmall = settingsAt(small);
  if (settingsLarge != null && settingsSmall != null && settingsLarge === settingsSmall) {
    typeProblems.push(
      `the settings rows are ${settingsLarge}px at both 1920x1080 and 1024x640 ` +
      '— they are not following the scale',
    );
  }

  return {
    bySize: Object.fromEntries(Object.entries(results).map(([size, r]) => [size, {
      overlaps: r.overlaps, offscreen: r.offscreen, distinctFontSizes: r.distinctFontSizes,
    }])),
    worstCollisions: [...worst.entries()].map(([key, value]) => ({ pair: key, ...value })),
    typeProblems,
    ok: worst.size === 0 && typeProblems.length === 0
      && Object.values(results).every(r => r.offscreen.length === 0),
    fontSizes: results['1280x720'].fontSizes,
    settingsRows: { at1920: settingsLarge, at1024: settingsSmall },
  };
}
