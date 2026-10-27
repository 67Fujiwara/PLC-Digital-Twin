/* PLC Digital Twin - ブラウザ UI (依存なし・ビルド不要)
 * サーバから 30Hz で届く frame (10ms サンプルの束) を受け、
 * デバイス表示 / 波形 / ライン図 / イベントを描画する。描画はタイミングに関与しない。
 */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  cfg: null,
  samples: [],          // {seq, wall, out, in, watch, p, r, w}
  maxSamples: 12000,
  latest: null,         // 最新サンプル
  prev: null,           // 直前サンプル (変化検出用)
  sim: null, stat: null, forces: {bits: [], words: []},
  tags: [],             // 全タグ (block 付き)
  tagByKey: new Map(),
  wave: [],             // 波形チャンネル (tag key)
  paused: false, pauseSeq: 0, winSec: 10,
  events: [], evWords: false,
  wsFps: 0, wsCount: 0, latency: 0,
  changedAt: new Map(), // key -> 最終変化時刻
  cursorX: null,
  hidden: new Set(),    // 非表示タグ (tag key)
  watchEntries: [],     // 監視デバイス一覧 (編集用)
};

// ---------------------------------------------------------------------------
// ユーティリティ
// ---------------------------------------------------------------------------
function tagKey(t) { return `${t.block}.${t.name}`; }
function s16(v) { return v & 0x8000 ? v - 0x10000 : v; }
function s32(v) { return v & 0x80000000 ? v - 0x100000000 : v; }
function u32(lo, hi) { return (lo | 0) + (hi | 0) * 65536; }
function blockArr(s, block) { return block === "in" ? s.in : block === "out" ? s.out : s.watch; }
function getVal(s, t) {
  if (!s) return 0;
  const a = blockArr(s, t.block);
  if (!a || t.word >= a.length) return 0;
  if (t.kind === "bit") return (a[t.word] >> t.bit) & 1;
  if (t.dtype === "u32" || t.dtype === "s32") {
    const v = u32(a[t.word], a[t.word + 1] || 0);
    return t.dtype === "s32" ? s32(v) : v;
  }
  return t.dtype === "s16" ? s16(a[t.word]) : a[t.word];
}
function fmtTime(wall) {
  const d = new Date(wall * 1000);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}
function hex(v, w = 4) { return (v >>> 0).toString(16).toUpperCase().padStart(w, "0"); }
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
let ws = null, wsRetry = 500;
function connect() {
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
  ws = new WebSocket(url);
  ws.onopen = () => { wsRetry = 500; $("status").textContent = "接続中…"; pingLoop(); };
  ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
  ws.onclose = () => {
    $("led").className = "led"; $("status").textContent = "サーバ切断 - 再接続中";
    setTimeout(connect, wsRetry); wsRetry = Math.min(5000, wsRetry * 1.5);
  };
  ws.onerror = () => ws.close();
}
function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
function pingLoop() {
  if (!ws || ws.readyState !== 1) return;
  send({type: "ping", t: performance.now()});
  setTimeout(pingLoop, 2000);
}

function onMessage(m) {
  if (m.type === "hello") { setup(m.config); return; }
  if (m.type === "error") { addLog({t: Date.now() / 1000, level: "error", msg: m.msg}); alert(m.msg); return; }
  if (m.type === "pong") { state.latency = performance.now() - m.t; return; }
  if (m.type === "history") { for (const s of m.samples) pushSample(s, false); state.prev = state.latest; return; }
  if (m.type !== "frame") return;
  state.wsCount++;
  for (const s of m.samples) pushSample(s, true);
  state.sim = m.sim; state.stat = m.stat; state.forces = m.forces;
  if (m.stat && m.stat.cycle_ms && state.cfg && m.stat.cycle_ms !== state.cfg.cycle_ms) {
    state.cfg.cycle_ms = m.stat.cycle_ms;
    state.maxSamples = Math.max(3000, Math.round(state.cfg.record_seconds * 1000 / m.stat.cycle_ms));
    $("h-wave-res").textContent = `${m.stat.cycle_ms}ms 分解能`;
  }
  for (const l of m.logs || []) addLog(l);
  updateHeader(m);
  updateDevices();
}

function pushSample(s, detect) {
  if (!s.watch) s.watch = state.latest ? state.latest.watch : [];   // 省略 = 前回と同じ
  state.samples.push(s);
  if (state.samples.length > state.maxSamples) state.samples.splice(0, state.samples.length - state.maxSamples);
  if (detect && state.latest) detectEvents(state.latest, s);
  state.latest = s;
}

// ---------------------------------------------------------------------------
// 初期化 (hello 受信時)
// ---------------------------------------------------------------------------
function setup(cfg) {
  const rehello = !!state.cfg;
  state.cfg = cfg;
  document.title = cfg.title;
  $("title").textContent = cfg.title;
  $("plcinfo").textContent = `${cfg.plc.type.toUpperCase()} ${cfg.plc.ip || ""}${cfg.plc.port ? ":" + cfg.plc.port : ""}${cfg.plc.driver === "mock" ? " (モック: 未接続)" : ""}`;
  state.maxSamples = Math.max(3000, Math.round(cfg.record_seconds * 1000 / cfg.cycle_ms));
  state.tags = []; state.tagByKey.clear();
  for (const b of ["out", "in", "watch"]) for (const t of cfg.tags[b] || []) { state.tags.push(t); state.tagByKey.set(tagKey(t), t); }
  $("h-out").textContent = `${cfg.out.dev}${cfg.out.addr} × ${cfg.out.words}w`;
  $("h-in").textContent = `${cfg.in.dev}${cfg.in.addr} × ${cfg.in.words}w`;
  $("h-watch").textContent = cfg.watch.length > 6
    ? `${cfg.watch.length} エントリ / ${cfg.watch_words} ワード`
    : (cfg.watch.map(w => `${w.dev}${w.addr}×${w.words}w`).join(", ") || "なし");
  $("p-watch").classList.toggle("hidden", !cfg.watch.length);
  state.watchEntries = cfg.watch.map(w => ({dev: w.dev, addr: w.addr, words: w.words, names: w.names, bits: w.bits, source: w.source}));
  loadHidden();
  buildDevices();
  buildRaw();
  buildLineButtons();
  buildWatchList();
  loadWave();
  if (!rehello) { state.samples = []; state.latest = null; state.prev = null; }
  else state.wave = state.wave.filter(k => state.tagByKey.has(k));
  $("status").textContent = "";
}

