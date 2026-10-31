// ライン設定の確認: node tools/line_check.cjs http://localhost:8080 out.png
const { chromium } = require("playwright");
(async () => {
const [url, shot] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = []; page.on("pageerror", (e) => errors.push(e.message)); page.on("dialog", (d) => d.accept());
await page.goto(url);
await page.waitForFunction(() => document.querySelectorAll("#groups .grp").length > 0, null, { timeout: 15000 });
await page.waitForTimeout(8000);
const st = () => page.evaluate(() => ({ line: document.getElementById("h-line").textContent,
  cams: state.sim.objects.filter(o => o.type === "camera").map(o => `${o.name}:${o.px}@x${o.x_offset}`), sol: state.sim.objects.filter(o => o.type === "airblow").map(o => `${o.nozzles}x${o.rows}rows gap${o.row_gap} at${o.at}`),
  tags: state.tags.length }));
console.log("before:", JSON.stringify(await st()));
await page.click("#b-line"); await page.waitForTimeout(500);
console.log("derived:", await page.locator("#line-derived").textContent());
await page.fill("#ln-cam-count", "3"); await page.fill("#ln-px-x", "800"); await page.selectOption("#ln-ej-rows", "1"); await page.fill("#ln-ej-count", "24"); await page.fill("#ln-speed", "400");
await page.click("#line-save"); await page.waitForTimeout(3000);
console.log("after:", JSON.stringify(await st()));
if (shot) await page.screenshot({ path: shot });
console.log("errors:", errors.length ? errors : "none");
await browser.close(); process.exit(errors.length ? 1 : 0);
})();
