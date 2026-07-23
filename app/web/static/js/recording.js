// BiliLiveCut \u5f55\u5236\u63a7\u5236:\u542f\u52a8/\u505c\u6b62\u3001\u4f1a\u8bdd\u5217\u8868\u3001\u8f6c\u5199\u3001\u5f39\u5e55
import { $, api, toast, esc, badge, DANMAKU_TYPE_LABEL } from "./common.js";

// ----------------------------- \u542f\u52a8/\u505c\u6b62\u5f55\u5236 ----------------------------- //
async function startRoom(id) {
  try {
    await api("POST", `/api/rooms/${id}/start`, { pipeline: true, produce: false });
    toast("\u5df2\u5f00\u59cb\u5f55\u5236");
    const { loadRooms } = await import("./rooms.js");
    loadRooms();
  } catch (e) { toast("\u542f\u52a8\u5931\u8d25:" + e.message); }
}

async function stopRoom(id, force = false) {
  const label = force ? "强制停止" : "停止录制";
  if (!window.confirm(`${label}？已完成的片段会保留并继续处理。`)) return;
  try {
    const result = await api("POST", `/api/rooms/${id}/stop`, { mode: force ? "force" : "graceful", cancel_pending: false });
    toast(result.forced ? "已强制停止，末尾片段可能不完整" : "录制已停止并完成收尾");
    const { loadRooms } = await import("./rooms.js");
    loadRooms();
  } catch (e) { toast("\u505c\u6b62\u5931\u8d25:" + e.message); }
}

async function resumeRoom(id) {
  try {
    await api("POST", `/api/rooms/${id}/resume`, { pipeline: true, produce: false });
    toast("已恢复录制；本次会创建新会话并保留暂停缺口");
    const { loadRooms } = await import("./rooms.js");
    loadRooms();
  } catch (e) { toast("恢复失败:" + e.message); }
}

async function markHighlight(id) {
  const note = window.prompt("给这个高光点加一句备注（可留空）：", "");
  if (note === null) return;
  try {
    const result = await api("POST", `/api/rooms/${id}/markers`, {
      pre_roll_s: 20,
      post_roll_s: 40,
      note: note.trim() || null,
    });
    toast(`已打点：候选 #${result.candidate_id}（前 20 秒 / 后 40 秒）`);
  } catch (e) { toast("打点失败:" + e.message); }
}

// ----------------------------- \u6e32\u67d3:\u5f55\u5236\u72b6\u6001 ----------------------------- //
async function loadRecording() {
  const [rows, prog] = await Promise.all([
    api("GET", "/api/recording"),
    api("GET", "/api/progress"),
  ]);
  $("#progress-title").textContent = prog.total_segments ? `\u603b\u7247\u6bb5 ${prog.total_segments}` : "";
  $("#progress-bar").style.width = prog.progress_pct + "%";
  $("#progress-text").textContent = prog.total_segments
    ? `\u5df2\u5f55\u5236 ${prog.recorded} \u00b7 \u5df2\u8f6c\u5199 ${prog.transcribed} \u00b7 \u5df2\u8bc4\u5206 ${prog.scored} (${prog.progress_pct}%)`
    : "\u6682\u65e0\u8fdb\u884c\u4e2d\u7684\u5f55\u5236\u4f1a\u8bdd";
  $("#recording-list").innerHTML = rows.length ? rows.map((s) => {
    const reconnectInfo = s.last_reconnected_at
      ? ` \u00b7 \u6700\u8fd1\u91cd\u8fde ${esc(s.last_reconnected_at).substring(11, 19)}`
      : "";
    return `
    <div class="item">
      <div class="head">
        <div class="title">\u4f1a\u8bdd #${s.id} \u00b7 room ${s.room_id} ${badge(s.status)}</div>
        <div class="sub">${s.segments} \u4e2a\u7247\u6bb5 \u00b7 \u91cd\u8fde ${s.reconnect_count} \u6b21${reconnectInfo} \u00b7 ${esc(s.stream_format || "-")}</div>
      </div>
      ${s.error_message ? `<div class="sub" style="color:var(--red)">${esc(s.error_message)}</div>` : ""}
    </div>`}).join("") : `<div class="empty">\u6682\u65e0\u5f55\u5236\u4f1a\u8bdd\u3002</div>`;
}

// ----------------------------- \u6e32\u67d3:\u5b9e\u65f6\u8f6c\u5199 ----------------------------- //
async function loadTranscripts() {
  const rows = await api("GET", "/api/transcripts?limit=30");
  $("#transcripts-list").innerHTML = rows.length ? rows.map((t) => `
    <div class="item">
      <div class="sub">\u7247\u6bb5 #${t.segment_id} \u00b7 ${esc(t.language || "")} \u00b7 ${esc(t.created_at || "")}</div>
      <div class="txt">${esc(t.text) || "(\u7a7a)"}</div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u8f6c\u5199\u3002\u5f00\u59cb\u5e26 --pipeline \u7684\u5f55\u5236\u540e\u4f1a\u51fa\u73b0\u3002</div>`;
}

// ----------------------------- \u6e32\u67d3:\u5f39\u5e55\u70ed\u5ea6 ----------------------------- //
async function loadDanmaku() {
  const data = await api("GET", "/api/danmaku?limit=60");
  const sessions = data.sessions || [];
  $("#danmaku-sessions").innerHTML = sessions.length ? sessions.map((s) => `
    <div class="item">
      <div class="sub">\u4f1a\u8bdd #${s.session_id}</div>
      <div class="title">\u5f39\u5e55 ${s.count} \u6761 \u00b7 \u5f3a\u5ea6 ${s.intensity}</div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u5f39\u5e55\u3002\u5f00\u542f\u5f55\u5236(COLLECT_DANMAKU=true)\u540e\u4f1a\u81ea\u52a8\u91c7\u96c6\u3002</div>`;
  const recent = data.recent || [];
  $("#danmaku-list").innerHTML = recent.length ? recent.map((d) => `
    <div class="item">
      <div class="sub">#${d.session_id} \u00b7 ${DANMAKU_TYPE_LABEL[d.type] || d.type} \u00b7 ${esc(d.user || "\u533f\u540d")} \u00b7 ${esc(d.ts || "")}</div>
      <div class="txt">${esc(d.content) || "(\u65e0\u6587\u672c)"}</div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u5f39\u5e55\u8bb0\u5f55\u3002</div>`;
}

export { startRoom, stopRoom, resumeRoom, markHighlight, loadRecording, loadTranscripts, loadDanmaku };
