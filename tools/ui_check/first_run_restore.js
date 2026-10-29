// ui_drive first-run restore scenario. The fixture supplies one backup but no
// live database. Native file picking is replaced with bridge stand-ins so the
// first-run flow still previews and restores through the real backend.

export default async function firstRunRestore(helpers) {
  const { click, press, evaluate, waitFor, check, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  await waitFor("document.getElementById('firstrunWrap').style.display !== 'none'", 10000);

  const setup = await evaluate(`(() => {
    const bridge = window.pywebview.api;
    const previewRestore = bridge.preview_restore.bind(bridge);
    const restoreBackup = bridge.restore_backup.bind(bridge);
    window.__firstRunRestoreCalls = { discard: [], restore: [] };
    window.__firstRunRestoreChoose = async () => {
      const preview = await previewRestore(${JSON.stringify(fixture.filename)});
      return { ...preview, token: 'stub-token', source_name: ${JSON.stringify(fixture.filename)} };
    };
    bridge.choose_restore_file = () => window.__firstRunRestoreChoose();
    bridge.restore_from_file = (token, fingerprint) => {
      window.__firstRunRestoreCalls.restore.push([token, fingerprint]);
      return restoreBackup(${JSON.stringify(fixture.filename)}, fingerprint);
    };
    bridge.discard_restore_file = token => {
      window.__firstRunRestoreCalls.discard.push(token);
      return Promise.resolve({ ok: true });
    };
    return true;
  })()`);
  check("first-run restore bridge stand-ins are installed", setup === true, String(setup));

  // a) the first-run entry point is available.
  const firstRunReady = await evaluate(
    "document.getElementById('firstrunWrap').style.display !== 'none' && " +
    "document.getElementById('fr-restore-btn').offsetParent !== null"
  );
  check("first-run card and Restore from a backup button are visible", firstRunReady);

  // b) the preview uses literal text for account names from the backup.
  await click("#fr-restore-btn");
  const modalShown = await waitFor(
    "document.getElementById('firstRunRestoreModalScrim').classList.contains('open')", 10000
  ).then(() => true).catch(() => false);
  check("clicking Restore from a backup opens its confirmation modal", modalShown);
  const previewText = await evaluate("document.getElementById('frr-msg').textContent");
  check("first-run preview contains the backup filename", previewText.includes(fixture.filename), previewText);
  check("first-run preview contains Checking's transaction count",
    previewText.includes("Checking (2 transactions)"), previewText);
  const literalSavings = await evaluate(
    "!document.querySelector('#frr-msg b') && " +
    "document.getElementById('frr-msg').textContent.includes('<b>Savings</b> & \\\"Co\\\" (1 transaction)')"
  );
  check("first-run preview renders the Savings name as literal text", literalSavings, previewText);

  // c) Cancel discards the held selection and returns to first-run setup.
  await click("#frr-cancel");
  const cancelled = await waitFor(
    "!document.getElementById('firstRunRestoreModalScrim').classList.contains('open')", 5000
  ).then(() => true).catch(() => false);
  const discardAfterCancel = await evaluate("JSON.stringify(window.__firstRunRestoreCalls.discard)");
  const firstRunAfterCancel = await evaluate("document.getElementById('firstrunWrap').style.display !== 'none'");
  check("Cancel closes the first-run restore modal", cancelled);
  check("Cancel discards the held stub selection once", discardAfterCancel === '[\"stub-token\"]', discardAfterCancel);
  check("Cancel leaves the first-run card shown", firstRunAfterCancel);

  // d) Escape has the same cancellation behavior, only while this modal is open.
  await click("#fr-restore-btn");
  await waitFor("document.getElementById('firstRunRestoreModalScrim').classList.contains('open')", 10000);
  await press("Escape");
  const escaped = await waitFor(
    "!document.getElementById('firstRunRestoreModalScrim').classList.contains('open')", 5000
  ).then(() => true).catch(() => false);
  const discardAfterEscape = await evaluate("JSON.stringify(window.__firstRunRestoreCalls.discard)");
  check("Escape closes the first-run restore modal", escaped);
  check("Escape records a second discard", discardAfterEscape === '[\"stub-token\",\"stub-token\"]', discardAfterEscape);

  // f) an empty backup is refused before any confirmation appears.
  await evaluate("window.__firstRunRestoreChoose = async () => ({ ok: true, token: 'stub-token', source_name: 'empty.db', accounts: [] }); true");
  await click("#fr-restore-btn");
  const emptyError = await waitFor(
    "document.getElementById('fr-err').textContent === 'That backup has no accounts in it.'", 5000
  ).then(() => true).catch(() => false);
  const noEmptyModal = await evaluate(
    "!document.getElementById('firstRunRestoreModalScrim').classList.contains('open')"
  );
  const discardAfterEmpty = await evaluate("JSON.stringify(window.__firstRunRestoreCalls.discard)");
  check("an empty backup shows the first-run error", emptyError,
    await evaluate("document.getElementById('fr-err').textContent"));
  check("an empty backup opens no confirmation modal", noEmptyModal);
  check("an empty backup discards its held selection", discardAfterEmpty === '[\"stub-token\",\"stub-token\",\"stub-token\"]', discardAfterEmpty);

  // e) restoring applies the real backup through the stand-in bridge.
  await evaluate(`window.__firstRunRestoreChoose = async () => {
    const preview = await window.pywebview.api.preview_restore(${JSON.stringify(fixture.filename)});
    return { ...preview, token: 'stub-token', source_name: ${JSON.stringify(fixture.filename)} };
  }; true`);
  await click("#fr-restore-btn");
  await waitFor("document.getElementById('firstRunRestoreModalScrim').classList.contains('open')", 10000);
  const fingerprint = await evaluate("RESTORE.preview.fingerprint");
  await click("#frr-confirm");
  const restored = await waitFor(
    "document.getElementById('registerWrap').style.display !== 'none' && " +
    "!document.getElementById('firstRunRestoreModalScrim').classList.contains('open')", 15000
  ).then(() => true).catch(() => false);
  const restoreCalls = await evaluate("JSON.stringify(window.__firstRunRestoreCalls.restore)");
  const firstRunHidden = await evaluate("document.getElementById('firstrunWrap').style.display === 'none'");
  const config = await evaluate("api().get_config()");
  const categories = await evaluate("api().get_categories()");
  check("Restore calls restore_from_file with the stub token and real fingerprint",
    restoreCalls === JSON.stringify([["stub-token", fingerprint]]), restoreCalls);
  check("Restore closes its modal and shows the register", restored);
  check("Restore hides the first-run card", firstRunHidden);
  check("Restore creates both backup accounts", config.has_account === true &&
    config.accounts.map(account => account.name).sort().join("|") === ['Checking', '<b>Savings</b> & "Co"'].sort().join("|"),
    JSON.stringify(config.accounts));
  check("Restore brings back the backup category", categories.categories.some(category => category.name === "Restore Check"),
    JSON.stringify(categories.categories));
  const errorsClear = await evaluate(
    "document.getElementById('fr-err').textContent === '' && document.getElementById('frr-err').textContent === ''"
  );
  check("Restore clears both first-run errors", errorsClear);

  // g) backup text never becomes executable markup.
  const pwned = await evaluate("window.__pwned === undefined");
  check("backup content never runs script", pwned);
}
