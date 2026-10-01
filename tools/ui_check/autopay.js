// ui_drive autopay scenario for Simple Account Balancer. The fixture plants
// one autopay whose register date is today. Checks that the app posts it at
// launch, dated the pay date, and says so in the teal bottom-bar notice.

export default async function autopay(helpers) {
  const { evaluate, waitFor, check, screenshot, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  await waitFor("document.getElementById('registerWrap').style.display !== 'none'", 10000);

  const shown = await waitFor("document.getElementById('autopayNotice').style.display !== 'none'", 10000)
    .then(() => true).catch(() => false);
  const text = await evaluate("document.getElementById('autopayNoticeText').textContent");
  check("the launch notice says the autopay was added", shown && text === "Added 1 autopay to the register.", text);
  const isError = await evaluate("document.getElementById('autopayNotice').classList.contains('is-error')");
  check("the notice is not shown as an error", isError === false, String(isError));

  const rows = JSON.parse(await evaluate(
    `api().get_transactions(${fixture.account_id}, "2000-01-01").then(r => JSON.stringify(r.rows))`
  ));
  const rent = rows.filter(r => r.payee === "Rent");
  check("exactly one Rent transaction was posted", rent.length === 1, JSON.stringify(rent));
  check("it is dated the pay date", rent.length === 1 && rent[0].date === fixture.pay_date,
    `${rent[0] && rent[0].date} vs ${fixture.pay_date}`);
  await screenshot("autopay-posted");
}
