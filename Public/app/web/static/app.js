// BiliLiveCut 控制台前端:轮询 REST API 并渲染各视图。
"use strict";

const $ = (sel) => document.querySelector(sel);
let activeTab = "rooms";

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return resp.status === 204 ? null : resp.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2600);
}

function esc(s) {
  return (s ?? "").toString().replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function badge(status) {
  const map = {
    running: "green", recording: "green", ready: "green", approved: "green",
    pending: "yellow", reviewing: "yellow", reconnecting: "yellow", reconnected: "green",
    rejected: "red", error: "red",
  };
  return `<span class="badge ${map[status] || "gray"}">${esc(status)}</span>`;
}

// ----------------------------- 标签切换 ----------------------------- //
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    activeTab = btn.dataset.tab;
    $(`#tab-${activeTab}`).classList.add("active");
    refresh();
  });
});

// ----------------------------- 添加直播间 ----------------------------- //
$("#btn-add").addEventListener("click", async () => {
  const url = $("#new-url").value.trim();
  const authorized = $("#new-auth").checked;
  if (!url) return toast("请输入直播间 URL 或房间号");
  try {
    await api("POST", "/api/rooms", { url, authorized });
    $("#new-url").value = "";
    toast("已添加直播间");
    loadRooms();
  } catch (e) { toast("添加失败:" + e.message); }
});

// ----------------------------- 渲染:直播间 ----------------------------- //
async function loadRooms() {
  const data = await api("GET", "/api/dashboard");
  $("#stat-candidates").textContent = data.counts.candidates;
  $("#stat-clips").textContent = data.counts.clips;
  $("#stat-sessions").textContent = data.counts.active_sessions;

  const modes = data.modes;
  const html = data.rooms.map((r) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">${esc(r.title || r.input_url)} ${badge(r.running ? "running" : "stopped")}</div>
          <div class="sub">db_id=${r.id} · room_id=${r.room_id ?? "-"} · 授权:${r.authorized ? "是" : "否"}</div>
        </div>
        <div class="actions">
          ${r.running
            ? `<button class="danger" onclick="stopRoom(${r.id})">停止录制</button>`
            : `<button class="ok" onclick="startRoom(${r.id})">开始录制</button>`}
        </div>
      </div>
      <div class="thresholds">
        <label>审核模式
          <select id="mode-${r.id}">
            ${modes.map((m) => `<option value="${m}" ${m === r.mode ? "selected" : ""}>${m}</option>`).join("")}
          </select>
        </label>
        <label>高光阈值
          <input type="number" step="0.05" min="0" max="1" id="ht-${r.id}" value="${r.highlight_threshold}" />
        </label>
        <label>自动发布阈值
          <input type="number" step="0.05" min="0" max="1" id="at-${r.id}" value="${r.auto_publish_threshold}" />
        </label>
        <button onclick="saveRoom(${r.id})">保存</button>
      </div>
      <div class="thresholds" style="margin-top: 6px">
        <label class="switch-row">
          <input type="checkbox" id="sw-se-${r.id}" ${r.schedule_enabled ? "checked" : ""} ${r.running ? "disabled" : ""} />
          预约录制
        </label>
        <label class="switch-row">
          <input type="checkbox" id="sw-at-${r.id}" ${r.auto_threshold_enabled ? "checked" : ""} ${r.running ? "disabled" : ""} />
          阈值自学习
        </label>
        <label class="switch-row">
          <input type="checkbox" id="sw-ds-${r.id}" ${r.danmaku_sentiment_enabled ? "checked" : ""} ${r.running ? "disabled" : ""} />
          弹幕情绪
        </label>
        ${r.running ? '<span class="muted">(录制中锁定)</span>' : ""}
      </div>
      ${r.auto_threshold_enabled ? `<div id="tl-${r.id}" class="threshold-learning"></div>` : ""}
    </div>`).join("");
  $("#rooms-list").innerHTML = html || `<div class="empty">还没有直播间,先在上方添加。</div>`;

  // 异步加载阈值学习摘要。
  data.rooms.forEach((r) => {
    if (r.auto_threshold_enabled) loadThresholdLearning(r.id);
  });
}

window.startRoom = async (id) => {
  try {
    await api("POST", `/api/rooms/${id}/start`, { pipeline: true, produce: false });
    toast("已开始录制"); loadRooms();
  } catch (e) { toast("启动失败:" + e.message); }
};
window.stopRoom = async (id) => {
  try { await api("POST", `/api/rooms/${id}/stop`); toast("已停止"); loadRooms(); }
  catch (e) { toast("停止失败:" + e.message); }
};
window.saveRoom = async (id) => {
  try {
    await api("PATCH", `/api/rooms/${id}`, {
      mode: $(`#mode-${id}`).value,
      highlight_threshold: parseFloat($(`#ht-${id}`).value),
      auto_publish_threshold: parseFloat($(`#at-${id}`).value),
      schedule_enabled: ($(`#sw-se-${id}`) || {}).checked,
      auto_threshold_enabled: ($(`#sw-at-${id}`) || {}).checked,
      danmaku_sentiment_enabled: ($(`#sw-ds-${id}`) || {}).checked,
    });
    toast("已保存阈值/模式");
  } catch (e) { toast("保存失败:" + e.message); }
};

