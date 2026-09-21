// BiliLiveCut 控制台入口:导入模块、初始化标签切换与轮询
import { $ } from "./js/common.js";
import { loadRooms, saveRoom, saveRoomConfig, loadFeatureSwitches, saveWebPort, saveFeatureSwitches, loadThresholdLearning, loadSchedules, delSchedule, loadTopics, toggleCollection, hasRoomDraft } from "./js/rooms.js";
import { loadConfiguration, hasConfigurationDraft, selectConfigurationGroup } from "./js/configuration.js";
import { startRoom, stopRoom, resumeRoom, markHighlight, copyTranscriptSourceFile, correctTranscript, cancelTranscriptCorrection, retranscribeTranscript, loadRecording, loadTranscripts, loadDanmaku, hasTranscriptDraft } from "./js/recording.js";
import { loadSessionTimelines, toggleSessionTimeline, requestSessionReanalysis, regenerateSessionSummary } from "./js/timeline.js";
import { approveCand, rejectCand, delCand } from "./js/review.js";
import { loadClips, publishClip, enqueueClip, rejectClip } from "./js/clips.js";
import { loadUploads, retryUpload, pollNotifications } from "./js/publishing.js";
import { loadTrends, loadAnalytics, hasTrendDraft } from "./js/dashboard.js";
import { loadLLM, loadLogs, loadTasks, retryTask, cancelTask, loadCookieStatus, loadTemplates, exportTemplate, detTempl, loadIntroTemplates, detIntro, hasLLMDraft } from "./js/settings.js";
import { loadMonitor, triggerMaintenance } from "./js/monitor.js";
import { loadJobs, cancelJob, retryJob } from "./js/jobs.js";
import { loadPlugins, hasPluginMutationPending } from "./js/plugins.js";

// 挂载全局函数:供 HTML 内联 onclick 使用
window.saveRoom = saveRoom;
window.saveRoomConfig = saveRoomConfig;
window.saveFeatureSwitches = saveFeatureSwitches;
window.saveWebPort = saveWebPort;
window.delSchedule = delSchedule;
window.toggleCollection = toggleCollection;
window.startRoom = startRoom;
window.stopRoom = stopRoom;
window.resumeRoom = resumeRoom;
window.markHighlight = markHighlight;
window.copyTranscriptSourceFile = copyTranscriptSourceFile;
window.correctTranscript = correctTranscript;
window.cancelTranscriptCorrection = cancelTranscriptCorrection;
window.retranscribeTranscript = retranscribeTranscript;
window.toggleSessionTimeline = toggleSessionTimeline;
window.requestSessionReanalysis = requestSessionReanalysis;
window.regenerateSessionSummary = regenerateSessionSummary;
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
window.cancelJob = cancelJob;
window.retryJob = retryJob;

function hasUnsavedDashboardDraft() {
  return hasConfigurationDraft() || hasRoomDraft()
    || hasTranscriptDraft()
    || hasTrendDraft()
    || hasLLMDraft()
    || hasPluginMutationPending();
}

window.addEventListener("beforeunload", (event) => {
  if (!hasUnsavedDashboardDraft()) return;
  event.preventDefault();
  event.returnValue = "";
});

// ----------------------------- 标签切换 ----------------------------- //
let activeTab = "rooms";
const settingsTabs = new Set(["settings", "features", "models", "plugins", "login", "templates", "intro-templates"]);
function navigate(tab, updateHistory = true, group = null, sessionId = null) {
  const panel = $(`#tab-${tab}`);
  if (!panel) return;
  activeTab = tab;
  document.querySelectorAll(".tab").forEach(button => {
    button.classList.remove("active");
    if (button.dataset.tab === tab || (button.dataset.tab === "settings" && settingsTabs.has(tab))) button.classList.add("active");
  });
  document.querySelectorAll(".panel").forEach(item => item.classList.remove("active"));
  panel.classList.add("active");
  $("#settings-navigation").hidden = !settingsTabs.has(tab);
  if (tab === "settings") selectConfigurationGroup(group || "all");
  if (updateHistory && window.history?.pushState) {
    const url = new URL(window.location.href);
    url.searchParams.set("tab", tab);
    if (group) url.searchParams.set("group", group); else url.searchParams.delete("group");
    if (sessionId) url.searchParams.set("session_id", sessionId); else url.searchParams.delete("session_id");
    window.history.pushState(null, "", url);
  }
  refresh();
}
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => navigate(btn.dataset.tab));
});
document.addEventListener("click", event => {
  const link = event.target.closest?.('a[href^="/?tab="]');
  if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button > 0) return;
  const params = new URLSearchParams(link.getAttribute("href").split("?")[1]);
  if (!$(`#tab-${params.get("tab")}`)) return;
  event.preventDefault();
  navigate(params.get("tab"), true, params.get("group"), params.get("session_id"));
});

// ----------------------------- 轮询 ----------------------------- //
const loaders = {
  settings: loadConfiguration,
  rooms: loadRooms, recording: loadRecording, transcripts: loadTranscripts,
  danmaku: loadDanmaku, trends: loadTrends, candidates: loadSessionTimelines,
  clips: loadClips, uploads: loadUploads, features: loadFeatureSwitches, models: loadLLM, logs: loadLogs,
  schedules: loadSchedules, login: loadCookieStatus, tasks: async () => Promise.all([loadTasks(), loadJobs()]),
  topics: loadTopics, monitor: loadMonitor, templates: loadTemplates,
  "intro-templates": loadIntroTemplates, analytics: loadAnalytics, plugins: loadPlugins,
};

async function refresh() {
  try {
    await loadRooms();
    if (activeTab !== "rooms" && loaders[activeTab]) await loaders[activeTab]();
  } catch (e) { /* 静默,避免打断轮询 */ }
  pollNotifications();
}

function navigateFromUrl() {
  const params = new URLSearchParams(window.location?.search || "");
  const requestedTab = params.get("tab") || "rooms";
  const exists = [...document.querySelectorAll(".tab")].some(btn => btn.dataset.tab === requestedTab);
  navigate(exists ? requestedTab : "rooms", false, params.get("group"));
}
window.addEventListener("popstate", navigateFromUrl);
navigateFromUrl();
let _refresh_lock = false;
async function scheduleRefresh() {
  if (_refresh_lock) return;
  _refresh_lock = true;
  try { await refresh(); } catch (e) { console.warn("定时刷新失败:", e); }
  _refresh_lock = false;
  setTimeout(scheduleRefresh, 5000);
}
setTimeout(scheduleRefresh, 5000);
