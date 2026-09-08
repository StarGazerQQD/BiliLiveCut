// 完整配置表单：草稿仅在内存保留，刷新不覆盖，提交按版本整体保存。
import { $, api, esc } from "./common.js";

const effects = { new_room: "新建房间时", next_task: "下一任务", next_recording: "下次录制", next_poll: "下次调度检查", immediate: "下一次读取", restart: "重启后", deployment: "部署配置" };
const groups = { recording: "录制与自动化", asr: "本地语音模型", llm: "大模型与预算", trends: "网感采集", highlights: "高光与评分", review: "审核", output: "成片与编码", storage: "存储与清理", publishing: "上传与发布", notifications: "通知", system: "账号与系统" };
const common = new Set(["recording_pipeline_enabled", "segment_duration_s", "stream_quality", "live_poll_interval_s", "recording_max_duration_s", "asr_primary", "asr_task_max_concurrency", "transcript_llm_refine_enabled", "llm_daily_budget", "trend_enabled", "trend_schedule_enabled", "trend_schedule_start", "trend_schedule_end", "trend_schedule_interval_min", "highlight_threshold", "highlight_review_threshold", "highlight_auto_approve_threshold", "clip_subtitle", "clip_max_duration_s", "clip_video_crf", "min_free_disk_gb", "storage_cleanup_enabled", "raw_retention_days", "biliup_enabled", "auto_upload", "auto_publish_threshold", "bilibili_cookie", "notify_on_clip", "notify_on_error", "notify_on_disk_alert"]);
const scoringLabels = { volume: "音量", danmaku: "弹幕", keywords: "关键词", speech_rate: "语速", audio_events: "音频事件", trend: "网感", laughter: "笑声", danmaku_sentiment: "弹幕情绪", alpha: "规则分融合系数", beta: "大模型分融合系数", pre_roll_s: "前置留白（秒）", post_roll_s: "后置留白（秒）", iou_threshold: "重叠去重阈值", cooldown_s: "冷却时间（秒）" };
const links = {
  all: [["features", "房间自动化与阈值"], ["models", "大模型服务商"], ["login", "账号登录"], ["plugins", "插件中心"]],
  recording: [["features", "房间自动化与开播守候"], ["rooms", "房间管理与标题状态"], ["schedules", "录制预约"]],
  asr: [["monitor", "实际模型加载状态"], ["rooms", "房间热词与别名"]],
  llm: [["models", "服务商、密钥与连通测试"]], trends: [["trends", "资料库与采集状态"]],
  highlights: [["features", "房间自动化与阈值"], ["rooms", "房间词典与评分模式"]],
  review: [["features", "房间审核阈值"]], output: [["templates", "字幕模板"], ["intro-templates", "片头片尾模板"]],
  publishing: [["uploads", "上传队列"], ["login", "投稿账号"]], storage: [["monitor", "磁盘用量与手动维护"]],
  system: [["features", "Web 端口（重启生效）"], ["plugins", "插件配置"], ["login", "Bilibili 账号"]],
};
let view = null;
let loading = false;
let saving = false;
let generation = 0;
const draft = new Map();

export function hasConfigurationDraft() { return saving || draft.size > 0; }

function display(value) {
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  return typeof value === "object" ? "评分参数组" : String(value ?? "未设置");
}

function input(field, value, path = "", schema = field) {
  const id = `configuration-${field.key}${path ? "-" + path.replaceAll(".", "-") : ""}`;
  const attrs = `id="${id}" data-config-key="${esc(field.key)}" data-path="${esc(path)}" ${field.editable ? "" : "disabled"} aria-describedby="configuration-help-${field.key}"`;
  if (schema.type === "boolean") return `<input ${attrs} type="checkbox" ${value ? "checked" : ""} />`;
  if (schema.enum) return `<select ${attrs}>${schema.enum.map(item => `<option value="${esc(item)}" ${item === value ? "selected" : ""}>${esc(item)}</option>`).join("")}</select>`;
  const type = field.secret ? "password" : ["number", "integer"].includes(schema.type) ? "number" : "text";
  return `<input ${attrs} type="${type}" value="${esc(field.secret ? "" : value)}" ${type === "number" ? `step="${schema.type === "integer" ? "1" : "any"}"` : ""} ${schema.minimum == null ? "" : `min="${schema.minimum}"`} ${schema.maximum == null ? "" : `max="${schema.maximum}"`} ${field.secret ? `autocomplete="new-password" placeholder="${field.configured ? "已配置；留空保持" : "未配置"}"` : ""} />`;
}

