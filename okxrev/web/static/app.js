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
  el.classList.toggle("sortable", !!opts.sortable);
  const sortCls = (c) => {
    if (!opts.sortable || !(c.sk || c.k)) return "";
    if (el.dataset.sk !== (c.sk || c.k)) return " can-sort";
    return el.dataset.sd === "1" ? " can-sort sorted-asc" : " can-sort sorted-desc";
  };
  const head = `<thead><tr>${cols.map((c, i) => `<th class="${c.cls || ""}${sortCls(c)}" data-i="${i}">${esc(c.t)}</th>`).join("")}</tr></thead>`;
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
  if (!document.getElementById(`tab-${t}`)) t = "dash";
  currentTab = t;
  document.querySelectorAll("nav button").forEach((b) => {
    const on = b.dataset.tab === t;
    b.classList.toggle("active", on);
    if (on) { b.setAttribute("aria-current", "page"); $("#page-title").textContent = b.textContent; }
    else b.removeAttribute("aria-current");
  });
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${t}`));
  if (location.hash.slice(1) !== t) location.hash = t;
  window.scrollTo({ top: 0 });
  if (t === "lab") labSteps();
  if (t === "dash") loadDash();
  if (t === "ctrl") loadLog();
  if (t === "bt") { loadResults(); pollJob(); }
  if (t === "ev") loadEvents();
  if (t === "ac") { loadAccount(); pollAcctJob(); }
  if (t === "dc") { loadDiscover(); pollDiscJob(); loadScan(); }
  if (t === "pp") { loadPaper(); pollPaperJobs(); }
  if (t === "ht") { loadHeat(); loadHeatValidation(); pollHeatJob(); }
  if (t === "ai") { loadInsts(); aiInit(); }
  if (t === "lab") { loadInsts(); labInit(); }
  if (t === "cp") { loadCampaigns(); loadCampaignResults(); pollCampJob(); }
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

  const cv = s.coverage || {};
  const names = ["30 天", "90 天", "180 天", "一年"];
  const layerHtml = s.current_layer ? s.layers.map((L, i) => {
    const p = L.total ? (L.done / L.total) * 100 : 0;
    return `<div>第 ${L.layer} 層（${names[i] || ""}，到 ${utc(L.target).slice(0, 10)}）：${L.done}/${L.total}</div>
      <div class="bar ${p >= 100 ? "done" : ""}"><div style="width:${p.toFixed(1)}%"></div></div>`;
  }).join("") : "";
  const old = cv.old;
  const fr = (s.freshness || []).map((f) => {
    const bad = f.late > Math.max(2, f.n * 0.1);
    return `<tr><td class="l">${esc(f.label)}</td><td>${f.n}</td><td class="${bad ? "warn" : ""}">${num(f.median_h, 1)} h</td><td>${num(f.max_h, 1)} h</td>
      <td class="muted">${f.key === "funding" ? "依各幣結算間隔" : `每 ${f.expect_h >= 1 ? num(f.expect_h, 0) + " h" : Math.round(f.expect_h * 60) + " 分"}`}</td>
      <td class="${bad ? "warn" : "muted"}">${f.late ? `${f.late} 個逾期` : "-"}</td></tr>`;
  }).join("");
  $("#dash-layers").innerHTML = `
    <dl class="kv">
      <div>主資料：${cv.main_from ? `${utc(cv.main_from).slice(0, 10)} ～ ${utc(cv.main_to)}，${cv.n_insts} 個合約（${cv.n_full} 個從 ${utc(cv.target).slice(0, 10)} 起完整，${cv.n_from_listing} 個從上市起完整）` : "-"}
        ${cv.n_incomplete ? `<span class="badge warn">${cv.n_incomplete} 個回補中</span>` : '<span class="badge ok">歷史完整</span>'}</div>
      <div>舊資料（盲測用）：${old && old.from ? `${utc(old.from).slice(0, 10)} ～ ${utc(old.to).slice(0, 10)}，${old.n_insts} 個合約` : old && old.busy ? "忙碌中" : "無"}</div>
    </dl>${layerHtml}
    <h3>衍生品資料新鮮度 <small class="muted">（最新一筆距今；黃色＝超過預期間隔的幣太多）</small></h3>
    <table><thead><tr><th class="l">資料</th><th>幣數</th><th>中位數</th><th>最舊</th><th>預期</th><th>逾期</th></tr></thead><tbody>${fr || '<tr><td colspan="6" class="muted">（無資料）</td></tr>'}</tbody></table>`;

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
  if (s.current_layer) {
    const L = s.layers[s.current_layer - 1];
    parts.push(`<span class="badge">回補第 ${L.layer}/${s.layers.length} 層 ${L.done}/${L.total}</span>`);
  }
  const late = (s.freshness || []).filter((f) => f.late > Math.max(2, f.n * 0.1)).map((f) => f.label);
  if (late.length) parts.push(`<span class="badge warn" title="${esc(late.join("、"))}">衍生品落後 ${late.length} 項</span>`);
  if (s.lag_min) parts.push(`<span class="badge ${s.lag_min.max > 15 ? "warn" : "ok"}">延遲 ${s.lag_min.max.toFixed(0)} 分</span>`);
  $("#hdr-state").innerHTML = parts.join(" ");
}

const TASK_RESULT = { 0: "成功", 267009: "執行中", 267011: "尚未執行", 2147946720: "上次仍在執行，略過", 1: "錯誤" };
async function loadTaskBox() {
  let t;
  try { t = await api("/api/task"); } catch (e) { $("#dash-task").textContent = e.message; return; }
  if (t.collect) renderCollect({ ...t.collect, task: { registered: t.registered, state: t.state } });
  const state = t.paused ? '<span class="badge bad">資料收集已暫停</span>' : '<span class="badge ok">運作中</span>';
  const running = t.sync_running ? '<span class="badge warn">同步執行中</span>' : '<span class="badge">閒置</span>';
  const html = t.registered
    ? `<dl class="kv"><div>${state} ${running}</div>
        <div>工作排程器狀態：${esc(t.state)}</div>
        <div>上次觸���：${utc(Date.parse(t.last_run))} UTC（${esc(TASK_RESULT[t.last_result] ?? t.last_result)}）</div>
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
async function confirmable(path, label) {
  try {
    let r = await post(path);
    if (r.needs_confirm) {
      if (!confirm(r.message)) return null;
      r = await post(path + (path.includes("?") ? "&" : "?") + "force=true");
    }
    toast(r.message, !r.ok);
    return r;
  } catch (e) { toast(e.message, true); return null; }
}
$("#btn-run").addEventListener("click", async () => { await confirmable("/api/task/run"); setTimeout(() => { loadTaskBox(); loadLog(); }, 800); });
$("#btn-pause").addEventListener("click", () => collectSet("pause"));
$("#btn-resume").addEventListener("click", () => collectSet("resume"));

/* ---------------- data collection switch ---------------- */
let collectState = null;
function hoursText(h) { return h < 1 ? `${Math.round(h * 60)} 分鐘` : h < 48 ? `${h.toFixed(1)} 小時` : `${(h / 24).toFixed(1)} 天`; }
function renderCollect(s) {
  collectState = s;
  const b = $("#collect-sw");
  if (!s) { b.textContent = "資料收集：?"; return; }
  const taskOff = s.task && s.task.state === "Disabled";
  b.classList.toggle("off", !!s.paused || !!taskOff);
  b.textContent = s.paused ? `資料收集：已暫停（${hoursText(s.hours)}）` : taskOff ? "資料收集：排程已停用" : "資料收集：執行中";
  const lost = (s.lost || []).map((f) => `${f.name}（約 ${hoursText(f.missing_h)} 無法補回）`).join("、");
  const offHint = taskOff ? '<br><span class="warn">Windows 排程目前是停用狀態，資料不會收集；請以系統管理員身分開 PowerShell 執行 <code>Enable-ScheduledTask -TaskName okx-reversal-sync</code></span>' : "";
  const html = (s.paused
    ? `<span class="badge bad">已暫停</span> 自 ${utc(s.since)} UTC，已 ${hoursText(s.hours)}${lost ? `<br>恢復後無法補回：${esc(lost)}` : ""}`
    : `<span class="badge ok">執行中</span>`) + offHint;
  if ($("#ctrl-collect")) $("#ctrl-collect").innerHTML = html;
}
async function loadCollect() {
  try { renderCollect(await api("/api/collect")); } catch { renderCollect(null); }
}
async function collectSet(op) {
  if (op === "pause" && !confirm("暫停資料收集？\n同步、紙上交易、過熱更新與提醒都會停止；網頁的即時查詢不受影響。")) return;
  if (op === "resume" && collectState && collectState.paused) {
    const lost = (collectState.lost || []).map((f) => f.name).join("、");
    if (!confirm(`恢復資料收集？已停 ${hoursText(collectState.hours)}，下一次同步會補回可回補的資料${lost ? `；${lost} 有一段無法補回` : ""}。`)) return;
  }
  try {
    const r = await post(op === "pause" ? "/api/collect/pause" : "/api/collect/resume");
    toast(r.message, !r.ok);
    renderCollect(r);
  } catch (e) { toast(e.message, true); }
  setTimeout(() => { loadCollect(); loadTaskBox(); }, 800);
}
$("#collect-sw").addEventListener("click", () => {
  if (!collectState) return;
  collectSet(collectState.paused ? "resume" : "pause");
});
loadCollect();
setInterval(() => { if (!document.hidden) loadCollect(); }, 30000);
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
    { t: "24h 平���", f: (r) => pctC(r.fwd_ret_24h_mean) },
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

let tradeMarks = [];
let tradeLines = [];
let priceLines = [];
function openKline(inst, start, end, marks, lines) {
  tradeMarks = marks || [];
  tradeLines = lines || [];
  $("#kl-inst").value = inst;
  $("#kl-start").value = toLocalInput(start);
  $("#kl-end").value = toLocalInput(end);
  showTab("kl");
  loadKline();
}
$("#btn-kl").addEventListener("click", loadKline);
$("#btn-kl-lab").addEventListener("click", () => {
  const inst = $("#kl-inst").value.trim().toUpperCase();
  if (!inst || !charts) return;
  const r = charts.p.timeScale().getVisibleRange();
  if (!r) return;
  openLab(inst, Math.round((r.from + r.to) * 500));
});
$("#kl-inst").addEventListener("keydown", (e) => { if (e.key === "Enter") loadKline(); });
$("#kl-span").addEventListener("change", () => {
  const end = fromLocalInput($("#kl-end").value) || Date.now();
  $("#kl-end").value = toLocalInput(end);
  $("#kl-start").value = toLocalInput(end - +$("#kl-span").value * 86400e3);
  loadKline();
});
["#kl-start", "#kl-end"].forEach((s) => $(s).addEventListener("keydown", (e) => { if (e.key === "Enter") loadKline(); }));
$("#kl-btc").addEventListener("change", () => { if ($("#kl-inst").value.trim()) loadKline(); });

let klSeq = 0, klTimer = null;
function klOverlay(html) {
  let o = $("#kl-overlay");
  if (!o) {
    o = document.createElement("div");
    o.id = "kl-overlay";
    o.className = "chart-overlay";
    $("#kl-price").parentElement.style.position = "relative";
    $("#kl-price").parentElement.appendChild(o);
  }
  o.style.display = html ? "" : "none";
  if (html) o.innerHTML = html;
  return o;
}
function klBusy(on, t0) {
  const b = $("#btn-kl");
  b.disabled = on;
  b.textContent = on ? "載入中…" : "載入";
  clearInterval(klTimer);
  if (!on) return;
  const tick = () => {
    const s = (performance.now() - t0) / 1000;
    klOverlay(`<div class="spin"></div><div>載入中${s >= 1 ? `… ${s.toFixed(0)} 秒` : "…"}</div>${s >= 2 ? '<div class="muted">可能正在等同步寫入資料庫（最多約 10 秒）</div>' : ""}`);
  };
  tick();
  klTimer = setInterval(tick, 250);
}

async function loadKline() {
  const inst = $("#kl-inst").value.trim().toUpperCase();
  if (!inst) return toast("請輸入合約", true);
  const q = new URLSearchParams({ inst, btc: $("#kl-btc").checked && inst !== "BTC-USDT-SWAP" });
  const st = fromLocalInput($("#kl-start").value), en = fromLocalInput($("#kl-end").value);
  if (st) q.set("start", st);
  if (en) q.set("end", en);
  const seq = ++klSeq;
  const t0 = performance.now();
  $("#kl-info").textContent = "";
  klBusy(true, t0);
  let d;
  try { d = await api(`/api/candles?${q}`); } catch (e) {
    if (seq !== klSeq) return;
    klBusy(false);
    const busy = /忙碌|busy|503/i.test(e.message);
    klOverlay(`<div class="neg">載入失敗：${esc(e.message)}</div>${busy ? '<div class="muted">資料庫正在被同步寫入，稍後再試即可</div>' : ""}<button class="small" id="kl-retry">重試</button>`);
    $("#kl-retry").addEventListener("click", loadKline);
    return;
  }
  if (seq !== klSeq) return;
  klBusy(false);
  klOverlay(null);
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
  }));
  for (const cpn of d.campaigns || []) {
    const add = (ms, text, color, shape) => {
      if (ms != null && ms >= d.start && ms < d.end) marks.push({ time: Math.floor(ms / 300000) * 300, position: "belowBar", shape, color, text });
    };
    add(cpn.p_time, `${cpn.kind}公告`, "#4aa3ff", "circle");
    add(cpn.start_ts, `${cpn.kind}開始`, "#4aa3ff", "arrowUp");
    add(cpn.end_ts, `${cpn.kind}結束`, "#f0a020", "arrowUp");
    add(cpn.dist_ts, "發獎勵", "#f0a020", "circle");
  }
  if (tradeMarks.length && tradeMarks[0].inst === inst) marks.push(...tradeMarks.map(({ inst: _, ...m }) => m));
  marks.sort((a, b) => a.time - b.time);
  const seen = new Set();
  c.cs.setMarkers(marks.filter((m) => !seen.has(m.time) && seen.add(m.time)));
  priceLines.forEach((l) => c.cs.removePriceLine(l));
  priceLines = tradeLines.filter((l) => l.inst === inst).map((l) => c.cs.createPriceLine({ price: l.price, color: l.color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: l.title }));
  c.p.timeScale().fitContent();
  $("#kl-start").value = toLocalInput(d.start);
  $("#kl-end").value = toLocalInput(d.end);
  const secs = (performance.now() - t0) / 1000;
  const days = (d.end - d.start) / 86400e3;
  const info = $("#kl-info");
  info.textContent = `${inst}，${days >= 1 ? num(days, days < 10 ? 1 : 0) + " 天" : Math.round(days * 24) + " 小時"}，${d.t.length.toLocaleString()} 根${(d.events || []).length ? `，${d.events.length} 個事件` : ""}${(d.campaigns || []).length ? `，${d.campaigns.length} 檔瓜分活動` : ""}；${secs.toFixed(1)} 秒，${new Date().toLocaleTimeString()} 更新`;
  if (!d.t.length) klOverlay('<div class="muted">這段時間沒有 K 線資料</div>');
  info.classList.remove("flash");
  void info.offsetWidth;
  info.classList.add("flash");
}

/* ---------------- account ---------------- */
const usd = (v, d = 2) => (v == null || Number.isNaN(v) ? "-" : `<span class="${v < 0 ? "neg" : v > 0 ? "pos" : ""}">${v > 0 ? "+" : ""}${Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}</span>`);
const num = (v, d = 2) => (v == null || Number.isNaN(v) ? "-" : Number(v).toFixed(d));
const mins = (m) => (m == null ? "-" : m < 60 ? `${m.toFixed(0)}m` : m < 1440 ? `${(m / 60).toFixed(1)}h` : `${(m / 1440).toFixed(1)}d`);
const SIDE = { long: "多", short: "空" };
const VERDICT = { "可複製": "ok", "證據不足": "warn", "無效": "bad" };
let acCharts = null;
let acctWasRunning = false;

function acQuery() {
  const q = new URLSearchParams();
  for (const [k, id] of [["since", "#ac-since"], ["until", "#ac-until"], ["inst_type", "#ac-type"], ["side", "#ac-side"], ["inst", "#ac-inst"], ["source", "#ac-source"]]) {
    if ($(id).value) q.set(k, $(id).value);
  }
  return q;
}