// ---------------------------------------------------------------------------
// 表示設定 (非表示タグ)
// ---------------------------------------------------------------------------
function loadHidden() {
  try { state.hidden = new Set(JSON.parse(localStorage.getItem("twin.hidden." + state.cfg.title) || "[]")); } catch (e) { state.hidden = new Set(); }
}
function saveHidden() { try { localStorage.setItem("twin.hidden." + state.cfg.title, JSON.stringify([...state.hidden])); } catch (e) { /* ignore */ } }
function applyHidden() {
  for (const t of state.tags) {
    const key = tagKey(t), hid = state.hidden.has(key);
    const c = dom.chips.get(key); if (c) c.classList.toggle("hidden", hid);
    const r = dom.rows.get(key); if (r) r.tr.classList.toggle("hidden", hid);
  }
}
function openVisible() {
  const list = $("vis-list"); list.innerHTML = "";
  const f = $("vis-filter").value.trim().toLowerCase();
  for (const b of ["out", "in", "watch"]) {
    const tags = (state.cfg.tags[b] || []).filter(t => !f || t.name.toLowerCase().includes(f) || t.address.toLowerCase().includes(f));
    if (!tags.length) continue;
    list.appendChild(el("h4", null, b === "out" ? "PLC → PC" : b === "in" ? "PC → PLC" : "監視デバイス"));
    for (const t of tags) {
      const key = tagKey(t);
      const lb = el("label"); const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !state.hidden.has(key);
      cb.onchange = () => { cb.checked ? state.hidden.delete(key) : state.hidden.add(key); saveHidden(); applyHidden(); };
      lb.appendChild(cb); lb.appendChild(el("span", null, t.name)); lb.appendChild(el("span", "ad", t.address));
      list.appendChild(lb);
    }
  }
  $("modal").classList.remove("hidden");
}
$("b-visible").onclick = openVisible;
$("vis-close").onclick = () => $("modal").classList.add("hidden");
$("modal").onclick = (e) => { if (e.target.id === "modal") $("modal").classList.add("hidden"); };
$("vis-filter").oninput = openVisible;
$("vis-all").onclick = () => { state.hidden.clear(); saveHidden(); applyHidden(); openVisible(); };
$("vis-none").onclick = () => { for (const t of state.tags) state.hidden.add(tagKey(t)); saveHidden(); applyHidden(); openVisible(); };

// ---------------------------------------------------------------------------
// 監視デバイスの編集 (画面から追加/削除/保存)
// ---------------------------------------------------------------------------
function buildWatchList() {
  const box = $("watch-list"); box.innerHTML = "";
  state.watchEntries.forEach((w, i) => {
    const d = el("span", "we", `${w.dev}${w.addr} ×${w.words}${w.bits ? " (bit)" : ""}${w.source === "import" ? " 📄" : ""}`);
    if (w.source === "import") d.title = "CSV 取込由来 (.tags.yaml)";
    const x = el("span", "x", "✕"); x.title = "削除";
    x.onclick = () => { state.watchEntries.splice(i, 1); sendWatch(false); };
    d.appendChild(x); box.appendChild(d);
  });
  if (!state.watchEntries.length) box.appendChild(el("span", "dim", "監視デバイスなし。右上の欄に DM100 8 のように入力して追加"));
}
function parseWatch(text) {
  // "DM100 8" / "DM100" / "D0-D15" / "MR200 2" / "X10-X1F" (16進デバイス)
  const t = text.trim().toUpperCase();
  let m = t.match(/^([A-Z]+)([0-9A-F]+)\s*[-〜~]\s*(?:[A-Z]+)?([0-9A-F]+)$/);
  const hexDev = (d) => state.cfg.plc.type === "melsec" ? ["X", "Y", "B", "SB", "W", "SW", "ZR", "DX", "DY"].includes(d)
                       : state.cfg.plc.type === "keyence" ? d === "B" : false;
  const bitDev = (d) => state.cfg.plc.type === "melsec" ? ["X", "Y", "M", "L", "F", "V", "B", "S", "SM", "SB"].includes(d)
                       : ["MR", "R", "LR", "CR", "VB", "B", "M", "X", "Y"].includes(d);
  if (m) {
    const dev = m[1], a = parseInt(m[2], hexDev(dev) ? 16 : 10), b = parseInt(m[3], hexDev(dev) ? 16 : 10);
    if (Number.isNaN(a) || Number.isNaN(b) || b < a) return null;
    const isBit = bitDev(dev);
    const per = isBit ? (state.cfg.plc.type === "keyence" && dev !== "B" ? 100 : 16) : 1;
    return {dev, addr: a, words: Math.max(1, Math.floor((b - a) / per) + 1), bits: isBit};
  }
  m = t.match(/^([A-Z]+)([0-9A-F]+)(?:\s+(\d+))?$/);
  if (!m) return null;
  const dev = m[1], addr = parseInt(m[2], hexDev(dev) ? 16 : 10);
  if (Number.isNaN(addr)) return null;
  return {dev, addr, words: Math.max(1, parseInt(m[3] || "1", 10)), bits: bitDev(dev)};
}
function sendWatch(save) {
  send({type: "set_watch", save, watch: state.watchEntries.map(w => {
    const o = {dev: w.dev, addr: w.addr, words: w.words, bits: w.bits};
    if (w.names && w.names.length) o.names = w.names;
    if (w.source) o.source = w.source;
    return o;
  })});
}
function addWatch() {
  const inp = $("watch-add");
  const w = parseWatch(inp.value);
  if (!w) { alert("書式: DM100 8  /  MR200 2  /  D0-D15  (デバイス 先頭 [ワード数] または 先頭-末尾)"); return; }
  if (w.words > 64) { alert("1 エントリは 64 ワードまでにしてください"); return; }
  state.watchEntries.push(w);
  inp.value = "";
  sendWatch(false);
}
$("b-watch-add").onclick = addWatch;
$("watch-add").onkeydown = (e) => { if (e.key === "Enter") addWatch(); };
$("b-watch-save").onclick = () => sendWatch(true);