// ----------------------------- 渲染:录制状态 ----------------------------- //
async function loadRecording() {
  const [rows, prog] = await Promise.all([
    api("GET", "/api/recording"),
    api("GET", "/api/progress"),
  ]);
  $("#progress-title").textContent = prog.total_segments ? `总片段 ${prog.total_segments}` : "";
  $("#progress-bar").style.width = prog.progress_pct + "%";
  $("#progress-text").textContent = prog.total_segments
    ? `已录制 ${prog.recorded} · 已转写 ${prog.transcribed} · 已评分 ${prog.scored} (${prog.progress_pct}%)`
    : "暂无进行中的录制会话";
  $("#recording-list").innerHTML = rows.length ? rows.map((s) => {
    const reconnectInfo = s.last_reconnected_at
      ? ` · 最近重连 ${esc(s.last_reconnected_at).substring(11, 19)}`
      : "";
    return `
    <div class="item">
      <div class="head">
        <div class="title">会话 #${s.id} · room ${s.room_id} ${badge(s.status)}</div>
        <div class="sub">${s.segments} 个片段 · 重连 ${s.reconnect_count} 次${reconnectInfo} · ${esc(s.stream_format || "-")}</div>
      </div>
      ${s.error_message ? `<div class="sub" style="color:var(--red)">${esc(s.error_message)}</div>` : ""}
    </div>`}).join("") : `<div class="empty">暂无录制会话。</div>`;
}

// ----------------------------- 渲染:实时转写 ----------------------------- //
async function loadTranscripts() {
  const rows = await api("GET", "/api/transcripts?limit=30");
  $("#transcripts-list").innerHTML = rows.length ? rows.map((t) => `
    <div class="item">
      <div class="sub">片段 #${t.segment_id} · ${esc(t.language || "")} · ${esc(t.created_at || "")}</div>
      <div class="txt">${esc(t.text) || "(空)"}</div>
    </div>`).join("") : `<div class="empty">暂无转写。开始带 --pipeline 的录制后会出现。</div>`;
}

// ----------------------------- 渲染:弹幕热度 ----------------------------- //
const DANMAKU_TYPE_LABEL = {
  danmaku: "弹幕", gift: "礼物", superchat: "SC", interact: "互动", other: "其它",
};
async function loadDanmaku() {
  const data = await api("GET", "/api/danmaku?limit=60");
  const sessions = data.sessions || [];
  $("#danmaku-sessions").innerHTML = sessions.length ? sessions.map((s) => `
    <div class="item">
      <div class="sub">会话 #${s.session_id}</div>
      <div class="title">弹幕 ${s.count} 条 · 强度 ${s.intensity}</div>
    </div>`).join("") : `<div class="empty">暂无弹幕。开启录制(COLLECT_DANMAKU=true)后会自动采集。</div>`;
  const recent = data.recent || [];
  $("#danmaku-list").innerHTML = recent.length ? recent.map((d) => `
    <div class="item">
      <div class="sub">#${d.session_id} · ${DANMAKU_TYPE_LABEL[d.type] || d.type} · ${esc(d.user || "匿名")} · ${esc(d.ts || "")}</div>
      <div class="txt">${esc(d.content) || "(无文本)"}</div>
    </div>`).join("") : `<div class="empty">暂无弹幕记录。</div>`;
}

