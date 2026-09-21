// 独立录播分析页：分步流式上传、持久作业和分析结果导航。
import { $, api, esc } from "./common.js";

let records = [];
let draft = null;
let pendingDeclaration = null;
let operationRevision = 0;
let busy = false;
let cancelled = false;
let activeUpload = null;
let listLoading = false;
let listSignature = "";
let actionPending = false;
let actionError = "";
let pageActive = true;
let pollTimer = null;
let videoLimit = 50 * 1024 ** 3;
let commentLimit = 32 * 1024 ** 2;

export function uploadFile(path, file, onProgress, onRequest) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", path);
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.upload.onprogress = event => {
      if (event.lengthComputable) onProgress(Math.round(event.loaded / event.total * 100));
    };
    request.onload = () => {
      let data;
      try { data = JSON.parse(request.responseText); } catch (_error) { reject(new Error("服务器返回无效响应")); return; }
      if (request.status >= 200 && request.status < 300) resolve(data);
      else reject(new Error(typeof data.detail === "string" ? data.detail : `上传失败（${request.status}）`));
    };
    request.onerror = () => reject(new Error("上传连接中断，可从导入记录继续"));
    request.onabort = () => reject(new Error("上传已停止，可重新选择未完成文件继续"));
    onRequest(request);
    request.send(file);
  });
}

function renderDraft() {
  const declaration = draft || pendingDeclaration;
  $("#import-fields").disabled = busy;
  $("#import-title").disabled = Boolean(declaration);
  $("#import-offset").disabled = Boolean(declaration);
  $("#import-video").disabled = Boolean(draft?.video_uploaded);
  $("#import-video").required = !draft?.video_uploaded;
  $("#import-comments").disabled = Boolean(declaration && (!declaration.comments_name || draft?.comments_uploaded));
  $("#import-comments").required = Boolean(declaration?.comments_name && !draft?.comments_uploaded);
  $("#import-submit").disabled = busy;
  $("#import-submit").textContent = draft || pendingDeclaration ? "继续上传并分析" : "导入并开始分析";
  $("#import-new").hidden = !(draft || pendingDeclaration) || busy;
  $("#import-resume").hidden = !declaration;
  $("#import-resume").textContent = declaration
    ? `继续「${declaration.title}」：视频 ${draft?.video_uploaded ? "已上传" : declaration.video_name}；弹幕 ${draft?.comments_uploaded ? "已上传" : declaration.comments_name || "未选择"}。已上传文件无需重选。更换素材请新建导入。`
    : "";
}

function resetForm() {
  draft = null;
  pendingDeclaration = null;
  $("#import-form").reset();
  $("#import-progress").hidden = true;
  $("#import-error").textContent = "";
  renderDraft();
}

function validateFile(file, expectedName, limit, label) {
  if (!file) throw new Error(`请选择${label}`);
  if (expectedName && file.name !== expectedName) throw new Error(`请重新选择 ${expectedName}`);
  if (!file.size || file.size > limit) throw new Error(`${label}为空或超过大小限制`);
}

