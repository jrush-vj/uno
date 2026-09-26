/**
 * Proves that entering the table no longer waits on the camera.
 *
 * Entering used to await getUserMedia before it would even open the socket, so
 * the table sat still for as long as the device query took - in practice that
 * is the permission prompt, and it can be seconds. A warm permission grant
 * resolves instantly, so measuring the real device proves nothing; the camera
 * has to be stubbed with a controllable delay.
 *
 * The stub is installed as a real <script> tag, because that is the only way
 * this harness can reach the page's own world:
 *   - page.evaluate runs in an isolated world, so replacing navigator.mediaDevices
 *     there changes a different object and the app never sees it
 *   - addInitScript works but opens a fresh context, and Chrome's local-network
 *     check then refuses the loopback WebSocket
 * A script tag is evaluated by the page itself, needs no new context, and runs
 * before the app reads getUserMedia.
 *
 * The stub eventually returns a genuine live track (from a canvas), so the
 * late-attach path is exercised for real rather than being short-circuited by
 * a failure.
 *
 * The stub is also what the probe's own report is built from - it records into
 * the DOM as it goes - because the harness cannot read page-world JS.
 */
export default async function run(page, ui) {
  const CAMERA_DELAY_MS = 3000;

  await page.addScriptTag({
    content: `
      (function () {
        const root = document.documentElement;
        const canvas = document.createElement('canvas');
        canvas.width = 320;
        canvas.height = 240;
        const ctx = canvas.getContext('2d');
        let frame = 0;
        setInterval(() => {
          frame += 1;
          ctx.fillStyle = frame % 2 ? '#14243a' : '#1d3a2a';
          ctx.fillRect(0, 0, canvas.width, canvas.height);
        }, 100);

        const media = navigator.mediaDevices;
        if (!media) return;
        media.getUserMedia = async function () {
          root.dataset.cameraCalledAt = String(Date.now());
          await new Promise(function (resolve) { setTimeout(resolve, ${CAMERA_DELAY_MS}); });
          root.dataset.cameraResolvedAt = String(Date.now());
          return canvas.captureStream(10);
        };
      })();
    `,
  });

  const stubInstalled = await page.evaluate(() =>
    document.documentElement.dataset.stubReady === 'yes'
    || typeof navigator.mediaDevices?.getUserMedia === 'function');

  await page.evaluate(() => {
    document.querySelector('#inputName').value = 'Slowcam';
    document.querySelector('#checkCamera').checked = true;
    document.querySelector('#btnCreateGame').click();
  });
  await page.waitForTimeout(900);

  const startedAt = Date.now();
  await page.evaluate(() => document.querySelector('#btnEnterTable').click());

  let seatedMs = null;
  let cameraMs = null;
  for (let attempt = 0; attempt < 240; attempt++) {
    const state = await page.evaluate(() => {
      const video = document.querySelector('#myCamBox video');
      return {
        seated: document.querySelector('#game-view').classList.contains('active'),
        attached: !!video && !!video.srcObject,
        tracks: video && video.srcObject ? video.srcObject.getVideoTracks().length : 0,
      };
    });
    const elapsed = Date.now() - startedAt;
    if (state.seated && seatedMs == null) seatedMs = elapsed;
    if (state.attached && state.tracks > 0 && cameraMs == null) cameraMs = elapsed;
    if (state.attached && state.tracks > 0 && seatedMs != null) break;
    await page.waitForTimeout(25);
  }

  const report = await page.evaluate(() => {
    const root = document.documentElement;
    const video = document.querySelector('#myCamBox video');
    const camGroup = document.querySelector('#camGroup');
    return {
      seated: document.querySelector('#game-view').classList.contains('active'),
      cameraWasCalled: !!root.dataset.cameraCalledAt,
      cameraResolved: !!root.dataset.cameraResolvedAt,
      cameraAttached: !!video && !!video.srcObject,
      cameraTracks: video && video.srcObject ? video.srcObject.getVideoTracks().length : 0,
      cameraToggleActive: camGroup ? camGroup.classList.contains('active') : null,
    };
  });

  const problems = [];
  if (!report.cameraWasCalled) {
    problems.push('the camera stub never ran - the app did not ask for a camera at all');
  }
  if (seatedMs == null) {
    problems.push('the table never seated');
  } else if (seatedMs >= CAMERA_DELAY_MS) {
    problems.push(
      `entering the table took ${seatedMs}ms - it is still waiting on the camera ` +
      `(the stub holds it for ${CAMERA_DELAY_MS}ms)`,
    );
  }
  if (seatedMs != null && cameraMs != null && seatedMs >= cameraMs) {
    problems.push(`the seat appeared only once the camera arrived (seat ${seatedMs}ms, camera ${cameraMs}ms)`);
  }
  if (!report.cameraAttached) problems.push('the camera never attached to my own tile');
  if (report.cameraTracks === 0) problems.push('no live video track was attached');
  if (report.cameraToggleActive !== true) problems.push('the camera toggle did not light up');

  return {
    stubInstalled,
    cameraDelayMs: CAMERA_DELAY_MS,
    seatedMs,
    cameraAttachedMs: cameraMs,
    seatedBeforeCamera: seatedMs != null && cameraMs != null && seatedMs < cameraMs,
    ...report,
    problems,
    ok: problems.length === 0,
  };
}
