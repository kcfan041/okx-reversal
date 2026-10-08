"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const o = { ...opts, headers: { "x-okxrev": "1", ...(opts.body ? { "content-type": "application/json" } : {}), ...(opts.headers || {}) } };
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });

function toast(msg, bad) {
  const d = document.createElement("div");
  d.textContent = msg;
  if (bad) d.style.borderColor = "var(--down)";
  $("#toast").appendChild(d);
  setTimeout(() => d.remove(), 4000);
}

const fmtN = (v) => (v == null ? "-" : Number(v).toLocaleString());
const pct = (v, d = 1) => (v == null || Number.isNaN(v) ? "-" : `${(v * 100).toFixed(d)}%`);
const pctC = (v, d = 1) => (v == null ? "-" : `<span class="${v < 0 ? "neg" : v > 0 ? "pos" : ""}">${pct(v, d)}</span>`);
function utc(ms, withDate = true) {
  if (ms == null) return "-";
  const s = new Date(ms).toISOString();
  return withDate ? `${s.slice(0, 10)} ${s.slice(11, 16)}` : s.slice(11, 16);
}
function ago(ms) {
  if (ms == null) return "-";
  const m = (Date.now() - ms) / 60000;
  if (m < 1) return "剛剛";
  if (m < 60) return `${m.toFixed(0)} 分鐘前`;
  if (m < 1440) return `${(m / 60).toFixed(1)} 小時前`;
  return `${(m / 1440).toFixed(1)} 天前`;
}
function table(el, cols, rows, opts = {}) {
  const head = `<thead><tr>${cols.map((c, i) => `<th class="${c.cls || ""}" data-i="${i}">${esc(c.t)}</th>`).join("")}</tr></thead>`;
  const body = rows.map((r, ri) => `<tr data-ri="${ri}">${cols.map((c) => `<td class="${c.cls || ""}">${c.f ? c.f(r) : esc(r[c.k])}</td>`).join("")}</tr>`).join("");
  el.innerHTML = head + `<tbody>${body || `<tr><td colspan="${cols.length}" class="muted l">（無資料）</td></tr>`}</tbody>`;
  if (opts.sortable) {
    el.querySelectorAll("th").forEach((th) => th.addEventListener("click", () => {
      const c = cols[+th.dataset.i];
      const key = c.sk || c.k;
      if (!key) return;
      const dir = el.dataset.sk === key && el.dataset.sd === "1" ? -1 : 1;
      el.dataset.sk = key; el.dataset.sd = dir === 1 ? "1" : "-1";
      rows.sort((a, b) => {
        const x = a[key], y = b[key];
        if (x == null) return 1; if (y == null) return -1;
        return (x > y ? 1 : x < y ? -1 : 0) * dir;
      });
      table(el, cols, rows, opts);
    }));
  }
  if (opts.onClick) {
    el.querySelectorAll("tbody tr[data-ri]").forEach((tr) => tr.addEventListener("click", () => {
      el.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel"));
      tr.classList.add("sel");
      opts.onClick(rows[+tr.dataset.ri]);
    }));
  }
}