// 仅在用户未编辑时同步定时采集控件,避免覆盖正在输入的值。
let scheduleDirty = false;
function renderScheduler(s) {
  if (!scheduleDirty) {
    $("#sw-trend-schedule").checked = !!s.schedule_enabled;
    if (s.window_start) $("#trend-start").value = s.window_start;
    if (s.window_end) $("#trend-end").value = s.window_end;
    if (s.interval_min) $("#trend-interval").value = s.interval_min;
  }
  let st = s.running ? "调度运行中" : "调度未运行";
  if (!s.trend_enabled) st += " · 资料库未启用(TREND_ENABLED=false)";
  else if (s.paused_by_recording) st += " · 已因录制暂停";
  else if (s.collecting) st += " · 正在采集";
  if (s.last_run_at) st += ` · 上次采集 ${esc(s.last_run_at)}(${s.last_saved} 条)`;
  $("#trend-schedule-status").textContent = st;
}

// ----------------------------- 渲染:多大模型配置 ----------------------------- //
function llmRow(p) {
  p = p || {};
  const keyPlaceholder = p.api_key_set ? `已配置 ${esc(p.api_key_hint || "")}(留空不改)` : "填写 API Key";
  return `
  <div class="item llm-row" data-id="${esc(p.id || "")}">
    <div class="row" style="gap:8px; flex-wrap:wrap">
      <input class="llm-name" style="width:120px" placeholder="名称" value="${esc(p.name || "")}" />
      <input class="llm-base" style="width:260px" placeholder="base_url" value="${esc(p.base_url || "")}" />
      <input class="llm-model" style="width:150px" placeholder="模型" value="${esc(p.model || "")}" />
      <input class="llm-key" type="password" style="width:180px" placeholder="${keyPlaceholder}" />
    </div>
    <div class="row" style="gap:8px; flex-wrap:wrap; margin-top:6px; align-items:center">
      <input class="llm-search" style="width:150px" placeholder="联网参数(如 enable_search)" value="${esc(p.web_search_param || "")}" />
      <span class="muted">优先级</span>
      <input class="llm-priority" type="number" style="width:80px" value="${p.priority != null ? p.priority : 100}" />
      <label class="switch-row"><input type="checkbox" class="llm-enabled" ${p.enabled === false ? "" : "checked"} /> 启用</label>
      <button class="llm-del" data-act="del-llm">删除</button>
    </div>
  </div>`;
}

async function loadLLM() {
  const data = await api("GET", "/api/llm-providers");
  $("#llm-status").textContent = `已配置 ${data.providers.length} 个 · 可用 ${data.active_count} 个(按优先级从小到大调用)`;
  $("#llm-list").innerHTML = data.providers.length
    ? data.providers.map(llmRow).join("")
    : `<div class="empty">尚未配置。点击「+ 新增模型」,或使用 .env 的单模型配置。</div>`;
}

function collectLLM() {
  return [...document.querySelectorAll(".llm-row")].map((row) => ({
    id: row.dataset.id || "",
    name: row.querySelector(".llm-name").value.trim(),
    base_url: row.querySelector(".llm-base").value.trim(),
    model: row.querySelector(".llm-model").value.trim(),
    api_key: row.querySelector(".llm-key").value,
    web_search_param: row.querySelector(".llm-search").value.trim(),
    priority: parseInt(row.querySelector(".llm-priority").value || "100", 10),
    enabled: row.querySelector(".llm-enabled").checked,
  })).filter((p) => p.base_url && p.model);
}

