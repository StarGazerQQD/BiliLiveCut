// BiliLiveCut 插件独立设置页
import { $, api, esc } from "./common.js";

const root = $("#plugin-settings-root");
const pluginId = root.dataset.pluginId;
let settingsDirty = false;
let settingsRevision = 0;

function markSettingsDirty() {
  settingsDirty = true;
  settingsRevision += 1;
  const status = $("#plugin-settings-status");
  status.textContent = "有未保存的修改。";
  status.className = "hint warn";
}

function settingControl(field) {
  const id = `plugin-setting-${field.key}`;
  let control;
  if (field.kind === "boolean") {
    control = `<label class="switch-row"><input id="${id}" data-key="${esc(field.key)}" data-kind="boolean" type="checkbox" ${field.value ? "checked" : ""} /> 启用</label>`;
  } else if (field.kind === "select") {
    control = `<select id="${id}" data-key="${esc(field.key)}" data-kind="select">${field.choices.map((choice) => `<option value="${esc(choice)}" ${choice === field.value ? "selected" : ""}>${esc(choice)}</option>`).join("")}</select>`;
  } else {
    const type = field.kind === "password" ? "password" : field.kind === "number" ? "number" : "text";
    const bounds = field.kind === "number"
      ? `${field.minimum == null ? "" : ` min="${field.minimum}"`}${field.maximum == null ? "" : ` max="${field.maximum}"`}`
      : "";
    const placeholder = field.kind === "password" && field.configured ? "已配置（留空不修改）" : "";
    control = `<input id="${id}" data-key="${esc(field.key)}" data-kind="${esc(field.kind)}" type="${type}" value="${esc(field.value ?? "")}" placeholder="${placeholder}"${bounds} ${field.required ? "required" : ""} />`;
  }
  return `<div class="plugin-setting-field"><label for="${id}">${esc(field.label)}</label>${control}${field.description ? `<small>${esc(field.description)}</small>` : ""}</div>`;
}

async function loadSettings(force = false) {
  if (settingsDirty && !force) return;
  const revision = settingsRevision;
  try {
    const data = await api("GET", `/api/plugins/${encodeURIComponent(pluginId)}/settings`);
    if (settingsDirty || settingsRevision !== revision) return;
    $("#plugin-settings-form").innerHTML = data.fields.length
      ? data.fields.map(settingControl).join("")
      : '<div class="empty">该插件没有可配置项。</div>';
    if (!force) $("#plugin-settings-status").textContent = "";
    $("#btn-save-plugin-settings").disabled = data.fields.length === 0;
  } catch (error) {
    if (settingsDirty || settingsRevision !== revision) return;
    $("#plugin-settings-form").innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    $("#plugin-settings-status").textContent = "请返回插件中心启用插件后再设置。";
    $("#btn-save-plugin-settings").disabled = true;
  }
}

async function saveSettings() {
  const revision = settingsRevision;
  const values = {};
  root.querySelectorAll("[data-key]").forEach((control) => {
    if (control.dataset.kind === "boolean") values[control.dataset.key] = control.checked;
    else if (control.dataset.kind === "number") values[control.dataset.key] = control.value === "" ? null : Number(control.value);
    else values[control.dataset.key] = control.value;
  });
  const status = $("#plugin-settings-status");
  try {
    await api("PATCH", `/api/plugins/${encodeURIComponent(pluginId)}/settings`, { values });
    if (settingsRevision !== revision) {
      status.textContent = "已保存提交时的设置；保存期间还有新修改，请再次保存。";
      status.className = "hint warn";
      return;
    }
    settingsDirty = false;
    status.textContent = "设置已保存。";
    status.className = "hint ok";
    await loadSettings(true);
  } catch (error) {
    status.textContent = "保存失败：" + error.message;
    status.className = "hint warn";
  }
}

$("#plugin-settings-form").addEventListener("input", markSettingsDirty);
$("#plugin-settings-form").addEventListener("change", markSettingsDirty);
$("#btn-save-plugin-settings").addEventListener("click", saveSettings);
window.addEventListener("beforeunload", (event) => {
  if (!settingsDirty) return;
  event.preventDefault();
  event.returnValue = "";
});
loadSettings();