/* ---------------- tabs ---------------- */
let currentTab = "dash";
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(t) {
  currentTab = t;
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${t}`));
  location.hash = t;
  if (t === "dash") loadDash();
  if (t === "ctrl") loadLog();
  if (t === "bt") { loadResults(); pollJob(); }
  if (t === "ev") loadEvents();
  if (t === "kl") {
    loadInsts();
    resizeCharts();
    if (!$("#kl-inst").value) { $("#kl-inst").value = "BTC-USDT-SWAP"; loadKline(); }
  }
}

/* ---------------- dashboard ---------------- */
async function loadDash() {
  let s;
  try { s = await api("/api/status"); } catch (e) { $("#dash-cards").innerHTML = `<div class="panel neg">${esc(e.message)}</div>`; return; }
  updateHeader(s);
  if (!s.exists) { $("#dash-cards").innerHTML = `<div class="panel">資料庫尚未建立</div>`; return; }
  const lr = s.last_run || {};
  const lag = s.lag_min || {};
  const cards = [
    ["資料庫", `${fmtN(s.db_size_mb)} MB`, `${fmtN(s.candles)} 根 K 線`],
    ["合約", `${s.n_live}`, `上架中；下架 ${s.n_delisted}；無資料 ${s.n_no_data}`],
    ["最新資料延遲", lag.max == null ? "-" : `${lag.max.toFixed(0)} 分`, `中位數 ${lag.median?.toFixed(0) ?? "-"} 分，BTC ${lag.btc?.toFixed(0) ?? "-"} 分`],
    ["上次執行", ago(lr.started_at), lr.started_at ? `${lr.duration_sec?.toFixed(0)} 秒，${fmtN(lr.rows_written)} 列，${lr.note}` : ""],
    ["過去 24 小時", `${fmtN(s.last_24h.rows)} 列`, `${s.last_24h.runs} 次，${fmtN(s.last_24h.requests)} 請求，${s.last_24h.errors} 錯誤`],
  ];
  $("#dash-cards").innerHTML = cards.map(([k, v, sub]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${esc(sub)}</div></div>`).join("");

  const names = ["30 天", "90 天", "180 天", "一年"];
  $("#dash-layers").innerHTML = s.layers.map((L, i) => {
    const p = L.total ? (L.done / L.total) * 100 : 0;
    const cur = s.current_layer === L.layer;
    return `<div>第 ${L.layer} 層（${names[i] || ""}，到 ${utc(L.target).slice(0, 10)}）：${L.done}/${L.total} ${cur ? '<span class="badge warn">進行中</span>' : p >= 100 ? '<span class="badge ok">完成</span>' : ""}</div>
      <div class="bar ${p >= 100 ? "done" : ""}"><div style="width:${p.toFixed(1)}%"></div></div>`;
  }).join("");

  table($("#dash-cov"), [
    { t: "合約", k: "inst_id", cls: "l" },
    { t: "最早", f: (r) => utc(r.oldest_ts) },
    { t: "最新", f: (r) => utc(r.newest_ts) },
    { t: "根數", f: (r) => fmtN(r.bars) },
    { t: "完成", f: (r) => (r.done ? "是" : "否") },
  ], s.least_covered);

  $("#dash-errors").innerHTML = s.errors.length
    ? `<table>${s.errors.slice(0, 30).map((e) => `<tr><td class="l">${esc(e.inst_id)}</td><td class="l neg">${esc(e.error.slice(0, 160))}</td></tr>`).join("")}</table>`
    : '<p class="muted">無</p>';

  loadTaskBox();
  loadRuns();
}

function updateHeader(s) {
  const parts = [];
  if (s.paused) parts.push('<span class="badge bad">已暫停</span>');
  if (s.current_layer) {
    const L = s.layers[s.current_layer - 1];
    parts.push(`<span class="badge">回補第 ${L.layer}/${s.layers.length} 層 ${L.done}/${L.total}</span>`);
  } else if (s.layers) parts.push('<span class="badge ok">回補完成</span>');
  if (s.lag_min) parts.push(`<span class="badge ${s.lag_min.max > 15 ? "warn" : "ok"}">延遲 ${s.lag_min.max.toFixed(0)} 分</span>`);
  $("#hdr-state").innerHTML = parts.join(" ");
}

const TASK_RESULT = { 0: "成功", 267009: "執行中", 267011: "尚未執行", 2147946720: "上次仍在執行，略過", 1: "錯誤" };
async function loadTaskBox() {
  let t;
  try { t = await api("/api/task"); } catch (e) { $("#dash-task").textContent = e.message; return; }
  const state = t.paused ? '<span class="badge bad">已暫停（PAUSE 旗標）</span>' : '<span class="badge ok">運作中</span>';
  const running = t.sync_running ? '<span class="badge warn">同步執行中</span>' : '<span class="badge">閒置</span>';
  const html = t.registered
    ? `<dl class="kv"><div>${state} ${running}</div>
        <div>工作排程器狀態：${esc(t.state)}</div>
        <div>上次觸發：${utc(Date.parse(t.last_run))} UTC（${esc(TASK_RESULT[t.last_result] ?? t.last_result)}）</div>
        <div>下次觸發：${t.next_run ? utc(Date.parse(t.next_run)) + " UTC" : "-"}</div></dl>`
    : `<div>${state} ${running}</div><p class="warn">找不到排程：${esc(t.error || "")}</p>`;
  $("#dash-task").innerHTML = html;
  $("#ctrl-state").innerHTML = `${state} ${running}`;
}

