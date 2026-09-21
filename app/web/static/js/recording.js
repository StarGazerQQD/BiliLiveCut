// BiliLiveCut \u5f55\u5236\u63a7\u5236:\u542f\u52a8/\u505c\u6b62\u3001\u4f1a\u8bdd\u5217\u8868\u3001\u8f6c\u5199\u3001\u5f39\u5e55
import { $, api, toast, esc, badge, DANMAKU_TYPE_LABEL, sessionTitle } from "./common.js";

const dirtyTranscriptIds = new Set();
const openTranscriptDetails = new Set();
const transcriptRevisions = new Map();
const transcriptSnapshots = new Map();
let transcriptEditorRevision = 0;
let transcriptListSignature = "";
let danmakuListSignature = "";
let sessionHistory = [];
let transcriptSessionOptionsSignature = "";
let danmakuSessionOptionsSignature = "";

const TRANSCRIPT_SESSION_STORAGE_KEY = "bililivecut.transcripts.session-id";
const DANMAKU_SESSION_STORAGE_KEY = "bililivecut.danmaku.session-id";
let transcriptSelectedSessionId = readStoredSessionId(TRANSCRIPT_SESSION_STORAGE_KEY);
let danmakuSelectedSessionId = readStoredSessionId(DANMAKU_SESSION_STORAGE_KEY);

function readStoredSessionId(key) {
  try {
    return window.sessionStorage?.getItem(key) ?? null;
  } catch (_error) {
    return null;
  }
}

function storeSessionId(key, value) {
  try {
    window.sessionStorage?.setItem(key, value);
  } catch (_error) {
    // 禁用 Web Storage 时仍保留当前页面内的选择。
  }
}

function sessionOptionLabel(row, countField, countLabel) {
  const started = String(row.started_at_gmt8 || row.started_at || "时间未知").replace("T", " ").slice(0, 19);
  return `${recordingTitle(row)} · ${started} · 会话 #${row.session_id} · ${row[countField] || 0} ${countLabel}`;
}

function recordingTitle(row) {
  const source = row.source_label || "未知来源";
  return row.title_state === "local" ? source : `${source} · ${sessionTitle(row)}`;
}

function selectedSessionRow(selectedId) {
  return sessionHistory.find((row) => String(row.session_id) === String(selectedId || ""));
}

function renderSessionSelect(kind) {
  const isTranscript = kind === "transcript";
  if (isTranscript && transcriptEditorProtected()) return;

  const select = $(`#${kind}-session-select`);
  const countField = isTranscript ? "transcript_count" : "danmaku_count";
  const countLabel = isTranscript ? "条转写" : "条弹幕";
  let selectedId = isTranscript ? transcriptSelectedSessionId : danmakuSelectedSessionId;
  const params = new URLSearchParams(window.location?.search || "");
  const requestedId = isTranscript && params.get("tab") === "transcripts" ? params.get("session_id") : null;
  if (requestedId !== null) selectedId = requestedId;
  const hasStoredSelection = selectedId !== null
    && sessionHistory.some((row) => String(row.session_id) === String(selectedId));
  if (!hasStoredSelection && requestedId === null) selectedId = sessionHistory.length ? String(sessionHistory[0].session_id) : "";

  const optionsSignature = JSON.stringify(sessionHistory.map((row) => [
    row.session_id,
    row.source_label,
    row.session_title,
    row.title_snapshot,
    row.started_at_gmt8,
    row.status,
    row[countField],
  ]));
  const previousSignature = isTranscript ? transcriptSessionOptionsSignature : danmakuSessionOptionsSignature;
  if (optionsSignature !== previousSignature || !select.innerHTML) {
    select.innerHTML = sessionHistory.length
      ? sessionHistory.map((row) => `<option value="${row.session_id}">${esc(sessionOptionLabel(row, countField, countLabel))}</option>`).join("")
      : `<option value="">暂无录制场次</option>`;
    if (isTranscript) transcriptSessionOptionsSignature = optionsSignature;
    else danmakuSessionOptionsSignature = optionsSignature;
  }
  select.value = selectedId;

  const row = selectedSessionRow(selectedId);
  $(`#${kind}-session-meta`).textContent = row
    ? `${recordingTitle(row)} · ${row.status || "状态未知"} · ${row.transcript_count || 0} 条转写 · ${row.danmaku_count || 0} 条弹幕`
    : requestedId !== null ? "指定录播场次不存在，请重新选择。" : "暂无可查看的录制场次。";
  if (isTranscript) transcriptSelectedSessionId = selectedId;
  else danmakuSelectedSessionId = selectedId;
  if (selectedId) storeSessionId(isTranscript ? TRANSCRIPT_SESSION_STORAGE_KEY : DANMAKU_SESSION_STORAGE_KEY, selectedId);
}

