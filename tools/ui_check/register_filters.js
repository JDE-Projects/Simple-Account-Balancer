// ui_drive register-filters scenario for Simple Account Balancer. The fixture
// plants mixed deposits and withdrawals in the default range plus an older
// row. Checks direction, search, custom dates, true running balances, and the
// register's four empty-list messages through real controls.

export default async function registerFilters(helpers) {
  const { click, type, press, evaluate, waitFor, check, screenshot, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  await waitFor("document.getElementById('registerWrap').style.display !== 'none'", 10000);

  const rowsFor = predicate => fixture.rows.filter(predicate);
  const defaultRows = rowsFor(r => r.date >= fixture.default_from);
  const narrowRows = rowsFor(r => r.date >= fixture.narrow_from && r.date <= fixture.narrow_to);
  const shownIds = async () => JSON.parse(await evaluate(
    "JSON.stringify([...document.querySelectorAll('#registerBody tr[data-id]')].map(tr => Number(tr.dataset.id)))"
  ));
  const payeesFor = ids => ids.map(id => fixture.rows.find(r => r.id === id).payee).join(", ");

  async function expectRows(want, label) {
    const ids = want.map(r => r.id);
    const matched = await waitFor(
      `JSON.stringify([...document.querySelectorAll('#registerBody tr[data-id]')].map(tr => Number(tr.dataset.id))) === ${JSON.stringify(JSON.stringify(ids))}`,
      8000
    ).then(() => true).catch(() => false);
    const got = await shownIds();
    check(label, matched, `want [${payeesFor(ids)}], got [${payeesFor(got)}]`);
  }

  async function setDirection(direction) {
    await click(`#dirFilterSeg .${direction}`);
    await waitFor(`document.querySelector('#dirFilterSeg .${direction}').classList.contains('active')`, 5000);
  }

  async function setSearch(text) {
    await type("#searchInput", text);
    await waitFor(`currentSearch === ${JSON.stringify(text.trim())}`, 5000);
  }

  async function chooseCustom(from, to) {
    const select = "#rangeControlSlot .range-select";
    await click(select);
    const current = await evaluate(`document.querySelector('${select}').value`);
    const steps = current === "30" ? 2 : current === "custom" ? 0 : 1;
    for (let i = 0; i < steps; i++) await press("ArrowDown");
    if (steps) await press("Enter");
    await waitFor("document.querySelector('#rangeControlSlot .range-custom').style.display !== 'none'", 5000);
    // Date fields take the PC's month/day/year order.
    const typed = iso => iso.slice(5, 7) + iso.slice(8, 10) + iso.slice(0, 4);
    await type("#rangeControlSlot .range-from", typed(from));
    await type("#rangeControlSlot .range-to", typed(to));
    await waitFor(`currentRange.from === '${from}' && currentRange.to === '${to}'`, 5000);
  }

  function formatCents(cents) {
    const absolute = Math.abs(cents);
    const dollars = String(Math.floor(absolute / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return `${cents < 0 ? "-" : ""}$${dollars}.${String(absolute % 100).padStart(2, "0")}`;
  }

  async function expectBalances(rows, label) {
    const actual = JSON.parse(await evaluate(
      "JSON.stringify([...document.querySelectorAll('#registerBody tr[data-id]')].map(tr => ({" +
      "id:Number(tr.dataset.id), balance:tr.querySelector('.balance-amt').textContent})))"
    ));
    const want = rows.map(r => ({ id: r.id, balance: formatCents(r.balance_cents) }));
    check(label, JSON.stringify(actual) === JSON.stringify(want),
      `want ${JSON.stringify(want)}, got ${JSON.stringify(actual)}`);
  }

  // The register opens on Last 30 Days, excluding the older fixture row.
  await expectRows(defaultRows, "All shows exactly the default-range rows");
  await expectBalances(defaultRows, "All shows each row's true running balance");

  const withdrawals = defaultRows.filter(r => r.sign === "withdraw");
  const deposits = defaultRows.filter(r => r.sign === "deposit");
  await setDirection("withdraw");
  await expectRows(withdrawals, "Withdraws shows exactly the withdrawal rows");
  await expectBalances(withdrawals, "Withdraws retains true running balances");
  await setDirection("deposit");
  await expectRows(deposits, "Deposits shows exactly the deposit rows");
  await expectBalances(deposits, "Deposits retains true running balances");

  await setDirection("withdraw");
  await setSearch("shared");
  const sharedWithdrawals = withdrawals.filter(r => r.payee.toLowerCase().includes("shared"));
  await expectRows(sharedWithdrawals, "Withdraws plus search shows exactly matching withdrawals");
  await expectBalances(sharedWithdrawals, "Withdraws plus search keeps true running balances");
  await setDirection("deposit");
  const sharedDeposits = deposits.filter(r => r.payee.toLowerCase().includes("shared"));
  await expectRows(sharedDeposits, "Deposits plus search shows exactly matching deposits");
  await expectBalances(sharedDeposits, "Deposits plus search keeps true running balances");

  await setSearch("");
  await chooseCustom(fixture.narrow_from, fixture.narrow_to);
  await setDirection("withdraw");
  await expectRows(narrowRows.filter(r => r.sign === "withdraw"), "Withdraws plus custom range shows exactly matching rows");
  await setDirection("deposit");
  await expectRows(narrowRows.filter(r => r.sign === "deposit"), "Deposits plus custom range shows exactly matching rows");
  await expectBalances(narrowRows.filter(r => r.sign === "deposit"), "Custom range retains true running balances");
  await screenshot("register-deposits-custom-range");

  // Each empty state is selected through the same controls a person uses.
  // The deposit-only day holds no withdrawals.
  await chooseCustom(fixture.deposit_only_day, fixture.deposit_only_day);
  await setDirection("withdraw");
  await waitFor("document.getElementById('emptyHint').style.display !== 'none'", 5000);
  let message = await evaluate("document.getElementById('emptyHint').textContent");
  check("direction-only empty message is exact", message === "No withdraws in this range.", message);

  await setDirection("deposit");
  await setSearch("none");
  await waitFor("document.getElementById('emptyHint').style.display !== 'none'", 5000);
  message = await evaluate("document.getElementById('emptyHint').textContent");
  check("direction-plus-search empty message is exact", message === "No deposits matching 'none' in this range.", message);

  await setDirection("all");
  await waitFor("document.getElementById('emptyHint').style.display !== 'none'", 5000);
  message = await evaluate("document.getElementById('emptyHint').textContent");
  check("search-only empty message is exact", message === "No matches for 'none' in this range.", message);

  await setSearch("");
  await chooseCustom(fixture.empty_from, fixture.empty_to);
  await waitFor("document.getElementById('emptyHint').style.display !== 'none'", 5000);
  message = await evaluate("document.getElementById('emptyHint').textContent");
  check("nothing-in-range empty message is exact", message === "No transactions in this range.", message);
}