// ---------------------------------------------------------------------------
// CSV 取込 (PLC のラベル / デバイスコメント)
// ---------------------------------------------------------------------------
$("b-import").onclick = () => $("f-import").click();
$("f-import").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  e.target.value = "";
  const sys = confirm(`${f.name} を取り込みます。\n\nシステムデバイス (KV: CM/CR, 三菱: SM/SD) も取り込みますか?\n[OK]=取り込む  [キャンセル]=除外 (通常はこちら)`) ? 1 : 0;
  const body = await f.arrayBuffer();
  const r = await fetch(`/api/import?name=${encodeURIComponent(f.name)}&system=${sys}`, {method: "POST", body});
  const j = await r.json();
  if (!r.ok) { alert("取込失敗: " + (j.error || r.status) + (j.skipped ? "\n" + j.skipped.map(x => x.join(": ")).join("\n") : "")); return; }
  const sk = (j.skipped || []).map(x => `${x[0]}: ${x[1]}`).join("\n");
  alert(`取込完了\n  PLC→PC: ${j.out} 個\n  PC→PLC: ${j.in} 個\n  監視デバイス: ${j.watch} エントリ (${j.words} ワード)\n  保存: ${j.saved}` + (sk ? `\n\n除外:\n${sk}` : ""));
};

// ---------------------------------------------------------------------------
// デバイス表示
// ---------------------------------------------------------------------------
const dom = {chips: new Map(), rows: new Map(), raw: {out: [], in: []}};

function buildDevices() {
  dom.chips.clear(); dom.rows.clear();
  for (const b of ["out", "in", "watch"]) {
    const bits = $(`bits-${b}`), words = $(`words-${b}`);
    bits.innerHTML = ""; words.innerHTML = "";
    for (const t of state.cfg.tags[b] || []) {
      if (t.kind === "bit") bits.appendChild(makeChip(t));
      else words.appendChild(makeRow(t));
    }
  }
  applyHidden();
}

function makeChip(t) {
  const key = tagKey(t);
  const c = el("div", "chip");
  c.title = `${t.address}${t.comment ? "  " + t.comment : ""}\nクリック: 波形に追加/削除`;
  c.appendChild(el("span", "dot"));
  c.appendChild(el("span", "nm", t.name));
  c.appendChild(el("span", "pin", "📌"));
  c.appendChild(el("span", "ad", t.address));
  if (t.block !== "out") {
    const sw = el("span", "sw");
    sw.title = t.block === "in" ? "強制 ON/OFF (Sim の値を上書き)" : "PLC へ書込み";
    sw.onclick = (e) => { e.stopPropagation(); toggleBit(t); };
    c.appendChild(sw);
    const rel = el("span", "rel", "✕");
    rel.title = "強制解除";
    rel.onclick = (e) => { e.stopPropagation(); send({type: "force_bit", block: t.block, word: t.word, bit: t.bit, value: null}); };
    c.appendChild(rel);
  }
  c.onclick = () => toggleWave(key);
  dom.chips.set(key, c);
  return c;
}

function toggleBit(t) {
  const cur = getVal(state.latest, t);
  if (t.block === "in") {
    send({type: "force_bit", block: "in", word: t.word, bit: t.bit, value: cur ? 0 : 1});
  } else if (t.block === "watch") {
    send({type: "write_watch_bit", word: t.word, bit: t.bit, value: cur ? 0 : 1});
  }
}

function makeRow(t) {
  const key = tagKey(t);
  const tr = el("tr");
  tr.title = `${t.address} (${t.dtype})${t.comment ? "  " + t.comment : ""}\nクリック: 波形に追加/削除`;
  const nm = el("td", "nm", t.name);
  if (t.comment) nm.appendChild(el("span", "cm", t.comment));
  tr.appendChild(nm);
  tr.appendChild(el("td", "ad", `${t.address}${t.dtype !== "u16" ? ":" + t.dtype : ""}`));
  const val = el("td", "val", "0"); tr.appendChild(val);
  const hx = el("td", "hex", "0000"); tr.appendChild(hx);
  const sp = el("td", "sp"); const cv = document.createElement("canvas"); cv.width = 70; cv.height = 18; sp.appendChild(cv); tr.appendChild(sp);
  const ed = el("td", "ed");
  if (t.block !== "out") {
    val.classList.add("editable");
    val.title = t.block === "in" ? "クリックして値を入力 (強制)。10進 or 0x.. / Enter で確定, Esc で取消" : "クリックして値を入力 (PLC へ書込み)";
    val.onclick = (e) => {
      e.stopPropagation();
      if (val.querySelector("input")) return;
      const inp = document.createElement("input"); inp.className = "w"; inp.value = String(r.last ?? 0);
      let closed = false;   // Chrome は remove() 中に blur を同期発火するため二重 remove を防ぐ
      const close = () => { if (closed) return; closed = true; inp.remove(); r.last = null; };
      inp.onclick = (ev) => ev.stopPropagation();
      inp.onkeydown = (ev) => {
        if (ev.key === "Enter") {
          const txt = inp.value.trim().toLowerCase();
          const v = parseInt(txt, txt.startsWith("0x") ? 16 : 10);
          if (!Number.isNaN(v)) {
            if (t.block === "in") send({type: "force_word", block: "in", word: t.word, dtype: t.dtype, value: v});
            else send({type: "write_watch", word: t.word, value: v});
          }
          close();
        } else if (ev.key === "Escape") { close(); }
      };
      inp.onblur = close;
      val.textContent = ""; val.appendChild(inp); inp.focus(); inp.select();
    };
    if (t.block === "in") {
      const rel = el("span", "rel", "✕"); rel.title = "強制解除";
      rel.onclick = (e) => { e.stopPropagation(); send({type: "force_word", block: "in", word: t.word, dtype: t.dtype, value: null}); };
      ed.appendChild(rel);
    }
  }
  tr.appendChild(ed);
  const r = {tr, val, hx, cv, last: null};
  tr.onclick = () => toggleWave(key);
  dom.rows.set(key, r);
  return tr;
}

function buildRaw() {
  for (const b of ["out", "in"]) {
    const g = $(`raw-${b}`); g.innerHTML = ""; dom.raw[b] = [];
    const spec = state.cfg[b];
    $(`raw-${b}-addr`).textContent = `${spec.dev}${spec.addr}〜`;
    for (let i = 0; i < spec.words; i++) {
      const d = el("div"); const idx = el("span", null, String(spec.addr + i)); d.appendChild(idx); d.appendChild(document.createTextNode("0000"));
      g.appendChild(d); dom.raw[b].push(d);
    }
  }
}

