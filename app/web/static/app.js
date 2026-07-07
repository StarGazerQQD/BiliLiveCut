// BiliLiveCut 控制台入口:导入模块、初始化标签切换与轮询
import { $ } from "./js/common.js";
import { loadRooms, saveRoom, saveRoomConfig, loadThresholdLearning, loadSchedules, delSchedule, loadTopics, toggleCollection } from "./js/rooms.js";
import { startRoom, stopRoom, loadRecording, loadTranscripts, loadDanmaku } from "./js/recording.js";
import { loadCandidates } from "./js/candidates.js";
import { approveCand, rejectCand, delCand } from "./js/review.js";
import { loadClips, publishClip, enqueueClip, rejectClip } from "./js/clips.js";
import { loadUploads, retryUpload, pollNotifications } from "./js/publishing.js";
import { loadTrends, loadAnalytics } from "./js/dashboard.js";
import { loadLLM, loadLogs, loadTasks, retryTask, cancelTask, loadCookieStatus, loadTemplates, exportTemplate, detTempl, loadIntroTemplates, detIntro } from "./js/settings.js";
import { loadMonitor, triggerMaintenance } from "./js/monitor.js";

// 挂载全局函数:供 HTML 内联 onclick 使用
window.saveRoom = saveRoom;
window.saveRoomConfig = saveRoomConfig;
window.delSchedule = delSchedule;
window.toggleCollection = toggleCollection;
window.startRoom = startRoom;
window.stopRoom = stopRoom;
window.approveCand = approveCand;
window.rejectCand = rejectCand;
window.delCand = delCand;
window.publishClip = publishClip;
window.enqueueClip = enqueueClip;
window.rejectClip = rejectClip;
window.retryUpload = retryUpload;
window.retryTask = retryTask;
window.cancelTask = cancelTask;
window.exportTemplate = exportTemplate;
window.detTempl = detTempl;
window.detIntro = detIntro;
window.triggerMaintenance = triggerMaintenance;

// ----------------------------- 标签切换 ----------------------------- //
let activeTab = "rooms";
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    activeTab = btn.dataset.tab;
    $(`#tab-${activeTab}`).classList.add("active");
    const batchBar = document.getElementById("batch-bar");
    if (batchBar) batchBar.style.display = activeTab === "candidates" ? "" : "none";
    refresh();
  });
});

// ----------------------------- 轮询 ----------------------------- //
const loaders = {
  rooms: loadRooms, recording: loadRecording, transcripts: loadTranscripts,
  danmaku: loadDanmaku, trends: loadTrends, candidates: loadCandidates,
  clips: loadClips, uploads: loadUploads, models: loadLLM, logs: loadLogs,
  schedules: loadSchedules, login: loadCookieStatus, tasks: loadTasks,
  topics: loadTopics, monitor: loadMonitor, templates: loadTemplates,
  introTemplates: loadIntroTemplates, analytics: loadAnalytics,
};

async function refresh() {
  try {
    await loadRooms();
    if (activeTab !== "rooms" && loaders[activeTab]) await loaders[activeTab]();
  } catch (e) { /* 静默,避免打断轮询 */ }
  pollNotifications();
}

refresh();
let _refresh_lock = false;
async function scheduleRefresh() {
  if (_refresh_lock) return;
  _refresh_lock = true;
  try { await refresh(); } catch (e) { console.warn("定时刷新失败:", e); }
  _refresh_lock = false;
  setTimeout(scheduleRefresh, 5000);
}
setTimeout(scheduleRefresh, 5000);
