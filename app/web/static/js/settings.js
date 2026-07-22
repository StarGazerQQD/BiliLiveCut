// BiliLiveCut \u8bbe\u7f6e:\u6a21\u578b\u3001\u8d26\u53f7\u3001\u65e5\u5fd7\u3001\u4efb\u52a1\u961f\u5217\u3001\u5b57\u5e55/\u7247\u5934\u7247\u5c3e\u6a21\u677f
import { $, api, toast, esc, badge } from "./common.js";

// ----------------------------- \u591a\u5927\u6a21\u578b\u914d\u7f6e ----------------------------- //
function llmRow(p) {
  p = p || {};
  const keyPlaceholder = p.api_key_set ? "\u5df2\u914d\u7f6e (\u7559\u7a7a\u4e0d\u6539)" : "\u586b\u5199 API Key";
  return `
  <div class="item llm-row" data-id="${esc(p.id || "")}">
    <div class="row" style="gap:8px; flex-wrap:wrap">
      <input class="llm-name" style="width:120px" placeholder="\u540d\u79f0" value="${esc(p.name || "")}" />
      <input class="llm-base" style="width:260px" placeholder="base_url" value="${esc(p.base_url || "")}" />
      <input class="llm-model" style="width:150px" placeholder="\u6a21\u578b" value="${esc(p.model || "")}" />
      <input class="llm-key" type="password" style="width:180px" placeholder="${keyPlaceholder}" />
    </div>
    <div class="row" style="gap:8px; flex-wrap:wrap; margin-top:6px; align-items:center">
      <input class="llm-search" style="width:150px" placeholder="\u8054\u7f51\u53c2\u6570(\u5982 enable_search)" value="${esc(p.web_search_param || "")}" />
      <span class="muted">\u4f18\u5148\u7ea7</span>
      <input class="llm-priority" type="number" style="width:80px" value="${p.priority != null ? p.priority : 100}" />
      <label class="switch-row"><input type="checkbox" class="llm-enabled" ${p.enabled === false ? "" : "checked"} /> \u542f\u7528</label>
      <button class="llm-del" data-act="del-llm">\u5220\u9664</button>
    </div>
  </div>`;
}

async function loadLLM() {
  const data = await api("GET", "/api/llm-providers");
  $("#llm-status").textContent = `\u5df2\u914d\u7f6e ${data.providers.length} \u4e2a \u00b7 \u53ef\u7528 ${data.active_count} \u4e2a(\u6309\u4f18\u5148\u7ea7\u4ece\u5c0f\u5230\u5927\u8c03\u7528)`;
  $("#llm-list").innerHTML = data.providers.length
    ? data.providers.map(llmRow).join("")
    : `<div class="empty">\u5c1a\u672a\u914d\u7f6e\u3002\u70b9\u51fb\u300c+ \u65b0\u589e\u6a21\u578b\u300d,\u6216\u4f7f\u7528 .env \u7684\u5355\u6a21\u578b\u914d\u7f6e\u3002</div>`;
}

function collectLLM() {
  return [...document.querySelectorAll(".llm-row")].map((row) => ({
    id: row.dataset.id || "",
    name: row.querySelector(".llm-name").value.trim(),
    base_url: row.querySelector(".llm-base").value.trim(),
    model: row.querySelector(".llm-model").value.trim(),
    api_key: row.querySelector(".llm-key").value,
    web_search_param: row.querySelector(".llm-search").value.trim(),
    priority: parseInt(row.querySelector(".llm-priority").value || "100", 10),
    enabled: row.querySelector(".llm-enabled").checked,
  })).filter((p) => p.base_url && p.model);
}

// ----------------------------- \u6e32\u67d3:\u65e5\u5fd7 ----------------------------- //
async function loadLogs() {
  const rows = await api("GET", "/api/logs?limit=100");
  $("#logs-list").innerHTML = rows.length ? `<div class="card">${rows.map((l) => `
    <div class="log-line"><span class="lvl-${l.level}">[${esc(l.level)}]</span>
      ${esc(l.created_at || "")} ${esc(l.module || "")}:${esc(l.event || "")} \u2014 ${esc(l.message)}</div>`).join("")}</div>`
    : `<div class="empty">\u6682\u65e0 WARNING/ERROR \u65e5\u5fd7\u3002</div>`;
}