async function acctRun(sync) {
  const ls = $("#ac-log"); ls.style.display = "";
  try { const r = await post("/api/account/run", { sync, replicate: $("#ac-rep").checked }); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
  pollAcctJob();
}
$("#btn-ac-run").addEventListener("click", () => acctRun(true));
$("#btn-ac-analyze").addEventListener("click", () => acctRun(false));
$("#btn-ac-stop").addEventListener("click", async () => {
  try { const r = await post("/api/account/stop"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
});
$("#btn-ac-filter").addEventListener("click", () => loadAccount(true));
$("#ac-source").addEventListener("change", () => loadAccount(true));
$("#ac-inst").addEventListener("keydown", (e) => { if (e.key === "Enter") loadAccount(true); });

async function pollAcctJob() {
  const j = await api("/api/account/job").catch(() => null);
  if (!j) return;
  if (j.running || j.log.length) { $("#ac-log").style.display = ""; renderLog($("#ac-log"), j.log); }
  $("#btn-ac-run").disabled = $("#btn-ac-analyze").disabled = j.running;
  if (acctWasRunning && !j.running) {
    toast(j.returncode === 0 ? "戰績分析完成" : `分析結束，代碼 ${j.returncode}`, j.returncode !== 0);
    loadAccount();
  }
  acctWasRunning = j.running;
}

function acMakeCharts() {
  if (acCharts) return acCharts;
  const base = {
    layout: { background: { color: "#171d24" }, textColor: "#d6dde4" },
    grid: { vertLines: { color: "#222b35" }, horzLines: { color: "#222b35" } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#2a323c" },
    rightPriceScale: { borderColor: "#2a323c", minimumWidth: 80 },
  };
  const e = LightweightCharts.createChart($("#ac-equity"), base);
  const d = LightweightCharts.createChart($("#ac-daily"), base);
  acCharts = { e, d, es: e.addAreaSeries({ lineColor: "#4aa3ff", topColor: "rgba(74,163,255,.3)", bottomColor: "rgba(74,163,255,0)", lineWidth: 2 }),
    ds: d.addHistogramSeries({}) };
  return acCharts;
}

async function loadAccount(filterOnly) {
  const st = await api("/api/account/status").catch((e) => ({ error: e.message }));
  const parts = [];
  if (st.error) parts.push(`<span class="neg">${esc(st.error)}</span>`);
  else {
    parts.push(st.credentials ? '<span class="badge ok">API 金鑰已設定</span>' : '<span class="badge bad">未設定 API 金鑰（.env）</span>');
    if (st.exists) parts.push(`合約平倉紀錄 ${fmtN(st.positions)} 筆、現貨成交 ${fmtN(st.fills)} 筆、機器人 ${fmtN(st.bots ?? 0)} 個、跟單倉位 ${fmtN(st.copy_positions ?? 0)} 筆`);
    else parts.push("尚未同步");
  }
  $("#ac-state").innerHTML = parts.join("　");

  const q = acQuery();
  let s;
  try { s = await api(`/api/account/summary?${q}`); } catch (e) { $("#ac-cards").innerHTML = `<div class="panel neg">${esc(e.message)}</div>`; return; }
  const o = s.overall || { n: 0 };
  if (!o.n) {
    $("#ac-cards").innerHTML = '<div class="panel">尚無交易資料。設定 .env 後按「同步並分析」。</div>';
  } else {
    const cards = [
      ["總損益", usd(o.pnl), `${o.n} 筆（有計價 ${o.n_priced}）${o.unrealized ? `；含未實現 ${num(o.unrealized)}（運行中 ${o.n_open}）` : ""}`],
      ["勝率", pct(o.win_rate), `平均賺 ${num(o.avg_win)} / 平均賠 ${num(o.avg_loss)}`],
      ["盈虧比 / 獲利因子", `${num(o.payoff)} / ${num(o.profit_factor)}`, `每筆期望值 ${num(o.expectancy)}`],
      ["最大回撤", usd(o.max_drawdown), `最佳 ${num(o.best)}，最差 ${num(o.worst)}`],
      ["手續費 / 資金費", `${num(o.fees)} / ${num(o.funding)}`, `強平 ${o.liquidations} 次`],
      ["持倉中位數", mins(o.median_hold_min), `${utc(s.first_ts).slice(0, 10)} ～ ${utc(s.last_ts).slice(0, 10)}`],
    ];
    $("#ac-cards").innerHTML = cards.map(([k, v, sub]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${esc(sub)}</div></div>`).join("");
    $("#ac-src-cards").innerHTML = (s.by_source || []).length > 1 || $("#ac-source").value === ""
      ? (s.by_source || []).map((r) => `<div class="card"><div class="k">${esc(r.label)}</div><div class="v">${usd(r.pnl)}</div>
          <div class="s">${r.n} ${r.key === "bot" ? "個" : "筆"}，勝率 ${pct(r.win_rate, 0)}，手續費 ${num(r.fees)}${r.unrealized ? `，含未實現 ${num(r.unrealized)}` : ""}</div></div>`).join("")
      : "";
    const c = acMakeCharts();
    [[c.e, "#ac-equity"], [c.d, "#ac-daily"]].forEach(([ch, sel]) => ch.resize($(sel).clientWidth, $(sel).clientHeight));
    const pts = []; let last = -Infinity;
    s.equity.t.forEach((t, i) => { const tt = t <= last ? last + 1 : t; last = tt; pts.push({ time: tt, value: s.equity.v[i] }); });
    c.es.setData(pts);
    c.ds.setData(s.daily.map((d) => ({ time: d.day, value: d.pnl, color: d.pnl >= 0 ? "rgba(38,166,154,.8)" : "rgba(239,83,80,.8)" })));
    c.e.timeScale().fitContent(); c.d.timeScale().fitContent();
  }
  const grpCols = [
    { t: "", k: "key", cls: "l", f: (r) => esc(SIDE[r.key] || r.key) },
    { t: "筆數", k: "n" }, { t: "損益", k: "pnl", f: (r) => usd(r.pnl) }, { t: "勝率", k: "win_rate", f: (r) => pct(r.win_rate) },
    { t: "盈虧比", k: "payoff", f: (r) => num(r.payoff) }, { t: "PF", k: "profit_factor", f: (r) => num(r.profit_factor) },
    { t: "手續費", k: "fees", f: (r) => num(r.fees) }, { t: "持倉中位", k: "median_hold_min", f: (r) => mins(r.median_hold_min) },
  ];
  table($("#ac-month"), [{ t: "月份", k: "month", cls: "l" }, { t: "筆數", k: "n" }, { t: "損益", f: (r) => usd(r.pnl) },
    { t: "勝率", f: (r) => pct(r.win_rate) }, { t: "PF", f: (r) => num(r.profit_factor) }, { t: "回撤", f: (r) => usd(r.max_drawdown) }], s.monthly || []);
  table($("#ac-side-t"), grpCols, [...(s.by_side || []), ...(s.by_type || [])]);
  table($("#ac-inst-t"), grpCols, s.by_inst || [], { sortable: true });

  if (!filterOnly) { loadReview(); loadExitPlan(); loadPositions(); loadBots(); }
  loadTrades(q);
}

const GROUP = { long: "做多", short: "做空", all: "多空合併" };
const UNIT = { pct: "固定百分比", atr: "ATR 倍數" };
async function loadExitPlan() {
  const p = await api("/api/account/exit-plan").catch(() => null);
  const el = $("#ac-exit-plan");
  if (!p || !p.groups) { el.innerHTML = `<p class="muted">${esc((p && p.note) || "尚未執行出場最佳化。按「同步並分析」或「只重新分析」。")}</p>`; return; }
  let html = `<p class="muted">${p.n_trades} 筆可模擬；驗證分段 A ${p.folds[0].join(" ～ ")}（${p.fold_sizes[0]} 筆）／B ${p.folds[1].join(" ～ ")}（${p.fold_sizes[1]} 筆）。
    「樣本外」= 用 A 找參數測 B、用 B 找參數測 A 的平均。</p>`;
  for (const [g, r] of Object.entries(p.groups)) {
    const a = r.actual;
    const rows = [{ name: "你實際", ...a, text: `持倉中位數 ${mins(a.median_hold_min)}` },
      ...Object.entries(r.units).map(([u, x]) => ({ name: `${u === r.chosen_unit ? "★ " : ""}${UNIT[u]}`, ...x }))];
    html += `<h3>${GROUP[g]}（${r.n} 筆，進場時 ATR 中位數 ${pct(r.atr_median, 2)}）</h3><table>
      <thead><tr><th class="l">方式</th><th class="l">規則</th><th>每筆平均</th><th>勝率</th><th>合計 USD</th><th>A 段</th><th>B 段</th><th>樣本外</th><th>平均持倉</th></tr></thead><tbody>
      ${rows.map((x) => `<tr><td class="l">${esc(x.name)}</td><td class="l wrap">${esc(x.text)}</td><td>${pctC(x.mean_ret, 2)}</td><td>${pct(x.win_rate, 0)}</td>
        <td>${usd(x.usd)}</td><td>${pctC(x.fold_means[0], 2)}</td><td>${pctC(x.fold_means[1], 2)}</td><td>${x.oos_mean == null ? "-" : pctC(x.oos_mean, 2)}</td>
        <td>${x.avg_hold_min == null ? "-" : mins(x.avg_hold_min)}${x.liquidations ? `<div class="warn">原槓桿會強平 ${x.liquidations} 筆</div>` : ""}</td></tr>`).join("")}</tbody></table>
      <details><summary class="muted">各出場方式最佳組合與固定持有時間</summary>
      ${Object.entries(r.units).map(([u, x]) => `<table><thead><tr><th class="l">${UNIT[u]}</th><th class="l">規則</th><th>每筆</th><th>A</th><th>B</th></tr></thead><tbody>
        ${Object.entries(x.best_by_family).map(([f, b]) => `<tr><td class="l">${esc(f)}</td><td class="l wrap">${esc(b.text)}</td><td>${pctC(b.mean_ret, 2)}</td><td>${pctC(b.fold_means[0], 2)}</td><td>${pctC(b.fold_means[1], 2)}</td></tr>`).join("")}
        </tbody></table>`).join("")}
      <table><thead><tr><th class="l">固定持有</th>${Object.keys(r.hold_curve).map((k) => `<th>${k}</th>`).join("")}</tr></thead>
        <tbody><tr><td class="l">每筆淨報酬</td>${Object.values(r.hold_curve).map((v) => `<td>${pctC(v.mean, 2)}</td>`).join("")}</tr></tbody></table></details>`;
  }
  const cd = p.cooldown || {};
  const cdLine = (k, name) => {
    const rec = (cd[k] || {}).recommended;
    return rec ? `${name}：建議 <b>${rec.minutes} 分鐘</b>內不開新單（過去 ${rec.skipped} 筆，勝率 ${pct(rec.skipped_win_rate, 0)}，可少虧 ${num(rec.saved_usd)} USD，兩段時間都成立）`
      : `${name}：資料不支持設定冷卻時間`;
  };
  html += `<h3>兩筆交易間隔</h3><p>${cdLine("after_loss", "虧損後")}<br>${cdLine("after_win", "獲利後")}</p>
    <div class="scroll"><table id="ac-cd-t"></table></div>
    <ul class="muted">${(p.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`;
  el.innerHTML = html;
  table($("#ac-cd-t"), [
    { t: "上一筆", f: (x) => (x.prev === "loss" ? '<span class="neg">虧損</span>' : '<span class="pos">獲利</span>') },
    { t: "間隔", k: "bucket" }, { t: "筆數", k: "n" }, { t: "合計", f: (x) => usd(x.sum) }, { t: "每筆", f: (x) => usd(x.mean) }, { t: "勝率", f: (x) => pct(x.win_rate, 0) },
  ], cd.table || []);
}

async function loadBots() {
  const b = await api("/api/account/bots").catch(() => null);
  const st = await api("/api/account/status").catch(() => ({}));
  const leaders = st.copy_leaders || [];
  $("#ac-copy").innerHTML = leaders.length
    ? `跟單：${leaders.map((l) => `${esc(l.nick_name)}（${utc(l.begin_time).slice(0, 10)} 起，投入 ${num(l.copy_total_amt)}，損益 ${usd(l.copy_total_pnl)}）`).join("、")}；跟單倉位 ${st.copy_positions || 0} 筆`
    : "";
  if (!b || !b.n) { $("#ac-bot-find").innerHTML = '<p class="muted">沒有機器人紀錄（或尚未分析）</p>'; table($("#ac-bots"), [], []); return; }
  const pf = b.portfolio && b.portfolio.running;
  const d = pf && pf.detail;
  $("#ac-portfolio").innerHTML = d ? `<h3>智能組合（運行中，自 ${utc(pf.start).slice(0, 10)} 起有紀錄）</h3>
    <p>目前市值 ${num(d.value_now)} USD（含組合內 USDT ${num(d.cash_usdt)}）；已實現 ${usd(d.realized)}、
      <b>未實現 ${usd(d.unrealized)}</b> <span class="badge">未實現</span>；合計 ${usd(pf.pnl)}</p>
    <table><thead><tr><th class="l">幣</th><th>數量</th><th>成本</th><th>現價</th><th>市值</th><th>已實現</th><th>未實現</th><th>調倉次數</th></tr></thead><tbody>
    ${d.coins.map((c) => `<tr><td class="l">${esc(c.ccy)}</td><td>${Number(c.qty_end).toPrecision(5)}</td><td>${Number(c.avg_cost).toPrecision(5)}</td>
      <td>${Number(c.px_now).toPrecision(5)}</td><td>${num(c.value_now)}</td><td>${usd(c.realized)}</td><td>${usd(c.unrealized)}</td><td>${c.n_orders}</td></tr>`).join("")}
    </tbody></table><p class="muted">${esc(d.cost_note)}</p>` : "";
  $("#ac-bot-find").innerHTML = `<p>${b.n} 個機器人（運行中 ${b.n_running || 0}），合計 ${usd(b.pnl)} USD（其中未實現 ${usd(b.unrealized)}），勝率 ${pct(b.win_rate, 0)}；網格套利收益 ${usd(b.grid_profit)}、方向／浮動損益 ${usd(b.float_profit)}、手續費 ${num(b.fees)}、資金費 ${num(b.funding)}</p>`
    + (b.findings || []).map((f) => `<div class="issue" style="border-color:var(--warn)"><b>${esc(f.title)}</b><div>${esc(f.text)}</div></div>`).join("");
  const DIR = { long: "多", short: "空" };
  table($("#ac-bots"), [
    { t: "啟動 (UTC)", sk: "start", f: (x) => utc(x.start) },
    { t: "合約", k: "inst_id", cls: "l" },
    { t: "類型", k: "type_label" },
    { t: "方向", f: (x) => `<span class="${x.direction === "short" ? "neg" : "pos"}">${DIR[x.direction] || "-"}</span> ${num(x.lever, 0)}x` },
    { t: "投入", k: "investment", f: (x) => num(x.investment, 0) },
    { t: "運行", k: "hours", f: (x) => mins(x.hours * 60) },
    { t: "區間", f: (x) => (x.min_px ? `${Number(x.min_px).toPrecision(4)}–${Number(x.max_px).toPrecision(4)} / ${x.grid_num}格` : "-") },
    { t: "期間價格", k: "mkt_move", f: (x) => pctC(x.mkt_move) },
    { t: "區間內", k: "mkt_in_range_rate", f: (x) => pct(x.mkt_in_range_rate, 0) },
    { t: "跑出區間", f: (x) => (x.mkt_broke ? `<span class="warn">${x.mkt_broke === "down" ? "跌破" : "突破"}${x.mkt_first_exit_min != null ? ` ${mins(x.mkt_first_exit_min)}` : ""}</span>` : "-") },
    { t: "網格收益", k: "grid_profit", f: (x) => usd(x.grid_profit) },
    { t: "總損益", k: "pnl", f: (x) => usd(x.pnl) },
    { t: "報酬率", k: "pnl_ratio", f: (x) => pctC(x.pnl_ratio) },
    { t: "停止後6h", k: "post_after_6h", f: (x) => pctC(x.post_after_6h) },
    { t: "停止", f: (x) => (x.state !== "stopped" ? '<span class="badge ok">運行中</span>' : esc(x.stop_reason || "")) },
  ], b.bots, {
    sortable: true,
    onClick: (x) => {
      if (x.inst_id.includes("/")) return toast("組合機器人含多個幣，請看上方明細", true);
      const inst = /-SWAP$/.test(x.inst_id) ? x.inst_id : `${x.inst_id.split("-")[0]}-USDT-SWAP`;
      const pad = Math.max(3 * 3600e3, (x.end - x.start) * 0.3);
      const lines = x.min_px ? [[x.max_px, "#9e9e9e", "上界"], [x.min_px, "#9e9e9e", "下界"]].map(([price, color, title]) => ({ inst, price, color, title })) : [];
      openKline(inst, x.start - pad, x.end + pad, [
        { inst, time: Math.floor(x.start / 300000) * 300, position: "belowBar", shape: "arrowUp", color: "#4aa3ff", text: `啟動 ${x.type_label}` },
        { inst, time: Math.floor(x.end / 300000) * 300, position: "aboveBar", shape: "circle", color: x.pnl >= 0 ? "#26a69a" : "#ef5350", text: `停止 ${num(x.pnl)}` },
      ], lines);
    },
  });
  const g = [{ t: "", k: "key", cls: "l", f: (x) => esc(DIR[x.direction] || x.direction || x.hold_bucket || "-") },
    { t: "個數", k: "n" }, { t: "損益", f: (x) => usd(x.pnl) }, { t: "平均", f: (x) => usd(x.mean) }, { t: "勝率", f: (x) => pct(x.win_rate, 0) }];
  table($("#ac-bot-dir"), g, b.by_direction || []);
  table($("#ac-bot-hold"), g, b.by_hold || []);
}

let posLoading = false;
async function loadPositions(refresh) {
  if (posLoading) return;
  posLoading = true;
  let r;
  try { r = await api(`/api/account/positions${refresh ? "?refresh=true" : ""}`); }
  catch (e) { $("#ac-cool").innerHTML = `<p class="muted">${esc(e.message)}</p>`; table($("#ac-pos"), [], []); posLoading = false; return; }
  posLoading = false;
  const cd = r.cooldown;
  $("#ac-cool").innerHTML = !r.ok ? `<p class="muted">${esc(r.message)}</p>` : cd && cd.active
    ? `<p class="warn">冷卻中：上一筆 ${esc(cd.last_inst)} ${usd(cd.last_pnl)} USD，建議 ${utc(cd.until_ts)} UTC（${new Date(cd.until_ts).toLocaleTimeString()} 本地）之後再開新單</p>`
    : cd && cd.minutes ? `<p class="muted">冷卻：上一筆 ${esc(cd.last_inst)} ${usd(cd.last_pnl)}，已過建議冷卻時間</p>` : "";
  const P = (v) => (v == null ? "-" : Number(v).toPrecision(6));
  const rb = r.running_bots || [];
  $("#ac-run-bots-note").textContent = (r.running_bots_errors || []).length ? `部分查詢失敗：${r.running_bots_errors.join("、")}` : "";
  table($("#ac-run-bots"), [
    { t: "合約", k: "inst_id", cls: "l" }, { t: "類型", k: "type_label" },
    { t: "方向", f: (x) => (x.direction ? `<span class="${x.direction === "short" ? "neg" : "pos"}">${SIDE[x.direction] || esc(x.direction)}</span> ${num(x.lever, 0)}x` : "-") },
    { t: "投入", f: (x) => num(x.investment) },
    { t: "總損益", f: (x) => `${usd(x.total_pnl)} <span class="muted">${pct(x.pnl_ratio, 1)}</span>` },
    { t: "網格／浮動", f: (x) => (x.grid_profit == null ? "-" : `${usd(x.grid_profit)}／${usd(x.float_profit)}`) },
    { t: "區間", f: (x) => (x.min_px ? `${P(x.min_px)} – ${P(x.max_px)}` : "-") },
    { t: "現價", f: (x) => `${P(x.px_now)}${x.in_range ? ` <span class="badge ${x.in_range === "區間內" ? "ok" : "warn"}">${esc(x.in_range)}</span>` : ""}` },
    { t: "持倉均價／張數", f: (x) => (x.pos_avg_px ? `${P(x.pos_avg_px)}／${fmtN(x.pos_size)}` : "-") },
    { t: "強平（距離）", f: (x) => (x.liq_px ? `<span class="neg">${P(x.liq_px)}</span> <span class="muted">${pct(x.liq_dist, 0)}</span>` : "-") },
    { t: "運行", f: (x) => (x.hours == null ? "-" : mins(x.hours * 60)) },
  ], rb, { onClick: (x) => openKline(x.inst_id.endsWith("-SWAP") ? x.inst_id : `${x.inst_id}-SWAP`, x.start - 6 * 3600e3, Date.now(), [],
    [x.min_px && { inst: x.inst_id, price: x.min_px, color: "#4aa3ff", title: "網格下緣" }, x.max_px && { inst: x.inst_id, price: x.max_px, color: "#4aa3ff", title: "網格上緣" },
     x.pos_avg_px && { inst: x.inst_id, price: x.pos_avg_px, color: "#bbbbbb", title: "持倉均價" }, x.liq_px && { inst: x.inst_id, price: x.liq_px, color: "#ff00aa", title: "強平" }].filter(Boolean)) });
  table($("#ac-bot-pos"), [
    { t: "幣", k: "ccy", cls: "l" }, { t: "機器人", k: "bot" },
    { t: "數量", f: (x) => Number(x.qty).toPrecision(6) }, { t: "現價", f: (x) => P(x.px_now) },
    { t: "市值", f: (x) => num(x.value_usd) }, { t: "成本", f: (x) => P(x.avg_cost) },
    { t: "未實現", f: (x) => `${usd(x.unrealized)} <span class="badge">未實現</span>` }, { t: "報酬", f: (x) => pctC(x.ret) },
    { t: "最早紀錄", f: (x) => (x.since ? utc(x.since).slice(0, 10) : "-") },
  ], r.bot_holdings || []);
  table($("#ac-pos"), [
    { t: "合約", k: "inst_id", cls: "l" },
    { t: "方向", f: (x) => `<span class="${x.side === "short" ? "neg" : "pos"}">${SIDE[x.side]}</span> ${num(x.lever, 0)}x` },
    { t: "進場", f: (x) => P(x.entry_px) },
    { t: "現價", f: (x) => P(x.mark_px) },
    { t: "浮動", f: (x) => pctC(x.cur_ret, 2) },
    { t: "最高浮盈", f: (x) => pct(x.peak_ret, 2) },
    { t: "止損", f: (x) => `<span class="neg">${P(x.sl_px)}</span>` },
    { t: "目前有效停損", f: (x) => `<b>${P(x.active_stop_px)}</b>${x.be_active ? ' <span class="badge ok">保本</span>' : ""}${x.trail_active ? ' <span class="badge ok">移動</span>' : ""}` },
    { t: "止盈", f: (x) => `<span class="pos">${P(x.tp_px)}</span>` },
    { t: "分批 / 移動啟動", f: (x) => `${P(x.partial_px)} / ${P(x.trail_act_px)}` },
    { t: "強平價", f: (x) => P(x.liq_px) },
    { t: "建議出場時間", f: (x) => (x.deadline_ts ? `${utc(x.deadline_ts)}<br><span class="${x.minutes_left < 0 ? "neg" : "muted"}">${x.minutes_left < 0 ? "已超過" : "剩"} ${mins(Math.abs(x.minutes_left))}</span>` : "-") },
    { t: "規則 / 提醒", cls: "l wrap", f: (x) => `${esc(x.error || x.rule_text || "")}${(x.warnings || []).map((w) => `<div class="warn">! ${esc(w)}</div>`).join("")}` },
  ], r.positions, {
    onClick: (x) => {
      const inst = /-SWAP$/.test(x.inst_id) ? x.inst_id : `${x.inst_id.split("-")[0]}-USDT-SWAP`;
      const t0 = x.open_ts || Date.now() - 6 * 3600e3;
      const lines = [[x.tp_px, "#26a69a", "止盈"], [x.active_stop_px, "#ef5350", "停損"], [x.entry_px, "#4aa3ff", "進場"], [x.liq_px, "#ff6f00", "強平"]]
        .filter(([p]) => p != null).map(([price, color, title]) => ({ inst, price, color, title }));
      openKline(inst, t0 - 6 * 3600e3, Date.now() + 3600e3,
        [{ inst, time: Math.floor(t0 / 300000) * 300, position: x.side === "short" ? "aboveBar" : "belowBar", shape: x.side === "short" ? "arrowDown" : "arrowUp", color: "#4aa3ff", text: "進場" }], lines);
    },
  });
}
$("#btn-ac-pos").addEventListener("click", () => loadPositions(true));

async function loadReview() {
  const r = await api("/api/account/review").catch(() => ({}));
  const rv = r.review;
  if (!rv || !rv.n_trades) {
    $("#ac-issues").innerHTML = '<p class="muted">尚未分析。按「同步並分析」或「只重新分析」。</p>';
    return;
  }
  const st = await api("/api/account/status").catch(() => ({}));
  $("#ac-rv-time").textContent = st.review_mtime ? new Date(st.review_mtime * 1000).toLocaleString() : "-";
  $("#ac-issues").innerHTML = rv.issues.length
    ? rv.issues.map((it, i) => `<div class="issue"><div><b>${i + 1}. ${esc(it.title)}</b> <span class="badge bad">影響約 ${num(it.impact_usd)} USD</span></div>
        <div>${esc(it.finding)}</div><div class="muted">建議：${esc(it.advice)}</div></div>`).join("")
    : '<p class="pos">沒有偵測到明顯的操作問題</p>';
  table($("#ac-cf"), [
    { t: "規則", k: "rule", cls: "l" }, { t: "n", k: "n" }, { t: "實際", f: (x) => usd(x.actual_usd) },
    { t: "試算", f: (x) => usd(x.cf_usd) }, { t: "差異", f: (x) => usd(x.delta_usd) }, { t: "試算勝率", f: (x) => pct(x.cf_win_rate) },
  ], rv.counterfactuals || []);
  table($("#ac-good"), [
    { t: "情境", k: "label", cls: "l" }, { t: "n", k: "n" }, { t: "損益", f: (x) => usd(x.pnl) },
    { t: "勝率", f: (x) => pct(x.win_rate) }, { t: "平均報酬", f: (x) => pctC(x.avg_ret, 2) },
  ], rv.strengths || []);
  const ex = rv.exit || {};
  const w = ex.winner || {}, l = ex.loser || {};
  $("#ac-exit").innerHTML = `<table>
    <tr><th class="l"></th><th>最大浮盈中位</th><th>最大浮虧中位</th><th>吃到浮盈</th><th>持倉中位</th><th>出場後 6h</th></tr>
    <tr><td>獲利單</td><td>${pct(w.mfe_med)}</td><td>${pct(w.mae_med)}</td><td>${pct(w.capture_med, 0)}</td><td>${mins(w.hold_min_med)}</td><td>${pctC(w.post_6h_med)}</td></tr>
    <tr><td>虧損單</td><td>${pct(l.mfe_med)}</td><td>${pct(l.mae_med)}</td><td>-</td><td>${mins(l.hold_min_med)}</td><td>${pctC(l.post_6h_med)}</td></tr>
    </table><p class="muted">虧損單中曾經浮盈 &gt; 2% 的比例：${pct(l.was_green_rate)}；「出場後 6h」為正代表出場後價格繼續往你原本的方向走</p>`;
  table($("#ac-lever"), [{ t: "槓桿", k: "b", cls: "l" }, { t: "筆數", k: "count" }, { t: "損益", f: (x) => usd(x.sum) }, { t: "每筆", f: (x) => usd(x.mean) }], (rv.risk || {}).lever_buckets || []);
  table($("#ac-hold"), [{ t: "持倉", k: "b", cls: "l" }, { t: "筆數", k: "count" }, { t: "損益", f: (x) => usd(x.sum) }, { t: "每筆", f: (x) => usd(x.mean) }], (rv.behavior || {}).hold_buckets || []);
  $("#ac-caveats").innerHTML = (rv.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("");

  const rp = r.replicate;
  if (!rp) { $("#ac-rep-note").textContent = "尚未執行可複製性掃描（勾選後按分析）"; table($("#ac-rep-t"), [], []); return; }
  $("#ac-rep-note").textContent = rp.note || (rp.in_sample_def ? `樣本內＝${rp.in_sample_def}。` : "") + "判定：樣本外 ≥ 30 次、95% 信賴區間下界 > 0、優於隨機基準、且多數月份為正 → 可複製";
  table($("#ac-rep-t"), [
    { t: "判定", f: (x) => `<span class="badge ${VERDICT[x.verdict] || ""}">${esc(x.verdict)}</span>` },
    { t: "情境", k: "label", cls: "l" },
    { t: "你的成績", f: (x) => `${x.your_n} 筆 ${usd(x.your_pnl)} ${pct(x.your_win_rate, 0)}` },
    { t: "模擬出場", f: (x) => `${mins(x.hold_bars * 5)} / 停損 ${pct(x.stop)}` },
    { t: "樣本內", f: (x) => `${pctC(x.in_sample.mean, 2)} <span class="muted">(${x.in_sample.n})</span>` },
    { t: "樣本外", f: (x) => `${pctC(x.out_of_sample.mean, 2)} <span class="muted">(${x.out_of_sample.n})</span>` },
    { t: "樣本外勝率", f: (x) => pct(x.out_of_sample.win_rate) },
    { t: "樣本外 CI95", f: (x) => (x.out_of_sample.ci95 ? `[${pct(x.out_of_sample.ci95[0], 2)}, ${pct(x.out_of_sample.ci95[1], 2)}]` : "-") },
    { t: "隨機基準", f: (x) => pctC(x.baseline.mean, 2) },
    { t: "正報酬月份", f: (x) => pct(x.oos_months_positive, 0) },
    { t: "BTC 強/中/弱", f: (x) => ["strong", "neutral", "weak"].map((k) => pct((x.by_btc_regime[k] || {}).mean, 1)).join(" / ") },
  ], rp.rules || []);
}

async function loadTrades(q) {
  const r = await api(`/api/account/trades?${q}`).catch(() => ({ total: 0, trades: [] }));
  $("#ac-count").textContent = `${r.total} 筆${r.total > r.trades.length ? `（顯示前 ${r.trades.length}）` : ""}；點選開啟 K 線`;
  table($("#ac-trades"), [
    { t: "平倉 (UTC)", sk: "close_ts", f: (x) => utc(x.close_ts) },
    { t: "合約", k: "inst_id", cls: "l" },
    { t: "來源", k: "source", f: (x) => (x.source === "bot" ? '<span class="badge warn">機器人</span>' : x.source === "copy" ? '<span class="badge">跟單</span>' : "手動") },
    { t: "方向", k: "side", f: (x) => `<span class="${x.side === "short" ? "neg" : "pos"}">${SIDE[x.side] || "-"}</span>` },
    { t: "持倉", k: "hold_min", f: (x) => mins(x.hold_min) },
    { t: "槓桿", k: "lever", f: (x) => num(x.lever, 0) },
    { t: "名目", k: "notional_usd", f: (x) => num(x.notional_usd, 0) },
    { t: "報酬", k: "ret_net", f: (x) => pctC(x.ret_net, 2) },
    { t: "損益", k: "pnl_usd", f: (x) => `${usd(x.pnl_usd)}${x.is_open ? ' <span class="badge">未實現</span>' : ""}` },
    { t: "MAE", k: "mae", f: (x) => pct(x.mae) },
    { t: "MFE", k: "mfe", f: (x) => pct(x.mfe) },
    { t: "吃到", k: "capture", f: (x) => pct(x.capture, 0) },
    { t: "出場後6h", k: "post_72", f: (x) => pctC(x.post_72) },
    { t: "前24h", k: "f_r24h", f: (x) => pctC(x.f_r24h) },
    { t: "BTC", k: "f_btc_regime" },
    { t: "類型", k: "close_type" },
    { t: "標記", f: (x) => [x.revenge ? "報復" : "", x.loss_streak_before >= 2 ? `連虧${x.loss_streak_before}` : "", x.candle_proxy ? "代理K" : ""].filter(Boolean).join(" ") },
  ], r.trades, {
    sortable: true,
    onClick: (x) => {
      const inst = x.candle_inst || x.inst_id;
      if (!/-USDT-SWAP$/.test(inst)) return toast("此商品沒有對應的 USDT 永續 K 線", true);
      const pad = Math.max(6 * 3600e3, (x.close_ts - x.open_ts) * 0.5);
      const up = x.side !== "short";
      openKline(inst, x.open_ts - pad, x.close_ts + pad, [
        { inst, time: Math.floor(x.open_ts / 300000) * 300, position: up ? "belowBar" : "aboveBar", shape: up ? "arrowUp" : "arrowDown", color: "#4aa3ff", text: `開${SIDE[x.side] || ""} ${x.entry_px}` },
        { inst, time: Math.floor(x.close_ts / 300000) * 300, position: up ? "aboveBar" : "belowBar", shape: "circle", color: x.pnl_usd >= 0 ? "#26a69a" : "#ef5350", text: `平 ${x.exit_px} (${(x.pnl_usd ?? 0).toFixed(2)})` },
      ]);
    },
  });
}

/* ---------------- discover ---------------- */
const FAMILY = { pump_reversal: "暴漲後反轉做空", dip_rebound: "大跌後反彈", breakout: "追漲突破", btc_follow: "順 BTC 方向", new_listing: "新幣上市" };
const VERD = { "可用": "ok", "觀察中": "warn", "無效": "bad" };
let discData = null;
let discWasRunning = false;

$("#btn-dc-run").addEventListener("click", async () => {
  try { const r = await post("/api/discover/run"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
  pollDiscJob();
});
$("#btn-dc-stop").addEventListener("click", async () => {
  try { const r = await post("/api/discover/stop"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
});
$("#btn-dc-scan").addEventListener("click", () => loadScan(true));
["#dc-verdict", "#dc-family", "#dc-h"].forEach((s) => $(s).addEventListener("change", renderDiscover));

async function pollDiscJob() {
  const j = await api("/api/discover/job").catch(() => null);
  if (!j) return;
  if (j.running || j.log.length) { $("#dc-log").style.display = ""; renderLog($("#dc-log"), j.log); }
  $("#btn-dc-run").disabled = j.running;
  if (discWasRunning && !j.running) { toast(j.returncode === 0 ? "探索完成" : `探索結束，代碼 ${j.returncode}`, j.returncode !== 0); loadDiscover(); loadScan(true); }
  discWasRunning = j.running;
}

async function loadDiscover() {
  discData = await api("/api/discover/results").catch(() => null);
  if (!discData) { $("#dc-meta").textContent = "尚未執行進場探索"; return; }
  const d = discData;
  $("#dc-meta").textContent = `${d.generated_at}；${d.n_rules} 條規則 × ${d.n_exits} 種出場，${d.n_insts} 個合約，${d.range[0]} ～ ${d.range[1]}；耗時 ${Math.round(d.elapsed_sec / 60)} 分`;
  const fams = [...new Set(d.results.map((r) => r.family))];
  $("#dc-family").innerHTML = '<option value="">全部策略</option>' + fams.map((f) => `<option value="${f}">${FAMILY[f] || f}</option>`).join("");
  const hs = [...new Set(d.results.map((r) => r.hgroup))];
  $("#dc-h").innerHTML = '<option value="">全部持有時間</option>' + hs.map((h) => `<option>${esc(h)}</option>`).join("");
  const cnt = d.results.reduce((a, r) => ((a[r.verdict] = (a[r.verdict] || 0) + 1), a), {});
  $("#dc-summary").innerHTML = `<p>切分：訓練 &lt; ${esc(d.split.valid_start)} ≤ 驗證 &lt; ${esc(d.split.holdout_start)} ≤ 保留。
    共測 ${fmtN(d.n_tested)} 個規則×出場組合，各策略／持有時間挑訓練段前 5 名，共 ${d.results.length} 條進入驗證：
    ${Object.entries(cnt).map(([k, v]) => `<span class="badge ${VERD[k]}">${k} ${v}</span>`).join(" ")}。
    洗牌基準（純運氣能達到的訓練 t 值 95 分位）＝ ${num(d.null.p95)}</p>`;
  $("#dc-caveats").innerHTML = (d.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("");
  renderDiscover();
  renderSecondary(d.secondary || []);
  renderGrid(d.grid);
}

function renderDiscover() {
  if (!discData) return;
  const v = $("#dc-verdict").value, f = $("#dc-family").value, h = $("#dc-h").value;
  const rows = discData.results.filter((r) => (!v || r.verdict === v) && (!f || r.family === f) && (!h || r.hgroup === h));
  const dm = (s) => (s && s.n ? `${pctC(s.day_mean, 2)} <span class="muted">(${s.n})</span>` : "-");
  table($("#dc-table"), [
    { t: "判定", f: (r) => `<span class="badge ${VERD[r.verdict]}">${r.verdict}</span>` },
    { t: "策略", f: (r) => esc(FAMILY[r.family] || r.family) },
    { t: "規則", k: "label", cls: "l wrap" },
    { t: "出場", k: "exit" },
    { t: "訓練", f: (r) => `${dm(r.train)} t=${num(r.train_score, 1)}` },
    { t: "驗證", f: (r) => dm(r.valid) },
    { t: "隨機", f: (r) => pctC(r.base_valid, 2) },
    { t: "保留", f: (r) => dm(r.holdout) },
    { t: "隨機", f: (r) => pctC(r.base_hold, 2) },
    { t: "正月份", f: (r) => pct(r.months_pos, 0) },
  ], rows, { onClick: showRule });
}

function showRule(r) {
  const s = (x, name) => `<tr><td class="l">${name}</td><td>${x.n || 0}</td><td>${x.n_days || 0}</td><td>${pctC(x.day_mean, 2)}</td><td>${pct(x.win_rate, 0)}</td>
    <td>${x.ci95 ? `[${pct(x.ci95[0], 2)}, ${pct(x.ci95[1], 2)}]` : "-"}</td></tr>`;
  $("#dc-detail").innerHTML = `<h3>${esc(r.label)}</h3><p>出場：${esc(r.exit)}；${r.n_insts} 個合約；訓練 t=${num(r.train_score, 2)}（${r.beats_null ? '<span class="pos">高於</span>' : '<span class="neg">未高於</span>'}洗牌基準）</p>
    <table><thead><tr><th class="l">段</th><th>次數</th><th>天數</th><th>日均報酬</th><th>勝率</th><th>CI95</th></tr></thead>
    <tbody>${s(r.train, "訓練")}${s(r.valid, "驗證")}${s(r.holdout, "保留")}</tbody></table>
    <h3>每月</h3><div class="runchart">${r.by_month.map((m) => `<div class="${m.mean < 0 ? "err" : ""}" style="height:${Math.min(100, Math.abs(m.mean) * 1000)}%" title="${m.m} ${pct(m.mean, 2)} (${m.n})"></div>`).join("")}</div>
    <h3>最常觸發的合約</h3><table>${r.top_insts.map((x) => `<tr><td class="l">${esc(x.inst)}</td><td>${x.count}</td><td>${pctC(x.mean, 2)}</td></tr>`).join("")}</table>
    <h3>最近觸發 <small class="muted">（點選開 K 線）</small></h3><table class="clickable" id="dc-ex">${r.examples.slice().reverse().map((x, i) => `<tr data-i="${i}"><td class="l">${esc(x.inst)}</td><td>${utc(x.ts)}</td><td>${pctC(x.ret, 2)}</td></tr>`).join("")}</table>`;
  const ex = r.examples.slice().reverse();
  $("#dc-ex").querySelectorAll("tr").forEach((tr) => tr.addEventListener("click", () => {
    const x = ex[+tr.dataset.i];
    openKline(x.inst, x.ts - 12 * 3600e3, x.ts + 36 * 3600e3, [{ inst: x.inst, time: Math.floor(x.ts / 300000) * 300, position: r.side === "short" ? "aboveBar" : "belowBar",
      shape: r.side === "short" ? "arrowDown" : "arrowUp", color: "#4aa3ff", text: `${r.exit} ${pct(x.ret, 1)}` }]);
  }));
}

function renderSecondary(sec) {
  $("#dc-secondary").innerHTML = sec.length ? sec.map((s) => `<h3>${esc(s.label)}（${esc(s.exit)}）<small class="muted"> ${s.since} 起有資料 ${s.n_with_data} 次，整體 ${pct(s.base.mean, 2)}</small></h3>
    <table><thead><tr><th class="l">條件</th><th>次數</th><th>天數</th><th>平均</th><th>勝率</th></tr></thead><tbody>
    ${s.filters.map((f) => `<tr><td class="l">${esc(f.cond)}</td><td>${f.n}</td><td>${f.n_days}</td><td>${pctC(f.mean, 2)}</td><td>${pct(f.win_rate, 0)}</td></tr>`).join("")}</tbody></table>`).join("")
    : '<p class="muted">沒有通過驗證的規則，或資金費率／OI 資料尚不足</p>';
}

function renderGrid(g) {
  if (!g || !g.n) { $("#dc-grid").innerHTML = '<p class="muted">尚未計算</p>'; return; }
  const LBL = { r24_b: "啟動前 24h 漲跌", btc: "BTC 環境", vol_b: "波動程度" };
  const MODE = { long: "做多", short: "做空", neutral: "中性" };
  $("#dc-grid").innerHTML = `<p class="muted">${esc(g.note)}；${fmtN(g.n)} 次模擬，${g.n_insts} 個合約。整體平均：做多 ${pct(g.overall.long, 2)}、做空 ${pct(g.overall.short, 2)}、中性 ${pct(g.overall.neutral, 2)}。
    「訓練最佳 → 驗證」= 用 ${esc(g.split.valid_start)} 以前挑出的最佳方向，在之後的實際表現</p>` +
    Object.entries(g.tables).map(([k, rows]) => `<h3>${LBL[k]}</h3><table><thead><tr><th class="l"></th><th>次數</th><th>做多</th><th>做空</th><th>中性</th><th>跑出區間</th><th>訓練最佳</th><th>→ 驗證</th><th>中性驗證</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td class="l">${esc(r.key)}</td><td>${fmtN(r.n)}</td><td>${pctC(r.long, 2)}</td><td>${pctC(r.short, 2)}</td><td>${pctC(r.neutral, 2)}</td>
        <td>${pct(r.break_rate, 0)}</td><td>${MODE[r.best_train] || "-"}</td><td>${pctC(r.best_train_test, 2)}</td><td>${pctC(r.neutral_test, 2)}</td></tr>`).join("")}</tbody></table>`).join("");
}

async function loadScan(refresh) {
  const r = await api(`/api/discover/scan${refresh ? "?refresh=true" : ""}`).catch((e) => ({ ok: false, message: e.message }));
  if (!r.ok) { $("#dc-scan-note").textContent = r.message || ""; table($("#dc-scan"), [], []); return; }
  $("#dc-scan-note").textContent = r.message || `使用 ${r.rules} 條規則；最近掃描 ${r.scanned_at ? new Date(r.scanned_at).toLocaleTimeString() : "-"}；${r.hits.length} 個訊號`;
  table($("#dc-scan"), [
    { t: "時間 (UTC)", f: (x) => utc(x.ts) }, { t: "合約", k: "inst", cls: "l" },
    { t: "方向", f: (x) => `<span class="${x.side === "short" ? "neg" : "pos"}">${SIDE[x.side]}</span>` },
    { t: "規則", k: "rule", cls: "l wrap" }, { t: "判定", f: (x) => `<span class="badge ${VERD[x.verdict]}">${x.verdict}</span>` },
    { t: "價格", f: (x) => Number(x.price).toPrecision(6) }, { t: "止損", f: (x) => (x.stop_px ? Number(x.stop_px).toPrecision(6) : "-") },
    { t: "持有", f: (x) => mins(x.hold_min) }, { t: "驗證日均", f: (x) => pctC(x.valid_mean, 2) }, { t: "保留日均", f: (x) => pctC(x.holdout_mean, 2) },
  ], r.hits || [], {
    onClick: (x) => openKline(x.inst, x.ts - 12 * 3600e3, Date.now() + 3600e3,
      [{ inst: x.inst, time: Math.floor(x.ts / 300000) * 300, position: x.side === "short" ? "aboveBar" : "belowBar", shape: x.side === "short" ? "arrowDown" : "arrowUp", color: "#ffcc00", text: "訊號" }],
      x.stop_px ? [{ inst: x.inst, price: x.stop_px, color: "#ef5350", title: "止損" }] : []),
  });
}

/* ---------------- paper trading ---------------- */
const KIND = { rule: "規則", random: "隨機對照", ml: "機器學習", llm: "LLM 審核", heat: "過熱分數", lab: "實驗室規則", follow: "跟單", inverse: "反指標", fade: "急漲回落", campaign: "瓜分活動" };
const EXIT_WHY = { time: "到期", stop: "止損", liquidation: "強平", stale: "資料中斷", "data gap": "資料缺口" };
let ppChart = null, ppSeries = {}, ppFilter = null, ppJobsRunning = {}, ppOpenRow = null, ppLastBoard = null, ppLastStatus = null;
const PP_KEY = "okxrev.pp";
const ppState = Object.assign({ src: "live", tab: "board", book: "C", kind: "", pos: "open" }, (() => {
  try { return JSON.parse(localStorage.getItem(PP_KEY) || "{}"); } catch { return {}; }
})());
const ppSave = () => { try { localStorage.setItem(PP_KEY, JSON.stringify(ppState)); } catch { /* private mode */ } };
(() => {
  const h = (location.hash || "").slice(1).split("/");
  if (h[0] !== "pp") return;
  const [, src, tab, kind, book] = h;
  if (src === "live" || src === "replay") ppState.src = src;
  if (["board", "equity", "pos", "cmp", "models"].includes(tab)) ppState.tab = tab;
  if (kind !== undefined && ["", "rule", "sl20", "follow", "inverse", "fade", "heat", "model", "random"].includes(kind)) ppState.kind = kind;
  if (["A", "B", "C"].includes(book)) ppState.book = book;
})();
const ppSrc = () => ppState.src;
const ppBook = () => ppState.book;

function ppSetOn(sel, attr, val) {
  document.querySelectorAll(`${sel} [data-${attr}]`).forEach((b) => b.classList.toggle("on", b.dataset[attr] === val));
}
function ppApplyUi() {
  ppSetOn(".pp-ctl .seg", "src", ppState.src);
  ppSetOn("#pp-book-tags", "book", ppState.book);
  ppSetOn("#pp-kind-tags", "kind", ppState.kind);
  ppSetOn("#pp-pos-seg", "pos", ppState.pos);
  const replay = ppState.src === "replay";
  document.querySelectorAll(".pp-tabs .replay-only").forEach((b) => (b.hidden = !replay));
  if (!replay && ppState.tab === "cmp") ppState.tab = "board";
  ppSetOn(".pp-tabs", "pptab", ppState.tab);
  document.querySelectorAll("#tab-pp .pp-pane").forEach((p) => (p.hidden = p.dataset.pane !== ppState.tab));
  $("#btn-pp-tick").hidden = replay;
  $("#pp-eq-book").textContent = ppState.book === "B" ? "B" : "A";
  const chip = $("#pp-filter-chip");
  chip.hidden = !ppFilter;
  chip.innerHTML = ppFilter ? `篩選：<b>${esc(ppFilter)}</b> <button aria-label="取消篩選" title="取消篩選">✕</button>` : "";
}
function ppSetFilter(tid) {
  ppFilter = tid;
  ppApplyUi();
  if (ppState.tab === "pos") loadPaperPositions();
  if (ppState.tab === "equity") loadPaperEquity();
}
function ppShowTab(t) {
  ppState.tab = t; ppSave(); ppApplyUi();
  if (t === "equity") loadPaperEquity();
  if (t === "pos") loadPaperPositions();
  if (t === "models" && ppSrc() === "live") loadLLM();
}

document.querySelectorAll(".pp-ctl .seg [data-src]").forEach((b) => b.addEventListener("click", () => {
  if (ppState.src === b.dataset.src) return;
  ppState.src = b.dataset.src; ppFilter = null; ppOpenRow = null; ppSave(); ppApplyUi(); loadPaper();
}));
document.querySelectorAll(".pp-tabs [data-pptab]").forEach((b) => b.addEventListener("click", () => ppShowTab(b.dataset.pptab)));
$("#pp-filter-chip").addEventListener("click", (e) => { if (e.target.closest("button")) { ppOpenRow = null; ppSetFilter(null); if (ppLastBoard) renderBoard(...ppLastBoard); } });
document.querySelectorAll("#pp-book-tags [data-book]").forEach((b) => b.addEventListener("click", () => {
  ppState.book = b.dataset.book; ppSave(); ppApplyUi();
  if (ppLastBoard) renderBoard(...ppLastBoard);
  if (ppState.tab === "equity") loadPaperEquity();
  if (ppState.tab === "pos") loadPaperPositions();
}));
document.querySelectorAll("#pp-kind-tags [data-kind]").forEach((b) => b.addEventListener("click", () => {
  ppState.kind = b.dataset.kind; ppSave(); ppApplyUi();
  if (ppLastBoard) renderBoard(...ppLastBoard);
}));
document.querySelectorAll("#pp-pos-seg [data-pos]").forEach((b) => b.addEventListener("click", () => {
  ppState.pos = b.dataset.pos; ppSave(); ppApplyUi(); loadPaperPositions();
}));
$("#btn-pp-more").addEventListener("click", (e) => {
  e.stopPropagation();
  const pop = $("#pp-more");
  pop.hidden = !pop.hidden;
  $("#btn-pp-more").setAttribute("aria-expanded", String(!pop.hidden));
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".menu")) { $("#pp-more").hidden = true; $("#btn-pp-more").setAttribute("aria-expanded", "false"); }
});
$("#btn-pp-tick").addEventListener("click", async () => { await confirmable("/api/paper/tick"); pollPaperJobs(); });
$("#btn-pp-train").addEventListener("click", async () => { $("#pp-more").hidden = true; try { const r = await post("/api/paper/train"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); } pollPaperJobs(); });
$("#btn-pp-export").addEventListener("click", async () => { $("#pp-more").hidden = true; try { const r = await post("/api/paper/export"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); } });
ppApplyUi();

function groupNames(lb) {
  const g = {};
  let k = 0;
  lb.forEach((r) => {
    const m = r.diag && r.diag.group;
    if (!m) return;
    const key = m.join(",");
    if (!(key in g)) g[key] = String.fromCharCode(65 + (k++ % 26));
  });
  return g;
}
const TIP = {
  same: "每筆報酬減去「同一天、同方向、同出場」隨機對照的平均，再以每日平均做 t 檢定；t > 2 且至少 20 個交易日才算顯著。用來分辨選股能力，還是只吃到那天的大盤方向。",
  eff: "依每天的交易筆數換算的等效天數（筆數擠在少數幾天時會很小）。少於 10 天或單日占比超過 25% 標黃色，代表成績可能只是幾次行情。",
  grp: "進場（同幣、同一小時）有 80% 以上互相重疊的交易員歸為同一組，等於同一個訊號，只能算一個證據。",
  booka: "1 萬 U、每筆 2% 本金、依時間順序複利；不是報酬加總。",
  mean: "每筆淨報酬的平均（已扣手續費、滑價、資金費）。",
};
const th = (t, tip) => ({ t, tip });
const lowSample = (r) => (r.b.n || 0) < 20 || (r.diag && r.diag.eff_days < 10);
const sameDay = (r) => { const v = r.diag && r.diag.vs_same_day; return v ? v.excess : null; };

function renderMarket(m) {
  const el = $("#pp-market");
  if (!m || !m.from) { el.innerHTML = ""; return; }
  const rnd = (side, exit) => (m.random || []).find((x) => x.side === side && x.exit === exit);
  const pick = ["24h", "6h", "1h"].find((e) => rnd("short", e) && rnd("long", e));
  const parts = [`期間 ${utc(m.from).slice(0, 10)} ～ ${utc(m.to).slice(0, 10)}（${num(m.days, 1)} 天）`];
  if (m.market_cum != null) {
    const dn = (m.market_days || []).filter((d) => d.down > 0.7).length;
    parts.push(`全市場累計 ${pctC(m.market_cum, 1)}${dn ? `，${dn} 天有七成以上的幣下跌` : ""}`);
  }
  if (pick) parts.push(`隨機進場（${pick}）：做空 ${pctC(rnd("short", pick).mean, 2)}、做多 ${pctC(rnd("long", pick).mean, 2)}`);
  const s = rnd("short", pick || "24h"), l = rnd("long", pick || "24h");
  const tilt = s && l && Math.abs(s.mean - l.mean) > 0.01 ? ` <span class="badge warn">大盤${s.mean > l.mean ? "偏跌，做空都會好看" : "偏漲，做多都會好看"}，請看「扣同日隨機」</span>` : "";
  el.innerHTML = parts.join("　·　") + tilt;
}

function renderSummary(lb, status) {
  const el = $("#pp-summary");
  const real = (lb || []).filter((r) => !r.trader_id.startsWith("random"));
  const eff = real.filter((r) => r.profitable);
  const sig = real.filter((r) => r.diag && r.diag.vs_same_day && r.diag.vs_same_day.significant);
  const best = (eff.length ? eff : []).slice().sort((a, b) => (sameDay(b) ?? -9) - (sameDay(a) ?? -9))[0];
  const bestBook = real.filter((r) => r.book_a_ret != null).sort((a, b) => b.book_a_ret - a.book_a_ret)[0];
  const item = (k, v) => `<span class="kv1"><span class="k">${k}</span><span class="v">${v}</span></span>`;
  const items = [
    item("有效交易員", best ? `<b>${esc(best.trader_id)}</b>（每筆 ${pctC(best.C.mean, 2)}）${eff.length > 1 ? `，共 ${eff.length} 個` : ""}` : '<span class="muted">目前沒有</span>'),
    item("扣同日隨機顯著", `${sig.length} 個`),
    item("帳本 A 最高", bestBook ? `${esc(bestBook.trader_id)} ${pctC(bestBook.book_a_ret, 1)}` : "-"),
  ];
  if (status) {
    const lt = status.last_tick || {};
    const bk = ppBook(), sum = (k) => real.reduce((a, r) => a + ((r[bk] || {})[k] || 0), 0);
    items.push(item(`持倉（帳本 ${bk}）`, fmtN(sum("open"))), item("已平倉", fmtN(sum("n"))), item("上一輪", `${ago(lt.tick_ts)}`));
  }
  el.innerHTML = items.join("");
}

async function showFollowInto(el, tid) {
  const d = await api(`/api/paper/follow?trader=${encodeURIComponent(tid)}&src=${ppSrc()}`).catch((e) => ({ error: e.message }));
  if (d.error) { el.innerHTML = `<span class="neg">${esc(d.error)}</span>`; return; }
  const fs = d.set || {};
  const cur = d.picks[0];
  const hist = d.picks.slice(0, 12).map((p) => `<tr><td>${utc(p.period_ts)}</td><td class="l">${p.picks.length ? p.picks.map((x) => `${esc(x.trader_id)} <span class="muted">${pct(x.score, 2)}（${x.n}）</span>`).join("、") : '<span class="muted">累積中（資料不足，不下單）</span>'}</td></tr>`).join("");
  const src = (d.by_source || []).map((x) => `${esc(x.trader_id)} ${x.n} 筆 ${pctC(x.mean, 2)}`).join("、");
  el.innerHTML = `<div class="muted">每 ${fs.step_h >= 24 ? fs.step_h / 24 + " 天" : fs.step_h + " 小時"}重選；回看 ${fs.look_h / 24} 天；跟前 ${fs.top} 名（至少 ${fs.min_n} 筆）；止損 20%${d.next_repick && ppSrc() === "live" ? `；下次重選 ${utc(d.next_repick)} UTC` : ""}</div>
    <div>目前跟：${cur ? (cur.picks.length ? cur.picks.map((x) => `<b>${esc(x.trader_id)}</b>`).join("、") : '<span class="muted">累積中</span>') : '<span class="muted">尚未重選</span>'}${src ? `　<span class="muted">已平倉來源：${src}</span>` : ""}</div>
    <table class="mini"><thead><tr><th>重選時間</th><th class="l">選中（分數、筆數）</th></tr></thead><tbody>${hist || '<tr><td colspan="2" class="muted">尚無紀錄</td></tr>'}</tbody></table>`;
}

function detailHtml(r, groups) {
  const b = r.b, d = r.diag || {}, v = d.vs_same_day || {}, vr = r.vs_random || {};
  const kv = (k, val) => `<div><span class="k">${k}</span>${val}</div>`;
  const grp = d.group ? `組 ${groups[d.group.join(",")]}：${esc(d.group.join("、"))}` : "-";
  return `<div class="pp-detail-grid">
      ${kv("類型", esc(KIND[r.kind] || r.kind))}
      ${kv("持倉中", fmtN(b.open || 0))}
      ${kv("交易天數", fmtN(b.n_days || 0))}
      ${kv("勝率", pct(b.win_rate, 0))}
      ${kv("日均", pctC(b.day_mean, 2))}
      ${kv("強平", b.liquidations ? `<span class="neg">${b.liquidations}</span>` : "0")}
      ${ppBook() !== "C" ? kv("權益", `${num(b.equity)} (${usd((b.equity || 0) - (b.start || 0))})`) : ""}
      ${kv("勝過隨機（全期）", vr.diff != null ? `${pctC(vr.diff, 2)} t=${num(vr.t, 1)}` : "-")}
      ${kv("扣同日隨機", v.excess != null ? `${pctC(v.excess, 2)}，t=${num(v.t, 1)}，${v.n_days} 天` : "-")}
      ${kv("最大單日占比", d.max_day_share != null ? pct(d.max_day_share, 0) : "-")}
      ${kv("最好 3 天貢獻", d.top3_days_ret_share != null ? pct(d.top3_days_ret_share, 0) : "-")}
      ${kv("重疊最多", d.overlap ? `${esc(d.overlap.with)} ${pct(d.overlap.share, 0)}` : "-")}
      ${kv("同一訊號", grp)}
    </div>
    <div class="muted">${esc(r.label)}</div>
    ${r.kind === "follow" ? '<div class="pp-follow-box">載入跟單資料…</div>' : ""}
    <div class="pp-detail-act"><button class="small" data-act="pos">看持倉／平倉</button> <button class="small" data-act="eq">看權益曲線</button></div>`;
}

function renderBoard(lb, market) {
  ppLastBoard = [lb, market];
  const book = ppBook();
  if (market !== undefined) renderMarket(market);
  const kind = ppState.kind;
  const ppKindOk = (r) => !kind || (kind === "sl20" ? r.trader_id.endsWith("_sl20")
    : kind === "rule" ? r.kind === "rule" && !r.trader_id.endsWith("_sl20")
    : kind === "model" ? ["ml", "llm"].includes(r.kind) || r.trader_id.startsWith("twin")
    : kind === "random" ? r.trader_id.startsWith("random") : r.kind === kind);
  const rows = lb.filter(ppKindOk).map((r) => ({ ...r, b: r[book] || {} }));
  const key = (r) => (r.trader_id.startsWith("random") && kind !== "random" ? -1e6 : sameDay(r) ?? (r.b.n ? -1e3 + (r.b.mean ?? 0) : -1e9));
  rows.sort((a, b) => key(b) - key(a));
  const groups = groupNames(lb);
  const cols = [
    { t: "交易員", cls: "l", f: (r) => `<span class="tid">${esc(r.trader_id)}</span>${r.profitable ? ' <span class="badge ok" title="每筆平均為正、帳本 A 獲利、扣同日隨機顯著">有效</span>' : ""}${!r.profitable && r.diag && r.diag.vs_same_day && r.diag.vs_same_day.significant ? ' <span class="badge" title="扣同日隨機顯著，但每筆或帳本 A 不賺">顯著</span>' : ""}${lowSample(r) && !r.trader_id.startsWith("random") ? ' <span class="badge dim">樣本不足</span>' : ""}${r.kind === "fade" && book === "B" ? ' <span class="badge bad" title="止損 50%，20 倍槓桿逆漲約 5% 就強平，這個帳本的結果沒有參考價值">帳本 B 不適用</span>' : ""}` },
    { t: "平倉", f: (r) => fmtN(r.b.n || 0) },
    { ...th("每筆平均", TIP.mean), f: (r) => pctC(r.b.mean, 2) },
    book === "C" ? { ...th("帳本 A", TIP.booka), f: (r) => pctC(r.book_a_ret, 1) }
      : { t: "權益", f: (r) => (r.b.equity == null ? "-" : usd((r.b.equity || 0) - (r.b.start || 0))) },
    { ...th("扣同日隨機", TIP.same), f: (r) => { const v = r.diag && r.diag.vs_same_day; return v ? `${pctC(v.excess, 2)} <span class="muted">t ${num(v.t, 1)}</span>` : "-"; } },
    { ...th("獨立天數", TIP.eff), f: (r) => { const d = r.diag; if (!d) return "-"; const bad = d.eff_days < 10 || d.max_day_share > 0.25; return `<span class="${bad ? "warn" : ""}">${num(d.eff_days, 1)}</span>`; } },
    { ...th("同一訊號", TIP.grp), cls: "l", f: (r) => (r.diag && r.diag.group ? `<span class="badge warn" title="${esc(r.diag.group.join("、"))}">組 ${groups[r.diag.group.join(",")]}</span>` : "") },
  ];
  const el = $("#pp-board");
  const head = `<thead><tr>${cols.map((c) => `<th class="${c.cls || ""}">${esc(c.t)}${c.tip ? ` <span class="tip" title="${esc(c.tip)}" tabindex="0">ⓘ</span>` : ""}</th>`).join("")}</tr></thead>`;
  const body = rows.map((r, i) => {
    const rnd = r.trader_id.startsWith("random") && kind !== "random";
    const open = ppOpenRow === r.trader_id;
    return `<tr data-ri="${i}" class="${rnd ? "dim" : ""}${open ? " sel" : ""}">${cols.map((c) => `<td class="${c.cls || ""}">${c.f(r)}</td>`).join("")}</tr>` +
      (open ? `<tr class="pp-detail"><td colspan="${cols.length}">${detailHtml(r, groups)}</td></tr>` : "");
  }).join("");
  el.innerHTML = head + `<tbody>${body || `<tr><td colspan="${cols.length}" class="muted l">（無資料）</td></tr>`}</tbody>`;
  el.querySelectorAll("tbody tr[data-ri]").forEach((tr) => tr.addEventListener("click", () => {
    const r = rows[+tr.dataset.ri];
    const same = ppOpenRow === r.trader_id;
    ppOpenRow = same ? null : r.trader_id;
    ppSetFilter(same ? null : r.trader_id);
    renderBoard(...ppLastBoard);
  }));
  const det = el.querySelector("tr.pp-detail");
  if (det) {
    const r = rows.find((x) => x.trader_id === ppOpenRow);
    det.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); ppShowTab(b.dataset.act === "pos" ? "pos" : "equity"); }));
    const fb = det.querySelector(".pp-follow-box");
    if (fb && r) showFollowInto(fb, r.trader_id);
  }
  renderSummary(lb, ppSrc() === "live" ? ppLastStatus : null);
}

async function loadReplay() {
  const r = await api("/api/replay/summary").catch((e) => ({ error: e.message }));
  if (r.error || !r.exists) {
    $("#pp-meta").textContent = r.error || "尚未執行盲測（python -m okxrev.replay --run）";
    $("#pp-replay-note").textContent = "";
    $("#pp-summary").innerHTML = "";
    table($("#pp-board"), [], []);
    table($("#pp-replay-cmp"), [], []);
    return;
  }
  const m = r.meta;
  $("#pp-meta").textContent = `盲測 ${m.start} ～ ${m.end}，${m.n_insts} 個合約，${fmtN(m.signals)} 個決策；產生於 ${m.generated_at}（耗時 ${Math.round(m.elapsed_sec / 60)} 分）`;
  $("#pp-src-meta").textContent = $("#pp-meta").textContent;
  $("#pp-replay-note").innerHTML = `<ul>${(r.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`;
  const rules = r.leaderboard.filter((x) => ["rule", "ml", "heat", "lab", "follow", "inverse", "fade"].includes(x.kind));
  rules.sort((a, b) => (sameDay(b) ?? -9) - (sameDay(a) ?? -9));
  table($("#pp-replay-cmp"), [
    { t: "交易員", cls: "l", f: (x) => `<span class="tid">${esc(x.trader_id)}</span>${x.profitable ? ' <span class="badge ok">有效</span>' : ""}` },
    { t: "探索判定", f: (x) => (x.discovery ? `<span class="badge ${VERD[x.discovery.verdict] || ""}">${esc(x.discovery.verdict)}</span>` : "-") },
    { t: "探索驗證段", f: (x) => (x.discovery ? pctC(x.discovery.valid, 2) : "-") },
    { t: "探索保留段", f: (x) => (x.discovery ? pctC(x.discovery.holdout, 2) : "-") },
    { t: "盲測筆數", f: (x) => `${fmtN(x.C.n || 0)} <span class="muted">${x.C.n_days || 0} 天</span>` },
    { t: "盲測每筆", f: (x) => pctC(x.C.mean, 2) },
    { t: "帳本 A", f: (x) => pctC(x.book_a_ret, 1) },
    { t: "扣同日隨機", f: (x) => { const v = x.diag && x.diag.vs_same_day; return v ? `${pctC(v.excess, 2)} <span class="muted">t ${num(v.t, 1)}</span>` : "-"; } },
    { t: "每月", f: (x) => `<div class="runchart" style="height:24px;width:160px">${(x.by_month || []).map((b) => `<div class="${b.mean < 0 ? "err" : ""}" style="height:${Math.min(100, Math.abs(b.mean) * 2000)}%" title="${b.m} ${pct(b.mean, 2)} (${b.n})"></div>`).join("")}</div>` },
  ], rules);
  renderBoard(r.leaderboard, r.market);
  if (ppState.tab === "pos") loadPaperPositions();
  if (ppState.tab === "equity") loadPaperEquity();
}

async function pollPaperJobs() {
  const j = await api("/api/paper/jobs").catch(() => null);
  if (!j) return;
  const lines = [...(j.train.log || []).map((l) => `[訓練] ${l}`), ...(j.tick.log || []).map((l) => `[模擬] ${l}`)];
  const busy = j.train.running || j.tick.running;
  const log = $("#pp-log");
  log.style.display = busy || (lines.length && Object.values(ppJobsRunning).some(Boolean)) ? "" : log.style.display;
  if (lines.length) renderLog(log, lines.slice(-300));
  $("#btn-pp-train").disabled = j.train.running;
  $("#btn-pp-tick").disabled = j.tick.running;
  $("#btn-pp-tick").textContent = j.tick.running ? "執行中…" : "立即跑一輪";
  for (const k of ["train", "tick"]) {
    if (ppJobsRunning[k] && !j[k].running) {
      toast(`${k === "train" ? "訓練" : "模擬"}結束，代碼 ${j[k].returncode}`, j[k].returncode !== 0);
      setTimeout(() => { if (!j.train.running && !j.tick.running) log.style.display = "none"; }, 8000);
      loadPaper();
    }
    ppJobsRunning[k] = j[k].running;
  }
}

async function loadPaper() {
  ppApplyUi();
  if (ppSrc() === "replay") { $("#pp-meta").textContent = "載入中…"; return loadReplay(); }
  const s = await api("/api/paper/status").catch((e) => ({ error: e.message }));
  if (s.error || !s.exists) {
    $("#pp-meta").textContent = s.error || "尚未開始紙上交易（按「立即跑一輪」或等下次同步）";
    $("#pp-summary").innerHTML = "";
    renderModels(s.models || {});
    return;
  }
  ppLastStatus = s;
  const lt = s.last_tick || {};
  $("#pp-meta").innerHTML = `已跑 ${fmtN(s.n_ticks)} 輪（自 ${utc(s.first_tick).slice(0, 10)}）· 最新 K 線 ${utc(lt.last_bar)} · 上一輪 ${ago(lt.tick_ts)}，${num(lt.duration, 1)} 秒 · 決策 ${fmtN(s.n_decisions)} · LLM ${s.llm_enabled ? `${esc(s.llm_model)}，24h ${s.llm_24h.calls} 次` : "未設定"}`;
  renderBoard(s.leaderboard, s.market);
  renderModels(s.models || {});
  if (ppState.tab === "pos") loadPaperPositions();
  if (ppState.tab === "equity") loadPaperEquity();
  if (ppState.tab === "models") loadLLM();
}

async function loadPaperPositions() {
  const book = ppBook();
  const q = (state) => `/api/paper/positions?src=${ppSrc()}&state=${state}&book=${book}${ppFilter ? `&trader=${encodeURIComponent(ppFilter)}` : ""}&limit=300`;
  const [o, c] = await Promise.all([api(q("open")).catch(() => []), api(q("closed")).catch(() => [])]);
  const cnt = (x) => `(${x.length >= 300 ? "300+" : x.length})`;
  $("#pp-open-n").textContent = cnt(o);
  $("#pp-closed-n").textContent = cnt(c);
  const P = (v) => (v == null ? "-" : Number(v).toPrecision(5));
  const click = (x) => {
    const t0 = x.entry_ts || x.signal_ts;
    const t1 = x.exit_ts || Date.now();
    const up = x.side !== "short";
    const marks = [{ inst: x.inst_id, time: Math.floor(t0 / 300000) * 300, position: up ? "belowBar" : "aboveBar", shape: up ? "arrowUp" : "arrowDown", color: "#4aa3ff", text: x.trader_id }];
    if (x.exit_ts) marks.push({ inst: x.inst_id, time: Math.floor(x.exit_ts / 300000) * 300, position: up ? "aboveBar" : "belowBar", shape: "circle", color: x.ret_net >= 0 ? "#26a69a" : "#ef5350", text: `${EXIT_WHY[x.exit_reason] || x.exit_reason} ${pct(x.ret_net, 1)}` });
    const lines = x.stop_px ? [{ inst: x.inst_id, price: x.stop_px, color: "#ef5350", title: "止損" }] : [];
    openKline(x.inst_id, t0 - 6 * 3600e3, t1 + 3 * 3600e3, marks, lines);
  };
  const side = (x) => `<span class="${x.side === "short" ? "neg" : "pos"}">${SIDE[x.side]}</span>`;
  const open = ppState.pos === "open";
  table($("#pp-pos"), open ? [
    { t: "交易員", k: "trader_id", cls: "l" }, { t: "合約", k: "inst_id", cls: "l" }, { t: "方向", f: side },
    { t: "進場", f: (x) => (x.state === "pending" ? '<span class="muted">等待成交</span>' : `${utc(x.entry_ts)} <span class="muted">${P(x.entry_px)}</span>`) },
    { t: "止損", f: (x) => P(x.stop_px) }, { t: "到期", f: (x) => (x.deadline_ts ? utc(x.deadline_ts) : "-") },
    { t: "最大浮虧", f: (x) => pctC(x.mae == null ? null : -x.mae, 1) }, { t: "最大浮盈", f: (x) => pctC(x.mfe, 1) },
  ] : [
    { t: "交易員", k: "trader_id", cls: "l" }, { t: "合約", k: "inst_id", cls: "l" }, { t: "方向", f: side },
    { t: "進場", f: (x) => utc(x.entry_ts) }, { t: "出場", f: (x) => utc(x.exit_ts) },
    { t: "原因", f: (x) => `<span class="${x.exit_reason === "stop" || x.exit_reason === "liquidation" ? "neg" : "muted"}">${EXIT_WHY[x.exit_reason] || esc(x.exit_reason)}</span>` },
    { t: "報酬", f: (x) => pctC(x.ret_net, 2) }, { t: "資金費", f: (x) => pct(x.funding, 3) },
    ...(book === "C" ? [] : [{ t: "損益", f: (x) => usd(x.pnl) }]),
  ], open ? o : c, { onClick: click });
}

async function loadPaperEquity() {
  const book = ppBook() === "B" ? "B" : "A";
  const d = await api(`/api/paper/equity?book=${book}&src=${ppSrc()}`).catch(() => ({}));
  if (ppState.tab !== "equity") return;
  if (!ppChart) {
    ppChart = LightweightCharts.createChart($("#pp-equity"), {
      layout: { background: { color: "#0f1419" }, textColor: "#d6dde4" }, grid: { vertLines: { color: "#1c242d" }, horzLines: { color: "#1c242d" } },
      timeScale: { timeVisible: true, borderColor: "#2a323c" }, rightPriceScale: { borderColor: "#2a323c" },
    });
  }
  ppChart.resize($("#pp-equity").clientWidth, 340);
  Object.values(ppSeries).forEach((s) => ppChart.removeSeries(s));
  ppSeries = {};
  const colors = ["#4aa3ff", "#26a69a", "#ffcc00", "#ef5350", "#ab47bc", "#ff7043", "#8d6e63", "#26c6da", "#d4e157", "#ec407a"];
  const ids = Object.keys(d).filter((k) => !ppFilter || k === ppFilter || k.startsWith("random"));
  ids.slice(0, 30).forEach((k, i) => {
    const pts = []; let last = -1;
    d[k].t.forEach((t, j) => { if (t > last) { pts.push({ time: t, value: d[k].v[j] }); last = t; } });
    if (!pts.length) return;
    const s = ppChart.addLineSeries({ color: k.startsWith("random") ? "#555f6b" : colors[i % colors.length], lineWidth: k.startsWith("random") ? 1 : 2,
      title: k.startsWith("random") ? "" : k, priceLineVisible: false, lastValueVisible: false });
    s.setData(pts);
    ppSeries[k] = s;
  });
  ppChart.timeScale().fitContent();
}

function renderModels(m) {
  const keys = Object.keys(m);
  if (!keys.length) { $("#pp-models").innerHTML = '<p class="muted">尚未訓練（「更多 ▾」→ 重新訓練模型，約 10-20 分鐘）</p>'; return; }
  $("#pp-models").innerHTML = keys.map((k) => {
    const x = m[k];
    const wf = x.walk_forward;
    const head = `<h3>${esc(k)} <small class="muted">版本 ${esc(x.version)}（共 ${x.n_versions} 版），門檻 ${num(x.threshold, 3)}，出場 ${esc(x.exit)}</small></h3>`;
    const body = wf
      ? `<p>逐段往前驗證：選中的平均 ${pctC(wf.mean_sel, 2)} vs 全部 ${pctC(wf.mean_all, 2)}，${wf.positive_folds}/${wf.n_folds} 段為正；訓練樣本 ${fmtN(x.n_train)}，基礎勝率 ${pct(x.base_rate, 0)}</p>
         <table><thead><tr><th class="l">測試期</th><th>選中</th><th>選中平均</th><th>勝率</th><th>全部平均</th></tr></thead><tbody>
         ${wf.folds.map((f) => `<tr><td class="l">${f.test_start.slice(0, 10)}</td><td>${f.n_sel}</td><td>${pctC(f.sel_mean, 2)}</td><td>${pct(f.sel_win, 0)}</td><td>${pctC(f.all_mean, 2)}</td></tr>`).join("")}</tbody></table>`
      : `<p>你的進場 ${x.n_pos} 筆 vs 未進場 ${x.n_neg} 筆；後段 AUC ${x.holdout_auc == null ? "-" : num(x.holdout_auc, 2)}；在你沒進場的時間點觸發率 ${pct(x.neg_fire_rate, 0)}。${esc(x.note || "")}<br>
         出場：${esc(x.exit_from_plan || x.exit)}；你自己的中位持倉 ${mins((x.your_hold_bars || 0) * 5)}</p>`;
    const imp = (x.importance || []).slice(0, 8).map((i) => esc(i.feature)).join("、");
    return head + body + `<p class="muted">最重要的特徵：${imp}</p>`;
  }).join("");
}

async function loadLLM() {
  const d = await api("/api/paper/decisions?kind=llm&limit=100").catch(() => []);
  $("#pp-llm-meta").textContent = d.length ? `（最近 ${d.length} 筆）` : "（尚無紀錄；在 .env 設定 LLM 後啟用）";
  const A = { enter: '<span class="badge ok">接受</span>', reject: '<span class="badge bad">拒絕</span>', skip_llm: '<span class="badge">未審核</span>' };
  table($("#pp-llm"), [
    { t: "時間", f: (x) => utc(x.bar_ts) }, { t: "合約", k: "inst_id", cls: "l" },
    { t: "方式", f: (x) => (x.trader_id.startsWith("llmb_") ? '<span class="badge">批次</span>' : '<span class="muted">單筆</span>') },
    { t: "方向", f: (x) => SIDE[x.side] }, { t: "結果", f: (x) => A[x.action] || esc(x.action) },
    { t: "理由", cls: "l wrap", f: (x) => esc((x.reason || "").split("｜").slice(-1)[0]) },
  ], d);
}

/* ---------------- heat ---------------- */
let heatChart = null, heatPrice = null, heatSeries = {}, heatCoin = null, heatJobRunning = false;
const HEAT_COLORS = { score: "#ffcc00", stretch: "#ef5350", flow: "#26a69a", leverage: "#ab47bc", premium: "#4aa3ff", sentiment: "#8d6e63" };
const ALERT_KIND = { hot: '<span class="badge bad">過熱</span>', cooling: '<span class="badge warn">降溫</span>', rule: '<span class="badge ok">規則觸發</span>', fade: '<span class="badge warn">急漲回落</span>',
  campaign: '<span class="badge">瓜分活動</span>', campaign_end: '<span class="badge warn">交易賺幣結束</span>' };
const heatCell = (v) => (v == null ? "-" : `<span style="color:${v >= 80 ? "var(--down)" : v >= 60 ? "var(--warn)" : "var(--text)"}">${Math.round(v)}</span>`);

$("#btn-ht-watch").addEventListener("click", async () => {
  const insts = $("#ht-watch").value.split(/[\s,]+/).filter(Boolean).map((s) => (s.includes("-") ? s.toUpperCase() : `${s.toUpperCase()}-USDT-SWAP`));
  try { const r = await post("/api/heat/watchlist", { insts }); toast(r.message, !r.ok); loadHeat(); } catch (e) { toast(e.message, true); }
});
$("#btn-ht-eval").addEventListener("click", async () => {
  try { const r = await post("/api/heat/evaluate"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
  pollHeatJob();
});
$("#btn-ht-kl").addEventListener("click", () => heatCoin && openKline(heatCoin, Date.now() - 5 * 86400e3, Date.now()));
$("#btn-ht-lab").addEventListener("click", () => {
  if (!heatCoin) return;
  $("#lab-inst").value = heatCoin;
  document.querySelector('input[name="lab-mode"][value="live"]').checked = true;
  showTab("lab");
  labLoad(true);
});

async function pollHeatJob() {
  const j = await api("/api/heat/job").catch(() => null);
  if (!j) return;
  if (j.running || j.log.length) { $("#ht-log").style.display = ""; renderLog($("#ht-log"), j.log); }
  $("#btn-ht-eval").disabled = j.running;
  if (heatJobRunning && !j.running) { toast(j.returncode === 0 ? "驗證完成" : `驗證結束，代碼 ${j.returncode}`, j.returncode !== 0); loadHeatValidation(); }
  heatJobRunning = j.running;
}

async function loadHeat() {
  const s = await api("/api/heat/snapshot").catch(() => null);
  if (!s) return;
  if (document.activeElement !== $("#ht-watch")) $("#ht-watch").value = (s.watchlist || []).join(" ");
  $("#ht-meta").textContent = s.ts ? `更新於 ${new Date(s.ts).toLocaleTimeString()}；提醒門檻 ${s.threshold}` : "尚未計算（等下一輪紙上交易）";
  const rows = s.rows || [];
  const cols = [
    { t: "#", f: (r) => r.rank ?? "-" },
    { t: "合約", k: "inst_id", cls: "l" },
    { t: "過熱分數", k: "score", f: (r) => `<b>${heatCell(r.score)}</b>${r.cooling ? ' <span class="badge warn">降溫</span>' : ""}` },
    ...Object.entries(s.dims || {}).map(([k, d]) => ({ t: d.label + (d.experimental ? "*" : ""), k, f: (r) => heatCell(r[k]) })),
    { t: "24h", k: "ret_24h", f: (r) => pctC(r.ret_24h) },
    { t: "資金費率", k: "funding", f: (r) => pct(r.funding, 3) },
    { t: "OI 24h", k: "oi_chg_24h", f: (r) => pctC(r.oi_chg_24h) },
    { t: "溢價", k: "premium", f: (r) => pct(r.premium, 3) },
    { t: "主動買/賣", k: "taker_ratio_6h", f: (r) => num(r.taker_ratio_6h) },
    { t: "主要原因", cls: "l wrap", f: (r) => (r.drivers || []).map(([a, b]) => `${esc(a)} ${Math.round(b)}`).join("、") },
  ];
  const watch = new Set(s.watchlist || []);
  $("#ht-rank-meta").textContent = `${rows.length} 個合約；* 為參考用（不計入分數）`;
  table($("#ht-rank"), cols, rows.slice(0, 60), { sortable: true, onClick: (r) => showHeatCoin(r.inst_id) });
  table($("#ht-watch-t"), cols, rows.filter((r) => watch.has(r.inst_id)), { onClick: (r) => showHeatCoin(r.inst_id) });
  const al = await api("/api/heat/alerts?hours=24").catch(() => []);
  table($("#ht-alerts"), [
    { t: "時間", f: (a) => new Date(a.ts).toLocaleTimeString() }, { t: "合約", k: "inst_id", cls: "l" },
    { t: "類型", f: (a) => ALERT_KIND[a.kind] || esc(a.kind) }, { t: "分數", f: (a) => heatCell(a.score) },
    { t: "說明", cls: "l wrap", f: (a) => esc(a.detail || "") },
  ], al, { onClick: (a) => showHeatCoin(a.inst_id) });
  if (heatCoin) showHeatCoin(heatCoin, true);
}

async function showHeatCoin(inst, quiet) {
  heatCoin = inst;
  const d = await api(`/api/heat/coin?inst=${encodeURIComponent(inst)}&days=10`).catch((e) => { if (!quiet) toast(e.message, true); return null; });
  if (!d || d.empty) return;
  $("#ht-coin-panel").style.display = "";
  $("#ht-coin-title").textContent = `${inst}　過熱 ${d.now.score == null ? "-" : Math.round(d.now.score)}`;
  const base = { layout: { background: { color: "#171d24" }, textColor: "#d6dde4" }, grid: { vertLines: { color: "#222b35" }, horzLines: { color: "#222b35" } },
    timeScale: { timeVisible: true, borderColor: "#2a323c" }, rightPriceScale: { borderColor: "#2a323c" } };
  if (!heatChart) {
    heatChart = LightweightCharts.createChart($("#ht-chart"), base);
    heatPrice = LightweightCharts.createChart($("#ht-price"), base);
    heatSeries.price = heatPrice.addLineSeries({ color: "#d6dde4", lineWidth: 1, priceFormat: { type: "custom", formatter: (x) => x.toPrecision(5), minMove: 1e-9 } });
    heatChart.timeScale().subscribeVisibleLogicalRangeChange((r) => r && heatPrice.timeScale().setVisibleLogicalRange(r));
  }
  heatChart.resize($("#ht-chart").clientWidth, 260);
  heatPrice.resize($("#ht-price").clientWidth, 140);
  Object.entries(d.series).forEach(([k, v]) => {
    if (!heatSeries[k]) heatSeries[k] = heatChart.addLineSeries({ color: HEAT_COLORS[k] || "#999", lineWidth: k === "score" ? 3 : 1, title: k === "score" ? "過熱" : d.dims[k]?.label || k, priceLineVisible: false, lastValueVisible: k === "score" });
    heatSeries[k].setData(d.t.map((t, i) => (v[i] == null ? null : { time: t, value: v[i] })).filter(Boolean));
  });
  heatSeries.score.setMarkers(d.t.map((t, i) => (d.cooling[i] ? { time: t, position: "aboveBar", shape: "circle", color: "#f0a020", text: "" } : null)).filter(Boolean));
  heatSeries.price.setData(d.t.map((t, i) => ({ time: t, value: d.close[i] })));
  heatChart.timeScale().fitContent();
  heatPrice.timeScale().fitContent();
  const ind = Object.values(d.now.indicators).sort((a, b) => (b.pct ?? -1) - (a.pct ?? -1));
  table($("#ht-ind"), [
    { t: "指標", k: "label", cls: "l" }, { t: "30 天百分位", f: (x) => heatCell(x.pct) },
    { t: "數值", f: (x) => (x.raw == null ? "-" : Math.abs(x.raw) < 0.1 ? x.raw.toPrecision(3) : num(x.raw, 2)) },
  ], ind);
  table($("#ht-liq"), [
    { t: "時間", f: (x) => utc(x.ts) }, { t: "爆倉方", f: (x) => (x.pos_side === "short" ? '<span class="pos">空單</span>' : '<span class="neg">多單</span>') },
    { t: "價格", f: (x) => Number(x.px).toPrecision(5) }, { t: "張數", f: (x) => fmtN(x.sz) },
  ], d.liquidations || []);
}

async function loadHeatValidation() {
  const v = await api("/api/heat/validation").catch(() => null);
  if (!v) return;
  const bf = v.backfill || {};
  const bfText = Object.entries(bf).map(([k, x]) => `${k.replace("fill:", "")} ${x.done}/${x.insts}`).join("、");
  const r = v.result;
  if (!r) { $("#ht-val-meta").textContent = `尚未驗證。歷史補資料進度：${bfText || "未開始"}（補完再按「重新驗證」）`; return; }
  $("#ht-val-meta").textContent = `產生於 ${r.generated_at}；找門檻期 ${r.discovery.join(" ～ ")}、盲測期 ${r.blind.join(" ～ ")}；歷史補資料：${bfText}`;
  const cell = (s, b) => (s && s.n ? `${pctC(s.day_mean, 2)} <span class="muted">(${s.n}/${s.n_days}天, 基準 ${pct(b, 2)})</span>` : "-");
  table($("#ht-val"), [
    { t: "判定", f: (x) => `<span class="badge ${VERD[x.verdict] || ""}">${x.verdict}</span>` },
    { t: "條件", k: "name", cls: "l" }, { t: "持有", f: (x) => x.best_h.replace("s_", "") },
    { t: "找門檻期（做空日均）", f: (x) => cell(x[x.best_h].disc, x[x.best_h].base_disc) },
    { t: "盲測期", f: (x) => cell(x[x.best_h].blind, x[x.best_h].base_blind) },
    { t: "盲測勝率", f: (x) => pct(x[x.best_h].blind.win_rate, 0) },
  ], r.results);
  const rc = (c, k) => { const s = c && c[k]; return s && s.n ? `${pctC(s.day_mean, 2)} <span class="muted">勝${pct(s.win_rate, 0)}</span>` : "-"; };
  const riskRows = [];
  (r.risk || []).forEach((x) => ["disc", "blind"].forEach((p) => x[p] && riskRows.push({ name: x.name, per: p, ...x[p] })));
  table($("#ht-risk"), [
    { t: "條件", k: "name", cls: "l" }, { t: "期間", f: (x) => (x.per === "disc" ? "找門檻期" : "盲測期") }, { t: "次數", k: "n" },
    { t: "24h 不設止損", f: (x) => rc(x, "s_24h") }, { t: "24h 止損 10%", f: (x) => rc(x, "s_24h_sl10") }, { t: "24h 止損 20%", f: (x) => rc(x, "s_24h_sl20") },
    { t: "6h 止損 10%", f: (x) => rc(x, "s_6h_sl10") },
    { t: "MAE 中位／90 分位", f: (x) => (x.mae ? `${pct(x.mae.p50, 0)}／${pct(x.mae.p90, 0)}` : "-") },
    { t: "曾逆漲 ≥20%／≥50%", f: (x) => (x.mae ? `${pct(x.mae.hit_20, 0)}／${pct(x.mae.hit_50, 0)}` : "-") },
    { t: "資金費 24h（空方）", f: (x) => (x.funding && x.funding.n ? `${pctC(x.funding.mean_24h, 3)} <span class="muted">n=${x.funding.n}</span>` : "-") },
    { t: "含資金費 24h", f: (x) => rc(x, "s_24h_with_funding") },
  ], riskRows);
  $("#ht-val-notes").innerHTML = (r.notes || []).map((n) => `<li>${esc(n)}</li>`).join("") +
    `<li>各類與 24h 做空報酬的等級相關：${Object.entries(r.spearman_24h || {}).map(([k, c]) => `${k} ${c == null ? "-" : c.toFixed(3)}`).join("、")}</li>`;
}

/* ---------------- AI chat ---------------- */
let aiConv = null, aiAbort = null, aiReady = false, aiBusy = false;
const TOOL_LABEL = {
  project_overview: "專案資料總覽", market_list: "全市場清單", price_summary: "K 線摘要", indicators: "指標", heat: "過熱分數",
  heat_validation: "過熱驗證", derivatives: "衍生品數據", live_market: "即時行情", rules_status: "規則狀態", rule_history: "規則歷史",
  pump_events: "回測事件", my_trades: "我的交易", trading_review: "操作診斷", bots: "機器人", positions: "目前持倉",
  paper_trading: "紙上交易", similar_situations: "相似情境", simulate_trade: "交易試算",
  fine_candles: "1s/1m K 線", recent_trades: "逐筆成交", analyze_event: "拆解事件", find_similar_events: "相似事件",
  test_template: "測試樣板", custom_rules: "自訂規則",
};
const QUICK = [
  "幫我全面分析這隻幣現在的狀況",
  "這隻幣現在過熱嗎？過熱分數可信嗎？",
  "歷史上類似現在的情況，之後通常怎麼走？",
  "我在這隻幣的交易有什麼問題？",
  "我的機器人在這隻幣表現如何？",
  "現在有沒有符合盲測通過的規則（rule03/04）？",
  "如果現在做空、止損 2×ATR、持有 6 小時，過去類似情況結果如何？",
];

function md(src) {
  const lines = esc(src || "").split("\n");
  const out = [];
  let list = null, table = null, code = false, codeBuf = [];
  const inline = (s) => s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})( UTC)?/g, '<span class="ai-ts" data-t="$1">$1$2</span>');
  const flush = () => {
    if (list) { out.push(`<${list.t}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.t}>`); list = null; }
    if (table) {
      const rows = table.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
      const cells = (r) => r.replace(/^\s*\||\|\s*$/g, "").split("|").map((c) => c.trim());
      out.push(`<table><thead><tr>${cells(rows[0]).map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows.slice(1).map((r) => `<tr>${cells(r).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      table = null;
    }
  };
  for (const ln of lines) {
    if (/^```/.test(ln)) { if (code) { out.push(`<pre class="ai-tool-detail">${codeBuf.join("\n")}</pre>`); codeBuf = []; } else flush(); code = !code; continue; }
    if (code) { codeBuf.push(ln); continue; }
    let m;
    if (/^\s*\|.*\|\s*$/.test(ln)) { if (list) flush(); (table = table || []).push(ln); continue; }
    if (table) flush();
    if ((m = ln.match(/^(#{1,4})\s+(.*)$/))) { flush(); out.push(`<h${Math.min(3, m[1].length)}>${inline(m[2])}</h${Math.min(3, m[1].length)}>`); continue; }
    if ((m = ln.match(/^\s*[-*]\s+(.*)$/))) { if (!list || list.t !== "ul") { flush(); list = { t: "ul", items: [] }; } list.items.push(m[1]); continue; }
    if ((m = ln.match(/^\s*\d+[.)]\s+(.*)$/))) { if (!list || list.t !== "ol") { flush(); list = { t: "ol", items: [] }; } list.items.push(m[1]); continue; }
    flush();
    out.push(ln.trim() ? `<p>${inline(ln)}</p>` : "");
  }
  if (code) out.push(`<pre class="ai-tool-detail">${codeBuf.join("\n")}</pre>`);
  flush();
  return out.join("");
}

function aiScroll() { const el = $("#ai-log"); el.scrollTop = el.scrollHeight; }
function aiBubble(role) {
  const d = document.createElement("div");
  d.className = `ai-msg ${role}`;
  $("#ai-log").appendChild(d);
  return d;
}
function aiToolChip(container, name, args, state) {
  const c = document.createElement("span");
  c.className = `ai-tool ${state}`;
  let a = "";
  try { const o = JSON.parse(args || "{}"); a = Object.entries(o).map(([k, v]) => `${k}=${v}`).join(" "); } catch { a = args || ""; }
  c.textContent = `${state === "run" ? "⏳ " : state === "bad" ? "✗ " : "✓ "}${TOOL_LABEL[name] || name}${a ? "（" + a + "）" : ""}`;
  container.appendChild(c);
  return c;
}

async function aiInit() {
  if (!aiReady) {
    aiReady = true;
    const info = await api("/api/chat/info").catch(() => null);
    if (info) {
      $("#ai-model").innerHTML = info.models.map((m) => `<option ${m === info.model ? "selected" : ""}>${m}</option>`).join("");
      if (!$("#ai-inst").value && info.watchlist.length) $("#ai-inst").value = info.watchlist[0];
      $("#ai-meta").textContent = info.enabled ? `今日工具呼叫 ${info.calls_today}/${info.limits.daily_calls}；可用工具 ${info.tools.length} 個` : "未設定 LLM（.env）";
    }
    $("#ai-quick").innerHTML = QUICK.map((q, i) => `<button data-i="${i}">${esc(q)}</button>`).join("");
    $("#ai-quick").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { $("#ai-text").value = QUICK[+b.dataset.i]; aiSend(); }));
    $("#btn-ai-send").addEventListener("click", () => aiSend());
    $("#btn-ai-stop").addEventListener("click", () => aiAbort && aiAbort.abort());
    $("#btn-ai-new").addEventListener("click", () => { aiConv = null; $("#ai-log").innerHTML = ""; aiLoadConvs(); $("#ai-text").focus(); });
    $("#ai-text").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); aiSend(); } });
    $("#ai-log").addEventListener("click", (e) => {
      const ts = e.target.closest(".ai-ts");
      if (ts) {
        const t = Date.parse(ts.dataset.t.replace(" ", "T") + ":00Z");
        const inst = ($("#ai-inst").value || "").trim().toUpperCase();
        if (inst && t && e.shiftKey) { openLab(inst.includes("-") ? inst : `${inst}-USDT-SWAP`, t); return; }
        if (inst && t) openKline(inst.includes("-") ? inst : `${inst}-USDT-SWAP`, t - 12 * 3600e3, t + 12 * 3600e3,
          [{ inst: inst.includes("-") ? inst : `${inst}-USDT-SWAP`, time: Math.floor(t / 300000) * 300, position: "aboveBar", shape: "arrowDown", color: "#ffcc00", text: "AI 提到" }]);
      }
      const chip = e.target.closest(".ai-tool");
      if (chip && chip._detail) {
        const n = chip.nextElementSibling;
        if (n && n.classList.contains("ai-tool-detail")) n.remove();
        else { const p = document.createElement("div"); p.className = "ai-tool-detail"; p.textContent = chip._detail; chip.after(p); }
      }
    });
  }
  aiLoadConvs();
}

async function aiLoadConvs() {
  const list = await api("/api/chat/conversations").catch(() => []);
  $("#ai-convs").innerHTML = list.map((c) => `<div class="ai-conv ${c.id === aiConv ? "sel" : ""}" data-id="${c.id}">
    <div class="t">${esc(c.title || "對話")}</div><div class="s"><span>${esc(c.inst || "")} ${new Date(c.updated).toLocaleString()}</span><span class="del" data-del="${c.id}">刪除</span></div></div>`).join("");
  $("#ai-convs").querySelectorAll(".ai-conv").forEach((d) => d.addEventListener("click", async (e) => {
    if (e.target.dataset.del) {
      if (!confirm("刪除這個對話？")) return;
      await post("/api/chat/delete", { conv_id: e.target.dataset.del }).catch(() => null);
      if (aiConv === e.target.dataset.del) { aiConv = null; $("#ai-log").innerHTML = ""; }
      return aiLoadConvs();
    }
    aiOpen(d.dataset.id);
  }));
}

async function aiOpen(id) {
  const r = await api(`/api/chat/conversation?id=${id}`).catch((e) => { toast(e.message, true); return null; });
  if (!r || !r.conversation) return;
  aiConv = id;
  if (r.conversation.inst) $("#ai-inst").value = r.conversation.inst;
  $("#ai-log").innerHTML = "";
  let tools = null;
  const results = {};
  r.messages.forEach((m) => { if (m.role === "tool") results[m.tool_call_id] = m; });
  for (const m of r.messages) {
    if (m.role === "user") { aiBubble("user").textContent = m.content; tools = null; }
    else if (m.role === "assistant") {
      if (m.tool_calls) {
        if (!tools) { tools = document.createElement("div"); tools.className = "ai-tools"; $("#ai-log").appendChild(tools); }
        m.tool_calls.forEach((tc) => {
          const res = results[tc.id];
          const chip = aiToolChip(tools, tc.function.name, tc.function.arguments, res && res._meta && res._meta.ok === false ? "bad" : "");
          chip._detail = res ? res.content : "";
        });
      }
      if (m.content) { aiBubble("assistant").innerHTML = md(m.content); tools = null; }
    }
  }
  aiLoadConvs();
  aiScroll();
}

async function aiSend(cont) {
  if (aiBusy) return;
  const text = $("#ai-text").value.trim();
  if (!text && !cont) return;
  aiBusy = true;
  $("#btn-ai-send").disabled = true;
  $("#ai-text").value = "";
  document.querySelectorAll(".ai-continue").forEach((b) => b.remove());
  if (text) aiBubble("user").textContent = text;
  const tools = document.createElement("div");
  tools.className = "ai-tools";
  $("#ai-log").appendChild(tools);
  let bubble = null, buf = "";
  const chips = {};
  aiAbort = new AbortController();
  try {
    const r = await fetch("/api/chat", {
      method: "POST", signal: aiAbort.signal,
      headers: { "x-okxrev": "1", "content-type": "application/json" },
      body: JSON.stringify({ conv_id: aiConv, text: text || null, inst: $("#ai-inst").value.trim() || null, model: $("#ai-model").value, cont: !!cont }),
    });
    if (!r.ok) { let d = null; try { d = await r.json(); } catch {} throw new Error((d && d.detail) || r.statusText); }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let rest = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      rest += dec.decode(value, { stream: true });
      const parts = rest.split("\n\n");
      rest = parts.pop();
      for (const p of parts) {
        if (!p.startsWith("data:")) continue;
        const ev = JSON.parse(p.slice(5));
        if (ev.type === "conv") aiConv = ev.id;
        else if (ev.type === "text") { if (!bubble) { bubble = aiBubble("assistant"); buf = ""; } buf += ev.text; bubble.innerHTML = md(buf); aiScroll(); }
        else if (ev.type === "tool_start") { if (bubble && buf) { bubble = null; } chips[ev.id] = aiToolChip(tools, ev.name, ev.args, "run"); aiScroll(); }
        else if (ev.type === "tool_end") {
          const c = chips[ev.id];
          if (c) { c.className = `ai-tool ${ev.ok ? "" : "bad"}`; c.textContent = c.textContent.replace("⏳ ", ev.ok ? "✓ " : "✗ ") + ` ${ev.ms}ms`; c._detail = ev.preview + (ev.chars > 400 ? `…（共 ${ev.chars} 字元）` : ""); }
        } else if (ev.type === "notice") { const n = document.createElement("div"); n.className = "ai-notice"; n.textContent = ev.text; $("#ai-log").appendChild(n); }
        else if (ev.type === "error") { toast(ev.text, true); const n = document.createElement("div"); n.className = "ai-notice"; n.textContent = "錯誤：" + ev.text; $("#ai-log").appendChild(n); }
        else if (ev.type === "done") {
          const n = document.createElement("div"); n.className = "muted"; n.style.fontSize = "11px";
          n.textContent = `查詢 ${ev.rounds} 回合、${ev.calls} 次工具、${Math.round(ev.chars / 1000)}k 字元`;
          $("#ai-log").appendChild(n);
          if (ev.exhausted) {
            const b = document.createElement("button"); b.className = "small ai-continue"; b.textContent = "繼續深入查詢";
            b.addEventListener("click", () => aiSend(true));
            $("#ai-log").appendChild(b);
          }
        }
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, true);
  } finally {
    aiBusy = false;
    $("#btn-ai-send").disabled = false;
    aiAbort = null;
    aiLoadConvs();
    aiScroll();
  }
}

/* ---------------- event lab ---------------- */
let labCharts = null, labReady = false, labEvent = null, labTpl = null, labTest = null, labJobRunning = false, labTimer = null, labLastLoad = 0, labPriceLines = [];
const SIDE_ZH = { short: "做空", long: "做多" };
const PER_ZH = { disc: "找參數期", valid: "驗證期", blind: "盲測期" };
const LAB_FIELDS = [
  { k: "rise_min", t: "漲幅 ≥", min: 0.15, max: 3, step: 0.01, f: "pct" },
  { k: "rise_max", t: "漲幅 ≤（空=不限）", min: 0.15, max: 5, step: 0.05, f: "pct", opt: true },
  { k: "rise_bars_max", t: "上漲時間 ≤", min: 6, max: 288, step: 6, f: "bars" },
  { k: "drop_min", t: "從高點跌 ≥", min: 0.05, max: 0.8, step: 0.01, f: "pct" },
  { k: "drop_max", t: "從高點跌 ≤（空=不限）", min: 0.05, max: 0.9, step: 0.01, f: "pct", opt: true },
  { k: "drop_bars_max", t: "高點後時間 ≤", min: 1, max: 143, step: 1, f: "bars" },
  { k: "retrace_min", t: "回吐漲幅 ≥", min: 0, max: 1, step: 0.05, f: "pct", opt: true },
  { k: "vol_min", t: "近 1h 量比 ≥", min: 1, max: 30, step: 0.5, f: "x", opt: true },
  { k: "ret_bar_max", t: "觸發 K 線漲跌 ≤", min: -0.3, max: 0, step: 0.01, f: "pct", opt: true },
  { k: "turnover_min", t: "24h 成交額 ≥ (M)", min: 0, max: 500, step: 1, f: "m", opt: true },
  { k: "turnover_max", t: "24h 成交額 ≤ (M)", min: 0, max: 500, step: 1, f: "m", opt: true },
  { k: "age_max_d", t: "上市 ≤ 天", min: 1, max: 365, step: 1, f: "d", opt: true },
  { k: "cooldown", t: "同幣冷卻（根）", min: 12, max: 864, step: 12, f: "bars" },
];
const fmtBars = (b) => (b >= 12 ? `${(b * 5 / 60).toFixed(1)}h` : `${b * 5}m`);
function labFmt(f, v) {
  if (v == null || v === "") return "不限";
  if (f === "pct") return pct(v, 0);
  if (f === "bars") return fmtBars(v);
  if (f === "x") return `${(+v).toFixed(1)}x`;
  if (f === "m") return `${(+v / 1e6).toFixed(0)}M`;
  return String(v);
}
const labInst = () => { const s = ($("#lab-inst").value || "").trim().toUpperCase(); return !s ? "" : s.includes("-") ? (s.endsWith("-SWAP") ? s : `${s}-SWAP`) : `${s}-USDT-SWAP`; };
const labMode = () => document.querySelector('input[name="lab-mode"]:checked').value;

async function labInit() {
  if (!labReady) {
    labReady = true;
    $("#btn-lab-load").addEventListener("click", () => labLoad(true));
    $("#lab-inst").addEventListener("keydown", (e) => { if (e.key === "Enter") labLoad(true); });
    $("#lab-bar").addEventListener("change", () => labLoad(true));
    document.querySelectorAll('input[name="lab-mode"]').forEach((r) => r.addEventListener("change", () => labLoad(true)));
    $("#btn-lab-mark").addEventListener("click", labMarkFromChart);
    $("#btn-lab-ask").addEventListener("click", labAskAI);
    $("#btn-lab-recent").addEventListener("click", () => labRecent(true));
    $("#btn-lab-index").addEventListener("click", async () => { try { const r = await post("/api/lab/build-index"); toast(r.message, !r.ok); labPollJob(); } catch (e) { toast(e.message, true); } });
    $("#btn-lab-stop").addEventListener("click", async () => { try { const r = await post("/api/lab/stop"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); } });
    $("#btn-lab-similar").addEventListener("click", labSimilarStart);
    $("#btn-lab-blind").addEventListener("click", labBlind);
    $("#btn-lab-save").addEventListener("click", labSave);
    const st = await api("/api/lab/status").catch(() => null);
    if (st) $("#lab-rule-exit").innerHTML = Object.entries(st.exits).map(([k, v]) => `<option value="${k}" ${k === "6h" ? "selected" : ""}>${esc(v)}</option>`).join("");
    if (!$("#lab-inst").value) {
      const info = await api("/api/chat/info").catch(() => null);
      $("#lab-inst").value = (info && info.watchlist[0]) || "BTC-USDT-SWAP";
    }
    labLoad(true);
    labSimilarShow();
  }
  labStatus();
  labRecent();
  labRules();
  labPollJob();
}

function labMakeCharts() {
  if (labCharts) return labCharts;
  const base = { layout: { background: { color: "#171d24" }, textColor: "#d6dde4" }, grid: { vertLines: { color: "#222b35" }, horzLines: { color: "#222b35" } },
    timeScale: { timeVisible: true, secondsVisible: true, borderColor: "#2a323c" }, rightPriceScale: { borderColor: "#2a323c", minimumWidth: 80 }, crosshair: { mode: 0 } };
  const p = LightweightCharts.createChart($("#lab-price"), base);
  const v = LightweightCharts.createChart($("#lab-vol"), base);
  const cs = p.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350", borderVisible: false, wickUpColor: "#26a69a", wickDownColor: "#ef5350",
    priceFormat: { type: "custom", formatter: (x) => x.toPrecision(6), minMove: 1e-9 } });
  const vw = p.addLineSeries({ color: "#f0a020", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, title: "VWAP" });
  const vs = v.addHistogramSeries({ priceFormat: { type: "volume" } });
  let sync = false;
  [p, v].forEach((c) => c.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (sync || !r) return; sync = true; [p, v].forEach((o) => o !== c && o.timeScale().setVisibleLogicalRange(r)); sync = false;
  }));
  labCharts = { p, v, cs, vw, vs };
  return labCharts;
}

async function labLoad(fit, quiet) {
  const inst = labInst();
  if (!inst) return;
  const bar = $("#lab-bar").value;
  const q = new URLSearchParams({ inst, bar, trades: labMode() === "live" });
  if (labMode() === "range") {
    const s = fromLocalInput($("#lab-start").value), e = fromLocalInput($("#lab-end").value);
    if (s) q.set("start", s);
    if (e) q.set("end", e);
  }
  if (!quiet) $("#lab-live-meta").textContent = "載入中…";
  let d;
  try { d = await api(`/api/lab/fine?${q}`); } catch (e) { $("#lab-live-meta").textContent = ""; if (!quiet) toast(e.message, true); return; }
  labLastLoad = Date.now();
  const c = labMakeCharts();
  c.p.resize($("#lab-price").clientWidth, 340); c.v.resize($("#lab-vol").clientWidth, 110);
  const cd = [], vd = [], vw = [];
  let pv = 0, vv = 0;
  for (let i = 0; i < d.t.length; i++) {
    cd.push({ time: d.t[i], open: d.o[i], high: d.h[i], low: d.l[i], close: d.c[i] });
    vd.push({ time: d.t[i], value: d.v[i] ?? 0, color: d.c[i] >= d.o[i] ? "rgba(38,166,154,.6)" : "rgba(239,83,80,.6)" });
    const typ = (d.h[i] + d.l[i] + d.c[i]) / 3; pv += typ * (d.v[i] || 0); vv += d.v[i] || 0;
    if (vv > 0) vw.push({ time: d.t[i], value: pv / vv });
  }
  c.cs.setData(cd); c.vs.setData(vd); c.vw.setData(vw);
  const marks = [];
  if (labEvent && labEvent.inst === inst) {
    const b = labEvent.bars;
    const add = (t, txt, col, pos) => t && marks.push({ time: Math.floor(t / 1000), position: pos, shape: pos === "aboveBar" ? "arrowDown" : "arrowUp", color: col, text: txt });
    add(b.base, "起漲", "#4aa3ff", "belowBar"); add(b.peak, "高點", "#ffcc00", "aboveBar"); add(b.trigger, "觸發", "#ff6f00", "aboveBar"); add(b.crash_low, "低點", "#26a69a", "belowBar");
  }
  const tmin = d.t[0] || 0, tmax = d.t[d.t.length - 1] || 0;
  c.cs.setMarkers(marks.filter((m) => m.time >= tmin && m.time <= tmax).sort((a, b) => a.time - b.time));
  labPriceLines.forEach((l) => c.cs.removePriceLine(l));
  labPriceLines = [];
  try {
    const pos = (await api("/api/account/positions").catch(() => null))?.positions || [];
    pos.filter((p) => p.inst_id === inst).forEach((p) => {
      [[p.entry_px, "#bbbbbb", "進場"], [p.sl_px, "#ef5350", "止損"], [p.liq_px, "#ff00aa", "強平"], [p.tp_px, "#26a69a", "止盈"]].forEach(([px, col, t]) => {
        if (px) labPriceLines.push(c.cs.createPriceLine({ price: px, color: col, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: t }));
      });
    });
  } catch { /* no positions */ }
  if (fit) c.p.timeScale().fitContent();
  if (labMode() === "range" || fit) { $("#lab-start").value = toLocalInput(d.start); $("#lab-end").value = toLocalInput(d.end); }
  $("#lab-summary").textContent = d.summary || "";
  const m = d.micro || {}, w = m.windows || {};
  const row = (k) => w[k] ? [k, pctC(w[k].change, 2), w[k].vol_ratio == null ? "-" : `${w[k].vol_ratio.toFixed(1)}x`, pct(w[k].upper_wick, 0), pct(w[k].lower_wick, 0), pct(w[k].range_pct, 2)] : null;
  $("#lab-micro").innerHTML = `<thead><tr><th>區間</th><th>漲跌</th><th>量比</th><th>上影</th><th>下影</th><th>振幅</th></tr></thead><tbody>${["1m", "5m", "15m"].map(row).filter(Boolean).map((r) => `<tr>${r.map((x) => `<td>${x}</td>`).join("")}</tr>`).join("")}</tbody>
    <tbody><tr><td colspan="6" class="l muted">離 VWAP ${pctC(m.dist_vwap, 2)}；區間高 ${m.range_high?.toPrecision(5) ?? "-"}（${m.range_high_at ? utc(m.range_high_at, false) : "-"}）低 ${m.range_low?.toPrecision(5) ?? "-"}；
    最大單根跌 ${m.biggest_down_bar ? pct(m.biggest_down_bar.change, 2) : "-"}；年化波動 ${m.rv_annual == null ? "-" : pct(m.rv_annual, 0)}</td></tr></tbody>`;
  if (d.flow) {
    const f = d.flow;
    $("#lab-flow").textContent = `${f.n} 筆／${f.seconds.toFixed(0)} 秒；主動買 ${pct(f.buy_share_notional, 0)}；大單 買 ${f.big_buy} 賣 ${f.big_sell}`;
    const thr = f.big_threshold_notional;
    table($("#lab-trades"), [
      { t: "時間", f: (x) => new Date(x.ts).toISOString().slice(11, 19) },
      { t: "方向", f: (x) => (x.side === "buy" ? '<span class="pos">買</span>' : '<span class="neg">賣</span>') },
      { t: "價格", f: (x) => Number(x.px).toPrecision(6) },
      { t: "數量", f: (x) => (x.px * x.sz >= thr ? `<b>${fmtN(x.sz)}</b>` : fmtN(x.sz)) },
    ], d.trades || []);
    table($("#lab-liq"), [
      { t: "時間", f: (x) => utc(x.ts) }, { t: "爆倉方", f: (x) => (x.pos_side === "short" ? '<span class="pos">空單</span>' : '<span class="neg">多單</span>') },
      { t: "價格", f: (x) => Number(x.px).toPrecision(5) }, { t: "張數", f: (x) => fmtN(x.sz) },
    ], d.liquidations || []);
  } else if (labMode() === "range") { $("#lab-flow").textContent = "（指定時段不顯示即時成交）"; $("#lab-trades").innerHTML = ""; $("#lab-liq").innerHTML = ""; }
  $("#lab-live-meta").textContent = `${d.t.length} 根 ${bar}${labMode() === "live" ? `，${bar === "1s" ? "每 2 秒" : "每 10 秒"}更新` : ""}；${new Date().toLocaleTimeString()}`;
}

function labTick() {
  if (document.hidden || currentTab !== "lab" || labMode() !== "live") return;
  const every = $("#lab-bar").value === "1s" ? 2000 : 10000;
  if (Date.now() - labLastLoad >= every - 200) labLoad(false, true);
}
setInterval(labTick, 1000);

function labMarkFromChart() {
  const inst = labInst();
  if (!inst || !labCharts) return;
  const r = labCharts.p.timeScale().getVisibleRange();
  const s = r ? r.from * 1000 : Date.now() - 6 * 3600e3, e = r ? r.to * 1000 : Date.now();
  labAnalyze(inst, Math.round((s + e) / 2), true);
}

async function labRecent(refresh) {
  const rows = await api(`/api/lab/recent?hours=24${refresh ? "&refresh=1" : ""}`).catch((e) => { toast(e.message, true); return []; });
  table($("#lab-recent"), [
    { t: "合約", k: "inst", cls: "l" }, { t: "漲幅", k: "rise", f: (r) => pctC(r.rise, 0) }, { t: "上漲", f: (r) => `${r.rise_hours.toFixed(1)}h` },
    { t: "高點後跌", k: "drop", f: (r) => pctC(-r.drop, 0) }, { t: "跌落", f: (r) => `${r.drop_hours.toFixed(1)}h` },
    { t: "高點", f: (r) => utc(r.pk_ts) }, { t: "現在距高點", f: (r) => pctC(-r.now_drop, 0) }, { t: "24h 成交額", f: (r) => `${(r.turnover24 / 1e6).toFixed(1)}M` },
  ], rows, { sortable: true, onClick: (r) => labAnalyze(r.inst, r.pk_ts, true) });
}

async function labStatus() {
  const s = await api("/api/lab/status").catch(() => null);
  if (!s) return;
  const ix = s.index;
  $("#lab-index-meta").innerHTML = ix.exists
    ? `索引 ${fmtN(ix.rows)} 列（主資料 ${fmtN(ix.by_src.main)}、舊資料 ${fmtN(ix.by_src.old || 0)}），${ix.age_h} 小時前建立${ix.age_h > 24 ? ' <span class="badge warn">建議更新</span>' : ""}；已存規則 ${s.rules} 條；盲測已使用 ${s.blind_attempts} 次`
    : '<span class="badge warn">尚未建立索引</span> 先按「建立／更新索引」（約 2-3 分鐘，背景低優先權）';
}

async function labPollJob() {
  const j = await api("/api/lab/job").catch(() => null);
  if (!j) return;
  if (j.running || j.log.length) { $("#lab-log").style.display = ""; renderLog($("#lab-log"), j.log); }
  $("#btn-lab-index").disabled = $("#btn-lab-similar").disabled = j.running;
  if (labJobRunning && !j.running) {
    toast(j.returncode === 0 ? "事件實驗室背景工作完成" : `背景工作結束，代碼 ${j.returncode}`, j.returncode !== 0);
    labStatus();
    if ((j.args || []).includes("--similar")) labSimilarShow();
  }
  labJobRunning = j.running;
}

async function labAnalyze(inst, at, showChart) {
  $("#lab-inst").value = inst;
  $("#lab-event-panel").style.display = "";
  $("#lab-ev-title").textContent = `${inst} 分析中…`;
  let a;
  try { a = await api(`/api/lab/analyze?inst=${encodeURIComponent(inst)}&at=${Math.round(at)}`); } catch (e) { $("#lab-ev-title").textContent = ""; return toast(e.message, true); }
  labEvent = a;
  const s = a.shape, b = a.bars;
  $("#lab-ev-title").innerHTML = `${esc(inst)}　高點 ${utc(b.peak)} UTC <button id="btn-lab-ev-ai" class="small">問 AI 這個事件</button>`;
  $("#btn-lab-ev-ai").addEventListener("click", () => labAskAI(true));
  const rows = [
    ["起漲（基準低點）", `${utc(b.base)}　${a.prices.base.toPrecision(5)}`], ["高點", `${utc(b.peak)}　${a.prices.peak.toPrecision(5)}`],
    ["觸發點（樣板）", b.trigger ? `${utc(b.trigger)}　${a.prices.trigger.toPrecision(5)}` : "-"], ["崩跌低點", `${utc(b.crash_low)}　${a.prices.crash_low.toPrecision(5)}`],
    ["漲幅／花費", `${pct(s.rise, 1)}／${s.rise_hours.toFixed(1)} 小時`], ["跌幅（高點起）／花費", `${pct(s.drop_total, 1)}／${s.drop_hours.toFixed(1)} 小時`],
    ["回吐漲幅", pct(s.retrace_total, 0)], ["起漲前 24h", pctC(s.pre_r24h)], ["最大量比 上漲／下跌", `${num(s.vmax_rise, 1)}x／${num(s.vmax_drop, 1)}x`],
    ["高點時 24h 成交額", s.turnover24_at_peak ? `${(s.turnover24_at_peak / 1e6).toFixed(1)}M` : "-"], ["BTC（高點時 4h）", `${pctC(s.btc_r4h_at_peak, 2)} ${esc(s.btc_regime_at_peak)}`],
    ["上市天數", a.age_d_at_peak == null ? "-" : a.age_d_at_peak.toFixed(1)],
    ["觸發後（下一根開盤）", Object.entries(a.after_trigger || {}).map(([k, v]) => `${k} ${pctC(v, 1)}`).join("　") || "-"],
  ];
  $("#lab-ev-shape").innerHTML = rows.map(([k, v]) => `<tr><td class="l muted">${k}</td><td class="l">${v}</td></tr>`).join("");
  const f = (a.fine || {}).m1, s1 = (a.fine || {}).s1;
  const fr = f ? [
    ["1m 高點", `${utc(f.peak_ts)}　${f.peak.toPrecision(5)}`], ["1m 起漲→高點", `${f.rise_min} 分`], ["1m 高點→低點", `${f.drop_min} 分`],
    ["最大 5 分跌", pctC(f.max_5m_drop, 1)], ["最大 1 分跌", pctC(f.max_1m_drop, 1)], ["最大 5 分漲（高點前）", pctC(f.max_5m_rise, 1)],
    ["高點那根上影", pct(f.peak_upper_wick, 0)], ["量集中度（最大/平均）", num(f.vol_conc, 1)], ["高點前後 10 分量占比", pct(f.peak_vol_share, 0)],
  ] : [["1m", (a.fine || {}).error || "無資料"]];
  if (s1) fr.push(["1s 最大 10 秒跌／漲", `${pct(s1.max_10s_drop, 1)}／${pct(s1.max_10s_rise, 1)}`]);
  $("#lab-ev-fine").innerHTML = fr.map(([k, v]) => `<tr><td class="l muted">${k}</td><td class="l">${v}</td></tr>`).join("");
  const dv = a.derivs || {};
  const parts = [];
  if (dv.funding) parts.push(`資金費率 ${dv.funding.slice(-3).map((x) => pct(x.rate, 3)).join(" → ")}`);
  if (dv.oi && dv.oi.length > 1) parts.push(`OI ${pctC(dv.oi[dv.oi.length - 1].oi_usd / dv.oi[0].oi_usd - 1)}`);
  if (dv.taker) { const bsum = dv.taker.reduce((s, x) => s + (x.buy || 0), 0), ssum = dv.taker.reduce((s, x) => s + (x.sell || 0), 0); if (bsum + ssum > 0) parts.push(`主動買占比 ${pct(bsum / (bsum + ssum), 0)}`); }
  if (dv.liquidations) parts.push(`爆倉 ${dv.liquidations.length} 筆（空單 ${dv.liquidations.filter((x) => x.pos_side === "short").length}）`);
  $("#lab-ev-derivs").textContent = parts.join("；") || "期間沒有衍生品資料";
  labSetTemplate(a.template, a.template_desc);
  if (showChart) {
    document.querySelector('input[name="lab-mode"][value="range"]').checked = true;
    $("#lab-bar").value = "1m";
    const s0 = Math.max(b.base - 3600e3, b.peak - 2.9 * 86400e3), e0 = Math.min(b.crash_low + 6 * 3600e3, s0 + 2.95 * 86400e3);
    $("#lab-start").value = toLocalInput(s0); $("#lab-end").value = toLocalInput(e0);
    labLoad(true);
  }
  $("#lab-event-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function labSetTemplate(t, desc) {
  labTpl = { ...t };
  $("#lab-tpl-panel").style.display = "";
  const el = $("#lab-tpl");
  el.innerHTML = LAB_FIELDS.map((f) => {
    const v = labTpl[f.k];
    return `<label><span class="k">${f.t}</span>${f.opt ? `<input type="checkbox" data-on="${f.k}" ${v != null ? "checked" : ""}>` : ""}
      <input type="range" data-k="${f.k}" min="${f.min}" max="${f.max}" step="${f.step}" value="${v ?? f.min}" ${f.opt && v == null ? "disabled" : ""}>
      <span class="v" id="lab-v-${f.k}">${labFmt(f.f, f.f === "m" && v != null ? v : v)}</span></label>`;
  }).join("") + `<label><span class="k">BTC 環境</span>${["weak", "neutral", "strong"].map((b) => `<label><input type="checkbox" data-btc="${b}" ${(labTpl.btc || []).includes(b) ? "checked" : ""}>${{ weak: "弱", neutral: "中", strong: "強" }[b]}</label>`).join("")}<span class="muted">（都不勾=不限）</span></label>`;
  el.querySelectorAll("input[type=range]").forEach((r) => r.addEventListener("input", () => {
    const f = LAB_FIELDS.find((x) => x.k === r.dataset.k);
    const v = f.f === "m" ? +r.value * 1e6 : +r.value;
    labTpl[f.k] = v;
    $(`#lab-v-${f.k}`).textContent = labFmt(f.f, v);
    labTestSoon();
  }));
  el.querySelectorAll("input[data-on]").forEach((cb) => cb.addEventListener("change", () => {
    const k = cb.dataset.on, r = el.querySelector(`input[data-k="${k}"]`), f = LAB_FIELDS.find((x) => x.k === k);
    r.disabled = !cb.checked;
    labTpl[k] = cb.checked ? (f.f === "m" ? +r.value * 1e6 : +r.value) : null;
    $(`#lab-v-${k}`).textContent = labFmt(f.f, labTpl[k]);
    labTestSoon();
  }));
  el.querySelectorAll("input[data-btc]").forEach((cb) => cb.addEventListener("change", () => {
    labTpl.btc = [...el.querySelectorAll("input[data-btc]:checked")].map((x) => x.dataset.btc);
    labTestSoon();
  }));
  LAB_FIELDS.filter((f) => f.f === "m").forEach((f) => { const r = el.querySelector(`input[data-k="${f.k}"]`); if (labTpl[f.k] != null) r.value = labTpl[f.k] / 1e6; });
  $("#lab-tpl-desc").textContent = desc || "";
  labRunTest(false);
}

function labTestSoon() { clearTimeout(labTimer); labTimer = setTimeout(() => labRunTest(false), 350); }

async function labRunTest(blind) {
  if (!labTpl) return;
  let r;
  try { r = await post("/api/lab/test", { template: labTpl, blind }); } catch (e) { return toast(e.message, true); }
  labTest = r;
  $("#lab-tpl-desc").textContent = r.desc;
  $("#lab-res-panel").style.display = "";
  const np = r.n_by_period;
  $("#lab-res-meta").textContent = `觸發 ${fmtN(r.n)} 次（${r.n_insts} 個幣）：找參數期 ${np.disc}、驗證期 ${np.valid}${r.blind_shown ? `、盲測期 ${np.blind}` : ""}；切分 ${r.periods.split_str}`;
  $("#lab-blind-note").textContent = r.blind_shown ? (r.blind_done_before ? "這個樣板已做過盲測（結果固定顯示）" : "已執行盲測") : `盲測期 ${np.blind} 次觸發（未顯示）；全部樣板已用盲測 ${r.blind_attempts_total} 次，次數越多越不可信`;
  $("#btn-lab-blind").disabled = r.blind_shown;
  const cell = (s, base) => (s && s.n ? `${pctC(s.day_mean, 2)} <span class="muted">${s.n}/${s.n_days}天 勝${pct(s.win_rate, 0)}${base != null ? ` 隨機${pct(base, 2)}` : ""}</span>` : "-");
  const cols = [
    { t: "方向", f: (c) => SIDE_ZH[c.side] }, { t: "出場", f: (c) => esc(c.exit) },
    { t: "找參數期（日均）", f: (c) => cell(c.disc, c.base_disc) }, { t: "驗證期", f: (c) => cell(c.valid, c.base_valid) },
    { t: "驗證 95% 區間", f: (c) => (c.valid && c.valid.ci95 ? `${pct(c.valid.ci95[0], 2)} ～ ${pct(c.valid.ci95[1], 2)}` : "-") },
  ];
  if (r.blind_shown) cols.push({ t: "盲測期", f: (c) => cell(c.blind, c.base_blind) });
  cols.push({ t: "判定", f: (c) => `<span class="badge ${/可用|驗證通過/.test(c.verdict) ? "ok" : /觀察|為正/.test(c.verdict) ? "warn" : "bad"}">${esc(c.verdict)}</span>${c.side === r.best.side && c.exit === r.best.exit ? " ★" : ""}` });
  table($("#lab-res"), cols, r.combos);
  table($("#lab-groups"), [{ t: "分組", f: (g) => `${esc(g.group)}：${esc(g.value)}` }, { t: "次數", k: "n" }, { t: "日均", f: (g) => pctC(g.day_mean, 2) }, { t: "勝率", f: (g) => pct(g.win_rate, 0) }, { t: "隨機", f: (g) => pct(g.base, 2) }], r.groups);
  table($("#lab-months"), [{ t: "月", k: "m" }, { t: "次數", k: "n" }, { t: "平均（★組合）", f: (m) => pctC(m.mean, 2) }], r.by_month);
  table($("#lab-examples"), [
    { t: "合約", k: "inst", cls: "l" }, { t: "觸發", f: (e) => utc(e.ts) }, { t: "期間", f: (e) => PER_ZH[e.period] },
    { t: "漲幅", f: (e) => pct(e.rise, 0) }, { t: "跌", f: (e) => pct(e.drop, 0) }, { t: "量比", f: (e) => num(e.vmax_1h, 1) },
    { t: "★報酬", f: (e) => pctC(e.ret, 2) }, { t: "24h 最大漲／跌", f: (e) => `${pct(e.up24, 0)}／${pct(e.dn24, 0)}` },
  ], r.examples, { onClick: (e) => labOpenExample(e) });
  $("#lab-rule-side").value = r.best.side;
  if ([...$("#lab-rule-exit").options].some((o) => o.value === r.best.exit)) $("#lab-rule-exit").value = r.best.exit;
}

function labOpenExample(e) {
  $("#lab-inst").value = e.inst;
  document.querySelector('input[name="lab-mode"][value="range"]').checked = true;
  $("#lab-bar").value = "1m";
  const s0 = Math.max((e.lo_ts || e.ts) - 3600e3, e.ts - 2.5 * 86400e3);
  $("#lab-start").value = toLocalInput(s0); $("#lab-end").value = toLocalInput(Math.min(e.ts + 12 * 3600e3, s0 + 2.95 * 86400e3));
  labLoad(true);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function labBlind() {
  if (!labTpl) return;
  if (!confirm("盲測期（2024-10～2025-09）只該看一次：看過後再調整樣板，盲測就不再是「沒看過的資料」。確定執行？")) return;
  await labRunTest(true);
  labStatus();
}

async function labSave() {
  if (!labTpl) return;
  const name = $("#lab-rule-name").value.trim() || (labEvent ? `${labEvent.inst.split("-")[0]} ${utc(labEvent.bars.peak).slice(5)}` : "自訂規則");
  try {
    const r = await post("/api/lab/rules", { name, template: labTpl, side: $("#lab-rule-side").value, exit: $("#lab-rule-exit").value,
      source: labEvent ? { inst: labEvent.inst, peak: labEvent.bars.peak, trigger: labEvent.bars.trigger } : null });
    toast(r.message);
    labRules(); labStatus();
  } catch (e) { toast(e.message, true); }
}

async function labRules() {
  const rules = await api("/api/lab/rules").catch(() => []);
  table($("#lab-rules"), [
    { t: "ID", f: (r) => `lab_${r.id}` }, { t: "名稱", k: "name", cls: "l" }, { t: "條件", cls: "l wrap", f: (r) => esc(r.desc) },
    { t: "方向／出場", f: (r) => `${SIDE_ZH[r.side]} ${esc(r.exit)}` },
    { t: "驗證期", f: (r) => pctC(r.result?.valid?.day_mean, 2) }, { t: "盲測期", f: (r) => (r.result?.blind ? pctC(r.result.blind.day_mean, 2) : '<span class="muted">未測</span>') },
    { t: "判定", f: (r) => `<span class="badge ${/可用|驗證通過/.test(r.verdict_label) ? "ok" : /觀察|為正/.test(r.verdict_label) ? "warn" : "bad"}">${esc(r.verdict_label)}</span>` },
    { t: "即時觸發", f: (r) => `${r.live.triggers}${r.live.last ? `（最近 ${utc(r.live.last)}）` : ""}` }, { t: "已平倉平均", f: (r) => (r.live.closed ? `${pctC(r.live.mean, 2)}（${r.live.closed}）` : "-") },
    { t: "狀態", f: (r) => (r.active === false ? '<span class="badge">暫停</span>' : '<span class="badge ok">觀察中</span>') },
    { t: "", f: (r) => `<button class="small" data-op="load" data-id="${r.id}">載入樣板</button> <button class="small" data-op="${r.active === false ? "resume" : "pause"}" data-id="${r.id}">${r.active === false ? "恢復" : "暫停"}</button>
      ${r.result?.blind_tested ? "" : `<button class="small" data-op="blind" data-id="${r.id}">盲測</button>`} <button class="small" data-op="delete" data-id="${r.id}">刪除</button>` },
  ], rules);
  $("#lab-rules").querySelectorAll("button[data-op]").forEach((b) => b.addEventListener("click", async () => {
    const r = rules.find((x) => x.id === b.dataset.id);
    if (b.dataset.op === "load") { labSetTemplate(r.template, r.desc); $("#lab-rule-side").value = r.side; $("#lab-rule-exit").value = r.exit; return; }
    if (b.dataset.op === "delete" && !confirm(`刪除規則 ${r.name}？（紙上交易紀錄保留）`)) return;
    if (b.dataset.op === "blind" && !confirm("執行盲測（只該看一次）？")) return;
    try { const x = await post("/api/lab/rules/op", { id: r.id, op: b.dataset.op }); toast(x.message); labRules(); labStatus(); } catch (e) { toast(e.message, true); }
  }));
}

async function labSimilarStart() {
  if (!labEvent) return toast("先在①選一個事件或用「標成事件」", true);
  try {
    const r = await post("/api/lab/similar", { inst: labEvent.inst, at: labEvent.bars.peak, confirm: +$("#lab-confirm").value || 0 });
    toast(r.message, !r.ok);
    $("#lab-sim-meta").textContent = "背景執行中（進度見右上「事件索引」區的日誌）";
    labPollJob();
  } catch (e) { toast(e.message, true); }
}

async function labSimilarShow() {
  const d = await api("/api/lab/similar").catch(() => null);
  if (!d) return;
  const s = d.similar, ev = d.event;
  $("#lab-sim-meta").innerHTML = `來源 ${esc(ev.inst)} 高點 ${utc(ev.bars.peak)}；${s.n} 筆相似（${new Date(d.generated).toLocaleString()}）；1m 確認前 ${s.confirmed_n} 筆。${esc(s.note)}
    <button class="small" id="btn-lab-sim-ev">載入這個事件</button>`;
  $("#btn-lab-sim-ev").addEventListener("click", () => labAnalyze(ev.inst, ev.bars.peak, true));
  const conf = s.rows.filter((r) => r.m1_like === true);
  const sumCols = [{ t: "方向", f: (x) => SIDE_ZH[x.side] }, { t: "出場", k: "exit" }, { t: "次數", k: "n" }, { t: "日均", f: (x) => pctC(x.day_mean, 2) },
    { t: "中位數", f: (x) => pctC(x.median, 2) }, { t: "勝率", f: (x) => pct(x.win_rate, 0) }, { t: "隨機", f: (x) => pct(x.base, 2) },
    { t: "1m 也像的（" + conf.length + "）", f: (x) => { const col = `${x.side === "short" ? "S" : "L"}_${x.exit}`; const v = conf.map((r) => r[col]).filter((y) => y != null); return v.length ? pctC(v.reduce((a, b) => a + b, 0) / v.length, 2) : "-"; } }];
  table($("#lab-sim-sum"), sumCols, s.summary);
  table($("#lab-sim"), [
    { t: "#", f: (r) => s.rows.indexOf(r) + 1 }, { t: "合約", k: "inst", cls: "l" }, { t: "觸發", f: (r) => utc(r.ts) }, { t: "期間", f: (r) => PER_ZH[r.period] },
    { t: "距離", f: (r) => num(r.dist, 2) }, { t: "1m", f: (r) => (r.m1_like === true ? '<span class="badge ok">像</span>' : r.m1_like === false ? '<span class="badge">不像</span>' : `<span class="muted">${esc(r.m1_note || "-")}</span>`) },
    { t: "漲幅", f: (r) => pct(r.rise, 0) }, { t: "上漲", f: (r) => fmtBars(r.rise_bars) }, { t: "跌", f: (r) => pct(r.drop, 0) },
    { t: "空 1h", f: (r) => pctC(r.S_1h, 1) }, { t: "空 6h", f: (r) => pctC(r.S_6h, 1) }, { t: "空 24h", f: (r) => pctC(r.S_24h, 1) },
  ], s.rows, { onClick: (r) => labOpenExample(r) });
}

function labAskAI(withEvent) {
  const inst = labInst();
  $("#ai-inst").value = inst;
  let q;
  if (withEvent === true && labEvent) {
    q = `分析 ${inst} 在 ${utc(labEvent.bars.peak)} UTC 高點附近那波急漲後爆跌：用 analyze_event 拆解、find_similar_events 找歷史類似案例，說明做空還是做多比較有優勢、證據強弱。`;
  } else {
    const bar = $("#lab-bar").value;
    q = `看 ${inst} 現在的 ${bar} K 線（fine_candles）和最近成交（recent_trades），描述目前的短線結構與風險。`;
  }
  showTab("ai");
  aiConv = null; $("#ai-log").innerHTML = "";
  $("#ai-text").value = q;
  setTimeout(() => aiSend(), 300);
}

function openLab(inst, at) {
  showTab("lab");
  setTimeout(() => labAnalyze(inst, at, true), 300);
}

/* ---------------- settings panel ---------------- */
let setData = null, setLastFocus = null;
const fmtSetVal = (f, v) => {
  if (v == null) return "-";
  if (f.type === "bool") return v ? "開" : "關";
  if (f.type === "choice") return f.options[v] || v;
  if (Array.isArray(v)) return v.length ? v.join(" ") : "（空）";
  return String(v);
};
function setInput(f) {
  const id = `set-${f.path.replace(/\./g, "-")}`;
  const v = f.value;
  if (f.type === "bool") return `<input type="checkbox" id="${id}" ${v ? "checked" : ""}>`;
  if (f.type === "choice") return `<select id="${id}">${Object.entries(f.options).map(([k, l]) => `<option value="${esc(k)}" ${k === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  if (f.type === "insts") return `<input id="${id}" value="${esc((v || []).join(" "))}" placeholder="例如 CT PUMP">`;
  if (f.type === "intlist") return `<input id="${id}" value="${esc((v || []).join(" "))}" placeholder="例如 80 90">`;
  return `<input type="number" id="${id}" value="${v ?? ""}" min="${f.min}" max="${f.max}" step="${f.type === "int" ? 1 : "any"}">`;
}
function readInput(f) {
  const el = $(`#set-${f.path.replace(/\./g, "-")}`);
  if (f.type === "bool") return el.checked;
  if (f.type === "choice") return el.value;
  if (f.type === "insts") return el.value;
  if (f.type === "intlist") return el.value.split(/[\s,]+/).filter(Boolean).map(Number);
  return el.value === "" ? null : Number(el.value);
}
function sameVal(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

async function openSettings(group) {
  setLastFocus = document.activeElement;
  $("#set-panel").classList.add("open");
  $("#set-panel").setAttribute("aria-hidden", "false");
  $("#set-backdrop").hidden = false;
  await loadSettings();
  const sec = group && $(`#set-sec-${group}`);
  if (sec) { sec.open = true; sec.scrollIntoView({ block: "start" }); sec.classList.add("flash"); }
  $("#set-close").focus();
}
function closeSettings() {
  $("#set-panel").classList.remove("open");
  $("#set-panel").setAttribute("aria-hidden", "true");
  $("#set-backdrop").hidden = true;
  if (setLastFocus) setLastFocus.focus();
}
async function loadSettings() {
  let d;
  try { d = await api("/api/settings"); } catch (e) { $("#set-body").innerHTML = `<p class="neg">${esc(e.message)}</p>`; return; }
  setData = d;
  const st = d.status;
  const c = collectState;
  $("#set-status").innerHTML = [
    `OKX 唯讀金鑰：${st.okx_key ? '<span class="pos">已設定</span>' : '<span class="warn">未設定</span>'}`,
    `LLM：${st.llm ? `<span class="pos">已設定</span>（${esc(st.llm_model || "")}）` : '<span class="warn">未設定</span>'}`,
    `資料收集：${c ? (c.paused ? `<span class="neg">已暫停</span>` : '<span class="pos">執行中</span>') : "-"}`,
    c && c.task ? `Windows 排程：${esc(c.task.state || "-")}` : "",
  ].filter(Boolean).join("　");
  const groups = {};
  d.fields.forEach((f) => (groups[f.group] = groups[f.group] || []).push(f));
  $("#set-body").innerHTML = Object.entries(d.groups).map(([g, label]) => {
    const fs = groups[g] || [];
    const extra = g === "collect" ? `<div class="set-row"><span class="k">資料收集總開關</span><button class="small" id="set-collect-toggle">${c && c.paused ? "恢復收集" : "暫停收集"}</button><span class="muted">同右上角開關</span></div>` : "";
    return `<details class="set-sec" id="set-sec-${g}" ${g === "collect" ? "open" : ""}><summary>${esc(label)} <span class="muted" id="set-cnt-${g}">${fs.filter((f) => f.overridden).length ? `（${fs.filter((f) => f.overridden).length} 項已修改）` : ""}</span></summary>
      ${extra}
      ${fs.map((f) => `<div class="set-row ${f.overridden ? "changed" : ""}" data-path="${esc(f.path)}">
          <label class="k" for="set-${f.path.replace(/\./g, "-")}">${esc(f.label)}</label>${setInput(f)}
          <div class="muted set-help">${f.help ? esc(f.help) + "；" : ""}生效：${esc(f.when)}${f.overridden ? `；預設 ${esc(fmtSetVal(f, f.default))} <a href="#" class="set-reset1" data-path="${esc(f.path)}">回預設</a>` : ""}</div>
        </div>`).join("")}
      <div class="set-actions"><button class="primary small" data-save="${g}">儲存</button><button class="small" data-reset="${g}">本區全部回預設</button><span class="muted" id="set-msg-${g}"></span></div>
    </details>`;
  }).join("");
  $("#set-body").querySelectorAll("[data-save]").forEach((b) => b.addEventListener("click", () => saveSettings(b.dataset.save)));
  $("#set-body").querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", () => resetSettings((groups[b.dataset.reset] || []).filter((f) => f.overridden).map((f) => f.path), b.dataset.reset)));
  $("#set-body").querySelectorAll(".set-reset1").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); resetSettings([a.dataset.path], BY_SET_PATH(a.dataset.path).group); }));
  const tg = $("#set-collect-toggle");
  if (tg) tg.addEventListener("click", async () => { await collectSet(collectState && collectState.paused ? "resume" : "pause"); setTimeout(loadSettings, 900); });
}
const BY_SET_PATH = (p) => setData.fields.find((f) => f.path === p);
async function saveSettings(group) {
  const changes = {};
  setData.fields.filter((f) => f.group === group).forEach((f) => {
    const v = readInput(f);
    const cmp = f.type === "insts" ? v.split(/[\s,]+/).filter(Boolean).map((x) => (x.includes("-") ? x.toUpperCase() : `${x.toUpperCase()}-USDT-SWAP`)) : v;
    if (!sameVal(cmp, f.value)) changes[f.path] = v;
  });
  if (!Object.keys(changes).length) { $(`#set-msg-${group}`).textContent = "沒有變更"; return; }
  try {
    const r = await post("/api/settings", { changes });
    toast(r.message);
    await loadSettings();
    $(`#set-sec-${group}`).open = true;
    $(`#set-msg-${group}`).textContent = "已儲存";
    afterSettings(Object.keys(changes));
  } catch (e) { $(`#set-msg-${group}`).innerHTML = `<span class="neg">${esc(e.message)}</span>`; }
}
async function resetSettings(paths, group) {
  if (!paths.length) return;
  const changes = Object.fromEntries(paths.map((p) => [p, null]));
  try {
    const r = await post("/api/settings", { changes });
    toast(`已回預設（${paths.length} 項）`, !r.ok);
    await loadSettings();
    if (group) $(`#set-sec-${group}`).open = true;
    afterSettings(paths);
  } catch (e) { toast(e.message, true); }
}
function afterSettings(paths) {
  if (paths.includes("heat.watchlist") && currentTab === "ht") loadHeat();
  if (paths.includes("chat.model")) aiReady = false;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-settings]");
  if (b) { e.preventDefault(); openSettings(b.dataset.settings || null); }
});
$("#set-close").addEventListener("click", closeSettings);
$("#set-backdrop").addEventListener("click", closeSettings);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#set-panel").classList.contains("open")) closeSettings(); });

