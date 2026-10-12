// ui_drive date-ranges scenario for Simple Account Balancer. The fixture
// plants, for each "Last N Days" choice, a row on the first date the range
// includes (N - 1 days back) and one on the first date it leaves out (N days
// back), plus a row dated today. Picks each choice in the register's range
// dropdown with real clicks and keys and checks exactly the expected rows
// show, then checks the Compare view's default range the same way.

export default async function dateRanges(helpers) {
  const { click, press, evaluate, waitFor, check, screenshot, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  await waitFor("document.getElementById('registerWrap').style.display !== 'none'", 10000);

  const expectedIds = days => fixture.rows.filter(r => r.back < days).map(r => r.id).sort((a, b) => a - b);
  const shownIds = async body => JSON.parse(await evaluate(
    `JSON.stringify([...document.querySelectorAll('#${body} tr[data-id]')].map(tr => Number(tr.dataset.id)).sort((a, b) => a - b))`
  ));
  const payeesFor = ids => ids.map(id => fixture.rows.find(r => r.id === id).payee).join(", ");

  async function expectRows(body, days, label) {
    const want = expectedIds(days);
    const matched = await waitFor(
      `JSON.stringify([...document.querySelectorAll('#${body} tr[data-id]')].map(tr => Number(tr.dataset.id)).sort((a, b) => a - b)) === ${JSON.stringify(JSON.stringify(want))}`,
      8000
    ).then(() => true).catch(() => false);
    const got = await shownIds(body);
    check(`${label} shows exactly the ${days} dates counting today`, matched,
      `want [${payeesFor(want)}], got [${payeesFor(got)}]`);
  }

  // Moves the register's range dropdown the way a person would: click it,
  // arrow to the choice, press Enter.
  async function pickRange(slot, steps, wantKey) {
    await click(`#${slot} .range-select`);
    const key = steps < 0 ? "ArrowUp" : "ArrowDown";
    for (let i = 0; i < Math.abs(steps); i++) await press(key);
    await press("Enter");
    const value = await evaluate(`document.querySelector('#${slot} .range-select').value`);
    check(`range dropdown moved to Last ${wantKey} Days`, value === wantKey, value);
  }

  // The register opens on Last 30 Days.
  const startValue = await evaluate("document.querySelector('#rangeControlSlot .range-select').value");
  check("register opens on Last 30 Days", startValue === "30", startValue);
  await expectRows("registerBody", 30, "register default (Last 30 Days)");

  // Choices in order: 7, 14, 30, 90, Custom.
  await pickRange("rangeControlSlot", -2, "7");
  await expectRows("registerBody", 7, "Last 7 Days");
  await screenshot("register-last-7");
  await pickRange("rangeControlSlot", 1, "14");
  await expectRows("registerBody", 14, "Last 14 Days");
  await pickRange("rangeControlSlot", 2, "90");
  await expectRows("registerBody", 90, "Last 90 Days");

  // Compare opens on its own Last 90 Days default.
  await click("#tabCompareBtn");
  await waitFor("document.getElementById('compareWrap').style.display !== 'none'", 5000);
  const compareValue = await evaluate("document.querySelector('#compareRangeSlot .range-select').value");
  check("Compare opens on Last 90 Days", compareValue === "90", compareValue);
  await expectRows("compareBody", 90, "Compare default (Last 90 Days)");
  await screenshot("compare-last-90");
}
