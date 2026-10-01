// ui_drive debug-log scenario for Simple Account Balancer. The fixture plants
// old debug logs and holds some open so pruning fails. Checks that a failed
// launch prune shows in the startup notice, that turning the toggle on while
// the window is up pushes a new prune failure to the error notice, and that
// the toggle follows the real logging state.
//
// Which files survive pruning, and that the log text is redacted, are read
// from the run folder's app copy after the run, not from the page.

export default async function debugLog(helpers) {
  const { click, evaluate, waitFor, check, screenshot, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);

  // a) the launch prune couldn't delete the held log; the window didn't exist
  // yet, so the warning joined the startup notice.
  const launchShown = await waitFor("document.getElementById('backupNotice').style.display !== 'none'", 10000)
    .then(() => true).catch(() => false);
  const launchText = await evaluate("document.getElementById('backupNoticeText').textContent");
  check("a failed launch prune of an old debug log shows at startup",
    launchShown && launchText.includes(`could not delete old log ${fixture.held_at_launch}`), launchText);
  await screenshot("launch-debug-log-notice");

  // b) once the late log is held, turn logging on: its prune fails while the
  // window is up, so the warning is pushed to the page.
  await waitFor(`Date.now() > ${fixture.late_ready_ms + 1000}`, 30000);
  await click(".dbg-toggle");
  const pushed = await waitFor("document.getElementById('errorNotice').style.display !== 'none'", 10000)
    .then(() => true).catch(() => false);
  const errText = await evaluate("document.getElementById('errorNoticeText').textContent");
  check("a prune failure while the window is up shows in the error notice",
    pushed && errText.includes(`could not delete old log ${fixture.held_late}`), errText);
  check("the toggle stays on, since logging itself works",
    await evaluate("document.getElementById('dbgToggle').checked") === true);
  await screenshot("pushed-debug-log-warning");

  // c) write a line, then turn logging off and back on through the toggle.
  await evaluate("api().set_backup_keep(5)");
  await click(".dbg-toggle");
  check("the toggle turns off", await evaluate("document.getElementById('dbgToggle').checked") === false);
  await click(".dbg-toggle");
  await waitFor("document.getElementById('dbgToggle').checked === true", 5000);
  check("the toggle turns back on", true);
}
