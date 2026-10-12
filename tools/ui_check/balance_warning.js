// ui_drive balance-warning scenario for Simple Account Balancer. The fixture
// plants an account starting at $500.00 with one transaction, so changing the
// starting balance in account settings asks for confirmation first. Types a
// series of entries into the starting balance and checks the warning appears
// only for an entry the save would accept as a real change, showing the
// amount the save would store; malformed or too-large entries skip it and
// show the save's own error, and nothing changes until "Change it".

export default async function balanceWarning(helpers) {
  const { click, type, evaluate, waitFor, check, screenshot, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  await waitFor("document.getElementById('registerWrap').style.display !== 'none'", 10000);

  const storedCents = async () => (await evaluate("api().get_config()")).account.starting_balance_cents;
  const shown = id => `document.getElementById('${id}').style.display !== 'none'`;

  async function openSettings() {
    if (!(await evaluate("document.getElementById('acctModalScrim').classList.contains('open')"))) {
      await click("#gearBtn");
      await waitFor("document.getElementById('acctModalScrim').classList.contains('open')", 5000);
    }
  }

  // Types an entry and presses Save; returns which of the three outcomes
  // happened: the warning, the save's own error, or a silent save. A
  // warning is cancelled back to the edit screen unless keepWarning is set.
  async function trySave(entry, keepWarning = false) {
    await openSettings();
    await type("#am-balance", entry);
    await click("#amScreenEdit .m-foot .btn.primary");
    await waitFor(
      `${shown("amScreenConfirm")} || document.getElementById('am-err').textContent.trim() !== '' ` +
      "|| !document.getElementById('acctModalScrim').classList.contains('open')", 10000);
    if (await evaluate(shown("amScreenConfirm"))) {
      const text = await evaluate("document.getElementById('am-confirm-msg').textContent");
      if (!keepWarning) {
        await click("#amScreenConfirm .m-foot .btn:not(.primary)");
        await waitFor(shown("amScreenEdit"), 5000);
      }
      return { outcome: "warning", text };
    }
    const err = await evaluate("document.getElementById('am-err').textContent.trim()");
    return err ? { outcome: "error", text: err } : { outcome: "saved", text: "" };
  }

  // a) malformed or too-large entries skip the warning and show the save's error.
  for (const entry of ["1e3", "$1,2,3", "--5", "$", "5$", "1 000", "1000000000000"]) {
    const r = await trySave(entry);
    check(`"${entry}" skips the warning and shows the save's error`, r.outcome === "error", JSON.stringify(r));
  }
  await screenshot("malformed-error");
  check("the malformed entries left the balance unchanged", (await storedCents()) === fixture.starting_cents,
    String(await storedCents()));

  // b) the same balance written another way saves without a warning.
  for (const entry of ["$500", "500.004", "+500.00"]) {
    const r = await trySave(entry);
    check(`"${entry}" equals the current balance and saves without a warning`, r.outcome === "saved", JSON.stringify(r));
  }

  // c) a real change warns, showing the amount the save would store, and
  // Cancel keeps the old balance.
  const cases = [
    ["10.999", "$11.00"],
    ["0.005", "$0.01"],
    ["-1,234.56", "-$1,234.56"],
    ["$999,999,999,999.99", "$999,999,999,999.99"],
  ];
  for (const [entry, money] of cases) {
    const r = await trySave(entry);
    check(`"${entry}" shows the warning for ${money}`,
      r.outcome === "warning" && r.text.includes(`to ${money} will update`), JSON.stringify(r));
  }
  await click("#amScreenEdit .m-foot .btn:not(.primary)");
  check("cancelled warnings left the balance unchanged", (await storedCents()) === fixture.starting_cents,
    String(await storedCents()));

  // d) "Change it" stores the warned amount.
  const r = await trySave("-1,234.56", true);
  check("the warning shows again for -1,234.56", r.outcome === "warning", JSON.stringify(r));
  await screenshot("warning");
  await click("#amScreenConfirm .m-foot .btn.primary");
  await waitFor("!document.getElementById('acctModalScrim').classList.contains('open') " +
    "|| document.getElementById('am-err').textContent.trim() !== ''", 10000);
  check("Change it stored the warned amount", (await storedCents()) === -123456, String(await storedCents()));
}
