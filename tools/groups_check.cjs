// ブロック表示の確認: node tools/groups_check.cjs http://localhost:8080 out.png
const { chromium } = require("playwright");
(async () => {
const [url, shot] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = []; page.on("pageerror", (e) => errors.push(e.message));
let promptAnswer = null;
page.on("dialog", async (d) => { if (d.type() === "prompt") await d.accept(promptAnswer); else await d.accept(); });
await page.goto(url);
await page.waitForFunction(() => document.querySelectorAll("#groups .grp").length > 0, null, { timeout: 15000 });
await page.waitForTimeout(1500);
const info = () => page.evaluate(() => ({
  groups: [...document.querySelectorAll("#groups .grp")].map(g => g.querySelector(".gname").textContent + " (" + g.querySelector(".cnt").textContent + ")"),
  wave: state.wave.length, chips: document.querySelectorAll("#groups .chip").length, rows: document.querySelectorAll("#groups table.words tr").length,
}));
let i = await info(); console.log("groups:", i.groups.length, i.groups.slice(0, 8).join(" | "), "chips", i.chips, "rows", i.rows);
// 2 番目のブロックの名前を変更
promptAnswer = "検査まわり";
await page.locator("#groups .grp").nth(1).locator("button", { hasText: "名前" }).click();
await page.waitForTimeout(600);
// 波形にブロック登録
await page.locator("#groups .grp").nth(1).locator("button", { hasText: "波形に登録" }).click();
await page.waitForTimeout(600);
i = await info(); console.log("after rename+wave:", i.groups[1], "wave ch", i.wave);
// Shift+クリックで 1 番目ブロックの先頭デバイスを 2 番目ブロックへ移動
promptAnswer = "2";
await page.locator("#groups .grp").nth(0).locator(".chip, table.words tr").first().click({ modifiers: ["Shift"] });
await page.waitForTimeout(800);
i = await info(); console.log("after move:", i.groups.slice(0, 2).join(" | "));
if (shot) await page.screenshot({ path: shot });
// 再読込しても保持される
await page.reload(); await page.waitForFunction(() => document.querySelectorAll("#groups .grp").length > 0); await page.waitForTimeout(1500);
i = await info(); console.log("after reload:", i.groups.slice(0, 2).join(" | "), "wave ch", i.wave);
console.log("errors:", errors.length ? errors : "none");
await browser.close(); process.exit(errors.length ? 1 : 0);
})();