// ----------------------------- 渲染:网感资料库 ----------------------------- //
async function loadTrends() {
  const data = await api("GET", "/api/trends?limit=30&days=7");
  $("#trends-status").innerHTML = data.enabled
    ? `已启用 · 联网搜索 ${data.web_search ? "开" : "关"} · 近 ${data.days} 天`
    : `未启用(设置 TREND_ENABLED=true 并配置大模型 API 后可用)`;
  renderScheduler(data.scheduler || {});
  const kw = data.keywords || [];
  $("#trends-keywords").innerHTML = kw.length
    ? `<div class="tagcloud">${kw.map((k) => `<span class="tagchip" title="出现 ${k.count} 次">${esc(k.keyword)} · ${k.heat}</span>`).join("")}</div>`
    : `<div class="empty">暂无热词。点击「立即联网采集」。</div>`;
  const items = data.items || [];
  $("#trends-list").innerHTML = items.length ? items.map((it) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">${esc(it.title)}</div>
          <div class="sub">${esc(it.source)} · ${esc(it.category || "")} · 热度 ${it.heat} · 出现 ${it.seen_count} 次</div>
        </div>
      </div>
      <div class="txt">${esc(it.summary || "")}</div>
      ${(it.tags || []).length ? `<div class="tagcloud">${it.tags.map((t) => `<span class="tagchip">${esc(t)}</span>`).join("")}</div>` : ""}
    </div>`).join("") : `<div class="empty">资料库暂无数据。</div>`;
}

// ----------------------------- 渲染:候选审核 ----------------------------- //
async function loadCandidates() {
  const status = $("#cand-filter").value;
  const rows = await api("GET", `/api/candidates?limit=50${status ? "&status=" + status : ""}`);
  $("#candidates-list").innerHTML = rows.length ? rows.map((c) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">候选 #${c.id} · 分数 ${c.highlight_score} ${badge(c.status)}</div>
          <div class="sub">规则 ${c.rule_score} / LLM ${c.llm_score} · ${esc(c.reason || "")}</div>
        </div>
        <div class="actions">
          <button class="ok" onclick="approveCand(${c.id})">批准并出片</button>
          <button class="danger" onclick="rejectCand(${c.id})">拒绝</button>
          <button onclick="delCand(${c.id})">删除</button>
        </div>
      </div>
      <div class="score-bar"><span style="width:${Math.round(c.highlight_score * 100)}%"></span></div>
    </div>`).join("") : `<div class="empty">暂无候选。</div>`;
}
window.approveCand = async (id) => {
  toast("出片中,请稍候…");
  try { const r = await api("POST", `/api/candidates/${id}/approve`); toast("已出片 clip #" + r.clip_id); loadCandidates(); }
  catch (e) { toast("出片失败:" + e.message); }
};
window.rejectCand = async (id) => {
  try { await api("POST", `/api/candidates/${id}/reject`); toast("已拒绝"); loadCandidates(); }
  catch (e) { toast(e.message); }
};
window.delCand = async (id) => {
  try { await api("DELETE", `/api/candidates/${id}`); toast("已删除"); loadCandidates(); }
  catch (e) { toast(e.message); }
};
$("#cand-filter").addEventListener("change", loadCandidates);