// ----------------------------- V0.1.6 \u6e32\u67d3:\u4efb\u52a1\u961f\u5217 ----------------------------- //
async function loadTasks() {
  try {
    const data = await api("GET", "/api/tasks?limit=40");
    const { tasks, stats } = data;
    const w = stats.worker || {};
    $("#task-stat-total").textContent = stats.total || 0;
    $("#task-stat-queued").textContent = (
      (stats.queued_for_transcription || 0) + (stats.queued_for_analysis || 0) + (stats.queued_for_render || 0)
    );
    $("#task-stat-active").textContent = (
      (w.transcribing || 0) + (w.analyzing || 0) + (w.rendering || 0)
    );
    $("#task-stat-failed").textContent = (stats.failed || 0) + (stats.transient_failed || 0);
    $("#task-stat-completed").textContent = stats.completed || 0;
    const el = $("#stat-tasks");
    if (el) el.textContent = stats.total || 0;

    $("#task-tbody").innerHTML = tasks.length ? tasks.map(t => `
      <tr>
        <td>${esc(t.id)}</td>
        <td>${esc(t.segment_id)}</td>
        <td><span class="badge badge-${esc(t.stage.replace(/_/g,'-'))}">${esc(t.stage)}</span></td>
        <td>${t.attempts}/${t.max_retries}</td>
        <td>${t.processing_time_ms != null ? t.processing_time_ms : "-"}</td>
        <td title="${esc(t.last_error || "")}">${(t.last_error || "").substring(0,40)}</td>
        <td>${esc(t.created_at || "").substring(0,19)}</td>
        <td>
          ${t.stage === "failed" || t.stage === "cancelled"
            ? `<button class="small" onclick="retryTask(${t.id})">\u91cd\u8bd5</button>`
            : t.stage === "completed" || t.stage === "failed" || t.stage === "cancelled"
              ? "-"
              : `<button class="small danger" onclick="cancelTask(${t.id})">\u53d6\u6d88</button>`
          }
        </td>
      </tr>`).join("") : `<tr><td colspan="8" class="empty">\u6682\u65e0\u4efb\u52a1\u3002</td></tr>`;
  } catch (e) { console.warn("\u52a0\u8f7d\u5931\u8d25:", e); }
}

async function retryTask(id) {
  try { await api("POST", `/api/tasks/${id}/retry`); toast("\u4efb\u52a1\u5df2\u91cd\u65b0\u5165\u961f"); loadTasks(); }
  catch (e) { toast("\u91cd\u8bd5\u5931\u8d25:" + e.message); }
}

async function cancelTask(id) {
  try { await api("POST", `/api/tasks/${id}/cancel`); toast("\u4efb\u52a1\u5df2\u53d6\u6d88"); loadTasks(); }
  catch (e) { toast("\u53d6\u6d88\u5931\u8d25:" + e.message); }
}

// ----------------------------- \u8d26\u53f7\u7ba1\u7406 ----------------------------- //
async function loadCookieStatus() {
  try {
    const info = await api("GET", "/api/cookie-status");
    const hint = $("#cookie-hint");
    if (info.has_cookie) {
      hint.innerHTML = `\u5df2\u767b\u5f55 \u00b7 UID: <b>${esc(info.uid || "?")}</b>`;
      hint.className = "hint ok";
    } else {
      hint.textContent = info.hint || "\u672a\u914d\u7f6e Cookie,\u5f39\u5e55\u91c7\u96c6/\u9274\u6743\u529f\u80fd\u4e0d\u53ef\u7528\u3002";
      hint.className = "hint warn";
    }
    hint.style.display = "";
  } catch (e) { console.warn("\u52a0\u8f7d\u5931\u8d25:", e); }
}