async function loadSessionHistory() {
  sessionHistory = await api("GET", "/api/sessions/history");
  renderSessionSelect("transcript");
  renderSessionSelect("danmaku");
}

function hasOpenTranscriptEditor() {
  return [...openTranscriptDetails].some((key) => key.startsWith("correction:"));
}

function transcriptEditorProtected() {
  return dirtyTranscriptIds.size > 0 || hasOpenTranscriptEditor();
}

function hasTranscriptDraft() {
  return dirtyTranscriptIds.size > 0;
}

function transcriptRevision(id) {
  return transcriptRevisions.get(id) || 0;
}

function bumpTranscriptRevision(id) {
  transcriptRevisions.set(id, transcriptRevision(id) + 1);
}

function updateTranscriptDirtyHint() {
  $("#transcripts-dirty-hint").style.display = transcriptEditorProtected() ? "" : "none";
}

// ----------------------------- \u542f\u52a8/\u505c\u6b62\u5f55\u5236 ----------------------------- //
async function startRoom(id) {
  try {
    await api("POST", `/api/rooms/${id}/start`, { produce: false });
    toast("\u5df2\u5f00\u59cb\u5f55\u5236");
    const { loadRooms } = await import("./rooms.js");
    await loadRooms();
  } catch (e) { toast("\u542f\u52a8\u5931\u8d25:" + e.message); }
}

async function stopRoom(id, force = false) {
  const label = force ? "强制停止" : "停止录制";
  if (!window.confirm(`${label}？已完成的片段会保留并继续处理。`)) return;
  try {
    const result = await api("POST", `/api/rooms/${id}/stop`, { mode: force ? "force" : "graceful", cancel_pending: false });
    toast(result.forced ? "已强制停止，末尾片段可能不完整" : "录制已停止并完成收尾");
    const { loadRooms } = await import("./rooms.js");
    await loadRooms();
  } catch (e) { toast("\u505c\u6b62\u5931\u8d25:" + e.message); }
}

async function resumeRoom(id) {
  try {
    await api("POST", `/api/rooms/${id}/resume`, { produce: false });
    toast("已恢复录制；本次会创建新会话并保留暂停缺口");
    const { loadRooms } = await import("./rooms.js");
    await loadRooms();
  } catch (e) { toast("恢复失败:" + e.message); }
}

