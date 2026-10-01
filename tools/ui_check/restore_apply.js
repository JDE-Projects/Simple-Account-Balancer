// ui_drive restore-apply scenario for Simple Account Balancer. Uses the
// restore fixture: its oldest backup holds one transaction, the live register
// holds that transaction plus a later one. The scenario edits the shared
// transaction and adds a category, checks the preview names each change,
// then really restores and checks the register and categories match the
// backup with no error shown.

export default async function restoreApply(helpers) {
  const { click, evaluate, waitFor, check, screenshot, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  await waitFor("document.getElementById('registerWrap').style.display !== 'none'", 10000);

  const payees = "api().get_transactions(null, '2000-01-01').then(r => JSON.stringify(r.rows.map(t => t.payee).sort()))";
  const categoryNames = "api().get_categories().then(r => JSON.stringify(r.categories.map(c => c.name)))";

  // a) change live data after the backup: edit one transaction, add a category.
  const edited = await evaluate(`api().get_transactions(null, '2000-01-01').then(r => {
    const t = r.rows.find(row => row.payee === 'Grocery store');
    return api().update_transaction(t.id, '2026-01-16', 'Grocery store (edited)', '', '', '40.25', 'withdraw');
  }).then(r => JSON.stringify(r))`);
  check("the shared transaction was edited", JSON.parse(edited).ok === true, edited);
  const added = await evaluate("api().add_category('Restore Check').then(r => JSON.stringify(r))");
  check("a category was added", JSON.parse(added).ok === true, added);

  // b) preview the oldest backup and read what it says.
  await evaluate("refreshForActiveAccount(); true");
  await click("#gearBtn");
  await click("button[onclick='openRestoreList()']");
  await waitFor("document.querySelectorAll('#restoreList .restore-row').length > 0", 10000);
  await click("#restoreList .restore-row:last-child");
  await waitFor("document.getElementById('amScreenRestoreConfirm').style.display === 'block'", 10000);
  check("the preview is for the oldest backup",
    (await evaluate("RESTORE.pendingFilename")) === fixture.real[0]);
  const text = await evaluate("document.getElementById('restore-confirm-msg').textContent");
  check("the edit shows once, as changed", text.includes("1 transaction edited or reordered since this backup will change back."), text);
  check("the edit is not also counted as deleted", !text.includes("deleted since this backup"), text);
  check("the later transaction shows as removed", text.includes("1 transaction added since this backup will be removed."), text);
  check("the added category shows", text.includes("Categories: will change back to the backup's list."), text);
  check("autopays show no differences", text.includes("Autopays: no differences."), text);
  await screenshot("apply-preview");

  // c) restore for real.
  const prerestoreBefore = await evaluate(
    "api().list_backups().then(r => r.backups.filter(b => b.is_prerestore).length)");
  await click("button[onclick='confirmRestore()']");
  const closed = await waitFor("!document.getElementById('acctModalScrim').classList.contains('open') " +
    "|| document.getElementById('restore-confirm-err').textContent.trim().length > 0", 15000)
    .then(() => true).catch(() => false);
  const err = await evaluate("document.getElementById('restore-confirm-err').textContent.trim()");
  check("the restore finished without an error", closed && err === "", err);
  const notice = await evaluate("document.getElementById('backupNotice').style.display === 'none' ? '' : document.getElementById('backupNoticeText').textContent");
  check("the top banner shows no warning", notice === "", notice);

  // d) live data now matches the backup.
  const after = await evaluate(payees);
  check("the register holds only the backup's transaction", after === JSON.stringify(["Grocery store"]), after);
  const cats = JSON.parse(await evaluate(categoryNames));
  check("the added category is gone", !cats.includes("Restore Check"), JSON.stringify(cats));
  const prerestoreAfter = await evaluate(
    "api().list_backups().then(r => r.backups.filter(b => b.is_prerestore).length)");
  check("a safety backup was taken first", prerestoreAfter === prerestoreBefore + 1,
    `${prerestoreBefore} before, ${prerestoreAfter} after`);
  await screenshot("apply-after");
}