function updateDevices() {
  const s = state.latest; if (!s) return;
  const now = performance.now();
  const forcedBits = new Set(state.forces.bits.map(f => `${f.block}.${f.word}.${f.bit}`));
  const forcedBitVal = new Map(state.forces.bits.map(f => [`${f.block}.${f.word}.${f.bit}`, f.value]));
  const forcedWords = new Set(state.forces.words.map(f => `${f.block}.${f.word}`));
  for (const t of state.tags) {
    const key = tagKey(t);
    if (state.hidden.has(key)) continue;
    const v = getVal(s, t);
    if (t.kind === "bit") {
      const c = dom.chips.get(key); if (!c) continue;
      const on = !!v;
      if (c.classList.contains("on") !== on) {
        c.classList.toggle("on", on);
        c.classList.remove("chg"); void c.offsetWidth; c.classList.add("chg");
      }
      const fk = `${t.block}.${t.word}.${t.bit}`;
      const f = forcedBits.has(fk);
      c.classList.toggle("forced", f);
      c.classList.toggle("fon", f && !!forcedBitVal.get(fk));
      c.classList.toggle("foff", f && !forcedBitVal.get(fk));
      c.classList.toggle("sel", state.wave.includes(key));
    } else {
      const r = dom.rows.get(key); if (!r) continue;
      if (r.last !== v && !r.val.querySelector("input")) {
        r.val.textContent = v.toLocaleString();
        r.hx.textContent = hex(v, t.dtype.endsWith("32") ? 8 : 4);
        r.val.classList.add("chg"); state.changedAt.set(key, now);
        r.last = v;
      } else if (state.changedAt.get(key) && now - state.changedAt.get(key) > 400) {
        r.val.classList.remove("chg"); state.changedAt.delete(key);
      }
      r.tr.classList.toggle("forced", forcedWords.has(`${t.block}.${t.word}`));
      r.tr.classList.toggle("sel", state.wave.includes(key));
      drawSpark(r.cv, t);
    }
  }
  if (!$("raw").classList.contains("hidden")) {
    for (const b of ["out", "in"]) {
      const a = blockArr(s, b), pa = state.prev ? blockArr(state.prev, b) : null;
      dom.raw[b].forEach((d, i) => {
        const v = a[i] || 0;
        d.lastChild.textContent = hex(v);
        d.classList.toggle("nz", v !== 0);
        d.classList.toggle("chg", !!pa && pa[i] !== v);
      });
    }
  }
  state.prev = s;
}

function drawSpark(cv, t) {
  const n = 70, ctx = cv.getContext("2d");
  const ss = state.samples, len = ss.length;
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (len < 2) return;
  const step = Math.max(1, Math.floor(300 / n));   // 直近 3 秒
  let mn = Infinity, mx = -Infinity; const vals = [];
  for (let i = Math.max(0, len - n * step); i < len; i += step) { const v = getVal(ss[i], t); vals.push(v); if (v < mn) mn = v; if (v > mx) mx = v; }
  if (mx === mn) { mx = mn + 1; }
  ctx.strokeStyle = "#4da3ff"; ctx.lineWidth = 1; ctx.beginPath();
  vals.forEach((v, i) => { const x = i * (cv.width / (n - 1)), y = cv.height - 1 - (v - mn) / (mx - mn) * (cv.height - 2); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.stroke();
}

// ---------------------------------------------------------------------------
// ヘッダ統計
// ---------------------------------------------------------------------------
let fpsLast = performance.now(), fpsCount = 0;
function updateHeader(m) {
  const st = m.stat || {};
  const ok = st.status === "connected" && m.loop_alive;
  $("led").className = "led " + (ok ? "ok" : "");
  $("status").textContent = ok ? (st.sim_enabled ? "" : "Sim 停止中") : (st.error || st.status || "");
  const fx = (v, d, w) => (v ?? 0).toFixed(d).padStart(w, " ");
  $("s-period").textContent = fx(st.period_ms, 2, 5);
  $("s-period").title = `設定周期 ${state.cfg.cycle_ms}ms${state.cfg.cycle_auto ? " (自動)" : ""}  通信 ${state.cfg.protocol || "tcp"}${state.cfg.parallel_rw ? " 読み書き並列" : ""}`;
  const j = st.jitter || {};
  $("s-jit").textContent = `${fx(j.avg, 2, 5)} / ${fx(j.p99, 2, 5)} / ${fx(j.max_all, 1, 5)}`;
  $("s-rw").textContent = `${fx(st.read_ms, 2, 5)} / ${fx(st.step_ms, 2, 5)} / ${fx(st.write_ms, 2, 5)}`;
  $("s-lat").textContent = fx(state.latency, 0, 4);
  $("s-rec").textContent = fx(state.samples.length * state.cfg.cycle_ms / 1000, 0, 3);
  fpsCount++;
  const now = performance.now();
  if (now - fpsLast > 1000) { state.wsFps = fpsCount * 1000 / (now - fpsLast); fpsCount = 0; fpsLast = now; $("s-fps").textContent = state.wsFps.toFixed(0).padStart(2, " "); }
  $("b-simtoggle").textContent = st.sim_enabled ? "Sim 停止" : "Sim 再開";
  $("b-simtoggle").classList.toggle("active", st.sim_enabled === false);
  const nf = state.forces.bits.length + state.forces.words.length;
  $("b-release").textContent = nf ? `強制解除 (${nf})` : "強制解除";
  $("b-release").classList.toggle("active", nf > 0);
}

// ---------------------------------------------------------------------------
// イベント (立上り/立下り/数値変化)
// ---------------------------------------------------------------------------
function detectEvents(a, b) {
  for (const t of state.tags) {
    if (t.kind === "bit") {
      const va = getVal(a, t), vb = getVal(b, t);
      if (va !== vb) addEvent({t: b.wall, seq: b.seq, kind: vb ? "up" : "dn", tag: t, text: vb ? "ON" : "OFF"});
    } else if (state.evWords) {
      const va = getVal(a, t), vb = getVal(b, t);
      if (va !== vb) addEvent({t: b.wall, seq: b.seq, kind: "wv", tag: t, text: `${va} → ${vb}`});
    }
  }
}
const evBox = () => $("events");
function addEvent(e) {
  state.events.push(e);
  if (state.events.length > 500) state.events.shift();
  const f = $("ev-filter").value.trim().toLowerCase();
  if (f && !e.tag.name.toLowerCase().includes(f) && !e.tag.address.toLowerCase().includes(f)) return;
  const box = evBox();
  const d = el("div", "ev");
  d.appendChild(el("span", "t", fmtTime(e.t)));
  d.appendChild(el("span", e.kind, e.kind === "up" ? "▲" : e.kind === "dn" ? "▽" : "≈"));
  d.appendChild(el("span", "n", e.tag.name));
  d.appendChild(el("span", "b", `${e.tag.block} ${e.tag.address}`));
  d.appendChild(el("span", e.kind, e.text));
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
  box.appendChild(d);
  while (box.children.length > 500) box.removeChild(box.firstChild);
  if (atBottom) box.scrollTop = box.scrollHeight;
  $("h-ev").textContent = `${state.events.length}`;
}
function addLog(l) {
  const box = $("logs");
  const d = el("div", "lg " + (l.level || "info"), `${fmtTime(l.t)}  ${l.msg}`);
  box.appendChild(d);
  while (box.children.length > 200) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------------------
// 波形 (ロジアナ風)
// ---------------------------------------------------------------------------
function loadWave() {
  try {
    const saved = JSON.parse(localStorage.getItem("twin.wave." + state.cfg.title) || "null");
    if (Array.isArray(saved) && saved.length) { state.wave = saved.filter(k => state.tagByKey.has(k)); }
  } catch (e) { /* ignore */ }
  if (!state.wave.length) {
    // 既定: out/in のビットを最大 12ch
    state.wave = state.tags.filter(t => t.kind === "bit" && t.block !== "watch").slice(0, 12).map(tagKey);
  }
}
function saveWave() { try { localStorage.setItem("twin.wave." + state.cfg.title, JSON.stringify(state.wave)); } catch (e) { /* ignore */ } }
function toggleWave(key) {
  const i = state.wave.indexOf(key);
  if (i >= 0) state.wave.splice(i, 1); else state.wave.push(key);
  saveWave(); updateDevices();
}

const waveCv = $("wave"), lineCv = $("line");
function resizeCanvas(cv, h) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || cv.parentElement.clientWidth;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); cv.style.height = h + "px";
  }
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [ctx, w, h];
}

