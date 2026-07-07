// BiliLiveCut \u4eea\u8868\u76d8:\u7f51\u611f\u8d44\u6599\u5e93\u3001\u6570\u636e\u5206\u6790\u3001\u8d8b\u52bf\u56fe\u8868
import { $, api, toast, esc } from "./common.js";

let scheduleDirty = false;

// ----------------------------- \u5b9a\u65f6\u91c7\u96c6\u63a7\u4ef6\u540c\u6b65 ----------------------------- //
function renderScheduler(s) {
  if (!scheduleDirty) {
    $("#sw-trend-schedule").checked = !!s.schedule_enabled;
    if (s.window_start) $("#trend-start").value = s.window_start;
    if (s.window_end) $("#trend-end").value = s.window_end;
    if (s.interval_min) $("#trend-interval").value = s.interval_min;
  }
  let st = s.running ? "\u8c03\u5ea6\u8fd0\u884c\u4e2d" : "\u8c03\u5ea6\u672a\u8fd0\u884c";
  if (!s.trend_enabled) st += " \u00b7 \u8d44\u6599\u5e93\u672a\u542f\u7528(TREND_ENABLED=false)";
  else if (s.paused_by_recording) st += " \u00b7 \u5df2\u56e0\u5f55\u5236\u6682\u505c";
  else if (s.collecting) st += " \u00b7 \u6b63\u5728\u91c7\u96c6";
  if (s.last_run_at) st += ` \u00b7 \u4e0a\u6b21\u91c7\u96c6 ${esc(s.last_run_at)}(${s.last_saved} \u6761)`;
  $("#trend-schedule-status").textContent = st;
}

