// BiliLiveCut 场次高光时间线：按录制会话聚合、展开与安全重分析
import { $, api, toast, esc, badge } from "./common.js";

const expandedSessions = new Set();
const knownRooms = new Map();
let loadGeneration = 0;

const PROCESSING_LABELS = {
  recording: "录制中",
  finalizing: "收尾/重分析中",
  processing: "分析中",
  partial_failure: "部分失败",
  ready: "已完成",
};

const SIGNAL_LABELS = {
  danmaku: "弹幕热度",
  audio: "音频峰值",
  transcript: "语义内容",
  visual: "画面变化",
  manual: "人工打点",
  trend: "网感趋势",
};

function formatGmt8(value) {
  if (!value) return "进行中";
  return String(value).replace("T", " ").slice(0, 19);
}

function formatDuration(seconds) {
  if (seconds == null) return "进行中";
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return [hours ? `${hours}时` : "", minutes ? `${minutes}分` : "", `${secs}秒`].filter(Boolean).join("");
}

function processingBadge(state) {
  const css = state === "ready" ? "green" : state === "partial_failure" ? "red" : "yellow";
  return `<span class="badge ${css}">${esc(PROCESSING_LABELS[state] || state || "未知")}</span>`;
}

function sourceLabel(session) {
  return session.source_label || session.uploader_name || session.room_title || `房间 ${session.room_id ?? "未知"}`;
}

