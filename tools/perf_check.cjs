// ブラウザ負荷の計測: node tools/perf_check.cjs http://localhost:8080
const { chromium } = require("playwright");
(async () => {
const url = process.argv[2] || "http://localhost:8080";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = []; page.on("pageerror", (e) => errors.push(e.message));
await page.goto(url);
await page.waitForFunction(() => document.querySelectorAll("#bits-out .chip, #bits-watch .chip").length > 0);
await page.waitForTimeout(6000);
const r = await page.evaluate(async () => {
  const t = (fn, n) => { const t0 = performance.now(); for (let i = 0; i < n; i++) fn(); return (performance.now() - t0) / n; };
  const upd = t(() => updateDevices(), 20), wave = t(() => drawWave(), 20), line = t(() => drawLine(), 20);
  // 5 秒間のロングタスク (50ms 超) と メインスレッド占有率
  let long = 0, busy = 0;
  const po = new PerformanceObserver((l) => { for (const e of l.getEntries()) { long++; busy += e.duration; } });
  po.observe({ entryTypes: ["longtask"] });
  const t0 = performance.now(); let frames = 0;
  await new Promise((res) => { const f = () => { frames++; if (performance.now() - t0 < 5000) requestAnimationFrame(f); else res(); }; requestAnimationFrame(f); });
  po.disconnect();
  return { updateDevices_ms: upd.toFixed(2), drawWave_ms: wave.toFixed(2), drawLine_ms: line.toFixed(2),
           samples: state.samples.length, cycle_ms: state.cfg.cycle_ms, tags: state.tags.length, wave_ch: state.wave.length,
           longtasks_5s: long, longtask_ms: busy.toFixed(0), raf_fps: (frames / 5).toFixed(0), ws_fps: document.getElementById("s-fps").textContent.trim() };
});
console.log(JSON.stringify(r));
console.log("errors:", errors.length ? errors : "none");
await browser.close();
process.exit(errors.length ? 1 : 0);
})();
