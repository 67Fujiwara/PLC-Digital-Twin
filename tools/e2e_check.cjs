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
await page.waitForFunction(() => document.querySelectorAll("#groups .chip.b-out").length > 0, null, { timeout: 10000 });
await page.waitForTimeout(2500);
await page.evaluate(() => fetch("/api/cmd", {method: "POST", body: JSON.stringify({type: "release_all"})}));
await page.waitForTimeout(300);
const info = await page.evaluate(() => ({
  led: document.getElementById("led").className,
  period: document.getElementById("s-period").textContent,
  jit: document.getElementById("s-jit").textContent,
  fps: document.getElementById("s-fps").textContent,
  rec: document.getElementById("s-rec").textContent,
  chipsOut: document.querySelectorAll("#groups .chip.b-out").length,
  chipsOn: document.querySelectorAll("#groups .chip.b-out.on, #groups .chip.b-in.on").length,
  rows: document.querySelectorAll("#groups table.words tr").length,
  events: document.querySelectorAll("#events .ev").length,
  waveH: document.getElementById("wave").height,
  lineH: document.getElementById("line").height,
  line: document.getElementById("h-line").textContent,
}));
console.log(JSON.stringify(info));
// 強制 ON: ESTOP (in) のスイッチをクリック -> CONV1_RUN が OFF になるはず
const estop = page.locator("#groups .chip.b-in", { hasText: "ESTOP" });
await estop.locator(".sw").click();
await page.waitForTimeout(400);
const afterForce = await page.evaluate(() => ({
  estop: [...document.querySelectorAll("#groups .chip.b-in")].find(c => c.textContent.includes("ESTOP")).className,
  conv1: document.querySelector("#groups .chip.b-out").className,
  release: document.getElementById("b-release").textContent,
}));
console.log("after force:", JSON.stringify(afterForce));
await estop.locator(".rel").click();
await page.waitForTimeout(600);
const afterRel = await page.evaluate(() => ({ conv1: document.querySelector("#groups .chip.b-out").className, release: document.getElementById("b-release").textContent }));
console.log("after release:", JSON.stringify(afterRel));
// ワード強制: AX1_POS に 12345 を入れて表示が変わるか
const row = page.locator("#groups tr.b-in", { hasText: "AX1_POS" });
await row.locator("td.val").click();
await row.locator("td.val input").fill("12345");
await row.locator("td.val input").press("Enter");
await page.waitForTimeout(400);
console.log("word force:", await row.locator("td.val").textContent(), await row.getAttribute("class"));
await row.locator(".rel").click();
// watch 書込み
const wrow = page.locator("#groups tr.b-watch", { hasText: "DM102" });
await wrow.locator("td.val").click();
await wrow.locator("td.val input").fill("0x1F");
await wrow.locator("td.val input").press("Enter");
await page.waitForTimeout(600);
console.log("watch write DM102:", await wrow.locator("td.val").textContent());
// 波形停止・カーソル
// 監視デバイスの追加 -> チップが増える -> 削除
const nWatchBefore = await page.locator("#groups .chip.b-watch").count();
await page.fill("#watch-add", "MR300 1");
await page.press("#watch-add", "Enter");
await page.waitForTimeout(800);
const nWatchAfter = await page.locator("#groups .chip.b-watch").count();
console.log("watch add:", nWatchBefore, "->", nWatchAfter, (await page.locator("#watch-list .we").allTextContents()).join(" | "));
await page.locator("#watch-list .we", { hasText: "MR300" }).locator(".x").click();
await page.waitForTimeout(800);
console.log("watch remove:", await page.locator("#groups .chip.b-watch").count());
// 表示設定: PE2 を非表示にして戻す
await page.click("#b-visible");
await page.fill("#vis-filter", "PE2");
await page.locator("#vis-list label", { hasText: "PE2" }).locator("input").uncheck();
await page.click("#vis-close");
console.log("PE2 hidden:", await page.locator("#groups .chip.b-in", { hasText: "PE2" }).first().getAttribute("class"));
await page.click("#b-visible"); await page.fill("#vis-filter", "PE2");
await page.locator("#vis-list label", { hasText: "PE2" }).locator("input").check(); await page.click("#vis-close");
console.log("line header:", await page.locator("#h-line").textContent());
await page.mouse.move(900, 700); await page.waitForTimeout(200);
const scroll = await page.evaluate(() => [...document.querySelectorAll(".panel")].map(p => p.scrollWidth - p.clientWidth).filter(d => d > 0));
console.log("panel overflow px:", JSON.stringify(scroll));
await page.screenshot({ path: shot, fullPage: true });
console.log("errors:", errors.length ? errors : "none");
await browser.close();
process.exit(errors.length ? 1 : 0);
})();