async function markHighlight(id) {
  const note = window.prompt("给这个高光点加一句备注（可留空）：", "");
  if (note === null) return;
  try {
    const result = await api("POST", `/api/rooms/${id}/markers`, {
      pre_roll_s: 60,
      post_roll_s: 40,
      note: note.trim() || null,
    });
    toast(`已打点：候选 #${result.candidate_id}（前 60 秒 / 后 40 秒）`);
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
        <div class="title">${esc(s.source_label || `房间 ${s.room_id}`)} · ${esc(sessionTitle(s))} · 会话 #${s.id} ${badge(s.status)}</div>
        <div class="sub">${s.segments} \u4e2a\u7247\u6bb5 \u00b7 \u91cd\u8fde ${s.reconnect_count} \u6b21${reconnectInfo} \u00b7 ${esc(s.stream_format || "-")}${s.pipeline_enabled == null ? "" : ` \u00b7 \u5b9e\u65f6\u8f6c\u5199:${s.pipeline_enabled ? "\u5f00" : "\u5173"}`}</div>
      </div>
      ${s.error_message ? `<div class="sub" style="color:var(--red)">${esc(s.error_message)}</div>` : ""}
    </div>`}).join("") : `<div class="empty">\u6682\u65e0\u5f55\u5236\u4f1a\u8bdd\u3002</div>`;
}

// ----------------------------- \u6e32\u67d3:\u5b9e\u65f6\u8f6c\u5199 ----------------------------- //
async function loadTranscripts(forceRender = false) {
  if (transcriptEditorProtected()) {
    updateTranscriptDirtyHint();
    return;
  }
  updateTranscriptDirtyHint();
  await loadSessionHistory();
  if (!selectedSessionRow(transcriptSelectedSessionId)) {
    $("#transcripts-list").innerHTML = '<div class="empty">暂无可查看的转写，请选择有效场次。</div>';
    transcriptListSignature = "";
    return;
  }
  const revision = transcriptEditorRevision;
  const selectedSessionId = transcriptSelectedSessionId;
  const query = selectedSessionId
    ? `/api/transcripts?limit=500&session_id=${encodeURIComponent(selectedSessionId)}`
    : "/api/transcripts?limit=30";
  const rows = await api("GET", query);
  if (
    transcriptEditorProtected()
    || transcriptEditorRevision !== revision
    || transcriptSelectedSessionId !== selectedSessionId
  ) {
    updateTranscriptDirtyHint();
    return;
  }
  const signature = `${selectedSessionId || "all"}:${JSON.stringify(rows)}`;
  if (!forceRender && signature === transcriptListSignature && $("#transcripts-list").innerHTML) return;
  const viewport = {
    x: Number(window.scrollX ?? window.pageXOffset ?? 0),
    y: Number(window.scrollY ?? window.pageYOffset ?? 0),
  };
  transcriptSnapshots.clear();
  rows.forEach((row) => transcriptSnapshots.set(Number(row.id), { text: row.text || "" }));
  $("#transcripts-list").innerHTML = rows.length ? rows.map((t) => {
    const rawDetailKey = `raw:${t.id}`;
    const correctionDetailKey = `correction:${t.id}`;
    const rawDetails = t.llm_refined && t.raw_text && t.raw_text !== t.text
      ? `<details data-transcript-detail="${rawDetailKey}" ${openTranscriptDetails.has(rawDetailKey) ? "open" : ""} style="margin-top:8px"><summary class="muted">查看原始 ASR</summary><div class="txt muted">${esc(t.raw_text)}</div></details>`
      : "";
    const sourceFile = t.source_file_name
      ? `<div class="sub" style="margin-top:6px"><b>源文件：</b><code id="transcript-source-file-${t.id}">${esc(t.source_file_name)}</code> <button type="button" class="secondary" onclick="copyTranscriptSourceFile(${t.id})">复制文件名</button> ${t.source_mp4_available ? `<a class="btn-link" href="/api/transcripts/${t.id}/source-mp4" download>无损导出 MP4</a>` : '<span class="muted">此格式不提供 TS 转 MP4 导出</span>'}</div>`
      : `<div class="sub" style="margin-top:6px"><b>源文件：</b>原始片段记录不可用</div>`;
    return `
    <div class="item" id="transcript-item-${t.id}">
      <div class="sub">${esc(recordingTitle(t))} · 会话 #${t.session_id ?? "-"} · 片段 #${t.segment_id} · ${esc(t.language || "")} · ${esc(t.primary_backend || "")} · ${esc(t.created_at || "")}</div>
      ${sourceFile}
      <div class="txt" id="transcript-final-${t.id}">${esc(t.text) || "(\u7a7a)"}</div>
      ${t.summary ? `<div class="sub" style="margin-top:8px"><b>片段概括：</b>${esc(t.summary)}</div>` : ""}
      ${rawDetails}
      <details class="transcript-correction" id="transcript-editor-${t.id}" data-transcript-editor="${t.id}" data-transcript-detail="${correctionDetailKey}" ${openTranscriptDetails.has(correctionDetailKey) ? "open" : ""} style="margin-top:8px">
        <summary>人工纠错并学习房间词典</summary>
        <label class="editor-label">纠正后的全文
          <textarea id="transcript-text-${t.id}" rows="6">${esc(t.text || "")}</textarea>
        </label>
        <label class="editor-label">专属词纠错（可选，每行“错误词=正确词”）
          <textarea id="transcript-aliases-${t.id}" rows="2" placeholder="查里斯=查理斯"></textarea>
        </label>
        <label class="chk"><input type="checkbox" id="transcript-learn-${t.id}" checked /> 将纠错词学习到 ${esc(t.source_label || "当前直播间")} 的 ASR 词典</label>
        <p class="hint">保存后会自动请求本场重分析；人工审核、人工边界、草稿和已生成成品不会被覆盖。</p>
        <div class="actions">
          <button class="primary" onclick="correctTranscript(${t.id})">保存纠错</button>
          <button onclick="cancelTranscriptCorrection(${t.id})">取消编辑</button>
          <button class="secondary" onclick="retranscribeTranscript(${t.id})">重新识别原音频</button>
        </div>
      </details>
    </div>`;
  }).join("") : `<div class="empty">\u6682\u65e0\u8f6c\u5199\u3002\u8bf7\u5728\u300c\u914d\u7f6e \u2192 \u529f\u80fd\u5f00\u5173\u300d\u542f\u7528\u201c\u5f55\u5236\u5b9e\u65f6\u8f6c\u5199\u201d\uff0c\u6216\u5728 CLI \u4f7f\u7528 --pipeline\u3002</div>`;
  transcriptListSignature = signature;
  if (typeof window.scrollTo === "function") {
    const restore = () => window.scrollTo(viewport.x, viewport.y);
    if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(restore);
    else restore();
  }
}

async function copyTranscriptSourceFile(id) {
  const filename = $(`#transcript-source-file-${id}`)?.textContent?.trim();
  if (!filename) {
    toast("源文件名不可用");
    return;
  }
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
    await navigator.clipboard.writeText(filename);
    toast(`已复制源文件名：${filename}`);
  } catch (_error) {
    window.prompt("复制源文件名：", filename);
  }
}

