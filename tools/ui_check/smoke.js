// ui_drive scenario for Simple Account Balancer (see
// build-tools/ui_drive/README.md for the helpers and how this is launched).
// Starts from a fresh app (no database, see the repo's ui_drive run notes)
// and drives the real window through the first-run setup, the register, an
// invalid-amount rejection, and a theme round trip confirmed through the
// real Python bridge.
//
// Does not cover: autopays, categories, compare-to-bank, export, backups,
// or the update check. Those stay for a person to check by hand.

function fmtMoney(cents) {
  const neg = cents < 0;
  const abs = Math.abs(cents);
  const s = (abs / 100).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return (neg ? "-$" : "$") + s;
}

export default async function smoke(helpers) {
  const { click, type, press, evaluate, waitFor, check, screenshot } = helpers;

  // a) the page-to-Python link answers, with the real version.
  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  const cfg1 = await evaluate("api().get_config()");
  check("get_config answers over the real bridge", !!cfg1 && cfg1.ok === true, JSON.stringify(cfg1));
  const verLabel = await evaluate("document.getElementById('verLabel').textContent");
  check("version label matches the version get_config returned",
    verLabel === "v" + cfg1.version && /^v\d+\.\d+\.\d+$/.test(verLabel), `${verLabel} vs v${cfg1.version}`);
  check("starts with no account (fresh app)", cfg1.has_account === false, JSON.stringify(cfg1.has_account));

  // b) first-run setup, filled in by typing.
  await waitFor("document.getElementById('firstrunWrap').style.display !== 'none'", 10000);
  await type("#fr-name", "Checking");
  await type("#fr-balance", "500.00");
  // fr-date is a native <input type="date">: type() fills it with real
  // per-character key events, starting from its first segment, the same
  // way a person would type a date that isn't today's default.
  await type("#fr-date", "01152026");
  const dateValue = await evaluate("document.getElementById('fr-date').value");
  check("starting date was typed in", dateValue === "2026-01-15", dateValue);
  await click(".firstrun-wrap .btn.primary");
  const registerShown = await waitFor(
    "document.getElementById('registerWrap').style.display !== 'none'", 10000
  ).then(() => true).catch(() => false);
  check("register appears after setup", registerShown);
  const startingBalanceText = await evaluate("document.getElementById('balanceBig').textContent");
  check("register shows the starting balance", startingBalanceText === fmtMoney(50000), startingBalanceText);
  const cfgAfterCreate = await evaluate("api().get_config()");
  check("account's starting date matches what was typed",
    cfgAfterCreate.account && cfgAfterCreate.account.starting_date === "2026-01-15",
    JSON.stringify(cfgAfterCreate.account && cfgAfterCreate.account.starting_date));

  // c) core workflow: one debit, one credit, through the UI.
  const rowCountBefore = await evaluate("document.querySelectorAll('#registerBody tr').length");

  await type("#f-payee", "Grocery store");
  await type("#f-amount", "40.25");
  await click("#entrySubmitBtn");
  await waitFor(`document.querySelectorAll('#registerBody tr').length === ${rowCountBefore + 1}`, 10000);

  await click("#dirSeg .deposit");
  await type("#f-payee", "Paycheck");
  await type("#f-amount", "125.50");
  await click("#entrySubmitBtn");
  await waitFor(`document.querySelectorAll('#registerBody tr').length === ${rowCountBefore + 2}`, 10000);

  const expectedBalanceCents = 50000 - 4025 + 12550;
  const balanceAfterTx = await evaluate("document.getElementById('balanceBig').textContent");
  check("running balance reflects both transactions", balanceAfterTx === fmtMoney(expectedBalanceCents),
    `${balanceAfterTx} vs ${fmtMoney(expectedBalanceCents)}`);

  // d) error path: an invalid amount is refused, nothing saved.
  await click("#dirSeg .withdraw");
  const rowCountBeforeBad = await evaluate("document.querySelectorAll('#registerBody tr').length");
  await type("#f-payee", "Bad amount");
  await type("#f-amount", "not a number");
  await click("#entrySubmitBtn");
  const errShown = await waitFor("document.getElementById('entry-err').textContent.trim().length > 0", 5000)
    .then(() => true).catch(() => false);
  const errText = await evaluate("document.getElementById('entry-err').textContent");
  check("an invalid amount is refused with a plain-language message", errShown && !/errno|exception|traceback/i.test(errText),
    errText);
  const rowCountAfterBad = await evaluate("document.querySelectorAll('#registerBody tr').length");
  check("nothing was saved for the invalid amount", rowCountAfterBad === rowCountBeforeBad,
    `${rowCountBeforeBad} -> ${rowCountAfterBad}`);
  await type("#f-amount", "");

  // e) theme round trip, screenshot of each, confirmed saved through Python.
  const startLight = await evaluate("document.body.classList.contains('light')");
  await click("#theme-btn");
  await waitFor(`document.body.classList.contains('light') === ${!startLight}`, 5000);
  const afterFirstToggle = await evaluate("document.body.classList.contains('light')");
  check("theme toggled to the other theme", afterFirstToggle === !startLight, String(afterFirstToggle));
  await screenshot(afterFirstToggle ? "theme-light" : "theme-dark");
  const cfgAfterFirstToggle = await evaluate("api().get_config()");
  check("theme change after the first toggle was saved through Python",
    cfgAfterFirstToggle.theme === (afterFirstToggle ? "light" : "dark"), JSON.stringify(cfgAfterFirstToggle.theme));

  await click("#theme-btn");
  await waitFor(`document.body.classList.contains('light') === ${startLight}`, 5000);
  const afterSecondToggle = await evaluate("document.body.classList.contains('light')");
  check("theme toggled back to the starting theme", afterSecondToggle === startLight, String(afterSecondToggle));
  await screenshot(afterSecondToggle ? "theme-light" : "theme-dark");
  const cfgAfterSecondToggle = await evaluate("api().get_config()");
  check("theme change after the second toggle was saved through Python",
    cfgAfterSecondToggle.theme === (afterSecondToggle ? "light" : "dark"), JSON.stringify(cfgAfterSecondToggle.theme));

  // f) no error notice left on screen at the end.
  const errorNoticeHidden = await evaluate("document.getElementById('errorNotice').style.display === 'none'");
  check("no error notice left on screen", errorNoticeHidden);
}
