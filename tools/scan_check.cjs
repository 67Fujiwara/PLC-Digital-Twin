// 全デバイス読込 / 差分読込の確認: node tools/scan_check.cjs http://localhost:8080 out.png
const { chromium } = require("playwright");
(async () => {
const [url, shot] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = []; page.on("pageerror", (e) => errors.push(e.message)); page.on("dialog", (d) => d.accept());
await page.goto(url);
await page.waitForFunction(() => document.querySelectorAll("#bits-out .chip").length > 0);
await page.waitForTimeout(1500);
await page.click("#b-scan-full");
await page.waitForFunction(() => document.querySelectorAll("#scan-list table tr").length > 1, null, { timeout: 30000 });
const full = await page.evaluate(() => ({ msg: document.getElementById("scan-msg").textContent, rows: document.querySelectorAll("#scan-list table tr").length - 1,
  first: [...document.querySelectorAll("#scan-list table tr")].slice(1, 6).map(tr => tr.children[1].textContent + "=" + tr.children[3].textContent) }));
console.log("full:", JSON.stringify(full));
if (shot) await page.screenshot({ path: shot });
// 1 行目にチェック → 監視に追加
await page.locator("#scan-list table tr").nth(1).locator("input").check();
const nBefore = await page.locator("#watch-list .we").count();
await page.click("#scan-add"); await page.waitForTimeout(800);
console.log("watch entries:", nBefore, "->", await page.locator("#watch-list .we").count());
// ESTOP を強制 ON (値が変わる) → 差分読込
await page.locator("#bits-in .chip", { hasText: "ESTOP" }).locator(".sw").click();
await page.waitForTimeout(500);
await page.click("#b-scan-diff");
await page.waitForFunction(() => document.getElementById("scan-msg").textContent.startsWith("読込中"), null, { timeout: 10000 });
await page.waitForFunction(() => document.getElementById("scan-msg").textContent.startsWith("完了"), null, { timeout: 30000 });
await page.waitForTimeout(300);
const diff = await page.evaluate(() => ({ msg: document.getElementById("scan-msg").textContent, rows: document.querySelectorAll("#scan-list table tr").length - 1,
  list: [...document.querySelectorAll("#scan-list table tr")].slice(1, 8).map(tr => tr.children[1].textContent + ":" + tr.children[3].textContent + "<-" + tr.children[5].textContent) }));
console.log("diff:", JSON.stringify(diff));
await page.locator("#bits-in .chip", { hasText: "ESTOP" }).locator(".rel").click();
console.log("errors:", errors.length ? errors : "none");
await browser.close();
process.exit(errors.length ? 1 : 0);
})();
