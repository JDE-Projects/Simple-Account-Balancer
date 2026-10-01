// ui_drive notices scenario for Simple Account Balancer. The fixture opens
// the app at its minimum window width with one autopay posted at launch.
// Shows all three notices at once, with long text, and checks the top banner
// at that width in both themes: every notice visible, text wrapped rather
// than cut off, nothing wider than the page, the banner above the view
// without covering it, and no space taken once every notice is closed.

const LONG = "Today's backup couldn't be saved. Tried to write it to the backup folder. " +
  "A temporary restore file from an earlier session couldn't be deleted. " +
  "Couldn't delete 3 old backups in the backup folder; they will be retried at the next launch.";

export default async function notices(helpers) {
  const { evaluate, waitFor, check, click, screenshot, fixture } = helpers;

  await waitFor("typeof api === 'function' && api() && typeof api().get_config === 'function'", 10000);
  await waitFor("document.getElementById('registerWrap').style.display !== 'none'", 10000);
  await waitFor("document.getElementById('autopayNotice').style.display !== 'none'", 10000);

  const width = await evaluate("window.innerWidth");
  // The minimum is set on the window, so the page can measure a few pixels off.
  check("the window opened at the minimum width", Math.abs(width - fixture.min_width) <= 8, `${width}px`);

  await evaluate(`showErrorNotice(${JSON.stringify("Couldn't switch accounts. " + LONG)}); ` +
    `showBackupNotice(${JSON.stringify(LONG)}); true`);

  const layout = `(() => {
    const ids = ["errorNotice", "backupNotice", "autopayNotice"];
    const banner = document.querySelector(".notice-banner");
    const view = document.getElementById("registerWrap");
    const rects = ids.map(id => document.getElementById(id).getBoundingClientRect());
    return JSON.stringify({
      shown: ids.map(id => document.getElementById(id).style.display !== "none"),
      fits: ids.map(id => { const e = document.getElementById(id + "Text"); return e.scrollWidth <= e.clientWidth + 1; }),
      inside: rects.map(r => r.left >= 0 && r.right <= document.documentElement.clientWidth),
      wrapped: rects[1].height > 40,
      pageFits: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
      bannerBottom: banner.getBoundingClientRect().bottom,
      viewTop: view.getBoundingClientRect().top,
    });
  })()`;

  for (const theme of ["first", "second"]) {
    const l = JSON.parse(await evaluate(layout));
    check(`${theme} theme: all three notices show at once`, l.shown.every(Boolean), JSON.stringify(l.shown));
    check(`${theme} theme: no notice text is cut off`, l.fits.every(Boolean), JSON.stringify(l.fits));
    check(`${theme} theme: every notice stays inside the window`, l.inside.every(Boolean), JSON.stringify(l.inside));
    check(`${theme} theme: long text wraps onto more lines`, l.wrapped);
    check(`${theme} theme: nothing is wider than the page`, l.pageFits);
    check(`${theme} theme: the banner sits above the view with space between`,
      l.viewTop - l.bannerBottom >= 8, `${l.bannerBottom} vs ${l.viewTop}`);
    await screenshot(`notices-${theme}-theme`);
    if (theme === "first") await click("#theme-btn");
  }

  for (const id of ["errorNotice", "backupNotice", "autopayNotice"]) {
    await click(`#${id} .notice-dismiss`);
  }
  const space = await evaluate("(() => { const b = document.querySelector('.notice-banner'); " +
    "return `${b.getBoundingClientRect().height}px + ${getComputedStyle(b).marginBottom}`; })()");
  check("with every notice closed the banner takes no space", space === "0px + 0px", space);
  await click("#theme-btn");
}