function renderField(field) {
  let control = input(field, field.value);
  if (field.type === "object") {
    control = '<div class="settings-scoring">' + Object.entries(field.value).flatMap(([key, value]) => {
      const entries = typeof value === "object" ? Object.entries(value).map(([sub, number]) => [`${key}.${sub}`, number, { type: "number", minimum: 0 }]) : [[key, value, field.schema.properties[key]]];
      return entries.map(([path, number, schema]) => `<label>${esc(scoringLabels[path.split(".").pop()] || path)}${input(field, number, path, schema)}</label>`);
    }).join("") + '</div>';
  }
  const source = { web: "Web 已保存覆盖", environment: "环境默认", default: "项目默认" }[field.source];
  const scope = field.scope === "new_room_default" ? "新房间默认；已有房间保持保存值" : "全局";
  const description = field.description || `${groups[field.group]}参数。`;
  return `<article class="card configuration-field" data-field="${field.key}">
    <div class="head"><label for="configuration-${field.key}"><strong>${esc(field.label)}</strong></label><span class="badge gray">${esc(effects[field.effect])}</span></div>
    <p class="hint"><code>${esc(field.env || field.key)}</code> · ${esc(scope)}</p>
    ${control}
    <p class="hint" id="configuration-help-${field.key}">${esc(description)} ${field.unit ? `单位：${esc(field.unit)}。` : ""}${field.minimum == null && field.maximum == null ? "" : `范围：${field.minimum ?? "不限"} ～ ${field.maximum ?? "不限"}。`}</p>
    <p class="hint">已保存有效值：${esc(field.secret ? (field.configured ? "已配置（不回显）" : "未配置") : display(field.saved))} · 来源：${source}；恢复默认：${esc(field.secret ? "环境/项目默认（不回显）" : display(field.baseline))}</p>
    ${field.reason ? `<p class="hint warn">${esc(field.reason)}</p>` : `<div class="row"><button type="button" data-config-action="reset" data-key="${field.key}">恢复环境/项目默认</button>${field.secret ? `<button type="button" data-config-action="clear" data-key="${field.key}">明确清空凭据</button>` : ""}</div>`}
    <p id="configuration-change-${field.key}" class="hint warn"></p><p id="configuration-error-${field.key}" class="hint warn" role="alert"></p>
  </article>`;
}

function filterFields() {
  if (!view) return;
  const query = $("#configuration-search").value.trim().toLowerCase();
  const group = $("#configuration-group").value;
  const advanced = $("#configuration-advanced").checked;
  let count = 0;
  for (const field of view.fields) {
    const matches = `${field.label} ${field.key} ${field.env} ${field.description}`.toLowerCase().includes(query);
    const visible = (group === "all" || field.group === group) && matches && (query || advanced || common.has(field.key));
    const article = $(`[data-field="${field.key}"]`);
    article.hidden = !visible;
    if (visible) count += 1;
  }
  document.querySelectorAll("[data-settings-group]").forEach(section => {
    section.hidden = ![...section.querySelectorAll("[data-field]")].some(article => !article.hidden);
    const advancedSection = section.querySelector("details");
    if (advancedSection) {
      advancedSection.hidden = ![...advancedSection.querySelectorAll("[data-field]")].some(article => !article.hidden);
      advancedSection.open = Boolean(query || advanced);
    }
  });
  $("#configuration-count").textContent = `显示 ${count} / ${view.fields.length} 项${count ? "" : "；请调整搜索或打开高级设置"}`;
  $("#configuration-links").innerHTML = (links[group] || links.all).map(([tab, label]) => `<a href="/?tab=${tab}" data-nav-tab="${tab}">${esc(label)}</a>`).join("");
}

