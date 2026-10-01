// ui_drive restore scenario for Simple Account Balancer. The fixture plants
// two real backups plus look-alike and script-shaped files in the backup
// folder. Checks that only the real backups are listed, that a script-shaped
// filename rendered into the list stays text and never runs, that a real
// backup previews, and that open_url refuses any site but jde-projects.com.
//
// Does not restore anything and never opens the system browser.

export default async function restore(helpers) {
  const { click, evaluate, waitFor, check, screenshot, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  await waitFor("document.getElementById('registerWrap').style.display !== 'none'", 10000);

  // a) the restore list shows the real backups only: the fixture's two plus
  // the backup the app itself takes at launch.
  await click("#gearBtn");
  await click("button[onclick='openRestoreList()']");
  await waitFor("document.querySelectorAll('#restoreList .restore-row').length > 0", 10000);
  const listed = JSON.parse(await evaluate("JSON.stringify(RESTORE.backups.map(b => b.filename))"));
  const rows = await evaluate("document.querySelectorAll('#restoreList .restore-row').length");
  check("one row per listed backup", rows === listed.length, `${rows} rows, ${listed.length} listed`);
  check("both planted backups are listed", fixture.real.every(n => listed.includes(n)), JSON.stringify(listed));
  check("no look-alike file is listed", !listed.some(n => fixture.look_alikes.includes(n)), JSON.stringify(listed));
  check("every listed file has an exact backup name",
    listed.every(n => /^balancer_(prerestore_)?[0-9]{8}_[0-9]{6}(_[0-9]{6})?\.db$/.test(n)), JSON.stringify(listed));
  check("oldest planted backup sorts last", listed[listed.length - 1] === fixture.real[0], JSON.stringify(listed));
  const tagCount = await evaluate("document.querySelectorAll('#restoreList .restore-row-tag').length");
  check("the pre-restore backup is tagged", tagCount === 1, String(tagCount));
  await screenshot("restore-list");

  // b) a script-shaped filename, forced into the list as if the backend had
  // returned it, renders as text, and clicking it runs no script.
  const hostile = fixture.look_alikes.find(n => n.includes("__pwned"));
  await evaluate(`RESTORE.backups = [{filename: ${JSON.stringify(hostile)}, timestamp: "2026-01-03T09:00:00", is_prerestore: false, size_bytes: 1}]; renderRestoreList(); true`);
  const inlineHandlers = await evaluate("document.querySelectorAll('#restoreList [onclick]').length");
  check("restore rows carry no inline click code", inlineHandlers === 0, String(inlineHandlers));
  await click("#restoreList .restore-row");
  const refused = await waitFor("document.getElementById('restore-list-err').textContent.trim().length > 0", 5000)
    .then(() => true).catch(() => false);
  check("clicking a script-shaped name is refused with a message", refused,
    await evaluate("document.getElementById('restore-list-err').textContent"));
  const pwned = await evaluate("window.__pwned === undefined");
  check("the script in the filename never ran", pwned);

  // c) back to the real list, preview a real backup, then cancel.
  await evaluate("loadRestoreList(); true");
  await waitFor(`document.querySelectorAll('#restoreList .restore-row').length === ${listed.length}`, 10000);
  await click("#restoreList .restore-row:last-child");
  const previewShown = await waitFor("document.getElementById('amScreenRestoreConfirm').style.display === 'block'", 10000)
    .then(() => true).catch(() => false);
  check("clicking a real backup opens its preview", previewShown);
  const pending = await evaluate("RESTORE.pendingFilename");
  check("the preview is for the clicked backup", pending === fixture.real[0], String(pending));
  await screenshot("restore-preview");
  await click("button[onclick='cancelRestoreConfirm()']");
  const backToList = await waitFor("document.getElementById('amScreenRestoreList').style.display === 'block'", 5000)
    .then(() => true).catch(() => false);
  check("cancel returns to the list without restoring", backToList);

  // d) open_url refuses other sites over the real bridge. The allowed site
  // is not opened here, so no browser window appears.
  const refusedUrl = await evaluate("api().open_url('https://evil.example')");
  check("open_url refuses a site that isn't jde-projects.com", refusedUrl && refusedUrl.ok === false,
    JSON.stringify(refusedUrl));
  const byline = await evaluate("document.querySelector('.byline a').getAttribute('onclick')");
  check("the header link points at jde-projects.com", byline === "openUrl('https://jde-projects.com')", byline);
}