function parseTranscriptAliases(value) {
  const aliases = {};
  for (const rawLine of String(value || "").split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    const separator = line.indexOf("=");
    if (separator <= 0 || !line.slice(separator + 1).trim()) {
      throw new Error(`词典格式错误：${line}（应为“错误词=正确词”）`);
    }
    aliases[line.slice(0, separator).trim()] = line.slice(separator + 1).trim();
  }
  return aliases;
}

function closeTranscriptEditor(id, text) {
  const detail = $(`#transcript-editor-${id}`);
  if (detail) detail.open = false;
  const textInput = $(`#transcript-text-${id}`);
  if (textInput && text !== undefined) textInput.value = text;
  const aliasesInput = $(`#transcript-aliases-${id}`);
  if (aliasesInput) aliasesInput.value = "";
  const learnInput = $(`#transcript-learn-${id}`);
  if (learnInput) learnInput.checked = true;
  openTranscriptDetails.delete(`correction:${id}`);
}

async function settleTranscriptEditor(id, text) {
  dirtyTranscriptIds.delete(id);
  bumpTranscriptRevision(id);
  transcriptEditorRevision += 1;
  closeTranscriptEditor(id, text);
  updateTranscriptDirtyHint();
  if (!transcriptEditorProtected()) await loadTranscripts(true);
}

async function correctTranscript(id) {
  try {
    const revision = transcriptRevision(id);
    const correctedText = $(`#transcript-text-${id}`).value.trim();
    if (!correctedText) throw new Error("纠正后的转写不能为空");
    const aliases = parseTranscriptAliases($(`#transcript-aliases-${id}`).value);
    const result = await api("PATCH", `/api/transcripts/${id}`, {
      corrected_text: correctedText,
      aliases,
      learn_dictionary: $(`#transcript-learn-${id}`).checked,
    });
    const learnedCount = Object.keys(result.learned_aliases || {}).length;
    if (transcriptRevision(id) !== revision) {
      updateTranscriptDirtyHint();
      toast(`已保存提交时的转写${learnedCount ? `，学习 ${learnedCount} 条房间词典` : ""}；保存期间还有新修改，请再次保存`);
      return;
    }
    transcriptSnapshots.set(id, { text: correctedText });
    const finalText = $(`#transcript-final-${id}`);
    if (finalText) finalText.textContent = correctedText;
    toast(`转写已保存并请求整场重分析${learnedCount ? `，学习 ${learnedCount} 条房间词典` : ""}`);
    await settleTranscriptEditor(id, correctedText);
  } catch (e) { toast("保存纠错失败：" + e.message); }
}

async function cancelTranscriptCorrection(id) {
  await settleTranscriptEditor(id, transcriptSnapshots.get(id)?.text || "");
}

async function retranscribeTranscript(id) {
  if (!window.confirm("重新识别会删除当前转写，并清理尚未人工审核或渲染的自动分析结果。继续吗？")) return;
  try {
    const result = await api("POST", `/api/transcripts/${id}/retranscribe`);
    dirtyTranscriptIds.delete(id);
    openTranscriptDetails.delete(`correction:${id}`);
    transcriptSnapshots.delete(id);
    bumpTranscriptRevision(id);
    transcriptEditorRevision += 1;
    updateTranscriptDirtyHint();
    toast(`片段 #${result.segment_id} 已重新加入转写队列`);
    if (transcriptEditorProtected()) $(`#transcript-item-${id}`)?.remove();
    else await loadTranscripts(true);
  } catch (e) { toast("重新识别失败:" + e.message); }
}