// ----------------------------- 渲染:成品切片 ----------------------------- //
async function loadClips() {
  const rows = await api("GET", "/api/clips?limit=50");
  $("#clips-list").innerHTML = rows.length ? rows.map((c) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">${esc(c.title || "(无标题)")} ${badge(c.status)}</div>
          <div class="sub">#${c.id} · ${c.duration_s ? c.duration_s.toFixed(0) + "s" : "-"} · 标签:${(c.tags || []).map(esc).join("、")}</div>
        </div>
        <div class="actions">
          <button class="ok" onclick="publishClip(${c.id})">发布(置 ready)</button>
          <button onclick="enqueueClip(${c.id})">加入上传队列</button>
          <button class="danger" onclick="rejectClip(${c.id})">拒绝</button>
        </div>
      </div>
      <div class="txt sub">${esc(c.description || "")}</div>
      <video controls preload="none" poster="/api/clips/${c.id}/cover" src="/api/clips/${c.id}/video"></video>
    </div>`).join("") : `<div class="empty">暂无成品切片。</div>`;
}
window.publishClip = async (id) => {
  try { const r = await api("POST", `/api/clips/${id}/publish`); toast(r.uploaded ? "已发布并进入上传:" + r.task_status : "已置 ready 并导出清单"); loadClips(); }
  catch (e) { toast(e.message); }
};
window.enqueueClip = async (id) => {
  try { const r = await api("POST", `/api/clips/${id}/enqueue`); toast("上传任务:" + r.status); loadUploads(); }
  catch (e) { toast(e.message); }
};
window.rejectClip = async (id) => {
  try { await api("POST", `/api/clips/${id}/reject`); toast("已拒绝"); loadClips(); }
  catch (e) { toast(e.message); }
};

// ----------------------------- 渲染:上传 / 设置 ----------------------------- //
async function loadUploads() {
  const s = await api("GET", "/api/settings");
  $("#sw-biliup").checked = s.biliup_enabled;
  $("#sw-auto").checked = s.auto_upload;
  $("#clips-dir-path").textContent = s.clips_dir;
  $("#upload-hint").textContent = s.upload_active
    ? "上传模块:已开启(biliup)。" + (s.biliup_cmd_configured ? "" : " 但未配置 BILIUP_UPLOAD_CMD,上传会安全失败。")
    : "上传模块:已关闭。直播结束将弹出切片目录,成品仅导出待上传清单。";

  const rows = await api("GET", "/api/uploads?limit=50");
  $("#uploads-list").innerHTML = rows.length ? rows.map((t) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">任务 #${t.id} · clip ${t.clip_id} ${badge(t.status)}</div>
          <div class="sub">上传器 ${esc(t.uploader)} · 尝试 ${t.attempts} 次 ${t.remote_id ? "· " + esc(t.remote_id) : ""}</div>
          ${t.last_error ? `<div class="sub" style="color:var(--red)">${esc(t.last_error)}</div>` : ""}
        </div>
        <div class="actions">
          <button onclick="retryUpload(${t.id})">重试</button>
        </div>
      </div>
    </div>`).join("") : `<div class="empty">暂无上传任务。</div>`;
}
async function saveSwitch() {
  try {
    await api("PATCH", "/api/settings", {
      biliup_enabled: $("#sw-biliup").checked,
      auto_upload: $("#sw-auto").checked,
    });
    toast("已保存上传开关");
    loadUploads();
  } catch (e) { toast(e.message); }
}
window.retryUpload = async (id) => {
  try { const r = await api("POST", `/api/uploads/${id}/retry`); toast("重试结果:" + r.status); loadUploads(); }
  catch (e) { toast(e.message); }
};

// ----------------------------- 渲染:日志 ----------------------------- //
async function loadLogs() {
  const rows = await api("GET", "/api/logs?limit=100");
  $("#logs-list").innerHTML = rows.length ? `<div class="card">${rows.map((l) => `
    <div class="log-line"><span class="lvl-${l.level}">[${esc(l.level)}]</span>
      ${esc(l.created_at || "")} ${esc(l.module || "")}:${esc(l.event || "")} — ${esc(l.message)}</div>`).join("")}</div>`
    : `<div class="empty">暂无 WARNING/ERROR 日志。</div>`;
}