function updateRoomFilter(rows) {
  rows.forEach((row) => {
    if (row.room_db_id != null) knownRooms.set(String(row.room_db_id), sourceLabel(row));
  });
  const select = $("#timeline-room-filter");
  const selected = select.value;
  const options = [...knownRooms.entries()]
    .sort((left, right) => left[1].localeCompare(right[1], "zh-CN"))
    .map(([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`)
    .join("");
  select.innerHTML = `<option value="">全部直播间</option>${options}`;
  if (knownRooms.has(selected)) select.value = selected;
}

function renderSessionCard(session) {
  const expanded = expandedSessions.has(session.session_id);
  const title = sourceLabel(session);
  const timeRange = `${formatGmt8(session.started_at_gmt8)} — ${formatGmt8(session.ended_at_gmt8)}`;
  return `
    <article class="timeline-session item" data-session-id="${session.session_id}">
      <div class="head timeline-session-head">
        <div>
          <div class="title">${esc(title)} · 会话 #${session.session_id} ${processingBadge(session.processing_state)} ${badge(session.status)}</div>
          <div class="sub">${esc(timeRange)} GMT+8 · ${formatDuration(session.duration_s)} · ${session.segment_count} 个录制片段</div>
          <div class="timeline-counts">
            <span>高光 <b>${session.highlight_count}</b></span>
            <span>待审 <b>${session.pending_review_count}</b></span>
            <span>已拒绝 <b>${session.rejected_count}</b></span>
          </div>
        </div>
        <div class="actions">
          <button onclick="toggleSessionTimeline(${session.session_id})">${expanded ? "收起" : "展开时间线"}</button>
          <button class="secondary" onclick="requestSessionReanalysis(${session.session_id}, false)">按新阈值重分析</button>
          <button class="secondary" onclick="requestSessionReanalysis(${session.session_id}, true)">按新词典重识别</button>
        </div>
      </div>
      <div id="timeline-detail-${session.session_id}" class="timeline-detail" ${expanded ? "" : "hidden"}>
        ${expanded ? '<div class="empty">正在载入时间线…</div>' : ""}
      </div>
    </article>`;
}

function renderDanmaku(items) {
  if (!items?.length) return '<div class="timeline-danmaku muted">该时刻没有可用高频弹幕</div>';
  return `<div class="timeline-danmaku">${items.slice(0, 2).map((item) => `
    <span title="出现 ${Number(item.count) || 1} 次">💬 ${esc(item.text)}${Number(item.count) > 1 ? ` ×${Number(item.count)}` : ""}</span>`).join("")}</div>`;
}

function renderSignals(point) {
  const labels = (point.source_signals || []).map((name) => SIGNAL_LABELS[name] || name);
  const provenance = point.provenance || {};
  if (provenance.dynamic_bounds) labels.push("动态头尾");
  if (provenance.cross_segment) labels.push("跨片段");
  if (provenance.danmaku_lag_s) labels.push(`弹幕回拨 ${provenance.danmaku_lag_s}s`);
  return labels.length
    ? `<div class="timeline-tags">${labels.map((label) => `<span>${esc(label)}</span>`).join("")}</div>`
    : "";
}

function renderTimelinePoint(point) {
  const confidence = Math.round((Number(point.confidence) || 0) * 100);
  const rejectedClass = point.rejected ? " rejected" : "";
  return `
    <li class="timeline-point${rejectedClass}">
      <div class="timeline-clock">${esc(point.clock_gmt8 || "--:--:--")}</div>
      <div class="timeline-node" aria-hidden="true"></div>
      <div class="timeline-point-card">
        <div class="head">
          <div>
            <div class="title">${esc(point.summary || "待生成高光梗概")}</div>
            <div class="sub">候选 #${point.candidate_id} · ${formatDuration(point.duration_s)} · 置信度 ${confidence}% · ${badge(point.review_status)}</div>
          </div>
          <a class="back-link" href="${esc(point.review_url)}" target="_blank" rel="noopener">精审/预览</a>
        </div>
        ${renderDanmaku(point.representative_danmaku)}
        ${renderSignals(point)}
        <details class="timeline-provenance">
          <summary>查看来源与评分</summary>
          <div class="sub">规则 ${Number(point.provenance?.rule_score || 0).toFixed(3)} · LLM ${Number(point.provenance?.llm_score || 0).toFixed(3)} · 综合 ${Number(point.provenance?.highlight_score || 0).toFixed(3)} · 区间 ${esc(point.start_at_gmt8 || "-")} 至 ${esc(point.end_at_gmt8 || "-")}</div>
        </details>
      </div>
    </li>`;
}

function renderTimelineDetail(data) {
  const points = data.points || [];
  if (!points.length) {
    return `<div class="empty">本场尚无${$("#timeline-include-rejected").checked ? "" : "未拒绝的"}高光节点；可等待分析完成或按新配置重分析。</div>`;
  }
  return `<ol class="session-timeline" aria-label="${esc(sourceLabel(data.session))} 的高光时间线">${points.map(renderTimelinePoint).join("")}</ol>`;
}

async function loadTimelineDetail(sessionId, generation = loadGeneration) {
  const target = $(`#timeline-detail-${sessionId}`);
  if (!target || !expandedSessions.has(sessionId)) return;
  try {
    const includeRejected = $("#timeline-include-rejected").checked ? "true" : "false";
    const data = await api("GET", `/api/sessions/${sessionId}/timeline?include_rejected=${includeRejected}`);
    if (generation !== loadGeneration || !expandedSessions.has(sessionId)) return;
    const current = $(`#timeline-detail-${sessionId}`);
    if (current) current.innerHTML = renderTimelineDetail(data);
  } catch (error) {
    const current = $(`#timeline-detail-${sessionId}`);
    if (current) current.innerHTML = `<div class="empty">时间线载入失败：${esc(error.message)}</div>`;
  }
}

async function loadSessionTimelines() {
  const generation = ++loadGeneration;
  const roomId = $("#timeline-room-filter").value;
  const suffix = roomId ? `&room_db_id=${encodeURIComponent(roomId)}` : "";
  const rows = await api("GET", `/api/sessions/timeline?limit=30${suffix}`);
  if (generation !== loadGeneration) return;
  updateRoomFilter(rows);
  $("#timeline-list").innerHTML = rows.length
    ? rows.map(renderSessionCard).join("")
    : '<div class="empty">暂无录制场次。开始录制后，这里会按场次生成高光时间线。</div>';
  await Promise.all(rows.filter((row) => expandedSessions.has(row.session_id)).map((row) => loadTimelineDetail(row.session_id, generation)));
}

async function toggleSessionTimeline(sessionId) {
  if (expandedSessions.has(sessionId)) expandedSessions.delete(sessionId);
  else expandedSessions.add(sessionId);
  await loadSessionTimelines();
}

async function requestSessionReanalysis(sessionId, retranscribe = false) {
  const message = retranscribe
    ? "重新识别会重跑本场自动转写和高光分析，但保留人工审核、人工边界和成品。继续吗？"
    : "按当前阈值、词典和模型配置重跑本场高光分析，并保留人工成果。继续吗？";
  if (!window.confirm(message)) return;
  try {
    const result = await api("POST", `/api/sessions/${sessionId}/reanalyze`, {
      reason: "timeline_manual_reanalysis",
      retranscribe,
    });
    toast(result.requested ? `会话 #${sessionId} 已加入重分析队列` : `会话 #${sessionId} 已有待处理的重分析请求`);
    expandedSessions.add(sessionId);
    await loadSessionTimelines();
  } catch (error) { toast("重分析请求失败：" + error.message); }
}

$("#timeline-room-filter").addEventListener("change", () => loadSessionTimelines());
$("#timeline-include-rejected").addEventListener("change", () => loadSessionTimelines());
$("#btn-refresh-timeline").addEventListener("click", () => loadSessionTimelines());

export { loadSessionTimelines, toggleSessionTimeline, requestSessionReanalysis };