/* ---------------- polling ---------------- */
setInterval(() => {
  if (document.hidden) return;
  if (currentTab === "dash") loadDash();
  if (currentTab === "ac") loadPositions();
  if (currentTab === "pp" && ppSrc() === "live") loadPaper();
  if (currentTab === "ht") loadHeat();
  if (currentTab === "dc" && Date.now() % 300000 < 15000) loadScan();
  if (currentTab === "lab") { labRules(); if (Date.now() % 300000 < 15000) labRecent(); }
  if (currentTab === "ctrl" && $("#log-follow").checked) { loadLog(); loadTaskBox(); }
}, 15000);
setInterval(() => {
  if (document.hidden) return;
  if (currentTab === "ctrl" && $("#log-follow").checked) loadLog();
  if (currentTab === "bt") pollJob();
  if (currentTab === "ac") pollAcctJob();
  if (currentTab === "dc") pollDiscJob();
  if (currentTab === "pp") pollPaperJobs();
  if (currentTab === "ht") pollHeatJob();
  if (currentTab === "lab") labPollJob();
  if (currentTab === "cp") pollCampJob();
}, 3000);

/* ---------------- campaigns (瓜分活動) ---------------- */
const CP_PHASE = { upcoming: '<span class="badge">即將開始</span>', running: '<span class="badge warn">進行中</span>', ended: '<span class="badge dim">已結束</span>' };
const CP_STATUS = { ok: '<span class="badge ok">已解析</span>', no_token: '<span class="badge dim">無指定代幣</span>', no_period: '<span class="badge bad">期間解析失敗</span>',
  no_page: '<span class="badge bad">內文抓取失敗</span>', pending: '<span class="badge">待解析</span>', skip: '<span class="badge dim">略過</span>' };
