// CSV 取込の確認: node tools/import_check.cjs http://localhost:8080 out.png labels.csv
const { chromium } = require("playwright");
(async () => {
const [url, shot, csvPath] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = []; const dialogs = [];
page.on("pageerror", (e) => errors.push(e.message));
page.on("dialog", async (d) => { dialogs.push(d.type() + ": " + d.message().slice(0, 300)); if (d.type() === "confirm") await d.dismiss(); else await d.accept(); });
await page.goto(url);
await page.waitForFunction(() => document.querySelectorAll("#bits-out .chip, #bits-watch .chip").length > 0, null, { timeout: 10000 });
await page.waitForTimeout(1500);
const info = () => page.evaluate(() => ({
  led: document.getElementById("led").className, period: document.getElementById("s-period").textContent, fps: document.getElementById("s-fps").textContent,
  chips: document.querySelectorAll("#bits-watch .chip").length, rows: document.querySelectorAll("#words-watch tr").length,
  entries: document.querySelectorAll("#watch-list .we").length, title: document.title,
  first: [...document.querySelectorAll("#bits-watch .chip .nm")].slice(0, 4).map(e => e.textContent),
  wrow: [...document.querySelectorAll("#words-watch tr td.nm")].slice(0, 3).map(e => e.textContent),
}));
console.log("initial:", JSON.stringify(await info()));
if (shot) await page.screenshot({ path: shot, fullPage: false });
if (csvPath) {
  await page.setInputFiles("#f-import", csvPath);
  await page.waitForTimeout(3000);
  console.log("after import:", JSON.stringify(await info()));
  console.log("dialogs:", dialogs);
}
console.log("errors:", errors.length ? errors : "none");
await browser.close();
process.exit(errors.length ? 1 : 0);
})();
