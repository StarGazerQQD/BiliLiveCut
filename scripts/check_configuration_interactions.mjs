#!/usr/bin/env node
/** Exercise the real configuration module against delayed and failed API boundaries. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const elements = new Map();
class Element {
  set value(value) { this._value = String(value); }
  get value() { return this._value; }
  constructor(id) {
    this.id = id; this.value = ""; this.type = "text"; this.dataset = {}; this.disabled = false;
    this.checked = false; this.hidden = false; this.listeners = new Map(); this.textContent = "";
  }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  async emit(name, target = this) { return this.listeners.get(name)({ target, preventDefault() {} }); }
  focus() { if (!this.disabled) document.activeElement = this; }
  scrollIntoView() {}
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  checkValidity() { return this.type !== "number" || (this.value !== "" && Number.isFinite(Number(this.value)) && Number(this.value) >= Number(this.min ?? -Infinity) && Number(this.value) <= Number(this.max ?? Infinity)); }
  querySelector() { return controls.find(item => item.dataset.configKey === this.dataset.field); }
  set innerHTML(html) {
    this.html = html;
    if (this.id !== "configuration-fields") return;
    controls = [];
    for (const match of html.matchAll(/<(input|select)\b([^>]+)>/g)) {
      const attrs = Object.fromEntries([...match[2].matchAll(/([\w-]+)="([^"]*)"/g)].map(item => [item[1], item[2]]));
      if (!attrs.id) continue;
      const item = new Element(attrs.id);
      Object.assign(item, { type: attrs.type || "text", value: attrs.value ?? "", min: attrs.min, max: attrs.max, disabled: /\sdisabled\b/.test(match[2]), checked: /\schecked\b/.test(match[2]) });
      item.dataset = { configKey: attrs["data-config-key"], path: attrs["data-path"] };
      elements.set(item.id, item); controls.push(item);
    }
    for (const match of html.matchAll(/data-field="([^"]+)"/g)) {
      const item = element(`field-${match[1]}`); item.dataset.field = match[1];
    }
  }
  get innerHTML() { return this.html || ""; }
}
function element(id) { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); }
let controls = [];
globalThis.window = globalThis;
globalThis.scrollX = 0; globalThis.scrollY = 123;
globalThis.scrollTo = (x, y) => { globalThis.scrollX = x; globalThis.scrollY = y; };
globalThis.confirm = () => true;
globalThis.document = {
  activeElement: null,
  getElementById: element,
  querySelector(selector) {
    const field = selector.match(/^\[data-field="(.+)"\]$/);
    return field ? element(`field-${field[1]}`) : element(selector.slice(1));
  },
  querySelectorAll(selector) {
    if (selector === "[data-settings-group]") return [];
    const key = selector.match(/^\[data-config-key="(.+)"\]$/);
    if (key) return controls.filter(item => item.dataset.configKey === key[1]);
    return controls;
  },
};
element("configuration-group").value = "all";
const fields = [
  { key: "clip_video_crf", label: "视频质量", group: "output", type: "integer", value: 20, baseline: 20, minimum: 0, maximum: 51 },
  { key: "smtp_password", label: "邮箱密码", group: "notifications", type: "string", value: null, baseline: null, secret: true, configured: false },
  { key: "stream_url", label: "文本参数", group: "recording", type: "string", value: "abcdef", baseline: "abcdef" },
  { key: "database_url", label: "数据库", group: "system", type: "string", value: null, secret: true, editable: false, reason: "启动前确定" },
].map(field => ({ editable: true, env: field.key.toUpperCase(), saved: field.value, source: "default", effect: "next_task", scope: "global", ...field }));
let server = { revision: 0, fields };
const requests = [];
let failSave = false;
let pendingRead = null;
let pendingSave = null;
globalThis.fetch = async (url, options) => {
  assert.equal(url, "/api/settings/configuration");
  if (options.method === "GET") {
    if (pendingRead) await pendingRead;
    return { ok: true, status: 200, json: async () => structuredClone(server) };
  }
  const payload = JSON.parse(options.body); requests.push(payload);
  if (pendingSave) await pendingSave;
  if (failSave) return { ok: false, status: 400, json: async () => ({ detail: '配置校验失败：[{"field":"clip_video_crf","message":"跨字段校验失败"}]' }) };
  for (const field of server.fields) {
    if (Object.hasOwn(payload.values, field.key)) {
      if (field.secret) field.configured = !!payload.values[field.key];
      else field.value = field.saved = payload.values[field.key];
      field.source = "web";
    }
    if (payload.clear.includes(field.key)) { field.configured = false; field.source = "web"; }
    if (payload.reset.includes(field.key)) { field.value = field.saved = field.baseline; field.source = "default"; }
  }
  server.revision += 1;
  return { ok: true, status: 200, json: async () => structuredClone(server) };
};
const root = dirname(dirname(fileURLToPath(import.meta.url)));
function moduleUrl(source, path) {
  const labeledSource = `${source}\n//# sourceURL=${pathToFileURL(path).href}\n`;
  return `data:text/javascript;base64,${Buffer.from(labeledSource).toString("base64")}`;
}

// Keep this behavior check independent of temporary-directory creation/copy/cleanup.
// The separate frontend graph check still exercises the real relative import layout.
console.error("configuration check: reading source modules");
const commonPath = join(root, "app/web/static/js/common.js");
const configurationPath = join(root, "app/web/static/js/configuration.js");
const [commonSource, configurationSource] = await Promise.all([
  readFile(commonPath, "utf8"), readFile(configurationPath, "utf8"),
]);
const dependency = /(\bfrom\s+)(["'])\.\/common\.js\2/g;
assert.equal([...configurationSource.matchAll(dependency)].length, 1, "expected exactly one common.js import");
const linkedSource = configurationSource.replace(dependency, (_match, prefix) => `${prefix}${JSON.stringify(moduleUrl(commonSource, commonPath))}`);
console.error("configuration check: loading source modules");
{
  const module = await import(moduleUrl(linkedSource, configurationPath));
  console.error("configuration check: exercising real module behavior");
  await module.loadConfiguration();
  const form = element("configuration-form");
  const edit = async (key, value) => { const control = element(`configuration-${key}`); control.value = value; await form.emit("input", control); };
  const action = async (key, name) => form.emit("click", { closest: () => ({ dataset: { key, configAction: name } }) });
  assert.equal(element("configuration-database_url").disabled, true);

  await edit("clip_video_crf", "27");
  assert.equal(module.hasConfigurationDraft(), true);
  await module.loadConfiguration();
  assert.equal(element("configuration-clip_video_crf").value, "27", "poll must retain draft");
  module.selectConfigurationGroup("asr"); module.selectConfigurationGroup("all");
  assert.equal(element("configuration-group").value, "all");

  let releaseRead;
  pendingRead = new Promise(resolve => { releaseRead = resolve; });
  const reload = module.loadConfiguration(true);
  await edit("stream_url", "abcXYZdef");
  element("configuration-stream_url").focus();
  element("configuration-stream_url").setSelectionRange(4, 6);
  releaseRead(); await reload; pendingRead = null;
  assert.equal(document.activeElement.id, "configuration-stream_url");
  assert.deepEqual([document.activeElement.selectionStart, document.activeElement.selectionEnd], [4, 6]);
  assert.equal(document.activeElement.value, "abcXYZdef");
  assert.equal(globalThis.scrollY, 123);

  failSave = true;
  await form.emit("submit");
  assert.equal(module.hasConfigurationDraft(), true);
  assert.equal(document.activeElement.id, "configuration-clip_video_crf", "backend error must focus enabled input");
  assert.equal(document.activeElement.disabled, false);
  failSave = false;
  await form.emit("submit");
  assert.equal(module.hasConfigurationDraft(), false);
  assert.equal(element("configuration-clip_video_crf").value, "27");
  assert.equal(requests.at(-1).revision, 0);

  await edit("clip_video_crf", "99");
  const before = requests.length;
  await form.emit("submit");
  assert.equal(requests.length, before, "invalid form must not send partial values");
  await edit("clip_video_crf", "27");
  assert.equal(module.hasConfigurationDraft(), false);
  assert.equal(element("configuration-status").textContent, "没有未保存修改。");

  await edit("smtp_password", "fixture-only-secret");
  let releaseSave;
  pendingSave = new Promise(resolve => { releaseSave = resolve; });
  const saving = form.emit("submit");
  assert.equal(element("configuration-smtp_password").disabled, true);
  await form.emit("submit");
  releaseSave(); await saving; pendingSave = null;
  assert.equal(element("configuration-smtp_password").value, "");
  assert.equal(element("configuration-fields").innerHTML.includes("fixture-only-secret"), false);
  assert.equal(module.hasConfigurationDraft(), false);
  await action("smtp_password", "clear"); await form.emit("submit");
  assert.deepEqual(requests.at(-1).clear, ["smtp_password"]);
  await action("clip_video_crf", "reset"); await form.emit("submit");
  assert.deepEqual(requests.at(-1).reset, ["clip_video_crf"]);
  assert.equal(element("configuration-clip_video_crf").value, "20");
  assert.equal(element("configuration-database_url").disabled, true);
  console.log("PASS: configuration draft, delayed reload/caret, validation focus, save failure, concurrent submit, secret clearing, reset and readonly controls");
}