async function submitImport(event) {
  event.preventDefault();
  if (busy) return;
  $("#import-error").textContent = "";
  const video = $("#import-video").files[0];
  const comments = $("#import-comments").files[0];
  try {
    const declaration = draft || pendingDeclaration;
    if (!draft?.video_uploaded) validateFile(video, declaration?.video_name, videoLimit, "视频文件");
    if (declaration?.comments_name && !draft?.comments_uploaded) validateFile(comments, declaration.comments_name, commentLimit, "弹幕文件");
    if (!draft && comments) validateFile(comments, null, commentLimit, "弹幕文件");
    busy = true;
    operationRevision += 1;
    cancelled = false;
    renderDraft();
    $("#import-status").textContent = "正在准备上传…";
    if (!draft) {
      pendingDeclaration ||= {
        title: $("#import-title").value.trim(), video_name: video.name,
        comments_name: comments?.name || null, offset_s: Number($("#import-offset").value || 0),
        request_id: Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join(""),
      };
      draft = await api("POST", "/api/recording-imports", pendingDeclaration);
    }
    for (const [kind, file, label] of [["video", video, "视频"], ["comments", comments, "弹幕"]]) {
      if (draft[`${kind}_uploaded`] || (kind === "comments" && !draft.comments_name)) continue;
      if (cancelled) throw new Error("上传已停止");
      $("#import-cancel").hidden = false;
      $("#import-progress").hidden = false;
      $("#import-progress").value = 0;
      $("#import-status").textContent = `正在上传${label}：0%`;
      draft = await uploadFile(`/api/recording-imports/${draft.id}/files/${kind}`, file, percent => {
        $("#import-progress").value = percent;
        $("#import-status").textContent = `正在上传${label}：${percent}%`;
      }, request => { activeUpload = request; });
      activeUpload = null;
    }
    if (cancelled) throw new Error("上传已停止");
    $("#import-cancel").hidden = true;
    $("#import-status").textContent = "上传完成，正在提交预处理…";
    await api("POST", `/api/recording-imports/${draft.id}/start`);
    $("#import-status").textContent = "已提交。预处理和分析会在后台继续，可在下方查看进度。";
    resetForm();
  } catch (error) {
    $("#import-error").textContent = error.message;
    $("#import-status").textContent = "未完成的导入会保留，可继续上传或在下方清理。";
  } finally {
    busy = false;
    activeUpload = null;
    $("#import-cancel").hidden = true;
    renderDraft();
    await refreshImports();
  }
}

export function importState(record) {
  if (record.session_id) {
    const progress = record.analysis;
    if (progress?.failed) return "部分分析失败";
    if (progress?.retrying) return "等待自动重试";
    if (progress?.asr_unavailable) return "转写证据不完整";
    return progress?.total && progress.done === progress.total ? "分析完成" : "分析中";
  }
  return { queued: "等待预处理", running: "正在预处理", cancelling: "正在停止",
    cancelled: "预处理已取消", failed: "预处理失败", succeeded: "预处理完成" }[record.job?.status]
    || (record.video_uploaded ? "等待提交" : "等待上传");
}