// ----------------------------- V0.1.2 渲染:录制预约 ----------------------------- //
async function loadSchedules() {
  const data = await api("GET", "/api/dashboard");
  // 填充预约创建表单的房间下拉。
  let roomOpts = data.rooms.map((r) =>
    `<option value="${r.id}">#${r.id} ${esc(r.title || r.input_url)}</option>`
  ).join("");
  $("#schedule-room").innerHTML = roomOpts;

  const rows = await api("GET", "/api/schedules");
  $("#schedules-list").innerHTML = rows.length ? rows.map((s) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">预约 #${s.id} · 房间 #${s.room_id} ${esc(s.uploader_name || s.room_title || "")}</div>
          <div class="sub">${esc(s.scheduled_at || "")} · ${s.recurrent === "daily" ? "每日" : "单次"} · ${s.triggered ? "已触发" : (s.enabled ? "等待中" : "已禁用")}</div>
        </div>
        <div class="actions">
          <button class="danger" onclick="delSchedule(${s.id})">删除</button>
        </div>
      </div>
    </div>`).join("") : `<div class="empty">暂无录制预约。</div>`;
}
window.delSchedule = async (id) => {
  try { await api("DELETE", `/api/schedules/${id}`); toast("已删除"); loadSchedules(); }
  catch (e) { toast(e.message); }
};
$("#btn-add-schedule").addEventListener("click", async () => {
  const roomId = parseInt($("#schedule-room").value, 10);
  const time = $("#schedule-time").value;
  const daily = $("#schedule-daily").checked;
  if (!time) return toast("请选择预约时间");
  try {
    await api("POST", "/api/schedules", {
      room_id: roomId,
      scheduled_at: new Date(time).toISOString(),
      recurrent: daily ? "daily" : "",
    });
    toast("已创建预约");
    loadSchedules();
  } catch (e) { toast("创建失败:" + e.message); }
});

// ----------------------------- V0.1.2 阈值自学习摘要 ----------------------------- //
async function loadThresholdLearning(roomId) {
  try {
    const tl = await api("GET", `/api/rooms/${roomId}/threshold-learning`);
    const div = $(`#tl-${roomId}`);
    if (!div) return;
    if (!tl.samples) {
      div.innerHTML = `<span class="muted">阈值自学习:尚无反馈样本</span>`;
      return;
    }
    const approvedRange = tl.approved_range
      ? `${tl.approved_range[0]}–${tl.approved_range[1]}`
      : "—";
    const rejectedRange = tl.rejected_range
      ? `${tl.rejected_range[0]}–${tl.rejected_range[1]}`
      : "—";
    let recHtml = "";
    if (tl.recommended && tl.recommended !== tl.current_threshold) {
      recHtml = ` · <span style="color:var(--accent)">推荐阈值:${tl.recommended}</span>`;
    }
    div.innerHTML = `
      <div class="row">
        <span class="score-chip">样本:${tl.samples}</span>
        <span class="score-chip">当前阈值:${tl.current_threshold}</span>
        <span class="score-chip">通过分:${approvedRange}</span>
        <span class="score-chip">拒绝分:${rejectedRange}</span>
        ${recHtml}
        ${!tl.ready ? `<span class="muted">(需${tl.min_samples}条)</span>` : ""}
      </div>`;
  } catch (e) { /* 静默 */ }
}

// 开关与按钮事件
$("#sw-biliup").addEventListener("change", saveSwitch);
$("#sw-auto").addEventListener("change", saveSwitch);
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
    toast("已保存定时采集设置");
    loadTrends();
  } catch (e) { toast("保存失败:" + e.message); }
});
$("#btn-add-llm").addEventListener("click", () => {
  const list = $("#llm-list");
  if (list.querySelector(".empty")) list.innerHTML = "";
  list.insertAdjacentHTML("beforeend", llmRow({ base_url: "https://api.deepseek.com/v1", model: "deepseek-chat", web_search_param: "enable_search", priority: 100 }));
});
$("#llm-list").addEventListener("click", (e) => {
  if (e.target.dataset.act === "del-llm") e.target.closest(".llm-row").remove();
});
$("#btn-save-llm").addEventListener("click", async () => {
  try {
    const r = await api("PUT", "/api/llm-providers", { providers: collectLLM() });
    toast(`已保存 ${r.providers.length} 个模型`);
    loadLLM();
  } catch (e) { toast("保存失败:" + e.message); }
});
$("#btn-test-llm").addEventListener("click", async () => {
  toast("测试中,请稍候…");
  try {
    const r = await api("POST", "/api/llm-providers/test");
    if (!r.results.length) return toast("无可用模型(需已启用且配置 key)");
    const ok = r.results.filter((x) => x.ok).map((x) => x.name);
    const bad = r.results.filter((x) => !x.ok).map((x) => x.name);
    toast(`可用:${ok.join("、") || "无"}${bad.length ? " · 失败:" + bad.join("、") : ""}`);
  } catch (e) { toast("测试失败:" + e.message); }
});
$("#btn-collect-trends").addEventListener("click", async () => {
  const topic = $("#trends-topic").value.trim();
  toast("联网采集中,请稍候…");
  try {
    const r = await api("POST", "/api/trends/collect", { topic });
    toast(r.enabled ? `采集完成,新增/更新 ${r.saved} 条` : (r.note || "未启用"));
    loadTrends();
  } catch (e) { toast("采集失败:" + e.message); }
});
$("#btn-open-dir").addEventListener("click", async () => {
  try { const r = await api("POST", "/api/open-clips-dir"); toast("已打开:" + r.clips_dir); }
  catch (e) { toast(e.message); }
});