function render() {
  $("#configuration-fields").innerHTML = Object.entries(groups).map(([group, label]) => {
    const fields = view.fields.filter(field => field.group === group);
    const basic = fields.filter(field => common.has(field.key));
    const advanced = fields.filter(field => !common.has(field.key));
    return `<section data-settings-group="${group}"><h3>${esc(label)}</h3>${basic.map(renderField).join("")}${advanced.length ? `<details class="configuration-advanced-group"><summary>高级设置 · ${advanced.length} 项</summary>${advanced.map(renderField).join("")}</details>` : ""}</section>`;
  }).join("");
  for (const [key, change] of draft) applyDraft(key, change);
  filterFields();
  updateStatus();
}

function applyDraft(key, change) {
  const field = view.fields.find(item => item.key === key);
  const value = change.action === "reset" ? field.baseline : change.action === "clear" ? "" : change.value;
  document.querySelectorAll(`[data-config-key="${key}"]`).forEach(control => {
    const selected = control.dataset.path ? control.dataset.path.split(".").reduce((item, part) => item[part], value) : value;
    if (control.type === "checkbox") control.checked = Boolean(selected);
    else control.value = field.secret && change.action !== "value" ? "" : selected;
  });
}

function updateStatus() {
  $("#configuration-save").disabled = saving || draft.size === 0;
  $("#configuration-discard").disabled = saving || draft.size === 0;
  $("#configuration-refresh").disabled = loading || saving;
  const counts = new Map();
  for (const field of view?.fields || []) {
    const change = draft.get(field.key);
    $("#configuration-change-" + field.key).textContent = !change ? "" : change.action === "reset" ? "待保存：删除 Web 覆盖，恢复环境/项目默认" : change.action === "clear" ? "待保存：明确清空，不回退到环境凭据" : "有未保存修改";
    if (change) counts.set(effects[field.effect], (counts.get(effects[field.effect]) || 0) + 1);
  }
  $("#configuration-impact").textContent = draft.size ? `本次影响 ${draft.size} 项：${[...counts].map(([effect, count]) => `${effect} ${count} 项`).join("；")}。已有素材不会自动重算。` : "没有未保存修改。";
}

export async function loadConfiguration(force = false) {
  if (loading || saving || (view && !force)) return;
  loading = true;
  const revision = generation;
  try {
    const latest = await api("GET", "/api/settings/configuration");
    // 请求等待期间继续编辑时，只更新基线并保留所有草稿。
    view = latest;
    const active = document.activeElement;
    const focus = active?.id;
    const selection = active?.selectionStart == null ? null : [active.selectionStart, active.selectionEnd];
    const position = [window.scrollX, window.scrollY];
    render();
    if (focus) {
      const control = document.getElementById(focus);
      control?.focus({ preventScroll: true });
      if (selection && control?.setSelectionRange) control.setSelectionRange(...selection);
    }
    window.scrollTo(...position);
    if (force) $("#configuration-status").textContent = `已读取最新配置${draft.size || generation !== revision ? "，草稿已保留；请核对后再保存" : ""}。`;
    $("#configuration-error").textContent = "";
  } catch (error) { $("#configuration-error").textContent = `读取失败：${error.message}；可点击“读取最新值”重试。`; }
  finally { loading = false; updateStatus(); }
}

function markDirty(event) {
  const control = event.target;
  const key = control.dataset.configKey;
  if (!key || !view || saving) return;
  const field = view.fields.find(item => item.key === key);
  let value;
  if (field.type === "object") {
    value = structuredClone(field.value);
    document.querySelectorAll(`[data-config-key="${key}"]`).forEach(item => {
      const parts = item.dataset.path.split(".");
      if (parts.length === 2) value[parts[0]][parts[1]] = item.value === "" ? null : Number(item.value);
      else value[parts[0]] = item.value === "" ? null : Number(item.value);
    });
  } else value = control.type === "checkbox" ? control.checked : control.type === "number" ? (control.value === "" ? null : Number(control.value)) : control.value;
  if ((field.secret && !String(value).trim()) || (!field.secret && JSON.stringify(value) === JSON.stringify(field.value))) draft.delete(key);
  else draft.set(key, { action: "value", value });
  generation += 1;
  $("#configuration-error-" + key).textContent = "";
  $("#configuration-status").textContent = draft.size ? "有未保存修改。" : "没有未保存修改。";
  updateStatus();
}

