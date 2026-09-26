/**
 * Checks the play-direction ring: that it is actually painted, that it is
 * concentric with the piles, that it clears its neighbours, and — the part
 * that cannot be eyeballed — that each arrowhead points the way play moves.
 *
 * The arrowhead test is geometric on purpose. A V of three points looks
 * plausible at any angle, and getting it wrong means the ring confidently
 * points the opposite way to the turn order. So for each arc the real tangent
 * at its end is taken from the path itself, and the head's bisector must point
 * back along it.
 */
export default async function run(page, ui) {
  await page.goto('http://127.0.0.1:8925/', { waitUntil: 'domcontentloaded' });
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.waitForTimeout(500);
  await page.evaluate(() => {
    document.querySelector('#inputName').value = 'Audit';
    document.querySelector('#checkCamera').checked = false;
    document.querySelector('#btnCreateGame').click();
  });
  await page.waitForTimeout(900);
  await page.evaluate(() => document.querySelector('#btnEnterTable').click());
  await page.waitForTimeout(1800);

  const report = await page.evaluate(() => {
    const arcs = [...document.querySelectorAll('.pile-loop-track use, use.pile-loop-track')];
    const trackUse = document.querySelector('use.pile-loop-track');
    const flowUse = document.querySelector('use.pile-loop-flow');
    const defs = document.getElementById('pileLoopArcs');
    const paths = defs ? [...defs.querySelectorAll('path')] : [];
    const heads = [...document.querySelectorAll('.pile-loop polyline')];

    /* Each arrowhead is a V: apex, then two barbs. The apex must sit at the
       end of its arc and the bisector of the barbs must point back along the
       direction of travel. */
    const analyseHead = (path, head) => {
      const total = path.getTotalLength();
      const end = path.getPointAtLength(total);
      const justBefore = path.getPointAtLength(total - 1);
      const travel = { x: end.x - justBefore.x, y: end.y - justBefore.y };
      const mag = Math.hypot(travel.x, travel.y) || 1;
      travel.x /= mag; travel.y /= mag;

      const pts = (head.getAttribute('points') || '')
        .trim().split(/\s+/)
        .map(pair => pair.split(',').map(Number));
      if (pts.length !== 3) return { error: 'expected 3 points, got ' + pts.length };
      const [b1, apex, b2] = pts;

      const bisector = { x: (b1[0] + b2[0]) / 2 - apex[0], y: (b1[1] + b2[1]) / 2 - apex[1] };
      const bMag = Math.hypot(bisector.x, bisector.y) || 1;
      bisector.x /= bMag; bisector.y /= bMag;

      /* dot of travel and bisector: -1 means the barbs trail exactly behind. */
      const dot = travel.x * bisector.x + travel.y * bisector.y;
      const apexOffset = Math.hypot(apex[0] - end.x, apex[1] - end.y);
      const barbLen = [
        Math.hypot(b1[0] - apex[0], b1[1] - apex[1]),
        Math.hypot(b2[0] - apex[0], b2[1] - apex[1]),
      ];
      return {
        apexAtArcEnd: Math.round(apexOffset * 10) / 10,
        /* must be near -1: the V opens backwards, so it reads as a head */
        alignment: Math.round(dot * 100) / 100,
        barbLengths: barbLen.map(n => Math.round(n * 10) / 10),
        travel: [Math.round(travel.x * 100) / 100, Math.round(travel.y * 100) / 100],
      };
    };

    const heads2 = paths.map((p, i) => heads[i] ? analyseHead(p, heads[i]) : null);

    const loop = document.querySelector('#pileLoop').getBoundingClientRect();
    const piles = document.querySelector('.table-piles').getBoundingClientRect();
    const banner = document.querySelector('.table-turn-banner').getBoundingClientRect();
    const startBtn = document.querySelector('#btnStartGame');
    const startRect = startBtn && getComputedStyle(startBtn).display !== 'none'
      ? startBtn.getBoundingClientRect() : null;

    const trackStyle = trackUse ? getComputedStyle(trackUse) : null;
    const flowStyle = flowUse ? getComputedStyle(flowUse) : null;

    return {
      /* The bug that started this: a dashed ring painted only a tenth of the
         loop, so there was no ring to see. The track must be solid. */
      trackDasharray: trackStyle ? trackStyle.strokeDasharray : null,
      trackStroke: trackStyle ? trackStyle.stroke : null,
      trackAnimation: trackStyle ? trackStyle.animationName : null,
      flowDasharray: flowStyle ? flowStyle.strokeDasharray : null,
      flowAnimation: flowStyle ? flowStyle.animationName : null,
      usesResolve: !!trackUse && !!flowUse && !!defs,
      pathCount: paths.length,
      headCount: heads.length,
      heads: heads2,
      geometry: {
        loop: [Math.round(loop.width), Math.round(loop.height)],
        piles: [Math.round(piles.width), Math.round(piles.height)],
        concentric: Math.abs((loop.left + loop.width / 2) - (piles.left + piles.width / 2)) < 1.5
                 && Math.abs((loop.top + loop.height / 2) - (piles.top + piles.height / 2)) < 1.5,
        clearsBanner: Math.round(loop.top - banner.bottom),
        clearsStart: startRect ? Math.round(startRect.top - loop.bottom) : 'no start button',
      },
    };
  });

  const problems = [];
  if (!report.usesResolve) problems.push('the <use> references did not resolve');
  if (report.pathCount !== 2) problems.push(`expected 2 arcs, got ${report.pathCount}`);
  if (report.headCount !== 2) problems.push(`expected 2 arrowheads, got ${report.headCount}`);

  /* The ring must be a solid, still loop. */
  if (report.trackDasharray && report.trackDasharray !== 'none') {
    problems.push(`the ring track is dashed (${report.trackDasharray}) — it must be solid`);
  }
  if (report.trackAnimation && report.trackAnimation !== 'none') {
    problems.push(`the ring track is animated (${report.trackAnimation}) — only the shimmer should move`);
  }
  if (!report.flowAnimation || report.flowAnimation === 'none') {
    problems.push('the travelling shimmer is not animating');
  }

  report.heads.forEach((head, index) => {
    if (!head) { problems.push(`arrowhead ${index} could not be analysed`); return; }
    if (head.error) { problems.push(`arrowhead ${index}: ${head.error}`); return; }
    if (head.apexAtArcEnd > 1) {
      problems.push(`arrowhead ${index} apex is ${head.apexAtArcEnd}px off its arc end`);
    }
    if (head.alignment > -0.9) {
      problems.push(
        `arrowhead ${index} does not point along travel (bisector·travel = ${head.alignment}, ` +
        `travel = ${head.travel}) — it reads backwards or sideways`,
      );
    }
    if (Math.abs(head.barbLengths[0] - head.barbLengths[1]) > 1.5) {
      problems.push(`arrowhead ${index} barbs are uneven: ${head.barbLengths}`);
    }
  });

  if (!report.geometry.concentric) problems.push('the ring is not concentric with the piles');
  if (report.geometry.clearsBanner < 4) {
    problems.push(`the ring is ${report.geometry.clearsBanner}px from the turn banner`);
  }
  if (typeof report.geometry.clearsStart === 'number' && report.geometry.clearsStart < 4) {
    problems.push(`the ring is ${report.geometry.clearsStart}px from the Start button`);
  }

  return { ...report, problems, ok: problems.length === 0 };
}
