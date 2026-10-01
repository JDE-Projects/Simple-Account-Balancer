// ui_drive categories scenario for Simple Account Balancer. The fixture plants
// a category used only by an autopay and one used by a transaction and an
// autopay. Checks that the category list counts autopays, that deleting the
// autopay-only category asks first and clears it from the autopay, and that a
// rename reaches the autopay list.

export default async function categories(helpers) {
  const { click, type, evaluate, waitFor, check, screenshot, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  await waitFor("document.getElementById('registerWrap').style.display !== 'none'", 10000);

  const rowText = id => evaluate(
    `(() => { const r = document.querySelector('.cat-row[data-id="${id}"] .cat-count'); return r ? r.textContent : null; })()`
  );
  const autopays = async () => JSON.parse(await evaluate(
    `api().get_autopays(${fixture.account_id}).then(r => JSON.stringify(r.autopays))`
  ));

  // a) counts include autopays.
  await evaluate("openCategoryModal()");
  await waitFor(`document.querySelector('.cat-row[data-id="${fixture.autopay_only_id}"]') !== null`, 10000);
  const onlyText = await rowText(fixture.autopay_only_id);
  check("an autopay-only category shows its autopay", onlyText === "0 used · 1 autopay", onlyText);
  const sharedText = await rowText(fixture.shared_id);
  check("a shared category shows both counts", sharedText === "1 used · 1 autopay", sharedText);
  await screenshot("category-counts");

  // b) deleting the autopay-only category asks first.
  await click(`.cat-row[data-id="${fixture.autopay_only_id}"] button[title="Delete"]`);
  const asked = await waitFor("document.getElementById('catScreenDelete').style.display !== 'none'", 5000)
    .then(() => true).catch(() => false);
  check("deleting an autopay-only category asks first", asked);
  const msg = await evaluate("document.getElementById('cat-del-msg').textContent");
  check("the question names the autopay", msg === "Delete Zumba Club? It's used on 1 autopay.", msg);
  const keep = await evaluate("document.getElementById('cat-keep-btn').textContent");
  check("the keep button says it clears the autopay", keep === "Clear it from 1 autopay", keep);
  await screenshot("category-delete-ask");

  // c) the shared category's button describes both effects.
  await click("button[onclick='cancelCategoryDelete()']");
  await click(`.cat-row[data-id="${fixture.shared_id}"] button[title="Delete"]`);
  await waitFor("document.getElementById('catScreenDelete').style.display !== 'none'", 5000);
  const sharedKeep = await evaluate("document.getElementById('cat-keep-btn').textContent");
  check("a shared category's keep button names both",
    sharedKeep === "Keep the label on 1 past transaction, clear it from 1 autopay", sharedKeep);
  await click("button[onclick='cancelCategoryDelete()']");

  // d) confirm the autopay-only delete.
  await click(`.cat-row[data-id="${fixture.autopay_only_id}"] button[title="Delete"]`);
  await waitFor("document.getElementById('catScreenDelete').style.display !== 'none'", 5000);
  await click("#cat-keep-btn");
  await waitFor(`document.querySelector('.cat-row[data-id="${fixture.autopay_only_id}"]') === null`, 5000);
  const gym = (await autopays()).find(a => a.id === fixture.gym_autopay_id);
  check("the deleted category is cleared from its autopay", gym && gym.category === "", JSON.stringify(gym));

  // e) rename reaches the autopay list.
  await click(`.cat-row[data-id="${fixture.shared_id}"] button[title="Rename"]`);
  await waitFor("document.getElementById('cat-edit-input') !== null", 5000);
  await evaluate("document.getElementById('cat-edit-input').value = ''");
  await type("#cat-edit-input", "New Bills");
  await click(`button[onclick='saveRenameCategory(${fixture.shared_id})']`);
  await waitFor(`(document.querySelector('.cat-row[data-id="${fixture.shared_id}"] .cat-name') || {}).textContent === 'New Bills'`, 5000);
  await evaluate("closeCategoryModal()");
  await click("button[onclick='openAutopayModal()']");
  await waitFor(`document.querySelector('.ap-row[data-id="${fixture.bills_autopay_id}"]') !== null`, 5000);
  const apCat = await evaluate(
    `(() => { const c = document.querySelector('.ap-row[data-id="${fixture.bills_autopay_id}"] .ap-row-cat'); return c ? c.textContent : null; })()`
  );
  check("the autopay list shows the renamed category", apCat === "New Bills", apCat);
  const gymCat = await evaluate(
    `document.querySelector('.ap-row[data-id="${fixture.gym_autopay_id}"] .ap-row-cat') === null`
  );
  check("the cleared autopay shows no category", gymCat === true, String(gymCat));
  await screenshot("autopays-after");
}
