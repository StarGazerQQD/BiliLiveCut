// BiliLiveCut 通用工具:选择器、API、UI 辅助
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

export function sessionTitle(row) {
  const title = row?.session_title || "标题未知";
  const snapshot = row?.title_snapshot;
  return row?.title_state === "stale" || (snapshot && !snapshot.observed_at && snapshot.last_title)
    ? `${title}（上次获取）` : title;
}

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (e) { /* ignore */ }
    if (Array.isArray(detail)) detail = detail.map(item => `${(item.loc || []).join(".")}: ${item.msg || "输入无效"}`).join("；");
    const error = new Error(detail);
    error.status = resp.status;
    throw error;
  }
  return resp.status === 204 ? null : resp.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2600);
}

function esc(s) {
  return (s ?? "").toString().replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function badge(status) {
  const map = {
    running: "green", recording: "green", ready: "green", approved: "green",
    pending: "yellow", reviewing: "yellow", reconnecting: "yellow", reconnected: "green",
    rejected: "red", error: "red",
  };
  return `<span class="badge ${map[status] || "gray"}">${esc(status)}</span>`;
}

const DANMAKU_TYPE_LABEL = {
  danmaku: "\u5f39\u5e55", gift: "\u793c\u7269", superchat: "SC", interact: "\u4e92\u52a8", other: "\u5176\u5b83",
};

export { $, $$, api, toast, esc, badge, DANMAKU_TYPE_LABEL };