const DIG_H = 26, ANA_H = 56, LBL_W = 150, AXIS_H = 18;
function drawWave() {
  const chans = state.wave.map(k => state.tagByKey.get(k)).filter(Boolean);
  const hgt = AXIS_H + chans.reduce((a, t) => a + (t.kind === "bit" ? DIG_H : ANA_H), 0) + 6;
  const [ctx, W, H] = resizeCanvas(waveCv, Math.max(80, hgt));
  ctx.clearRect(0, 0, W, H);
  const ss = state.samples; if (!ss.length || !state.cfg) { ctx.fillStyle = "#555"; ctx.fillText("波形: デバイスをクリックして追加", 10, 20); return; }
  const cyc = state.cfg.cycle_ms / 1000;
  const nowSeq = state.paused ? state.pauseSeq : ss[ss.length - 1].seq;
  const nSamp = Math.round(state.winSec / cyc);
  const x0 = LBL_W, PW = W - LBL_W - 6;
  const px = PW / nSamp;
  const seqToX = (seq) => x0 + PW - (nowSeq - seq) * px;
  // 時間軸
  ctx.font = "10px ui-monospace, monospace"; ctx.textBaseline = "top";
  const stepS = state.winSec <= 2 ? 0.2 : state.winSec <= 5 ? 0.5 : state.winSec <= 10 ? 1 : state.winSec <= 30 ? 5 : 10;
  ctx.strokeStyle = "#232a34"; ctx.fillStyle = "#6b7684";
  for (let s = 0; s <= state.winSec + 1e-9; s += stepS) {
    const x = x0 + PW - s / cyc * px;
    ctx.beginPath(); ctx.moveTo(x, AXIS_H - 4); ctx.lineTo(x, H); ctx.stroke();
    ctx.fillText(`-${s.toFixed(stepS < 1 ? 1 : 0)}s`, x - 10, 2);
  }
  // サンプル範囲
  let i0 = ss.length - 1;
  while (i0 > 0 && ss[i0].seq > nowSeq - nSamp) i0--;
  let i1 = ss.length - 1;
  while (i1 > 0 && ss[i1].seq > nowSeq) i1--;
  // カーソル
  let curSeq = null;
  if (state.cursorX != null && state.cursorX > x0) curSeq = Math.round(nowSeq - (x0 + PW - state.cursorX) / px);
  const tip = [];
  let y = AXIS_H;
  for (const t of chans) {
    const dig = t.kind === "bit";
    const rowH = dig ? DIG_H : ANA_H;
    // ラベル
    ctx.fillStyle = "#121620"; ctx.fillRect(0, y, LBL_W - 2, rowH - 1);
    ctx.fillStyle = t.block === "out" ? "#9fd0ff" : t.block === "in" ? "#b8f0c0" : "#e8d59a";
    ctx.font = "bold 11px system-ui, sans-serif"; ctx.textBaseline = "middle";
    ctx.fillText(t.name, 6, y + (dig ? 8 : 12), LBL_W - 44);
    ctx.font = "9px ui-monospace, monospace"; ctx.fillStyle = "#6b7684";
    ctx.fillText(t.address, 6, y + (dig ? 18 : 24), LBL_W - 44);
    // 現在値
    const cur = getVal(curSeq != null ? ss[findSeq(curSeq)] : ss[i1], t);
    ctx.font = "bold 11px ui-monospace, monospace"; ctx.textAlign = "right";
    ctx.fillStyle = dig ? (cur ? "#39d353" : "#7c8695") : "#ffd35c";
    ctx.fillText(dig ? (cur ? "ON" : "OFF") : String(cur), LBL_W - 6, y + 12);
    ctx.textAlign = "left";
    if (curSeq != null) tip.push(`${t.name.padEnd(14)} ${dig ? (cur ? "ON" : "OFF") : cur}`);
    // 波形
    ctx.beginPath(); ctx.strokeStyle = "#1f252e"; ctx.moveTo(x0, y + rowH - 1); ctx.lineTo(W, y + rowH - 1); ctx.stroke();
    if (dig) {
      ctx.strokeStyle = t.block === "out" ? "#4da3ff" : t.block === "in" ? "#39d353" : "#e8d59a"; ctx.lineWidth = 1.5;
      ctx.beginPath();
      let prev = null;
      for (let i = i0; i <= i1; i++) {
        const s = ss[i], v = getVal(s, t), x = seqToX(s.seq), yy = v ? y + 4 : y + rowH - 5;
        if (prev == null) ctx.moveTo(Math.max(x0, x), yy);
        else { ctx.lineTo(x, prev ? y + 4 : y + rowH - 5); ctx.lineTo(x, yy); }
        prev = v;
      }
      if (prev != null) ctx.lineTo(seqToX(nowSeq), prev ? y + 4 : y + rowH - 5);
      ctx.stroke();
      // ON 区間の塗り
      ctx.fillStyle = t.block === "out" ? "rgba(77,163,255,.12)" : t.block === "in" ? "rgba(57,211,83,.12)" : "rgba(232,213,154,.12)";
      let onStart = null;
      for (let i = i0; i <= i1; i++) {
        const s = ss[i], v = getVal(s, t);
        if (v && onStart == null) onStart = seqToX(s.seq);
        if (!v && onStart != null) { ctx.fillRect(Math.max(x0, onStart), y + 4, seqToX(s.seq) - Math.max(x0, onStart), rowH - 9); onStart = null; }
      }
      if (onStart != null) ctx.fillRect(Math.max(x0, onStart), y + 4, seqToX(nowSeq) - Math.max(x0, onStart), rowH - 9);
    } else {
      let mn = Infinity, mx = -Infinity;
      for (let i = i0; i <= i1; i++) { const v = getVal(ss[i], t); if (v < mn) mn = v; if (v > mx) mx = v; }
      if (mn === Infinity) { mn = 0; mx = 1; } if (mx === mn) { mx = mn + 1; }
      const yOf = (v) => y + rowH - 6 - (v - mn) / (mx - mn) * (rowH - 14);
      ctx.fillStyle = "#55606f"; ctx.font = "9px ui-monospace, monospace"; ctx.textAlign = "right";
      ctx.fillText(String(mx), LBL_W - 6, y + 32); ctx.fillText(String(mn), LBL_W - 6, y + rowH - 8); ctx.textAlign = "left";
      ctx.strokeStyle = "#ffd35c"; ctx.lineWidth = 1.5; ctx.beginPath();
      let first = true;
      for (let i = i0; i <= i1; i++) { const s = ss[i], x = seqToX(s.seq), yy = yOf(getVal(s, t)); if (x < x0) continue; first ? ctx.moveTo(x, yy) : ctx.lineTo(x, yy); first = false; }
      ctx.stroke();
    }
    ctx.lineWidth = 1;
    y += rowH;
  }
  // カーソル線
  if (curSeq != null) {
    const x = seqToX(curSeq);
    ctx.strokeStyle = "#ff8c42"; ctx.beginPath(); ctx.moveTo(x, AXIS_H - 4); ctx.lineTo(x, H); ctx.stroke();
    const s = ss[findSeq(curSeq)];
    const t = $("wave-tip"); t.classList.remove("hidden");
    t.textContent = `${fmtTime(s.wall)}  (-${((nowSeq - curSeq) * cyc).toFixed(2)}s)\n` + tip.join("\n");
  } else $("wave-tip").classList.add("hidden");
  if (state.paused) { ctx.fillStyle = "#f5b22b"; ctx.font = "bold 12px system-ui"; ctx.fillText("⏸ 停止中", x0 + 6, 4); }
}
function findSeq(seq) {
  const ss = state.samples; let lo = 0, hi = ss.length - 1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (ss[m].seq < seq) lo = m + 1; else hi = m; }
  return lo;
}
waveCv.addEventListener("mousemove", (e) => { const r = waveCv.getBoundingClientRect(); state.cursorX = e.clientX - r.left; });
waveCv.addEventListener("mouseleave", () => { state.cursorX = null; });
waveCv.addEventListener("click", () => togglePause());
function togglePause() {
  state.paused = !state.paused;
  if (state.paused && state.latest) state.pauseSeq = state.latest.seq;
  $("b-pause").textContent = state.paused ? "▶ 波形再開" : "⏸ 波形停止";
  $("b-pause").classList.toggle("active", state.paused);
}