function locateError(key, message) {
  $("#configuration-group").value = "all";
  $("#configuration-search").value = "";
  $("#configuration-advanced").checked = true;
  filterFields();
  const target = $(`[data-field="${key}"]`);
  $("#configuration-error-" + key).textContent = message;
  target?.querySelector("input, select")?.focus();
  target?.scrollIntoView({ block: "center" });
}

async function save(event) {
  event.preventDefault();
  if (saving || loading || !draft.size) return;
  for (const control of document.querySelectorAll("[data-config-key]")) {
    if (draft.has(control.dataset.configKey) && !control.disabled && (!control.checkValidity() || (control.type === "number" && control.value === ""))) {
      locateError(control.dataset.configKey, "请填写有效值并检查允许范围。");
      return;
    }
  }
  const payload = { values: {}, clear: [], reset: [], revision: view.revision };
  for (const [key, change] of draft) {
    if (change.action === "value") payload.values[key] = change.value;
    else payload[change.action].push(key);
  }
  saving = true;
  // 保存期间禁止编辑，避免服务端成功后清除后来输入的草稿。
  const editable = [...document.querySelectorAll("#configuration-form input, #configuration-form select, #configuration-form button")].filter(item => !item.disabled);
  editable.forEach(item => { item.disabled = true; });
  $("#configuration-status").textContent = "正在保存…";
  $("#configuration-error").textContent = "";
  updateStatus();
  try {
    view = await api("PATCH", "/api/settings/configuration", payload);
    draft.clear();
    generation += 1;
    render();
    $("#configuration-status").textContent = "设置已保存；已更新有效值和来源。活动任务保留原设置，标记重启的项目在下次启动生效。";
  } catch (error) {
    editable.forEach(item => { item.disabled = false; });
    $("#configuration-error").textContent = `保存失败：${error.message}。草稿已保留。`;
    $("#configuration-status").textContent = "未保存；若提示版本冲突，先读取最新值并核对草稿。";
    const prefix = error.message.indexOf("[");
    if (prefix >= 0) {
      try {
        const issues = JSON.parse(error.message.slice(prefix));
        for (const issue of issues) {
          const key = issue.field.split(".")[0];
          if (view.fields.some(field => field.key === key)) { locateError(key, issue.message); break; }
          if (key === "cross_fields") { locateError(draft.keys().next().value, issue.message); break; }
        }
      } catch { /* 跨字段或非结构化错误已在顶部完整显示。 */ }
    }
  } finally { saving = false; editable.forEach(item => { item.disabled = false; }); updateStatus(); }
}

$("#configuration-search").addEventListener("input", filterFields);
$("#configuration-group").addEventListener("change", () => {
  filterFields();
  if (window.history?.replaceState) {
    const url = new URL(window.location.href);
    url.searchParams.set("group", $("#configuration-group").value);
    window.history.replaceState(null, "", url);
  }
});
$("#configuration-advanced").addEventListener("change", filterFields);
$("#configuration-refresh").addEventListener("click", () => loadConfiguration(true));
$("#configuration-form").addEventListener("input", markDirty);
$("#configuration-form").addEventListener("change", markDirty);
$("#configuration-form").addEventListener("submit", save);
$("#configuration-form").addEventListener("click", event => {
  const button = event.target.closest?.("[data-config-action]");
  if (!button || saving) return;
  const key = button.dataset.key;
  draft.set(key, { action: button.dataset.configAction });
  generation += 1;
  applyDraft(key, draft.get(key));
  $("#configuration-status").textContent = "操作已加入草稿；点击保存后生效。";
  updateStatus();
});
$("#configuration-discard").addEventListener("click", () => {
  if (!window.confirm("撤销本页全部未保存修改？已保存配置保持不变。")) return;
  draft.clear(); generation += 1; render();
  $("#configuration-status").textContent = "已撤销草稿。";
});

export function selectConfigurationGroup(group) {
  if (group in groups || group === "all") $("#configuration-group").value = group;
  filterFields();
}