// ----------------------------- 通知(直播结束弹目录等) ----------------------------- //
let lastNotifyId = 0;
async function pollNotifications() {
  try {
    const rows = await api("GET", `/api/notifications?since_id=${lastNotifyId}`);
    for (const n of rows) {
      lastNotifyId = Math.max(lastNotifyId, n.id);
      toast(n.message);
      if (n.data && n.data.clips_dir) {
        // 直播结束且上传关闭:再次确保目录信息可见。
        console.info("切片目录:", n.data.clips_dir);
      }
    }
  } catch (e) { /* 静默 */ }
}

// ----------------------------- 账号管理 ----------------------------- //
async function loadCookieStatus() {
  try {
    const info = await api("GET", "/api/cookie-status");
    const hint = $("#cookie-hint");
    if (info.has_cookie) {
      hint.innerHTML = `已登录 · UID: <b>${esc(info.uid || "?")}</b> · Cookie: <code>${esc(info.hint || "")}</code>`;
      hint.className = "hint ok";
    } else {
      hint.textContent = info.hint || "未配置 Cookie,弹幕采集/鉴权功能不可用。";
      hint.className = "hint warn";
    }
    hint.style.display = "";
  } catch (e) { /* 静默 */ }
}

let _loginPolling = null;
async function doLogin() {
  const btn = $("#btn-login");
  const status = $("#login-status");
  btn.disabled = true;
  status.textContent = "正在启动浏览器…";
  try {
    const resp = await api("POST", "/api/login");
    const taskId = resp.task_id;
    // 轮询登录状态
    if (_loginPolling) clearInterval(_loginPolling);
    _loginPolling = setInterval(async () => {
      try {
        const s = await api("GET", `/api/login/status?task_id=${taskId}`);
        status.textContent = { starting: "正在启动浏览器…", waiting: "请在弹出窗口中完成登录…" }[s.status] || s.status;
        if (s.status === "done") {
          clearInterval(_loginPolling);
          _loginPolling = null;
          status.textContent = "登录成功！Cookie 已自动保存。";
          btn.disabled = false;
          toast("Bilibili 登录成功");
          await loadCookieStatus();
        } else if (s.error) {
          clearInterval(_loginPolling);
          _loginPolling = null;
          status.textContent = "登录失败: " + esc(s.error);
          btn.disabled = false;
        }
      } catch (e) {
        clearInterval(_loginPolling);
        _loginPolling = null;
        status.textContent = "状态查询异常: " + esc(e.message);
        btn.disabled = false;
      }
    }, 2000);
  } catch (e) {
    status.textContent = "启动失败: " + esc(e.message);
    btn.disabled = false;
  }
}

async function clearCookie() {
  if (!confirm("确定要清除 Cookie？\n清除后弹幕采集等需要登录态的功能将不可用。")) return;
  try {
    await api("POST", "/api/login/clear");
    toast("Cookie 已清除。");
    await loadCookieStatus();
  } catch (e) {
    toast("清除失败: " + esc(e.message));
  }
}

// 绑定事件（DOM 加载后执行）
document.addEventListener("DOMContentLoaded", () => {
  const btnLogin = $("#btn-login");
  const btnClear = $("#btn-clear-cookie");
  if (btnLogin) btnLogin.addEventListener("click", doLogin);
  if (btnClear) btnClear.addEventListener("click", clearCookie);
});


// ----------------------------- 轮询 ----------------------------- //
const loaders = {
  rooms: loadRooms, recording: loadRecording, transcripts: loadTranscripts,
  danmaku: loadDanmaku, trends: loadTrends, candidates: loadCandidates,
  clips: loadClips, uploads: loadUploads, models: loadLLM, logs: loadLogs,
  schedules: loadSchedules, login: loadCookieStatus,
};
async function refresh() {
  try {
    await loadRooms(); // 始终刷新顶部统计
    if (activeTab !== "rooms" && loaders[activeTab]) await loaders[activeTab]();
  } catch (e) { /* 静默,避免打断轮询 */ }
  pollNotifications();
}
refresh();
setInterval(refresh, 5000);