// ---------------------------------------------------------------------------
// ライン図 (2D)
// ---------------------------------------------------------------------------
function buildLineButtons() {
  const box = $("line-btns"); box.innerHTML = "";
  for (const o of state.cfg.objects) {
    if (o.type === "emitter") {
      const b = el("button", "small", `${o.name}: ワーク投入`);
      b.onclick = () => send({type: "emit", name: o.name}); box.appendChild(b);
    }
  }
  const c = el("button", "small", "ワーク全消去"); c.onclick = () => send({type: "remove_works"}); box.appendChild(c);
}

function drawLine() {
  const sim = state.sim;
  const objs = sim ? sim.objects : [];
  const convs = objs.filter(o => o.type === "conveyor");
  const cyls = objs.filter(o => o.type === "cylinder");
  const axes = objs.filter(o => o.type === "axis");
  const W0 = lineCv.clientWidth || lineCv.parentElement.clientWidth;
  const X0 = 110, PW = W0 - X0 - 20;
  const maxLen = Math.max(1, ...convs.map(c => c.length));
  const sx = PW / maxLen;                                   // mm -> px (X)
  const beltH = (c) => Math.max(26, Math.min(120, (c.width || 300) * sx));
  const rowOf = (c) => beltH(c) + 54;
  const total = convs.reduce((a, c) => a + rowOf(c), 0) + (cyls.length + axes.length) * 64 + 16;
  const [ctx, W, H] = resizeCanvas(lineCv, Math.max(90, total));
  ctx.clearRect(0, 0, W, H);
  if (!sim) { ctx.fillStyle = "#555"; ctx.fillText("ライン: データ待ち", 10, 20); return; }
  const st = sim.stats || {};
  $("h-line").textContent = `ワーク ${sim.works.length}  NG 排出 ${st.ng_ejected ?? 0}/${st.ng_total ?? 0}  誤排出 ${st.ok_ejected ?? 0}  見逃し ${st.ng_passed ?? 0}  t=${sim.t.toFixed(1)}s`;
  ctx.font = "11px system-ui, sans-serif"; ctx.textBaseline = "middle";
  let y = 10;
  for (const c of convs) {
    const bh = beltH(c), sy = bh / (c.width || 300);        // mm -> px (Y)
    const top = y + 30, yc = top + bh / 2, len = c.length * sx;
    const px = (mm) => X0 + mm * sx, py = (mm) => top + mm * sy;
    ctx.fillStyle = "#aab4c3"; ctx.font = "bold 12px system-ui"; ctx.fillText(c.name, 8, yc - 8);
    ctx.fillStyle = "#6b7684"; ctx.font = "10px ui-monospace"; ctx.fillText(`${c.v.toFixed(0)} mm/s${c.next ? " → " + c.next : ""}`, 8, yc + 8);
    ctx.fillText(`W${c.width || 300}`, 8, yc + 20);
    // ベルト
    ctx.fillStyle = c.running ? "#243244" : "#1e242d"; ctx.fillRect(X0, top, len, bh);
    ctx.strokeStyle = c.running ? "#4da3ff" : "#3a4452"; ctx.strokeRect(X0 + .5, top + .5, len, bh);
    if (c.running) {
      ctx.strokeStyle = "rgba(77,163,255,.18)";
      const off = ((sim.t * c.v) % 40) * sx;
      for (let x = X0 + off; x < X0 + len; x += 40 * sx) { ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + bh); ctx.stroke(); }
    }
    ctx.fillStyle = "#55606f"; ctx.font = "9px ui-monospace";
    ctx.fillText("0", X0, top + bh + 8); ctx.fillText(`${c.length}`, X0 + len - 22, top + bh + 8);
    ctx.fillText("Y0", X0 - 14, top + 4); ctx.fillText(`${c.width || 300}`, X0 - 22, top + bh - 4);
    // カメラ視野
    for (const o of objs) {
      if (o.type !== "camera" || o.conv !== c.name) continue;
      const x1 = px(o.at - o.fov / 2), x2 = px(o.at + o.fov / 2);
      ctx.fillStyle = o.flash ? "rgba(120,220,255,.35)" : "rgba(120,220,255,.08)"; ctx.fillRect(x1, top, x2 - x1, bh);
      ctx.setLineDash([4, 3]); ctx.strokeStyle = "#78dcff"; ctx.strokeRect(x1 + .5, top + .5, x2 - x1, bh); ctx.setLineDash([]);
      ctx.strokeStyle = o.done ? "#ffd35c" : "#78dcff"; ctx.beginPath(); ctx.moveTo(px(o.at), top - 6); ctx.lineTo(px(o.at), top + bh + 6); ctx.stroke();
      ctx.fillStyle = "#78dcff"; ctx.font = "10px system-ui"; ctx.textAlign = "center";
      ctx.fillText(`📷 ${o.name} (${o.captures})${o.done ? " DONE" : ""}${o.queued ? " +" + o.queued : ""}`, px(o.at), top - 14);
      ctx.textAlign = "left";
      if (o.last && o.last.length) { ctx.fillStyle = "#ffd35c"; ctx.font = "9px ui-monospace"; ctx.fillText(o.last.map(r => `(${r[0]},${r[1]})`).join(" "), x1, top + bh + 20); }
    }
    // エアブロー
    for (const o of objs) {
      if (o.type !== "airblow" || o.conv !== c.name) continue;
      const n = o.nozzles, pitch = (c.width || 300) / n;
      ctx.fillStyle = "#9aa5b5"; ctx.font = "10px system-ui"; ctx.textAlign = "center";
      ctx.fillText(`${o.name} ×${n} (${o.fired})`, px(o.at), top - 14); ctx.textAlign = "left";
      ctx.strokeStyle = "#3a4452"; ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(px(o.at), top); ctx.lineTo(px(o.at), top + bh); ctx.stroke(); ctx.setLineDash([]);
      for (let i = 0; i < n; i++) {
        const cy = py(pitch * (i + .5));
        const on = o.active[i], puff = o.puff[i];
        ctx.fillStyle = on ? "#ff8c42" : "#55606f";
        ctx.beginPath(); ctx.moveTo(px(o.at) - 7, cy - 4); ctx.lineTo(px(o.at) + 1, cy); ctx.lineTo(px(o.at) - 7, cy + 4); ctx.closePath(); ctx.fill();
        if (on || puff) {
          ctx.fillStyle = on ? "rgba(255,140,66,.45)" : "rgba(255,140,66,.18)";
          ctx.fillRect(px(o.at - o.reach / 2), py(pitch * i) + 1, o.reach * sx, pitch * sy - 2);
        }
        ctx.fillStyle = "#55606f"; ctx.font = "8px ui-monospace"; ctx.fillText(String(i + 1), px(o.at) + 3, cy);
      }
    }
    // センサ・ストッパ・供給
    for (const o of objs) {
      if (o.type === "sensor" && o.conv === c.name) {
        const x = px(o.at);
        ctx.fillStyle = o.on ? "#39d353" : "#3a4452";
        ctx.beginPath(); ctx.moveTo(x, top - 2); ctx.lineTo(x - 6, top - 12); ctx.lineTo(x + 6, top - 12); ctx.closePath(); ctx.fill();
        if (o.on) { ctx.strokeStyle = "rgba(57,211,83,.6)"; ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + bh); ctx.stroke(); }
        ctx.fillStyle = o.on ? "#39d353" : "#7c8695"; ctx.font = "10px system-ui"; ctx.textAlign = "center"; ctx.fillText(o.name, x, top - 18); ctx.textAlign = "left";
      } else if (o.type === "stopper" && o.conv === c.name) {
        const x = px(o.at);
        ctx.fillStyle = o.up ? "#ff5c5c" : "#3a4452";
        ctx.fillRect(x - 2, top + bh * (1 - o.level), 4, bh * o.level);
        ctx.fillStyle = o.up ? "#ff5c5c" : "#7c8695"; ctx.font = "10px system-ui"; ctx.textAlign = "center"; ctx.fillText(o.name, x, top + bh + 16); ctx.textAlign = "left";
      } else if (o.type === "emitter" && o.conv === c.name) {
        const x = px(o.at);
        ctx.fillStyle = "#7c8695"; ctx.beginPath(); ctx.moveTo(x, top - 14); ctx.lineTo(x + 5, top - 4); ctx.lineTo(x - 5, top - 4); ctx.closePath(); ctx.fill();
        ctx.font = "10px system-ui"; ctx.textAlign = "center"; ctx.fillText(`${o.name} (${o.total})`, x + 4, top - 20); ctx.textAlign = "left";
      }
    }
    // ワーク (NG は赤)
    for (const w of sim.works) {
      if (w.conv !== c.name) continue;
      const x1 = Math.max(X0, px(w.pos - w.len)), x2 = Math.min(X0 + len, px(w.pos));
      const ww = (w.w || w.len) * sy, wy = py(w.y ?? (c.width || 300) / 2) - ww / 2;
      ctx.fillStyle = w.ng ? (w.blocked ? "#c43b3b" : "#ff5c5c") : (w.blocked ? "#f5b22b" : "#ffd35c");
      ctx.fillRect(x1, wy, Math.max(2, x2 - x1), ww);
      if (w.rep) { ctx.fillStyle = "#78dcff"; ctx.fillRect(x2 - 4, wy, 3, 3); }
      ctx.fillStyle = "#222"; ctx.font = "9px ui-monospace"; ctx.textAlign = "center"; ctx.fillText(String(w.id), (x1 + x2) / 2, wy + ww / 2); ctx.textAlign = "left";
    }
    y += rowOf(c);
  }
  const ROW = 64;
  for (const c of cyls) {
    const yc = y + ROW / 2;
    ctx.fillStyle = "#aab4c3"; ctx.font = "bold 12px system-ui"; ctx.fillText(c.name, 8, yc - 8);
    ctx.fillStyle = "#6b7684"; ctx.font = "10px ui-monospace"; ctx.fillText(`${c.pos.toFixed(1)} / ${c.stroke} mm`, 8, yc + 8);
    const bodyW = 80, rodMax = Math.min(PW - bodyW - 60, 240);
    ctx.fillStyle = "#2a3a4e"; ctx.fillRect(X0, yc - 10, bodyW, 20); ctx.strokeStyle = "#4da3ff"; ctx.strokeRect(X0 + .5, yc - 10.5, bodyW, 20);
    const rod = rodMax * (c.pos / c.stroke);
    ctx.fillStyle = "#9aa5b5"; ctx.fillRect(X0 + bodyW, yc - 4, rod, 8);
    ctx.fillStyle = "#ffd35c"; ctx.fillRect(X0 + bodyW + rod, yc - 9, 6, 18);
    const led = (x, on, label) => { ctx.fillStyle = on ? "#39d353" : "#3a4452"; ctx.beginPath(); ctx.arc(x, yc, 5, 0, 7); ctx.fill(); ctx.fillStyle = "#7c8695"; ctx.font = "9px system-ui"; ctx.fillText(label, x + 8, yc); };
    led(X0 + bodyW + rodMax + 30, c.home, "HOME"); led(X0 + bodyW + rodMax + 80, c.end, "END");
    y += ROW;
  }
  for (const a of axes) {
    const yc = y + ROW / 2;
    ctx.fillStyle = "#aab4c3"; ctx.font = "bold 12px system-ui"; ctx.fillText(a.name, 8, yc - 8);
    ctx.fillStyle = "#6b7684"; ctx.font = "10px ui-monospace"; ctx.fillText(`${a.pos.toFixed(1)} → ${a.target.toFixed(1)}  ${a.v.toFixed(0)}/s`, 8, yc + 8);
    const lo = a.lo ?? Math.min(0, a.pos, a.target), hi = a.hi ?? Math.max(a.pos, a.target, 1);
    const tw = PW - 80, tx = (v) => X0 + (v - lo) / (hi - lo || 1) * tw;
    ctx.fillStyle = "#232a34"; ctx.fillRect(X0, yc - 3, tw, 6);
    ctx.fillStyle = "#55606f"; ctx.font = "9px ui-monospace"; ctx.fillText(String(lo), X0, yc + 16); ctx.fillText(String(hi), X0 + tw - 20, yc + 16);
    ctx.strokeStyle = "#ff8c42"; ctx.beginPath(); ctx.moveTo(tx(a.target), yc - 12); ctx.lineTo(tx(a.target), yc + 12); ctx.stroke();
    ctx.fillStyle = a.busy ? "#4da3ff" : "#39d353"; ctx.fillRect(tx(a.pos) - 8, yc - 10, 16, 20);
    ctx.fillStyle = "#7c8695"; ctx.font = "9px system-ui"; ctx.fillText(a.busy ? "BUSY" : "INPOS", X0 + tw + 10, yc);
    y += ROW;
  }
}