async function loadRuns() {
  const runs = await api("/api/runs?limit=150").catch(() => []);
  const max = Math.max(1, ...runs.map((r) => r.rows_written || 0));
  $("#dash-runchart").innerHTML = runs.slice().reverse().map((r) =>
    `<div class="${r.errors ? "err" : ""}" style="height:${((r.rows_written || 0) / max) * 100}%" title="${utc(r.started_at)}  ${fmtN(r.rows_written)} 列  ${esc(r.note)}"></div>`).join("");
  table($("#dash-runs"), [
    { t: "開始 (UTC)", f: (r) => utc(r.started_at) },
    { t: "秒", f: (r) => r.duration_sec?.toFixed(0) },
    { t: "請求", f: (r) => fmtN(r.requests) },
    { t: "寫入列數", f: (r) => fmtN(r.rows_written) },
    { t: "forward 檔", k: "forward_insts" },
    { t: "回補頁", k: "backfill_pages" },
    { t: "錯誤", f: (r) => (r.errors ? `<span class="neg">${r.errors}</span>` : 0) },
    { t: "備註", k: "note", cls: "l" },
  ], runs.slice(0, 50));
}

$("#btn-gaps").addEventListener("click", async () => {
  $("#dash-gaps").textContent = "計算中…";
  try {
    const s = await api("/api/status?gaps=true");
    const g = s.gaps;
    if (!g.n_insts) { $("#dash-gaps").innerHTML = '<p class="pos">無缺口</p>'; return; }
    $("#dash-gaps").innerHTML = `<p>${fmtN(g.n_gaps)} 段缺口，${g.n_insts} 個合約，共缺 ${g.missing_hours.toFixed(1)} 小時</p>
      <table>${g.top.map((r) => `<tr><td class="l">${esc(r.inst_id)}</td><td>${r.gaps} 段</td><td>${r.missing_hours.toFixed(1)} h</td></tr>`).join("")}</table>`;
  } catch (e) { $("#dash-gaps").innerHTML = `<span class="neg">${esc(e.message)}</span>`; }
});

/* ---------------- control ---------------- */
async function act(path, body) {
  try {
    const r = await post(path, body);
    toast(r.message, !r.ok);
  } catch (e) { toast(e.message, true); }
  setTimeout(() => { loadTaskBox(); loadLog(); }, 800);
}
$("#btn-run").addEventListener("click", () => act("/api/task/run"));
$("#btn-pause").addEventListener("click", () => act("/api/task/pause"));
$("#btn-resume").addEventListener("click", () => act("/api/task/resume"));
$("#btn-only").addEventListener("click", () => {
  const insts = $("#only-insts").value.split(/[\s,]+/).filter(Boolean);
  if (!insts.length) return toast("請輸入合約", true);
  act("/api/sync/only", { insts });
});

function renderLog(el, lines) {
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.innerHTML = lines.map((l) => {
    const cls = /ERROR|Traceback/.test(l) ? "le" : /WARNING/.test(l) ? "lw" : "";
    return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l);
  }).join("\n");
  if (atBottom) el.scrollTop = el.scrollHeight;
}
async function loadLog() {
  const r = await api("/api/log?name=sync&lines=300").catch(() => ({ lines: [] }));
  renderLog($("#sync-log"), r.lines);
}