let _loginPolling = null;
async function doLogin() {
  const btn = $("#btn-login");
  const status = $("#login-status");
  btn.disabled = true;
  status.textContent = "\u6b63\u5728\u542f\u52a8\u6d4f\u89c8\u5668\u2026";
  try {
    const resp = await api("POST", "/api/login");
    const taskId = resp.task_id;
    if (_loginPolling) clearInterval(_loginPolling);
    _loginPolling = setInterval(async () => {
      try {
        const s = await api("GET", `/api/login/status?task_id=${taskId}`);
        status.textContent = {
          starting: "\u6b63\u5728\u542f\u52a8\u6d4f\u89c8\u5668\u2026",
          installing_browser: "\u672a\u627e\u5230 Chrome\uff0c\u6b63\u5728\u4e0b\u8f7d Playwright Chromium\u2026",
          waiting: "\u8bf7\u5728\u5f39\u51fa\u7a97\u53e3\u4e2d\u5b8c\u6210\u767b\u5f55\u2026",
        }[s.status] || s.status;
        if (s.status === "done") {
          clearInterval(_loginPolling);
          _loginPolling = null;
          status.textContent = "\u767b\u5f55\u6210\u529f\uff01Cookie \u5df2\u81ea\u52a8\u4fdd\u5b58\u3002";
          btn.disabled = false;
          toast("Bilibili \u767b\u5f55\u6210\u529f");
          await loadCookieStatus();
        } else if (s.error) {
          clearInterval(_loginPolling);
          _loginPolling = null;
          status.textContent = "\u767b\u5f55\u5931\u8d25: " + esc(s.error);
          btn.disabled = false;
        }
      } catch (e) {
        clearInterval(_loginPolling);
        _loginPolling = null;
        status.textContent = "\u72b6\u6001\u67e5\u8be2\u5f02\u5e38: " + esc(e.message);
        btn.disabled = false;
      }
    }, 2000);
  } catch (e) {
    status.textContent = "\u542f\u52a8\u5931\u8d25: " + esc(e.message);
    btn.disabled = false;
  }
}

async function clearCookie() {
  if (!confirm("\u786e\u5b9a\u8981\u6e05\u9664 Cookie\uff1f\n\u6e05\u9664\u540e\u5f39\u5e55\u91c7\u96c6\u7b49\u9700\u8981\u767b\u5f55\u6001\u7684\u529f\u80fd\u5c06\u4e0d\u53ef\u7528\u3002")) return;
  try {
    await api("POST", "/api/login/clear");
    toast("Cookie \u5df2\u6e05\u9664\u3002");
    await loadCookieStatus();
  } catch (e) {
    toast("\u6e05\u9664\u5931\u8d25: " + esc(e.message));
  }
}