// ---------------------------------------------------------------------------
// 描画ループ
// ---------------------------------------------------------------------------
function raf() { try { drawWave(); drawLine(); } catch (e) { console.error(e); } requestAnimationFrame(raf); }

// ---------------------------------------------------------------------------
// ボタン
// ---------------------------------------------------------------------------
$("b-pause").onclick = togglePause;
$("sel-win").onchange = (e) => { state.winSec = parseFloat(e.target.value); };
$("b-release").onclick = () => send({type: "release_all"});
$("b-simreset").onclick = () => send({type: "sim_reset"});
$("b-simtoggle").onclick = () => send({type: "sim_enable", value: !(state.stat && state.stat.sim_enabled)});
$("b-wave-clear").onclick = () => { state.wave = []; saveWave(); updateDevices(); };
$("b-ev-clear").onclick = () => { state.events = []; evBox().innerHTML = ""; $("h-ev").textContent = "0"; };
$("ev-words").onchange = (e) => { state.evWords = e.target.checked; };
$("ev-filter").oninput = () => { const evs = state.events.slice(); state.events = []; evBox().innerHTML = ""; for (const e of evs) addEvent(e); };
$("h-raw").onclick = () => { const r = $("raw"); r.classList.toggle("hidden"); $("h-raw").textContent = (r.classList.contains("hidden") ? "▸" : "▾") + " 生データ (全ワード hex)"; };
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.code === "Space") { e.preventDefault(); togglePause(); }
});

connect();
requestAnimationFrame(raf);