/* ---------------- backtest ---------------- */
let jobWasRunning = false;
$("#btn-bt").addEventListener("click", async () => {
  const body = {
    since: $("#bt-since").value || null,
    until: $("#bt-until").value || null,
    sweep: $("#bt-sweep").checked,
    charts: $("#bt-charts").checked,
    insts: $("#bt-insts").value.split(/[\s,]+/).filter(Boolean),
  };
  if (!body.insts.length) body.insts = null;
  try { const r = await post("/api/backtest", body); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
  pollJob();
});
$("#btn-bt-stop").addEventListener("click", async () => {
  try { const r = await post("/api/backtest/stop"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
});

async function pollJob() {
  const j = await api("/api/backtest/job").catch(() => null);
  if (!j) return;
  renderLog($("#bt-log"), j.log.length ? j.log : [j.returncode == null ? "（尚未執行）" : `（結束，代碼 ${j.returncode}）`]);
  $("#btn-bt").disabled = j.running;
  if (jobWasRunning && !j.running) {
    toast(j.returncode === 0 ? "回測完成" : `回測結束，代碼 ${j.returncode}`, j.returncode !== 0);
    loadResults();
  }
  jobWasRunning = j.running;
}

const MODES = ["red_only", "pump_red", "pump_red_btcweak"];
const BASES = ["random_pump", "random_any"];
async function loadResults() {
  let s;
  try { s = await api("/api/results/summary"); } catch (e) { $("#bt-meta").textContent = e.message; return; }
  $("#bt-meta").textContent = `產生於 ${s.generated_at}，耗時 ${s.elapsed_sec} 秒；區間 ${s.range.since || "開始"} ～ ${s.range.until || "最新"}`;
  const c = s.data_coverage;
  $("#bt-coverage").textContent = `資料：${c.n_insts} 合約（測試 ${c.n_insts_tested}），${c.first_ts} ～ ${c.last_ts}，最早時間中位數 ${c.median_oldest_ts}，回補完成 ${c.backfill_done}`;
  const rows = [...MODES.map((m) => ({ name: m, ...s.modes[m] })), ...BASES.map((b) => ({ name: b, base: true, ...s.baselines[b] }))];
  const h24 = (r) => (r.horizons || {})["24h"] || {};
  table($("#bt-main"), [
    { t: "", f: (r) => (r.base ? `<span class="muted">${r.name}</span>` : `<b>${r.name}</b>`) },
    { t: "n", f: (r) => fmtN(h24(r).n ?? 0) },
    { t: "合約", f: (r) => h24(r).n_insts ?? 0 },
    { t: "天數", f: (r) => h24(r).n_days ?? 0 },
    { t: "24h 平均", f: (r) => pctC(h24(r).mean) },
    { t: "24h 中位", f: (r) => pctC(h24(r).median) },
    { t: "下跌率", f: (r) => pct(h24(r).bear_rate) },
    { t: "跌>20%", f: (r) => pct(r.big_drop_rate_24h) },
    { t: "再創新高", f: (r) => pct(r.new_high_rate_24h) },
    { t: "MAE 平均", f: (r) => pct(r.mae_mean) },
    { t: "MAE p90", f: (r) => pct(r.mae_p90) },
    { t: "MFE 平均", f: (r) => pct(r.mfe_mean) },
    { t: "做空淨報酬", f: (r) => pctC(h24(r).short_pnl_net_mean, 2) },
    { t: "平均 CI95", f: (r) => (h24(r).mean_ci95 ? `[${pct(h24(r).mean_ci95[0])}, ${pct(h24(r).mean_ci95[1])}]` : "-") },
  ], rows);

  const hz = Object.keys(s.config.metrics.horizons);
  table($("#bt-hz"), [
    { t: "", f: (r) => r.name },
    ...hz.map((h) => ({ t: h, f: (r) => { const x = (r.horizons || {})[h]; return x ? `${pctC(x.mean)} <span class="muted">(${x.n})</span>` : "-"; } })),
  ], rows);

  const pr = s.modes.pump_red || {};
  const reg = Object.entries(pr.by_btc_regime || {}).map(([k, v]) => ({ name: k, ...v }));
  table($("#bt-regime"), [
    { t: "BTC 環境", k: "name", cls: "l" },
    { t: "n", f: (r) => h24(r).n ?? 0 },
    { t: "24h 平均", f: (r) => pctC(h24(r).mean) },
    { t: "下跌率", f: (r) => pct(h24(r).bear_rate) },
    { t: "MAE", f: (r) => pct(r.mae_mean) },
  ], reg);
  const mon = Object.entries(pr.by_month || {}).map(([k, v]) => ({ m: k, ...v }));
  table($("#bt-month"), [
    { t: "月份", k: "m", cls: "l" },
    { t: "事件數", k: "n" },
    { t: "24h 平均", f: (r) => pctC(r.fwd_ret_24h_mean) },
  ], mon);

  const sw = await api("/api/results/sweep").catch(() => []);
  table($("#bt-sweep-t"), [
    { t: "紅K ≤", f: (r) => pct(r.red_ret_max, 0) },
    { t: "量能 ≥", f: (r) => `${r.red_vol_ratio_min}×` },
    { t: "n", k: "n" }, { t: "合約", k: "n_insts" }, { t: "天數", k: "n_days" },
    { t: "24h 平均", f: (r) => pctC(r.fwd_ret_24h_mean) },
    { t: "24h 中位", f: (r) => pctC(r.fwd_ret_24h_median) },
    { t: "下跌率", f: (r) => pct(r.bear_rate_24h) },
    { t: "MAE", f: (r) => pct(r.mae_mean) },
    { t: "做空淨報酬", f: (r) => pctC(r.short_pnl_net_24h_mean, 2) },
    { t: "CI95", f: (r) => (r.ci95_lo == null ? "-" : `[${pct(r.ci95_lo)}, ${pct(r.ci95_hi)}]`) },
  ], sw);
  $("#bt-caveats").innerHTML = (s.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("");
}

/* ---------------- events ---------------- */
let chartFiles = new Set();
$("#btn-ev").addEventListener("click", loadEvents);
$("#ev-inst").addEventListener("keydown", (e) => { if (e.key === "Enter") loadEvents(); });
$("#ev-mode").addEventListener("change", loadEvents);
$("#ev-regime").addEventListener("change", loadEvents);

function chartName(ev) {
  const d = new Date(ev.event_ts).toISOString();
  return `${ev.inst_id}_${d.slice(0, 4)}${d.slice(5, 7)}${d.slice(8, 10)}_${d.slice(11, 13)}${d.slice(14, 16)}.png`;
}
async function loadEvents() {
  const q = new URLSearchParams();
  if ($("#ev-mode").value) q.set("mode", $("#ev-mode").value);
  if ($("#ev-regime").value) q.set("regime", $("#ev-regime").value);
  if ($("#ev-inst").value) q.set("inst", $("#ev-inst").value);
  const r = await api(`/api/results/events?${q}`).catch((e) => { toast(e.message, true); return null; });
  if (!r) return;
  chartFiles = new Set(r.charts);
  $("#ev-count").textContent = `${r.total} 筆${r.total > r.events.length ? `（顯示前 ${r.events.length}）` : ""}`;
  table($("#ev-table"), [
    { t: "時間 (UTC)", sk: "event_ts", f: (e) => utc(e.event_ts) },
    { t: "合約", k: "inst_id", cls: "l" },
    { t: "模式", k: "mode", cls: "l" },
    { t: "紅K", k: "red_ret_prev_close", f: (e) => pctC(e.red_ret_prev_close) },
    { t: "量倍", k: "red_vol_ratio", f: (e) => e.red_vol_ratio?.toFixed(1) },
    { t: "前24h", k: "ret_24h_pre", f: (e) => pctC(e.ret_24h_pre) },
    { t: "BTC", k: "btc_regime" },
    { t: "1h", k: "fwd_ret_1h", f: (e) => pctC(e.fwd_ret_1h) },
    { t: "24h", k: "fwd_ret_24h", f: (e) => pctC(e.fwd_ret_24h) },
    { t: "MAE", k: "mae_24h", f: (e) => pct(e.mae_24h) },
    { t: "MFE", k: "mfe_24h", f: (e) => pct(e.mfe_24h) },
    { t: "新高", k: "new_high_24h", f: (e) => (e.new_high_24h == null ? "-" : e.new_high_24h ? "是" : "否") },
    { t: "圖", f: (e) => (chartFiles.has(chartName(e)) ? "有" : "") },
  ], r.events, { sortable: true, onClick: showEvent });
}
function showEvent(e) {
  const fields = [
    ["合約", e.inst_id], ["時間", `${utc(e.event_ts)} UTC`], ["模式", e.mode],
    ["前 1h / 6h / 24h", `${pct(e.ret_1h_pre)} / ${pct(e.ret_6h_pre)} / ${pct(e.ret_24h_pre)}${e.is_new_listing ? "（新上市）" : ""}`],
    ["紅K（前收 / 實體）", `${pct(e.red_ret_prev_close)} / ${pct(e.red_ret_body)}`],
    ["量能倍數（紅K / pump 最大）", `${e.red_vol_ratio?.toFixed(1)}× / ${e.pump_vol_ratio_max?.toFixed(1)}×`],
    ["BTC", `${e.btc_regime}，1h ${pct(e.btc_ret_1h, 2)}，4h ${pct(e.btc_ret_4h, 2)}，低於 EMA：${e.btc_below_ema}`],
    ["進場價", e.entry_price],
    ["報酬 30m / 1h / 6h / 24h / 3d", ["30m", "1h", "6h", "24h", "3d"].map((h) => pct(e[`fwd_ret_${h}`])).join(" / ")],
    ["MAE / MFE", `${pct(e.mae_24h)} / ${pct(e.mfe_24h)}（MAE 先發生：${e.mae_before_mfe}，${e.bars_to_mfe ?? "-"} 根到 MFE）`],
    ["24h 再創新高", e.new_high_24h],
    ["做空淨報酬 24h", pct(e.short_pnl_net_24h, 2)],
  ];
  const name = chartName(e);
  $("#ev-detail").innerHTML = `<dl>${fields.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    <div class="row" style="margin-top:10px"><button id="ev-to-kl" class="primary">在 K 線檢視開啟</button></div>
    ${chartFiles.has(name) ? `<img src="/charts/${encodeURIComponent(name)}" alt="">` : '<p class="muted">此事件沒有圖（回測時勾選「產生事件圖」，僅 pump_red）</p>'}`;
  $("#ev-to-kl").addEventListener("click", () => openKline(e.inst_id, e.event_ts - 6 * 3600e3, e.event_ts + 48 * 3600e3));
}

/* ---------------- kline ---------------- */
let charts = null;
function makeCharts() {
  if (charts) return charts;
  const base = {
    layout: { background: { color: "#171d24" }, textColor: "#d6dde4" },
    grid: { vertLines: { color: "#222b35" }, horzLines: { color: "#222b35" } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#2a323c" },
    rightPriceScale: { borderColor: "#2a323c", minimumWidth: 80 },
    crosshair: { mode: 0 },
  };
  const p = LightweightCharts.createChart($("#kl-price"), base);
  const v = LightweightCharts.createChart($("#kl-vol"), base);
  const b = LightweightCharts.createChart($("#kl-btc-chart"), base);
  const cs = p.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350", borderVisible: false, wickUpColor: "#26a69a", wickDownColor: "#ef5350",
    priceFormat: { type: "custom", formatter: (x) => x.toPrecision(6), minMove: 1e-9 } });
  const vs = v.addHistogramSeries({ priceFormat: { type: "volume" } });
  const vma = v.addLineSeries({ color: "#bbbbbb", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
  const bs = b.addLineSeries({ color: "#f57c00", lineWidth: 1 });
  const all = [p, v, b];
  let syncing = false;
  all.forEach((c) => c.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (syncing || !r) return;
    syncing = true;
    all.forEach((o) => { if (o !== c) o.timeScale().setVisibleLogicalRange(r); });
    syncing = false;
  }));
  charts = { p, v, b, cs, vs, vma, bs };
  return charts;
}
function resizeCharts() {
  if (!charts) return;
  [[charts.p, "#kl-price"], [charts.v, "#kl-vol"], [charts.b, "#kl-btc-chart"]].forEach(([c, s]) => {
    const el = $(s); c.resize(el.clientWidth, el.clientHeight);
  });
}
window.addEventListener("resize", resizeCharts);

let instsLoaded = false;
async function loadInsts() {
  if (instsLoaded) return;
  const list = await api("/api/instruments").catch(() => []);
  $("#kl-insts").innerHTML = list.map((i) => `<option value="${esc(i.inst_id)}">`).join("");
  instsLoaded = true;
}
const toLocalInput = (ms) => new Date(ms).toISOString().slice(0, 16);
const fromLocalInput = (s) => (s ? Date.parse(s + ":00Z") : null);

function openKline(inst, start, end) {
  $("#kl-inst").value = inst;
  $("#kl-start").value = toLocalInput(start);
  $("#kl-end").value = toLocalInput(end);
  showTab("kl");
  loadKline();
}
$("#btn-kl").addEventListener("click", loadKline);
$("#kl-inst").addEventListener("keydown", (e) => { if (e.key === "Enter") loadKline(); });
$("#kl-span").addEventListener("change", () => {
  const end = fromLocalInput($("#kl-end").value) || Date.now();
  $("#kl-end").value = toLocalInput(end);
  $("#kl-start").value = toLocalInput(end - +$("#kl-span").value * 86400e3);
});

async function loadKline() {
  const inst = $("#kl-inst").value.trim().toUpperCase();
  if (!inst) return toast("請輸入合約", true);
  const q = new URLSearchParams({ inst, btc: $("#kl-btc").checked && inst !== "BTC-USDT-SWAP" });
  const st = fromLocalInput($("#kl-start").value), en = fromLocalInput($("#kl-end").value);
  if (st) q.set("start", st);
  if (en) q.set("end", en);
  $("#kl-info").textContent = "載入中…";
  let d;
  try { d = await api(`/api/candles?${q}`); } catch (e) { $("#kl-info").textContent = ""; return toast(e.message, true); }
  const c = makeCharts();
  resizeCharts();
  const candles = [], vol = [], vma = [];
  for (let i = 0; i < d.t.length; i++) {
    candles.push({ time: d.t[i], open: d.o[i], high: d.h[i], low: d.l[i], close: d.c[i] });
    vol.push({ time: d.t[i], value: d.v[i] ?? 0, color: d.c[i] >= d.o[i] ? "rgba(38,166,154,.6)" : "rgba(239,83,80,.6)" });
    if (d.vma[i] != null) vma.push({ time: d.t[i], value: d.vma[i] });
  }
  c.cs.setData(candles);
  c.vs.setData(vol);
  c.vma.setData(vma);
  $("#kl-btc-chart").style.display = d.btc ? "" : "none";
  if (d.btc) {
    const pts = [];
    for (let i = 0; i < d.btc.t.length; i++) if (d.btc.c[i] != null) pts.push({ time: d.btc.t[i], value: d.btc.c[i] });
    c.bs.setData(pts);
  } else c.bs.setData([]);
  const marks = (d.events || []).map((e) => ({
    time: Math.floor(e.event_ts / 1000), position: "aboveBar", shape: "arrowDown",
    color: e.mode === "pump_red" ? "#ffcc00" : e.mode === "pump_red_btcweak" ? "#ff6f00" : "#9e9e9e",
    text: `${e.mode} ${pct(e.red_ret_prev_close)}`,
  })).sort((a, b) => a.time - b.time);
  const seen = new Set();
  c.cs.setMarkers(marks.filter((m) => !seen.has(m.time) && seen.add(m.time)));
  c.p.timeScale().fitContent();
  $("#kl-start").value = toLocalInput(d.start);
  $("#kl-end").value = toLocalInput(d.end);
  $("#kl-info").textContent = `${d.t.length.toLocaleString()} 根${(d.events || []).length ? `，${d.events.length} 個事件` : ""}`;
}

/* ---------------- polling ---------------- */
setInterval(() => {
  if (document.hidden) return;
  if (currentTab === "dash") loadDash();
  if (currentTab === "ctrl" && $("#log-follow").checked) { loadLog(); loadTaskBox(); }
}, 15000);
setInterval(() => {
  if (document.hidden) return;
  if (currentTab === "ctrl" && $("#log-follow").checked) loadLog();
  if (currentTab === "bt") pollJob();
}, 3000);

showTab((location.hash || "#dash").slice(1) || "dash");