function recordCard(record) {
  const status = importState(record);
  const job = record.job;
  const progress = record.analysis;
  const activeJob = ["queued", "running", "cancelling"].includes(job?.status);
  const action = (name, label) => `<button data-action="${name}" data-id="${esc(record.id)}">${label}</button>`;
  const controls = record.session_id
    ? `<a data-id="${esc(record.id)}" data-action="timeline" href="/?tab=candidates&amp;session_id=${record.session_id}">查看热点时间线</a>
       <a data-id="${esc(record.id)}" data-action="transcripts" href="/?tab=transcripts&amp;session_id=${record.session_id}">查看转写</a><a data-id="${esc(record.id)}" data-action="tasks" href="/?tab=tasks">全部任务与重试</a>`
    : activeJob ? (job.status === "cancelling" ? "" : action("cancel", "取消预处理"))
    : ["failed", "cancelled"].includes(job?.status) ? action("retry", "重试预处理") + action("discard", "清理导入")
    : action("resume", record.sealed ? "重新提交" : "继续导入") + action("discard", "清理导入");
  return `<article class="card import-record">
    <div class="head"><h3>${esc(record.title)}</h3><span class="badge ${status.includes("失败") ? "red" : status === "分析完成" ? "green" : "gray"}">${status}</span></div>
    <p class="hint">${esc(record.video_name)} · ${record.comments_name ? esc(record.comments_name) : "无弹幕文件"} · ${esc(new Date(record.created_at).toLocaleString("zh-CN"))}</p>
    ${progress ? `<p>已分析 ${progress.done} / ${progress.total} 段 · 转写记录 ${progress.transcripts} 段${progress.failed ? ` · ${progress.failed} 段需检查` : ""}${progress.retrying ? ` · ${progress.retrying} 段等待重试` : ""}</p>` : ""}
    ${progress?.asr_unavailable ? `<p class="warn">${progress.asr_unavailable} 段语音证据不可用或质量不足。请检查本地模型设置和任务详情，修复后重新识别。</p>` : ""}
    ${record.session_id ? `<p class="hint">会话 #${record.session_id} · ${record.segment_count} 个分段 · ${record.comment_count} 条文本事件</p>` : ""}
    ${activeJob ? `<progress max="100" value="${Number(job.progress) || 0}" aria-label="预处理进度"></progress><p class="hint">${esc(job.message)}</p>` : ""}
    ${job?.error ? `<p class="warn">${esc(job.error)}</p>` : ""}
    ${(record.warnings || []).map(warning => `<p class="warn">${esc(warning)}</p>`).join("")}
    <div class="row import-actions">${controls}</div></article>`;
}

async function refreshImports() {
  if (busy || listLoading || actionPending) return;
  listLoading = true;
  const revision = operationRevision;
  try {
    const data = await api("GET", "/api/recording-imports");
    if (revision !== operationRevision) return;
    records = data.imports;
    videoLimit = data.max_video_bytes;
    commentLimit = data.max_comment_bytes;
    const engines = { funasr_nano: "Fun-ASR-Nano", funasr: "Fun-ASR-Nano", nano: "Fun-ASR-Nano", paraformer: "Paraformer", whisper: "Whisper" };
    $("#import-engine").textContent = `本地主引擎：${engines[data.engine] || data.engine}`;
    $("#imports-list-error").textContent = actionError;
    if (!busy && draft) {
      const latest = records.find(record => record.id === draft.id);
      if (latest?.sealed || !latest) resetForm();
      else { draft = latest; renderDraft(); }
    }
    const signature = JSON.stringify(records);
    if (signature !== listSignature) {
      const focused = document.activeElement?.dataset;
      $("#imports-list").innerHTML = records.length ? records.map(recordCard).join("")
        : '<div class="card empty">还没有导入记录。选择一段视频，开始第一次录播分析。</div>';
      listSignature = signature;
      if (focused?.id && focused.action) {
        $("#imports-list").querySelector(`[data-id="${focused.id}"][data-action="${focused.action}"]`)?.focus();
      }
    }
  } catch (error) { $("#imports-list-error").textContent = `读取失败：${error.message}`; }
  finally { listLoading = false; }
}

$("#import-form").addEventListener("submit", submitImport);
$("#import-video").addEventListener("change", () => {
  if (!draft && !$("#import-title").value) $("#import-title").value = $("#import-video").files[0]?.name.replace(/\.[^.]+$/, "").slice(0, 200) || "";
});
$("#import-cancel").addEventListener("click", () => { cancelled = true; activeUpload?.abort(); });
$("#import-new").addEventListener("click", resetForm);
$("#imports-refresh").addEventListener("click", refreshImports);
$("#imports-list").addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button || busy || actionPending) return;
  const record = records.find(item => item.id === button.dataset.id);
  if (!record) return;
  actionPending = true;
  operationRevision += 1;
  actionError = "";
  button.disabled = true;
  try {
    if (button.dataset.action === "resume") {
      if (record.sealed) await api("POST", `/api/recording-imports/${record.id}/start`);
      else {
        resetForm();
        draft = record;
        $("#import-title").value = record.title;
        $("#import-offset").value = record.offset_s;
        renderDraft();
        $("#import-form").scrollIntoView({ behavior: "smooth", block: "start" });
        $(!record.video_uploaded ? "#import-video" : record.comments_name && !record.comments_uploaded ? "#import-comments" : "#import-submit").focus();
      }
    } else if (button.dataset.action === "discard") {
      if (!window.confirm(`清理「${record.title}」的未完成导入？原始文件不受影响。`)) return;
      await api("DELETE", `/api/recording-imports/${record.id}`);
      if (draft?.id === record.id) resetForm();
    } else {
      await api("POST", `/api/jobs/${record.job.id}/${button.dataset.action}`);
    }
  } catch (error) { actionError = error.message; $("#imports-list-error").textContent = actionError; }
  finally { actionPending = false; button.disabled = false; await refreshImports(); }
});
window.addEventListener("beforeunload", event => {
  if (!busy) return;
  event.preventDefault(); event.returnValue = "";
});
async function poll() {
  if (!document.hidden) await refreshImports();
  if (pageActive) pollTimer = window.setTimeout(poll, 3000);
}
window.addEventListener("pagehide", () => { pageActive = false; window.clearTimeout(pollTimer); });
window.addEventListener("pageshow", event => { if (event.persisted) { pageActive = true; poll(); } });
renderDraft();
poll();