function markTranscriptDirty(event) {
  const editor = event.target.closest?.("[data-transcript-editor]");
  if (!editor) return;
  const id = Number(editor.dataset.transcriptEditor);
  dirtyTranscriptIds.add(id);
  bumpTranscriptRevision(id);
  transcriptEditorRevision += 1;
  updateTranscriptDirtyHint();
}

$("#transcripts-list").addEventListener("input", markTranscriptDirty);
$("#transcripts-list").addEventListener("change", markTranscriptDirty);

$("#transcripts-list").addEventListener("toggle", (event) => {
  const detail = event.target.closest?.("[data-transcript-detail]");
  const key = detail?.dataset?.transcriptDetail;
  if (!key) return;
  if (detail.open) openTranscriptDetails.add(key);
  else openTranscriptDetails.delete(key);
  if (key.startsWith("correction:")) bumpTranscriptRevision(Number(key.split(":", 2)[1]));
  transcriptEditorRevision += 1;
  updateTranscriptDirtyHint();
}, true);

$("#transcript-session-select").addEventListener("change", async (event) => {
  if (transcriptEditorProtected()) {
    event.target.value = transcriptSelectedSessionId || "";
    updateTranscriptDirtyHint();
    toast("请先保存或取消当前转写纠错，再切换录制场次");
    return;
  }
  transcriptSelectedSessionId = String(event.target.value || "");
  if (window.history?.replaceState) {
    const url = new URL(window.location.href);
    url.searchParams.delete("session_id");
    window.history.replaceState(null, "", url);
  }
  storeSessionId(TRANSCRIPT_SESSION_STORAGE_KEY, transcriptSelectedSessionId);
  transcriptListSignature = "";
  await loadTranscripts(true);
});

// ----------------------------- \u6e32\u67d3:\u5f39\u5e55\u70ed\u5ea6 ----------------------------- //
async function loadDanmaku() {
  await loadSessionHistory();
  const selectedSessionId = danmakuSelectedSessionId;
  const query = selectedSessionId
    ? `/api/danmaku?limit=500&session_id=${encodeURIComponent(selectedSessionId)}`
    : "/api/danmaku?limit=60";
  const data = await api("GET", query);
  if (danmakuSelectedSessionId !== selectedSessionId) return;
  const signature = `${selectedSessionId || "all"}:${JSON.stringify(data)}`;
  if (signature === danmakuListSignature && $("#danmaku-list").innerHTML) return;
  const viewport = {
    x: Number(window.scrollX ?? window.pageXOffset ?? 0),
    y: Number(window.scrollY ?? window.pageYOffset ?? 0),
  };
  const sessions = data.sessions || [];
  $("#danmaku-sessions").innerHTML = sessions.length ? sessions.map((s) => `
    <div class="item">
      <div class="sub">${esc(s.source_label || "未知来源")} · 会话 #${s.session_id}</div>
      <div class="title">\u5f39\u5e55 ${s.count} \u6761 \u00b7 \u5f3a\u5ea6 ${s.intensity}</div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u5f39\u5e55\u3002\u5f00\u542f\u5f55\u5236(COLLECT_DANMAKU=true)\u540e\u4f1a\u81ea\u52a8\u91c7\u96c6\u3002</div>`;
  const recent = data.recent || [];
  $("#danmaku-list").innerHTML = recent.length ? recent.map((d) => `
    <div class="item">
      <div class="sub">${esc(d.source_label || "未知来源")} · 会话 #${d.session_id} · ${DANMAKU_TYPE_LABEL[d.type] || d.type} · ${esc(d.user || "匿名")} · ${esc(d.ts || "")}</div>
      <div class="txt">${esc(d.content) || "(\u65e0\u6587\u672c)"}</div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u5f39\u5e55\u8bb0\u5f55\u3002</div>`;
  danmakuListSignature = signature;
  if (typeof window.scrollTo === "function") {
    const restore = () => window.scrollTo(viewport.x, viewport.y);
    if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(restore);
    else restore();
  }
}

$("#danmaku-session-select").addEventListener("change", async (event) => {
  danmakuSelectedSessionId = String(event.target.value || "");
  storeSessionId(DANMAKU_SESSION_STORAGE_KEY, danmakuSelectedSessionId);
  danmakuListSignature = "";
  await loadDanmaku();
});

export { startRoom, stopRoom, resumeRoom, markHighlight, copyTranscriptSourceFile, correctTranscript, cancelTranscriptCorrection, retranscribeTranscript, loadRecording, loadTranscripts, loadDanmaku, hasTranscriptDraft };