// ----------------------------- \u5b57\u5e55\u6a21\u677f(V0.1.8 P0) ----------------------------- //
async function loadTemplates() {
  const rows = await api("GET", "/api/templates");
  $("#templates-list").innerHTML = rows.length ? rows.map((t) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">${esc(t.name)} ${t.is_default ? badge("\u9ed8\u8ba4") : ""}</div>
          <div class="sub">${esc(t.font_name || "")} ${t.font_size}px \u00b7 \u8f6e\u5ed3${t.outline} \u00b7 \u9634\u5f71${t.shadow} \u00b7 \u6bcf\u884c${t.max_chars_per_line}\u5b57</div>
        </div>
        <div class="actions">
          <button class="ok" onclick="exportTemplate(${t.id})">\u5bfc\u51fa .ass</button>
          <button onclick="detTempl(${t.id})">\u5220\u9664</button>
        </div>
      </div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u6a21\u677f\u3002\u53ef\u5bfc\u5165 .ass \u6587\u4ef6\u6216\u65b0\u5efa\u9ed8\u8ba4\u6a21\u677f\u3002</div>`;
}

function exportTemplate(id) { window.open(`/api/templates/${id}/export`, "_blank"); }

async function detTempl(id) {
  if (!confirm("\u786e\u8ba4\u5220\u9664?")) return;
  try { await api("DELETE", `/api/templates/${id}`); loadTemplates(); }
  catch (e) { toast(e.message); }
}

// ----------------------------- \u7247\u5934\u7247\u5c3e\u6a21\u677f(V0.1.8 P1.2) ----------------------------- //
async function loadIntroTemplates() {
  const rows = await api("GET", "/api/intro-templates");
  $("#intro-templates-list").innerHTML = rows.length ? rows.map((t) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">${esc(t.name)} ${t.is_default ? badge("\u9ed8\u8ba4") : ""}</div>
          <div class="sub">
            \u7247\u5934 ${t.intro_enabled ? esc(t.intro_text) + " (" + t.intro_duration_s + "s)" : "\u7981\u7528"}
            / \u7247\u5c3e ${t.outro_enabled ? esc(t.outro_text) + " (" + t.outro_duration_s + "s)" : "\u7981\u7528"}
          </div>
        </div>
        <div class="actions">
          <button onclick="detIntro(${t.id})">\u5220\u9664</button>
        </div>
      </div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u6a21\u677f\u3002</div>`;
}

async function detIntro(id) {
  if (!confirm("\u786e\u8ba4\u5220\u9664?")) return;
  try { await api("DELETE", `/api/intro-templates/${id}`); loadIntroTemplates(); }
  catch (e) { toast(e.message); }
}

// ----------------------------- \u4e8b\u4ef6\u7ed1\u5b9a ----------------------------- //
$("#btn-add-llm").addEventListener("click", () => {
  const list = $("#llm-list");
  if (list.querySelector(".empty")) list.innerHTML = "";
  list.insertAdjacentHTML("beforeend", llmRow({ base_url: "https://api.deepseek.com/v1", model: "deepseek-chat", web_search_param: "enable_search", priority: 100 }));
});
$("#llm-list").addEventListener("click", (e) => {
  if (e.target.dataset.act === "del-llm") e.target.closest(".llm-row").remove();
});
$("#btn-save-llm").addEventListener("click", async () => {
  try {
    const r = await api("PUT", "/api/llm-providers", { providers: collectLLM() });
    toast(`\u5df2\u4fdd\u5b58 ${r.providers.length} \u4e2a\u6a21\u578b`);
    loadLLM();
  } catch (e) { toast("\u4fdd\u5b58\u5931\u8d25:" + e.message); }
});
$("#btn-test-llm").addEventListener("click", async () => {
  toast("\u6d4b\u8bd5\u4e2d,\u8bf7\u7a0d\u5019\u2026");
  try {
    const r = await api("POST", "/api/llm-providers/test");
    if (!r.results.length) return toast("\u65e0\u53ef\u7528\u6a21\u578b(\u9700\u5df2\u542f\u7528\u4e14\u914d\u7f6e key)");
    const ok = r.results.filter((x) => x.ok).map((x) => x.name);
    const bad = r.results.filter((x) => !x.ok).map((x) => x.name);
    toast(`\u53ef\u7528:${ok.join("\u3001") || "\u65e0"}${bad.length ? " \u00b7 \u5931\u8d25:" + bad.join("\u3001") : ""}`);
  } catch (e) { toast("\u6d4b\u8bd5\u5931\u8d25:" + e.message); }
});

$("#btn-add-template").addEventListener("click", async () => {
  try { await api("POST", "/api/templates"); toast("\u9ed8\u8ba4\u6a21\u677f\u5df2\u521b\u5efa"); loadTemplates(); }
  catch (e) { toast(e.message); }
});

$("#btn-import-ass").addEventListener("click", () => {
  document.getElementById("file-import-ass").click();
});

document.getElementById("file-import-ass").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/templates/import/ass", { method: "POST", body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "\u5bfc\u5165\u5931\u8d25");
    toast(`\u6210\u529f\u5bfc\u5165 ${data.imported.length} \u4e2a\u6837\u5f0f`);
    loadTemplates();
  } catch (err) {
    toast("\u5bfc\u5165\u5931\u8d25: " + err.message);
  }
  e.target.value = "";
});

$("#btn-add-intro").addEventListener("click", async () => {
  try { await api("POST", "/api/intro-templates"); toast("\u9ed8\u8ba4\u6a21\u677f\u5df2\u521b\u5efa"); loadIntroTemplates(); }
  catch (e) { toast(e.message); }
});

document.addEventListener("DOMContentLoaded", () => {
  const btnLogin = $("#btn-login");
  const btnClear = $("#btn-clear-cookie");
  if (btnLogin) btnLogin.addEventListener("click", doLogin);
  if (btnClear) btnClear.addEventListener("click", clearCookie);
});

export { loadLLM, collectLLM, llmRow, loadLogs, loadTasks, retryTask, cancelTask, loadCookieStatus, doLogin, clearCookie, loadTemplates, exportTemplate, detTempl, loadIntroTemplates, detIntro };
