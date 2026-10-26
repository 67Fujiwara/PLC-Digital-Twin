// ブラウザ UI のスモークテスト (Playwright/Chromium)。サーバを起動した状態で:
//   node tools/e2e_check.cjs http://localhost:8080 out.png
const { chromium } = require("playwright");
(async () => {
const url = process.argv[2] || "http://localhost:8080";
const shot = process.argv[3] || "ui.png";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });
await page.goto(url);
await page.waitForFunction(() => document.querySelectorAll("#bits-out .chip").length > 0, null, { timeout: 10000 });
await page.waitForTimeout(2500);
await page.evaluate(() => fetch("/api/cmd", {method: "POST", body: JSON.stringify({type: "release_all"})}));
await page.waitForTimeout(300);
const info = await page.evaluate(() => ({
  led: document.getElementById("led").className,
  period: document.getElementById("s-period").textContent,
  jit: document.getElementById("s-jit").textContent,
  fps: document.getElementById("s-fps").textContent,
  rec: document.getElementById("s-rec").textContent,
  chipsOut: document.querySelectorAll("#bits-out .chip").length,
  chipsOn: document.querySelectorAll("#bits-out .chip.on, #bits-in .chip.on").length,
  rows: document.querySelectorAll("table.words tr").length,
  events: document.querySelectorAll("#events .ev").length,
  waveH: document.getElementById("wave").height,
  lineH: document.getElementById("line").height,
  line: document.getElementById("h-line").textContent,
}));
console.log(JSON.stringify(info));
// 強制 ON: ESTOP (in) のスイッチをクリック -> CONV1_RUN が OFF になるはず
const estop = page.locator("#bits-in .chip", { hasText: "ESTOP" });
await estop.locator(".sw").click();
await page.waitForTimeout(400);
const afterForce = await page.evaluate(() => ({
  estop: document.querySelector("#bits-in .chip:nth-child(7)").className,
  conv1: document.querySelector("#bits-out .chip").className,
  release: document.getElementById("b-release").textContent,
}));
console.log("after force:", JSON.stringify(afterForce));
await estop.locator(".rel").click();
await page.waitForTimeout(600);
const afterRel = await page.evaluate(() => ({ conv1: document.querySelector("#bits-out .chip").className, release: document.getElementById("b-release").textContent }));
console.log("after release:", JSON.stringify(afterRel));
// ワード強制: AX1_POS に 12345 を入れて表示が変わるか
const row = page.locator("#words-in tr", { hasText: "AX1_POS" });
await row.locator("td.val").click();
await row.locator("td.val input").fill("12345");
await row.locator("td.val input").press("Enter");
await page.waitForTimeout(400);
console.log("word force:", await row.locator("td.val").textContent(), await row.getAttribute("class"));
await row.locator(".rel").click();
// watch 書込み
const wrow = page.locator("#words-watch tr", { hasText: "DM102" });
await wrow.locator("td.val").click();
await wrow.locator("td.val input").fill("0x1F");
await wrow.locator("td.val input").press("Enter");
await page.waitForTimeout(600);
console.log("watch write DM102:", await wrow.locator("td.val").textContent());
// 波形停止・カーソル
await page.mouse.move(900, 700); await page.waitForTimeout(200);
const scroll = await page.evaluate(() => [...document.querySelectorAll(".panel")].map(p => p.scrollWidth - p.clientWidth).filter(d => d > 0));
console.log("panel overflow px:", JSON.stringify(scroll));
await page.screenshot({ path: shot, fullPage: true });
console.log("errors:", errors.length ? errors : "none");
await browser.close();
process.exit(errors.length ? 1 : 0);
})();
