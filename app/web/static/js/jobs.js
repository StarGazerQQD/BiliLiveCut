// BiliLiveCut Web 后台作业：进度、取消和重试
import { $, api, toast, esc, badge } from "./common.js";

async function loadJobs() {
  const root = $("#web-jobs-list");
  if (!root) return;
  try {
    const data = await api("GET", "/api/jobs?limit=30");
    root.innerHTML = data.jobs.length ? data.jobs.map(job => `
      <div class="item">
        <div class="head">
          <div>
            <div class="title">${esc(job.label)} ${badge(job.status)}</div>
            <div class="sub">${esc(job.message || "")} · 尝试 ${job.attempt} · ${esc((job.updated_at || "").substring(0,19))}</div>
            ${job.error ? `<div class="sub" style="color:var(--red)">${esc(job.error)}</div>` : ""}
          </div>
          <div class="actions">
            ${job.status === "queued" || (job.status === "running" && job.cancellable_while_running !== false) ? `<button class="danger" onclick="cancelJob('${job.id}')">取消</button>` : ""}
            ${["failed","cancelled"].includes(job.status) ? `<button onclick="retryJob('${job.id}')">重试</button>` : ""}
          </div>
        </div>
        <div class="progress-bar-container"><div class="progress-bar" style="width:${Number(job.progress || 0)}%"></div></div>
      </div>`).join("") : '<div class="empty">暂无用户触发的后台作业。</div>';
  } catch (error) {
    root.innerHTML = `<div class="empty">作业状态加载失败：${esc(error.message)}</div>`;
  }
}

async function cancelJob(id) {
  try { await api("POST", `/api/jobs/${id}/cancel`); toast("已请求停止作业"); await loadJobs(); }
  catch (error) { toast("取消失败：" + error.message); }
}

async function retryJob(id) {
  try { await api("POST", `/api/jobs/${id}/retry`); toast("作业已重新排队"); await loadJobs(); }
  catch (error) { toast("重试失败：" + error.message); }
}

async function watchJob(id, onUpdate) {
  for (;;) {
    const job = await api("GET", `/api/jobs/${id}`);
    onUpdate(job);
    if (["succeeded","failed","cancelled"].includes(job.status)) return job;
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
}

export { loadJobs, cancelJob, retryJob, watchJob };
