#!/usr/bin/env node
/** Exercise the real import module across HTTP, upload and DOM boundaries. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const elements = new Map();
let links = [];
class Element {
  constructor(id) { this.id = id; this.value = ""; this.files = []; this.dataset = {}; this.listeners = new Map(); this.textContent = ""; }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  async emit(name, target = this) { return this.listeners.get(name)({ target, preventDefault() {} }); }
  focus() { document.activeElement = this; }
  scrollIntoView() {}
  reset() { for (const id of ["import-title", "import-video", "import-comments", "import-offset"]) { element(id).value = ""; element(id).files = []; } }
  set innerHTML(value) {
    this.html = value;
    if (this.id !== "imports-list") return;
    document.activeElement = null;
    links = [...value.matchAll(/<(?:a|button) data-id="([^"]+)" data-action="([^"]+)"/g)].map(match => {
      const link = new Element(match[1] + match[2]); link.dataset = { id: match[1], action: match[2] }; return link;
    });
  }
  get innerHTML() { return this.html || ""; }
  querySelector(selector) {
    return links.find(link => selector.includes(`data-id="${link.dataset.id}"`) && selector.includes(`data-action="${link.dataset.action}"`));
  }
}
function element(id) { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); }
globalThis.window = globalThis;
globalThis.document = { hidden: false, activeElement: null, querySelector: selector => element(selector.slice(1)) };
const listeners = new Map();
globalThis.addEventListener = (name, fn) => listeners.set(name, fn);
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
globalThis.confirm = () => true;
const records = [];
const requests = [];
const uploads = [];
let loseDeclaration = false;
let loseUpload = false;
let failAction = false;
let holdUpload = false;
let uploadRequest = null;
let releaseRead = null;
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => structuredClone(data) });
globalThis.fetch = async (url, options) => {
  requests.push({ url, method: options.method, body: options.body });
  if (options.method === "GET") {
    const snapshot = { imports: structuredClone(records), max_video_bytes: 50 * 1024 ** 3, max_comment_bytes: 32 * 1024 ** 2, engine: "whisper" };
    if (releaseRead === true) await new Promise(resolve => { releaseRead = resolve; });
    return response(snapshot);
  }
  if (failAction) return response({ detail: "操作被拒绝" }, 409);
  if (url === "/api/recording-imports") {
    const body = JSON.parse(options.body);
    let record = records.find(row => row.id === body.request_id);
    if (!record) { record = { ...body, id: body.request_id, created_at: "2026-09-21T00:00:00Z", video_uploaded: false, comments_uploaded: false }; records.push(record); }
    if (loseDeclaration) { loseDeclaration = false; throw new Error("创建响应丢失"); }
    return response(record);
  }
  const id = url.split("/")[3];
  const record = records.find(row => row.id === id);
  if (url.endsWith("/start")) { record.sealed = true; record.job = { id: "job", status: "queued", progress: 0 }; return response(record); }
  if (options.method === "DELETE") { records.splice(records.indexOf(record), 1); return response({}); }
  return response({});
};
globalThis.XMLHttpRequest = class {
  constructor() { this.upload = {}; }
  open(method, path) { this.path = path; assert.equal(method, "PUT"); }
  setRequestHeader(name, value) { assert.equal(name, "Content-Type"); assert.equal(value, "application/octet-stream"); }
  send(file) {
    uploads.push({ path: this.path, file }); uploadRequest = this;
    if (!holdUpload) queueMicrotask(() => this.complete());
  }
  complete() {
    const [, , , id, , kind] = this.path.split("/");
    const record = records.find(row => row.id === id);
    assert.equal(record[`${kind}_uploaded`], false, "must not reupload a completed file");
    record[`${kind}_uploaded`] = true;
    this.upload.onprogress({ lengthComputable: true, loaded: 1, total: 1 });
    if (loseUpload) { loseUpload = false; this.onerror(); return; }
    this.status = 200; this.responseText = JSON.stringify(record); this.onload();
  }
  abort() { this.onabort(); }
};
const root = dirname(dirname(fileURLToPath(import.meta.url)));
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const common = moduleUrl(await readFile(join(root, "app/web/static/js/common.js"), "utf8"));
const source = (await readFile(join(root, "app/web/static/js/recording-imports.js"), "utf8")).replace('"./common.js"', JSON.stringify(common));
const module = await import(moduleUrl(source));
const settle = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve(); };
const select = (title, comments = null) => {
  const video = { name: "source.mp4", size: 100 };
  element("import-title").value = title; element("import-video").files = [video];
  element("import-comments").files = comments ? [comments] : []; return video;
};
const submit = () => element("import-form").emit("submit");
const refresh = () => element("imports-refresh").emit("click");
const action = (record, name) => element("imports-list").emit("click", { closest: () => ({ dataset: { id: record.id, action: name } }) });
await settle();

// Raw upload, optional input, double-submit prevention and progress separation.
const video = select("无弹幕录播");
holdUpload = true;
const pending = submit(); await settle();
await submit();
assert.equal(records.length, 1);
assert.equal(uploads.length, 1);
assert.equal(uploads[0].file, video, "video must be sent as the original File, without multipart/base64 buffering");
const readsWhileBusy = requests.filter(row => row.method === "GET").length;
await refresh();
assert.equal(requests.filter(row => row.method === "GET").length, readsWhileBusy, "upload must not start stale list reads");
uploadRequest.complete(); holdUpload = false; await pending;
assert.equal(records[0].sealed, true);
assert.equal(module.importState(records[0]), "等待预处理");
assert.equal(module.importState({ session_id: 1, analysis: { total: 2, done: 1 } }), "分析中");
assert.equal(module.importState({ session_id: 1, analysis: { total: 2, done: 2 } }), "分析完成");
assert.equal(module.importState({ session_id: 1, analysis: { total: 2, done: 2, asr_unavailable: 1 } }), "转写证据不完整");
assert.equal(module.importState({ session_id: 1, analysis: { retrying: 1 } }), "等待自动重试");

// Completed video survives response loss; only the missing comment is sent on retry.
select("<script>恶意标题</script>", { name: "events.ass", size: 50 }); loseUpload = true;
await submit();
assert.match(element("import-error").textContent, /中断/);
assert.equal(element("import-video").disabled, true);
assert.equal(element("import-comments").required, true);
const uploadCount = uploads.length;
await submit();
assert.equal(uploads.length, uploadCount + 1);
assert.ok(uploads.at(-1).path.endsWith("/comments"));
assert.ok(element("imports-list").innerHTML.includes("&lt;script&gt;"));
assert.ok(!element("imports-list").innerHTML.includes("<script>"));

// Creation is idempotent even if the HTTP response is lost.
select("创建响应丢失"); loseDeclaration = true;
await submit();
const createdCount = records.length;
assert.equal(element("import-comments").disabled, true, "a lost declaration response must still preserve declared file choices");
await submit();
assert.equal(records.length, createdCount);
assert.equal(records.at(-1).sealed, true);

// Cancellation preserves files and permits a later resume; failures stay visible across polls.
select("可停止上传"); holdUpload = true;
const cancelling = submit(); await settle();
await element("import-cancel").emit("click"); await cancelling; holdUpload = false;
const incomplete = records.at(-1);
assert.equal(Boolean(incomplete.sealed), false);
await action(incomplete, "resume");
assert.equal(document.activeElement.id, "import-video");
failAction = true; await action(incomplete, "discard"); await refresh();
assert.equal(element("imports-list-error").textContent, "操作被拒绝");
failAction = false;

// Polling another job must preserve file choices and keyboard focus on result links.
const selectedAgain = select("可停止上传");
records[0].session_id = 1; records[0].analysis = { total: 2, done: 1, transcripts: 1 };
await refresh();
const resultLink = links.find(link => link.dataset.action === "timeline"); resultLink.focus();
records[0].analysis.done = 2; await refresh();
assert.equal(document.activeElement.dataset.action, "timeline");
assert.equal(element("import-video").files[0], selectedAgain);
await action(incomplete, "discard");
assert.equal(records.includes(incomplete), false);

// A pre-upload GET arriving after failure cannot erase the recoverable draft and chosen files.
releaseRead = true;
const staleRefresh = refresh(); await settle();
const delayedVideo = select("旧响应保护");
holdUpload = true;
const delayedSubmit = submit(); await settle();
await element("import-cancel").emit("click"); await delayedSubmit; holdUpload = false;
releaseRead(); await staleRefresh; releaseRead = null;
assert.equal(element("import-new").hidden, false);
assert.equal(element("import-video").files[0], delayedVideo);
assert.match(element("import-resume").textContent, /旧响应保护/);
listeners.get("pagehide")();
console.log("PASS: recording imports, raw upload, idempotent recovery, cancellation, progress and focus");