const CP_VERD = { "顯著": "ok", "不顯著": "", "樣本不足": "dim" };
const CP_GRP = { earn: "閃賺", trade: "交易賺幣" };
const lpc = (v, d = 1) => (v == null ? "-" : pctC(Math.expm1(v), d));
const lp = (v, d = 1) => (v == null ? "-" : pct(Math.expm1(v), d));
let cpJobRunning = false;

$("#btn-cp-eval").addEventListener("click", async () => {
  try { const r = await post("/api/campaigns/evaluate"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
  pollCampJob();
});
$("#btn-cp-sync").addEventListener("click", async () => {
  try { const r = await post("/api/campaigns/sync"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
  pollCampJob();
});

function cpKline(inst, a, b) {
  openKline(inst, a - 7 * 86400e3, Math.min(Date.now(), (b || a) + 7 * 86400e3));
}

async function pollCampJob() {
  const j = await api("/api/campaigns/job").catch(() => null);
  if (!j) return;
  if (j.running || j.log.length) { $("#cp-log").style.display = ""; renderLog($("#cp-log"), j.log); }
  $("#btn-cp-eval").disabled = $("#btn-cp-sync").disabled = j.running;
  if (cpJobRunning && !j.running) {
    toast(j.returncode === 0 ? "完成" : `結束，代碼 ${j.returncode}`, j.returncode !== 0);
    loadCampaigns();
    loadCampaignResults();
  }
  cpJobRunning = j.running;
}

async function loadCampaigns() {
  const d = await api("/api/campaigns").catch((e) => { toast(e.message, true); return null; });
  if (!d) return;
  const st = d.status || {};
  $("#cp-meta").textContent = st.exists
    ? `公告 ${fmtN((st.announcements || {})["en-US"])} 則（${utc(st.from).slice(0, 10)} ～ ${utc(st.to).slice(0, 10)}）；上次檢查 ${ago(st.last_poll)}${st.backfill_done ? "" : "；歷史公告還在補"}`
    : "還沒有公告資料，按「更新公告」";
  table($("#cp-live"), [
    { t: "狀態", f: (r) => CP_PHASE[r.phase] || esc(r.phase) },
    { t: "代幣", k: "token", cls: "l" },
    { t: "類型", f: (r) => esc(d.kinds[r.kind] || r.kind) },
    { t: "公告", k: "p_time", f: (r) => utc(r.p_time) },
    { t: "開始", k: "start_ts", f: (r) => utc(r.start_ts) },
    { t: "結束", k: "end_ts", f: (r) => utc(r.end_ts) },
    { t: "發獎勵", f: (r) => utc(r.dist_ts) },
    { t: "獎池", f: (r) => esc(r.pool || "-") },
    { t: "報酬", k: "ret", f: (r) => lpc(r.ret) },
    { t: "vs 大盤", k: "vs_alts", f: (r) => lpc(r.vs_alts) },
    { t: "vs BTC", k: "vs_btc", f: (r) => lpc(r.vs_btc) },
    { t: "公告標題", cls: "l wrap", f: (r) => esc(r.title_zh || r.title) },
  ], d.live || [], { sortable: true, onClick: (r) => cpKline(r.inst_id, r.p_time, r.end_ts) });
  const all = d.campaigns || [];
  const nOk = all.filter((r) => r.status === "ok").length;
  const nBad = all.filter((r) => ["no_period", "no_page", "pending"].includes(r.status)).length;
  $("#cp-all-meta").textContent = `${all.length} 則，${nOk} 則已解析${nBad ? `，${nBad} 則待處理（可用 data/campaign_overrides.json 手動補）` : ""}`;
  table($("#cp-all"), [
    { t: "公告", k: "p_time", f: (r) => utc(r.p_time) },
    { t: "類型", f: (r) => esc(r.kind_label) },
    { t: "代幣", k: "tokens", cls: "l", f: (r) => esc(r.tokens || "-") },
    { t: "開始", k: "start_ts", f: (r) => utc(r.start_ts) },
    { t: "結束", k: "end_ts", f: (r) => utc(r.end_ts) },
    { t: "天數", f: (r) => (r.start_ts && r.end_ts ? num((r.end_ts - r.start_ts) / 86400e3, 1) : "-") },
    { t: "狀態", f: (r) => CP_STATUS[r.status] || esc(r.status) },
    { t: "標題", cls: "l wrap", f: (r) => `${esc(r.title_zh || r.title)} <a href="${esc(r.url)}" target="_blank" rel="noopener">原文</a>` },
  ], all, { sortable: true, onClick: (r) => r.tokens && cpKline(`${r.tokens.split(",")[0]}-USDT-SWAP`, r.p_time, r.end_ts) });
}

function cpCurve(c, title) {
  const W = 480, Hh = 210, L = 44, R = 10, T = 22, B = 26;
  if (!c || !c.h || c.h.length < 2) return `<div class="muted">${esc(title)}：樣本不足</div>`;
  const ys = [...c.lo, ...c.hi, 0].filter((v) => v != null).map((v) => Math.expm1(v) * 100);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.08 || 1;
  y0 -= pad; y1 += pad;
  const x0 = c.h[0], x1 = c.h[c.h.length - 1];
  const X = (h) => L + ((h - x0) / (x1 - x0)) * (W - L - R);
  const Y = (v) => T + (1 - (v - y0) / (y1 - y0)) * (Hh - T - B);
  const pts = (arr) => arr.map((v, i) => (v == null ? null : `${X(c.h[i]).toFixed(1)},${Y(Math.expm1(v) * 100).toFixed(1)}`)).filter(Boolean);
  const band = [...pts(c.hi), ...pts(c.lo).reverse()].join(" ");
  const step = Math.max(1, Math.round((y1 - y0) / 5));
  let grid = "";
  for (let v = Math.ceil(y0 / step) * step; v <= y1; v += step) {
    grid += `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--border)" stroke-width="${v === 0 ? 1.4 : 0.6}"/>`
      + `<text x="${L - 6}" y="${Y(v) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${v}%</text>`;
  }
  let xt = "";
  for (let dd = Math.ceil(x0 / 24); dd <= x1 / 24; dd++) {
    xt += `<text x="${X(dd * 24)}" y="${Hh - 8}" text-anchor="middle" font-size="11" fill="var(--muted)">${dd}</text>`;
  }
  return `<div><svg viewBox="0 0 ${W} ${Hh}" width="100%" role="img" aria-label="${esc(title)}">
    <text x="${L}" y="14" font-size="12" fill="var(--text)">${esc(title)}（最多 ${Math.max(...c.n)} 筆）</text>
    ${grid}${xt}
    <line x1="${X(0)}" x2="${X(0)}" y1="${T}" y2="${Hh - B}" stroke="var(--muted)" stroke-dasharray="3 3"/>
    <polygon points="${band}" fill="var(--accent)" opacity="0.18"/>
    <polyline points="${pts(c.mean).join(" ")}" fill="none" stroke="var(--accent)" stroke-width="1.8"/>
  </svg></div>`;
}

async function loadCampaignResults() {
  const r = await api("/api/campaigns/results").catch(() => null);
  if (!r) {
    $("#cp-res-meta").textContent = "尚未執行研究，按「重跑研究」";
    ["#cp-main", "#cp-curves", "#cp-vol", "#cp-ols", "#cp-caveats"].forEach((x) => { $(x).innerHTML = ""; });
    return;
  }
  $("#cp-res-meta").textContent = `產生於 ${r.generated_at}，資料到 ${r.data_to}；${r.n_events} 筆（${r.n_campaigns} 檔活動）`;
  $("#cp-main").innerHTML = Object.entries(r.main).map(([g, m]) => {
    const s = m.stats || {}, p = m.placebo || {};
    return `<div class="card"><div class="k">${esc(r.groups[g])}・${esc(r.windows[m.window])}（vs 同齡幣）</div>
      <div class="v">${lpc(s.mean)} <span class="badge ${CP_VERD[m.verdict] || ""}">${esc(m.verdict)}</span></div>
      <div class="s">${s.n || 0} 筆，${pct(s.neg, 0)} 為負；隨機日期平均 ${lp(p.placebo_mean)}，p=${p.p == null ? "-" : p.p.toFixed(3)}</div></div>`;
  }).join("");
  const rows = [];
  for (const g of ["earn", "trade"]) {
    for (const w of ["anno", "dur", "post3", "post7"]) {
      const S = r.summary[g][w];
      rows.push({ g, w, ab: S.ab || {}, raw: S.raw || {}, btc: S.ab_btc || {}, alt: S.ab_alt || {}, pl: r.placebo[`${g}_${w}`] });
    }
  }
  const sc = (s) => (s && s.n ? `${lpc(s.mean)} <span class="muted">中位 ${lp(s.median)}</span>` : "-");
  table($("#cp-sum"), [
    { t: "活動", cls: "l", f: (x) => esc(r.groups[x.g]) },
    { t: "時段", f: (x) => esc(r.windows[x.w]) + (r.main[x.g].window === x.w ? ' <span class="badge">主要</span>' : "") },
    { t: "筆數", f: (x) => x.ab.n || 0 },
    { t: "vs 同齡幣", f: (x) => sc(x.ab) },
    { t: "為負", f: (x) => pct(x.ab.neg, 0) },
    { t: "t", f: (x) => num(x.ab.t) },
    { t: "隨機日期 p", f: (x) => (x.pl && x.pl.p != null ? (x.pl.p < 0.05 ? `<b>${x.pl.p.toFixed(3)}</b>` : x.pl.p.toFixed(3)) : "-") },
    { t: "原始報酬", f: (x) => sc(x.raw) },
    { t: "vs BTC", f: (x) => sc(x.btc) },
    { t: "vs 大盤", f: (x) => sc(x.alt) },
  ], rows);
  const vs = (s) => (s && s.n ? `${lpc(s.mean)}（${s.n} 筆，t=${num(s.t)}）` : "-");
  $("#cp-vol").innerHTML = "活動期間 vs 公告前 7 天，扣同齡幣：" + ["earn", "trade"].map((g) =>
    `${esc(r.groups[g])} 波動度 ${vs(r.summary[g].vol_ratio_ab)}、成交額 ${vs(r.summary[g].v_ratio_ab)}`).join("；");
  $("#cp-curves").innerHTML = [["earn_start", "閃賺／空投賺幣・從活動開始（天）"], ["earn_end", "閃賺／空投賺幣・從活動結束（天）"],
    ["trade_start", "交易賺幣・從活動開始（天）"], ["trade_end", "交易賺幣・從活動結束（天）"]].map(([k, t]) => cpCurve(r.curves[k], t)).join("");
  const reg = [];
  for (const g of ["earn", "trade"]) for (const x of (r.regimes[g] || {}).splits || []) reg.push({ g, ...x });
  const rs = (s) => (s && s.n ? `${lpc(s.mean)} <span class="muted">${s.n} 筆</span>` : "-");
  table($("#cp-reg"), [
    { t: "活動", cls: "l", f: (x) => esc(r.groups[x.g]) },
    { t: "分組", f: (x) => esc(x.label) },
    { t: "上漲時", f: (x) => rs(x.up_raw) },
    { t: "下跌時", f: (x) => rs(x.down_raw) },
    { t: "差異 p", f: (x) => (x.p_raw == null ? "-" : x.p_raw.toFixed(2)) },
    { t: "上漲時（扣同齡）", f: (x) => rs(x.up_ab) },
    { t: "下跌時（扣同齡）", f: (x) => rs(x.down_ab) },
    { t: "差異 p", f: (x) => (x.p_ab == null ? "-" : x.p_ab.toFixed(2)) },
  ], reg);
  const XL = { btc_win: "BTC 同期", alt_win: "大盤同期", btc_pre: "BTC 前 7 天" };
  $("#cp-ols").innerHTML = "迴歸（大盤持平時的預期報酬 alpha）：" + ["earn", "trade"].map((g) => `${esc(r.groups[g])}：` +
    ((r.regimes[g] || {}).ols || []).filter((o) => o.y === "raw").map((o) => `對${XL[o.x] || o.x} alpha ${lpc(o.alpha)}（t=${num(o.t_alpha)}）、β ${num(o.beta)}`).join("，")).join("；");
  table($("#cp-events"), [
    { t: "代幣", k: "token", cls: "l" },
    { t: "類型", f: (e) => CP_GRP[e.grp] || esc(e.kind) },
    { t: "開始", k: "start_ts", f: (e) => utc(e.start_ts) },
    { t: "天數", k: "days", f: (e) => num(e.days, 1) },
    { t: "上市天數", k: "age_d", f: (e) => num(e.age_d, 0) },
    { t: "公告到開始", k: "anno_ab", f: (e) => lpc(e.anno_ab) },
    { t: "活動期間", k: "dur_ab", f: (e) => lpc(e.dur_ab) },
    { t: "結束後 3 天", k: "post3_ab", f: (e) => lpc(e.post3_ab) },
    { t: "結束後 7 天", k: "post7_ab", f: (e) => lpc(e.post7_ab) },
    { t: "BTC 同期", k: "btc_win", f: (e) => lpc(e.btc_win) },
  ], (r.events || []).slice().sort((a, b) => b.start_ts - a.start_ts), { sortable: true, onClick: (e) => cpKline(e.inst_id, e.p_time, e.end_ts) });
  $("#cp-caveats").innerHTML = (r.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("");
}

/* ---------------- shell: jobs indicator, sub-nav, lab steps ---------------- */
const GLOBAL_JOBS = [
  { url: "/api/backtest/job", label: "回測", tab: "bt" },
  { url: "/api/account/job", label: "戰績分析", tab: "ac" },
  { url: "/api/discover/job", label: "進場探索", tab: "dc" },
  { url: "/api/paper/jobs", label: "AI 模擬", tab: "pp", multi: true },
  { url: "/api/heat/job", label: "過熱驗證", tab: "ht" },
  { url: "/api/lab/job", label: "實驗室", tab: "lab" },
  { url: "/api/campaigns/job", label: "瓜分活動", tab: "cp" },
];
async function pollGlobalJobs() {
  const res = await Promise.all(GLOBAL_JOBS.map((g) => api(g.url).catch(() => null)));
  const running = GLOBAL_JOBS.filter((g, i) => {
    const j = res[i];
    if (!j) return false;
    return g.multi ? Object.values(j).some((x) => x && x.running) : j.running;
  });
  $("#jobs").innerHTML = running
    .map((g) => `<button class="jobpill" data-go="${g.tab}" title="前往 ${esc(g.label)}"><span class="pulse" aria-hidden="true"></span>${esc(g.label)}執行中</button>`)
    .join("");
}
$("#jobs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-go]");
  if (b) showTab(b.dataset.go);
});

document.querySelectorAll(".subnav [data-jump]").forEach((b) => b.addEventListener("click", () => {
  document.getElementById(b.dataset.jump)?.scrollIntoView({ behavior: "smooth", block: "start" });
}));

function labSteps() {
  const visible = (id) => { const el = document.getElementById(id); return el && el.style.display !== "none"; };
  const hasRows = (id) => { const el = document.getElementById(id); return el && el.querySelector("tbody tr[data-ri]"); };
  const done = {
    1: !!hasRows("lab-events") || !!hasRows("lab-recent"),
    2: visible("lab-event-panel"),
    3: !!hasRows("lab-tpl") || visible("lab-tpl-panel"),
    4: !!hasRows("lab-sim"),
    5: visible("lab-res-panel"),
    6: !!hasRows("lab-rules"),
  };
  let current = 6;
  for (let i = 1; i <= 6; i++) if (!done[i]) { current = i; break; }
  document.querySelectorAll("#lab-steps li").forEach((li) => {
    const n = +li.dataset.step;
    li.classList.toggle("done", !!done[n]);
    li.classList.toggle("current", n === current);
  });
}

window.addEventListener("hashchange", () => {
  const t = location.hash.slice(1).split("/")[0];
  if (t && t !== currentTab) showTab(t);
});
setInterval(() => { if (!document.hidden) pollGlobalJobs(); }, 5000);
setInterval(() => { if (!document.hidden && currentTab === "lab") labSteps(); }, 2000);
pollGlobalJobs();

showTab((location.hash || "#dash").slice(1).split("/")[0] || "dash");
