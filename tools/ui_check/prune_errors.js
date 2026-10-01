// ui_drive prune-errors scenario for Simple Account Balancer. The fixture
// holds old backups open so the app's own pruning fails. Checks that a failed
// launch prune shows in the bottom-bar notice, and that a failed prune during
// a restore shows there too, after the restore itself succeeds.
//
// Does not cover the closing backup's message box: a native Windows dialog
// the driver can't see or dismiss. The fixture releases the held regular
// backup before the app closes, so that prune succeeds and no box appears.

export default async function pruneErrors(helpers) {
  const { click, evaluate, waitFor, check, screenshot, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  await waitFor("document.getElementById('registerWrap').style.display !== 'none'", 10000);

  // a) the launch prune failed on the held regular backup.
  const launchShown = await waitFor("document.getElementById('backupNotice').style.display !== 'none'", 10000)
    .then(() => true).catch(() => false);
  const launchText = await evaluate("document.getElementById('backupNoticeText').textContent");
  check("a failed launch prune shows in the bottom bar", launchShown && /Couldn.t delete 1 old backup./.test(launchText),
    launchText);
  await screenshot("launch-prune-notice");
  await click("#backupNotice .notice-dismiss");
  await waitFor("document.getElementById('backupNotice').style.display === 'none'", 5000);

  // b) restore the planted pre-restore backup; its safety-copy prune fails.
  await click("#gearBtn");
  await click("button[onclick='openRestoreList()']");
  await waitFor("RESTORE.backups.length > 0", 10000);
  const index = await evaluate(`RESTORE.backups.findIndex(b => b.filename === ${JSON.stringify(fixture.target)})`);
  check("the restore target is listed", index >= 0, String(index));
  await click(`#restoreList .restore-row:nth-child(${index + 1})`);
  await waitFor("document.getElementById('amScreenRestoreConfirm').style.display === 'block'", 10000);
  await click("button[onclick='confirmRestore()']");
  const restoreShown = await waitFor("document.getElementById('backupNotice').style.display !== 'none'", 10000)
    .then(() => true).catch(() => false);
  const restoreText = await evaluate("document.getElementById('backupNoticeText').textContent");
  check("a failed prune during restore shows in the bottom bar", restoreShown && /Couldn.t delete 1 old backup./.test(restoreText),
    restoreText);
  const errText = await evaluate("document.getElementById('restore-confirm-err').textContent");
  check("the restore itself succeeded", errText === "" && await evaluate("document.getElementById('amScreenRestoreConfirm').offsetParent === null"),
    errText);
  const names = await evaluate("api().list_backups().then(r => JSON.stringify(r.backups.map(b => b.filename)))");
  check("new backup names carry microseconds",
    JSON.parse(names).some(n => /^balancer_(prerestore_)?2\d{7}_\d{6}_\d{6}\.db$/.test(n)), names);
  await screenshot("restore-prune-notice");

  // c) wait until the fixture lets go of the regular backup, so the closing
  // prune succeeds and no message box blocks the app from closing.
  await waitFor(`Date.now() > ${fixture.release_at_ms + 2000}`, 60000);
}
