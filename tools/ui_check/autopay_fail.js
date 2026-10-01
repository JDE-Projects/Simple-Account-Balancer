// ui_drive autopay-fail scenario for Simple Account Balancer. The fixture
// plants a due autopay and a trigger that refuses every new transaction.
// Checks that the failed launch posting shows as a red top-banner notice with
// nothing posted, then that saving a due autopay from the form keeps the
// rule and shows the "saved, but" notice in red.

export default async function autopayFail(helpers) {
  const { click, type, evaluate, waitFor, check, screenshot, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  await waitFor("document.getElementById('registerWrap').style.display !== 'none'", 10000);

  const txCount = async () => JSON.parse(await evaluate(
    `api().get_transactions(${fixture.account_id}, "2000-01-01").then(r => JSON.stringify(r.rows.length))`
  ));

  // a) launch posting failed.
  const shown = await waitFor("document.getElementById('autopayNotice').style.display !== 'none'", 10000)
    .then(() => true).catch(() => false);
  const text = await evaluate("document.getElementById('autopayNoticeText').textContent");
  check("a failed launch posting shows in the top banner",
    shown && /^Autopays couldn.t be added to the register today\./.test(text), text);
  check("the launch notice is shown as an error",
    await evaluate("document.getElementById('autopayNotice').classList.contains('is-error')") === true);
  const color = await evaluate("getComputedStyle(document.getElementById('autopayNotice')).color");
  const red = await evaluate(
    "(() => { const s = document.createElement('span'); s.style.color = 'var(--red)'; document.body.appendChild(s);" +
    " const c = getComputedStyle(s).color; s.remove(); return c; })()"
  );
  check("the launch notice is red", color === red, `${color} vs ${red}`);
  check("nothing was posted", await txCount() === 0);
  await screenshot("launch-post-failed");

  // b) save a new autopay that is due today; its posting fails too.
  await click("#autopayNotice .notice-dismiss");
  await click("button[onclick='openAutopayModal()']");
  await waitFor("document.getElementById('apScreenList').style.display !== 'none'", 5000);
  await click("button[onclick='openAddAutopayForm()']");
  await waitFor("document.getElementById('apScreenForm').style.display !== 'none'", 5000);
  await type("#ap-payee", "Phone");
  await type("#ap-amount", "50.00");
  await evaluate(
    `(() => { document.getElementById('ap-post-date').value = ${JSON.stringify(fixture.today)};` +
    ` document.getElementById('ap-pay-date').value = ${JSON.stringify(fixture.today)}; return true; })()`
  );
  await click("#ap-save-btn");
  const saveShown = await waitFor(
    "document.getElementById('autopayNotice').style.display !== 'none'" +
    " && /saved, but/.test(document.getElementById('autopayNoticeText').textContent)", 10000
  ).then(() => true).catch(() => false);
  const saveText = await evaluate("document.getElementById('autopayNoticeText').textContent");
  check("saving a due autopay reports the failed posting",
    saveShown && /^The autopay was saved, but payments already due couldn.t be added\./.test(saveText), saveText);
  check("the save notice is shown as an error",
    await evaluate("document.getElementById('autopayNotice').classList.contains('is-error')") === true);
  check("the form closed back to the list",
    await evaluate("document.getElementById('apScreenList').style.display !== 'none'") === true);
  const payees = JSON.parse(await evaluate(
    `api().get_autopays(${fixture.account_id}).then(r => JSON.stringify(r.autopays.map(a => a.payee)))`
  ));
  check("the new autopay was saved", payees.includes("Phone") && payees.includes("Rent"), JSON.stringify(payees));
  check("still nothing was posted", await txCount() === 0);
  await screenshot("save-post-failed");
}
