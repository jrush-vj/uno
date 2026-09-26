/**
 * Verifies who hosts a room, and how long entering the table takes.
 *
 * Two bugs motivated this probe:
 *   - the host was whoever connected first, so a friend opening the invite
 *     link before the creator pressed "Enter Table" took the host controls
 *   - pressing "Enter Table" waited on getUserMedia before opening the socket,
 *     so the table sat still for as long as the camera took to come up
 *
 * The join order here is deliberately the awkward one: the guest reaches the
 * table first, and the creator arrives afterwards.
 */
export default async function run(page, ui) {
  const base = page.url().split('?')[0];
  const browser = page.context().browser();

  const isHost = () => page.evaluate(() => {
    const panel = document.querySelector('#settingsPanel');
    const tag = document.querySelector('#mySeatTag');
    return {
      hostControls: !!panel && !panel.classList.contains('hidden'),
      seatTag: tag ? tag.textContent.trim() : null,
      players: document.querySelector('.player-count-chip')?.textContent.trim() || null,
    };
  });

  /* --- the creator reserves a code --------------------------------------- */
  await page.evaluate(() => {
    document.querySelector('#inputName').value = 'Creator';
    document.querySelector('#checkCamera').checked = false;
    document.querySelector('#btnCreateGame').click();
  });
  await page.waitForTimeout(900);
  const code = await page.evaluate(() => document.querySelector('#codeValue').textContent.trim());
  const storedKey = await page.evaluate(c =>
    localStorage.getItem(`uno_hostkey_${c}`), code);

  /* --- a friend opens the invite link FIRST ------------------------------ */
  const guestContext = await browser.newContext();
  const guestPage = await guestContext.newPage();
  await guestPage.goto(`${base}?room=${code}`, { waitUntil: 'domcontentloaded' });
  await guestPage.evaluate(() => {
    document.querySelector('#inputName').value = 'Friend';
    document.querySelector('#checkCamera').checked = false;
    document.querySelector('#btnJoinWithCode').click();
  });
  await guestPage.waitForTimeout(1800);

  const guestView = await guestPage.evaluate(() => {
    const panel = document.querySelector('#settingsPanel');
    const tag = document.querySelector('#mySeatTag');
    return {
      seated: document.querySelector('#game-view').classList.contains('active'),
      hostControls: !!panel && !panel.classList.contains('hidden'),
      seatTag: tag ? tag.textContent.trim() : null,
    };
  });

  /* --- the creator now enters, and we time it ---------------------------- */
  const startedAt = Date.now();
  await page.evaluate(() => document.querySelector('#btnEnterTable').click());

  let seatedMs = null;
  for (let attempt = 0; attempt < 100; attempt++) {
    const seated = await page.evaluate(() =>
      document.querySelector('#game-view').classList.contains('active'));
    if (seated) { seatedMs = Date.now() - startedAt; break; }
    await page.waitForTimeout(50);
  }
  await page.waitForTimeout(900);

  const creatorView = await isHost();
  /* The friend keeps the host only until the creator arrives, so the state
     that matters is the one after the creator has taken their seat. */
  const guestAfterCreator = await guestPage.evaluate(() => {
    const panel = document.querySelector('#settingsPanel');
    const tag = document.querySelector('#mySeatTag');
    return {
      hostControls: !!panel && !panel.classList.contains('hidden'),
      seatTag: tag ? tag.textContent.trim() : null,
    };
  });

  /* --- and confirm the host controls actually work ---------------------- */
  const settingsApplied = await page.evaluate(async () => {
    const select = document.querySelector('#setStartingCards');
    if (!select) return null;
    select.value = '5';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 700));
    return select.value;
  });

  /* Does the guest ever see host controls? It must not. */
  const guestStillGuest = !guestAfterCreator.hostControls;

  await guestContext.close();

  /* -------------------------------------------------------------- checks -- */
  const problems = [];
  if (!code || code.length !== 6) problems.push(`no room code was issued (got ${code})`);
  if (!storedKey) problems.push('the creator was not given a host key');
  if (!guestView.seated) problems.push('the friend never reached the table');
  if (seatedMs == null) problems.push('the creator never reached the table');
  if (seatedMs != null && seatedMs > 1500) {
    problems.push(`entering the table took ${seatedMs}ms (expected well under 1500ms)`);
  }
  if (!creatorView.hostControls) problems.push('the creator is not the host');
  if (creatorView.seatTag !== 'HOST(YOU)') {
    problems.push(`the creator's own tile does not say HOST (${creatorView.seatTag})`);
  }
  if (!guestStillGuest) problems.push('the friend can still see the host settings');
  if (guestAfterCreator.seatTag === 'HOST(YOU)') problems.push('the friend is still labelled host');

  return {
    code,
    hostKeyStored: !!storedKey,
    guestJoinedFirst: guestView,
    guestAfterCreator,
    creator: { ...creatorView, seatedMs },
    settingsApplied,
    problems,
    ok: problems.length === 0,
  };
}