// ----------------------------- \u6e32\u67d3:\u7f51\u611f\u8d44\u6599\u5e93 ----------------------------- //
async function loadTrends() {
  const data = await api("GET", "/api/trends?limit=30&days=7");
  $("#trends-status").innerHTML = data.enabled
    ? `\u5df2\u542f\u7528 \u00b7 \u8054\u7f51\u641c\u7d22 ${data.web_search ? "\u5f00" : "\u5173"} \u00b7 \u8fd1 ${data.days} \u5929`
    : `\u672a\u542f\u7528(\u8bbe\u7f6e TREND_ENABLED=true \u5e76\u914d\u7f6e\u5927\u6a21\u578b API \u540e\u53ef\u7528)`;
  renderScheduler(data.scheduler || {});
  const kw = data.keywords || [];
  $("#trends-keywords").innerHTML = kw.length
    ? `<div class="tagcloud">${kw.map((k) => `<span class="tagchip" title="\u51fa\u73b0 ${k.count} \u6b21">${esc(k.keyword)} \u00b7 ${k.heat}</span>`).join("")}</div>`
    : `<div class="empty">\u6682\u65e0\u70ed\u8bcd\u3002\u70b9\u51fb\u300c\u7acb\u5373\u8054\u7f51\u91c7\u96c6\u300d\u3002</div>`;
  const items = data.items || [];
  $("#trends-list").innerHTML = items.length ? items.map((it) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">${esc(it.title)}</div>
          <div class="sub">${esc(it.source)} \u00b7 ${esc(it.category || "")} \u00b7 \u70ed\u5ea6 ${it.heat} \u00b7 \u51fa\u73b0 ${it.seen_count} \u6b21</div>
        </div>
      </div>
      <div class="txt">${esc(it.summary || "")}</div>
      ${(it.tags || []).length ? `<div class="tagcloud">${it.tags.map((t) => `<span class="tagchip">${esc(t)}</span>`).join("")}</div>` : ""}
    </div>`).join("") : `<div class="empty">\u8d44\u6599\u5e93\u6682\u65e0\u6570\u636e\u3002</div>`;
}

// ----------------------------- \u6570\u636e\u5206\u6790(V0.1.8 P2) ----------------------------- //
async function loadAnalytics() {
  const data = await api("GET", "/api/analytics");

  $("#stat-total-clips").textContent = data.overview.total_clips;
  $("#stat-published").textContent = data.overview.published_clips;
  $("#stat-duration").textContent = data.overview.total_duration_h;
  $("#stat-avg-score").textContent = data.overview.avg_highlight_score;
  $("#stat-candidates").textContent = data.overview.total_candidates;
  $("#stat-sessions").textContent = data.overview.total_sessions;
  $("#stat-reconnects").textContent = data.overview.total_reconnects;
  $("#stat-raw-gb").textContent = data.overview.total_raw_gb;
  $("#stat-task-fail").textContent = data.overview.task_failed;

  const dist = data.score_distribution;
  const maxCount = Math.max(...Object.values(dist), 1);
  let distHtml = "";
  for (const [k, v] of Object.entries(dist)) {
    const pct = Math.round(v / maxCount * 100);
    distHtml += `<div style="flex:1;text-align:center;font-size:11px">
      <div style="background:#3b82f6;height:${pct}px;border-radius:4px 4px 0 0;min-height:4px"></div>
      ${v}<br/>${k}
    </div>`;
  }
  $("#score-dist").innerHTML = distHtml;

  renderTrendChart(data.daily_trend);

  const ranks = data.room_ranking;
  $("#room-ranking").innerHTML = ranks.length ? ranks.map((r, i) =>
    `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #333">
      <span>${i + 1}. ${esc(r.name)}</span>
      <span>${r.clips} \u7247 \u00b7 ${r.duration_h}h</span>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u6570\u636e\u3002</div>`;
}

function renderTrendChart(daily) {
  const canvas = document.getElementById("trend-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  const pad = { top: 20, right: 20, bottom: 40, left: 40 };
  const pw = W - pad.left - pad.right;
  const ph = H - pad.top - pad.bottom;

  let maxVal = 1;
  daily.forEach(d => {
    maxVal = Math.max(maxVal, d.sessions, d.clips, d.candidates);
  });
  maxVal = Math.ceil(maxVal * 1.2);

  const n = daily.length;
  const xStep = pw / (n - 1);
  const yScale = (v) => pad.top + ph - (v / maxVal * ph);

  ctx.strokeStyle = "#555"; ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top); ctx.lineTo(pad.left, pad.top + ph);
  ctx.lineTo(pad.left + pw, pad.top + ph);
  ctx.stroke();

  ctx.fillStyle = "#999"; ctx.font = "10px sans-serif"; ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const v = Math.round(maxVal * i / 4);
    const y = pad.top + ph - (v / maxVal * ph);
    ctx.fillText(v, pad.left - 6, y + 4);
    if (i > 0) {
      ctx.strokeStyle = "#333"; ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left + pw, y); ctx.stroke();
    }
  }

  ctx.textAlign = "center";
  for (let i = 0; i < n; i += 5) {
    const x = pad.left + i * xStep;
    ctx.fillText(daily[i].date, x, pad.top + ph + 16);
  }

  const colors = ["#3b82f6", "#10b981", "#f59e0b"];
  const keys = ["clips", "sessions", "candidates"];
  keys.forEach((key, ki) => {
    ctx.strokeStyle = colors[ki]; ctx.lineWidth = 2;
    ctx.beginPath();
    daily.forEach((d, i) => {
      const x = pad.left + i * xStep;
      const y = yScale(d[key] || 0);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.fillStyle = colors[ki];
    ctx.fillText({ clips: "\u5207\u7247", sessions: "\u5f55\u5236", candidates: "\u5019\u9009" }[key], pad.left + 10 + ki * 70, pad.top - 6);
  });
}

// \u4e8b\u4ef6\u7ed1\u5b9a
["#trend-start", "#trend-end", "#trend-interval"].forEach((sel) =>
  $(sel).addEventListener("input", () => { scheduleDirty = true; }));
$("#sw-trend-schedule").addEventListener("change", () => { scheduleDirty = true; });
$("#btn-save-schedule").addEventListener("click", async () => {
  const body = {
    trend_schedule_enabled: $("#sw-trend-schedule").checked,
    trend_schedule_start: $("#trend-start").value || "03:00",
    trend_schedule_end: $("#trend-end").value || "05:00",
    trend_schedule_interval_min: parseInt($("#trend-interval").value || "30", 10),
  };
  try {
    await api("PATCH", "/api/settings", body);
    scheduleDirty = false;
    toast("\u5df2\u4fdd\u5b58\u5b9a\u65f6\u91c7\u96c6\u8bbe\u7f6e");
    loadTrends();
  } catch (e) { toast("\u4fdd\u5b58\u5931\u8d25:" + e.message); }
});

$("#btn-collect-trends").addEventListener("click", async () => {
  const topic = $("#trends-topic").value.trim();
  toast("\u8054\u7f51\u91c7\u96c6\u4e2d,\u8bf7\u7a0d\u5019\u2026");
  try {
    const r = await api("POST", "/api/trends/collect", { topic });
    toast(r.enabled ? `\u91c7\u96c6\u5b8c\u6210,\u65b0\u589e/\u66f4\u65b0 ${r.saved} \u6761` : (r.note || "\u672a\u542f\u7528"));
    loadTrends();
  } catch (e) { toast("\u91c7\u96c6\u5931\u8d25:" + e.message); }
});

export { loadTrends, renderScheduler, loadAnalytics, renderTrendChart, scheduleDirty };
