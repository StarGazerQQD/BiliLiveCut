// BiliLiveCut \u8fd0\u7ef4\u9762\u677f:\u7cfb\u7edf\u72b6\u6001\u3001\u78c1\u76d8\u7ef4\u62a4
import { $, api, toast, esc } from "./common.js";

// ----------------------------- P3 \u8fd0\u7ef4\u9762\u677f ----------------------------- //
async function loadMonitor() {
  try {
    const d = await api("GET", "/api/monitor");
    $("#mon-disk").textContent = `${d.disk.free_gb}GB / ${d.disk.total_gb}GB (${d.disk.free_percent}%)`;
    $("#mon-raw").textContent = d.raw_size_gb;
    $("#mon-clips").textContent = d.clips_size_gb;
    $("#mon-cpu").textContent = d.cpu_percent != null ? d.cpu_percent.toFixed(0) : "--";
    $("#mon-mem").textContent = d.memory.percent.toFixed(0);
    const safeEl = $("#mon-safe");
    safeEl.textContent = d.disk_safe ? "\u2705 \u78c1\u76d8\u5b89\u5168" : "\u26a0 \u78c1\u76d8\u4e0d\u8db3";
    safeEl.style.color = d.disk_safe ? "var(--green)" : "var(--red)";
    let stageHtml = "";
    for (const [stage, count] of Object.entries(d.tasks.by_stage || {})) {
      stageHtml += `<span class="stat">${stage} <b>${count}</b></span>`;
    }
    $("#task-stage-stats").innerHTML = stageHtml || "<span>\u65e0\u4efb\u52a1</span>";
    $("#mon-oldest").textContent = d.tasks.oldest_wait_s;
    $("#mon-running").textContent = d.running_room_count;
    $("#mon-monitor").textContent = d.monitor.running ? "\u8fd0\u884c\u4e2d" : "\u5df2\u505c\u6b62";
    const failures = d.recent_failures || [];
    $("#mon-failures").innerHTML = failures.length ? failures.map(f =>
      `<div style="border-bottom:1px solid var(--border);padding:4px 0">
        <span class="muted">#${f.id} seg=${f.segment_id} ${f.stage} \u91cd\u8bd5${f.attempts}</span>
        <div style="color:var(--red);font-size:11px">${esc(f.error||"")}</div>
      </div>`
    ).join("") : "<span class='muted'>\u65e0\u5931\u8d25\u4efb\u52a1 \u2705</span>";
  } catch (e) { console.warn("\u52a0\u8f7d\u5931\u8d25:", e); }
}

async function triggerMaintenance() {
  toast("\u7ef4\u62a4\u4e2d\u2026");
  try {
    const r = await api("POST", "/api/monitor/disk-maintenance");
    toast(`\u7ef4\u62a4\u5b8c\u6210:\u6e05\u7406\u539f\u59cb ${r.cleaned_raw} \u4e2a,\u88ab\u62d2\u5207\u7247 ${r.cleaned_rejected} \u4e2a`);
    loadMonitor();
  } catch (e) { toast("\u7ef4\u62a4\u5931\u8d25:" + e.message); }
}

export { loadMonitor, triggerMaintenance };
