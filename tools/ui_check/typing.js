// ui_drive typing scenario for Simple Account Balancer. Types into the new
// entry and edit forms with real per-character key events, checking each word
// preserves the value and caret position. It does not cover input-method
// composition or visual appearance because the driver reads text, not pixels.

export default async function typing(helpers) {
  const { click, type, keysFocused, press, evaluate, waitFor, check, screenshot, fixture } = helpers;

  const newPayeeWords = ["Nova ", "quill ", "rivet ", "orbit"];
  const newNotesWords = ["Mellow ", "cinder ", "tally ", "drift"];
  const editPayeeWords = [" ember", " axis", " vega"];
  const editNotesWords = [" ember", " axis", " vega"];
  const prependWord = "lumen";

  async function fieldState(selector) {
    return evaluate(`(() => {
      const field = document.querySelector(${JSON.stringify(selector)});
      return { value: field.value, start: field.selectionStart, end: field.selectionEnd };
    })()`);
  }

  async function checkAtEnd(selector, expected, name) {
    const state = await fieldState(selector);
    check(name, state.value === expected && state.start === expected.length && state.end === expected.length,
      JSON.stringify({ expected, state }));
  }

  async function typeWordsAtEnd(selector, words, expected, fieldName) {
    await click(selector);
    await press("End");
    let value = expected;
    for (let index = 0; index < words.length; index += 1) {
      await keysFocused(words[index]);
      value += words[index];
      await checkAtEnd(selector, value, `${fieldName} keeps word ${index + 1} and its caret at the end`);
    }
    return value;
  }

  async function typeWordAtStart(selector, expected, fieldName) {
    await click(selector);
    await press("Home");
    await keysFocused(`${prependWord} `);
    const value = `${prependWord} ${expected}`;
    const state = await fieldState(selector);
    check(`${fieldName} keeps the word typed at the start and its caret after that word`,
      state.value === value && state.start === prependWord.length + 1 && state.end === prependWord.length + 1,
      JSON.stringify({ expected: value, state }));
    return value;
  }

  async function registerRows() {
    return evaluate("api().get_transactions(STATE.account.id, currentRange.from, currentRange.to, currentSearch)");
  }

  await waitFor("typeof api === 'function' && api() && typeof api().get_transactions === 'function'", 10000);
  await waitFor("document.getElementById('registerWrap').style.display !== 'none'", 10000);

  const newPayee = await typeWordsAtEnd("#f-payee", newPayeeWords, "", "new Payee");
  await press("Escape");
  const newNotes = await typeWordsAtEnd("#f-notes", newNotesWords, "", "new Notes");
  await type("#f-amount", "7.89");
  await click("#entrySubmitBtn");
  const newRowShown = await waitFor(
    `Array.from(document.querySelectorAll('#registerBody tr[data-id]')).some(row => row.children[1].textContent === ${JSON.stringify(newPayee)})`,
    10000
  ).then(() => true).catch(() => false);
  check("new entry appears in the register after save", newRowShown);

  const savedNew = await registerRows();
  const newRow = savedNew && savedNew.rows && savedNew.rows.find(row => row.payee === newPayee && row.notes === newNotes);
  check("new entry Payee and Notes were saved through the real bridge",
    !!newRow && newRow.payee === newPayee && newRow.notes === newNotes,
    JSON.stringify(newRow || savedNew));

  await click(`#registerBody tr[data-id='${fixture.transaction_id}'] button[title='Edit']`);
  await waitFor("document.getElementById('entryForm').classList.contains('editing')", 5000);
  const editStart = await evaluate("({ payee: document.getElementById('f-payee').value, notes: document.getElementById('f-notes').value })");
  check("edit form shows the fixture Payee and Notes", editStart.payee === fixture.payee && editStart.notes === fixture.notes,
    JSON.stringify(editStart));

  let editedPayee = await typeWordsAtEnd("#f-payee", editPayeeWords, fixture.payee, "edited Payee");
  await press("Escape");
  let editedNotes = await typeWordsAtEnd("#f-notes", editNotesWords, fixture.notes, "edited Notes");
  editedPayee = await typeWordAtStart("#f-payee", editedPayee, "edited Payee");
  await press("Escape");
  editedNotes = await typeWordAtStart("#f-notes", editedNotes, "edited Notes");

  await click("#entrySubmitBtn");
  await waitFor("!document.getElementById('entryForm').classList.contains('editing')", 10000);
  const savedEdit = await registerRows();
  const editedRow = savedEdit && savedEdit.rows && savedEdit.rows.find(row => row.id === fixture.transaction_id);
  check("edited Payee and Notes were saved through the real bridge",
    !!editedRow && editedRow.payee === editedPayee && editedRow.notes === editedNotes,
    JSON.stringify(editedRow || savedEdit));

  const errorNoticeHidden = await evaluate("document.getElementById('errorNotice').style.display === 'none'");
  check("no error notice left on screen", errorNoticeHidden);
  await screenshot("typing-after-edit-save");
}
